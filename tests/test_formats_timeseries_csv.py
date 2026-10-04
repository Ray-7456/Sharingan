"""数据文件格式模块测试：时间/数值文本、编码、字节级往返。"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from sharingan.fixtures import generate
from sharingan.formats import (
    TimeSeriesFile,
    detect_encoding,
    format_number,
    format_time,
    parse_time,
)


class FormatTimeTest(unittest.TestCase):
    def test_date_and_hour_not_padded_minute_second_padded(self):
        self.assertEqual(format_time(datetime(2026, 7, 17, 0, 0, 0)), "2026-7-17 0:00:00")
        self.assertEqual(format_time(datetime(2026, 7, 17, 1, 40, 0)), "2026-7-17 1:40:00")
        self.assertEqual(format_time(datetime(2026, 11, 5, 23, 5, 0)), "2026-11-5 23:05:00")

    def test_parse_roundtrip(self):
        moment = datetime(2026, 7, 17, 1, 40)
        self.assertEqual(parse_time(format_time(moment)), moment)

    def test_parse_rejects_garbage(self):
        self.assertIsNone(parse_time(""))
        self.assertIsNone(parse_time("2026/7/17 0:00"))
        self.assertIsNone(parse_time("2026-13-40 0:00:00"))


class FormatNumberTest(unittest.TestCase):
    def test_site_style(self):
        self.assertEqual(format_number(0.0), "0")
        self.assertEqual(format_number(-0.0), "0")
        self.assertEqual(format_number(3.0), "3")
        self.assertEqual(format_number(135.6), "135.6")
        self.assertEqual(format_number(99.60001), "99.60001")
        self.assertEqual(format_number(-1.8), "-1.8")
        self.assertEqual(format_number(None), "")

    def test_no_scientific_notation(self):
        self.assertNotIn("e", format_number(1e-7))

    def test_rejects_bool(self):
        with self.assertRaises(TypeError):
            format_number(True)


class EncodingTest(unittest.TestCase):
    def test_detect(self):
        self.assertEqual(detect_encoding("中文".encode("gbk")), "gbk")
        self.assertEqual(detect_encoding("中文".encode("utf-8")), "utf-8")
        self.assertEqual(detect_encoding(b"\xef\xbb\xbfabc"), "utf-8-sig")


class RoundTripTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name)
        self.result = generate()
        self.csv_path, _ = self.result.write(self.directory)

    def test_roundtrip_byte_identical(self):
        original = self.csv_path.read_bytes()
        data_file = TimeSeriesFile.read(self.csv_path)
        self.assertEqual(data_file.to_bytes(), original)

    def test_fresh_fixture_has_no_change_records(self):
        data_file = TimeSeriesFile.read(self.csv_path)
        self.assertEqual(data_file.changed_cells(), [])

    def test_gbk_crlf_and_tail_line(self):
        raw = self.csv_path.read_bytes()
        self.assertNotIn(b"\xef\xbb\xbf", raw)
        self.assertIn("此时间点无数据".encode("gbk"), raw)
        data_file = TimeSeriesFile.read(self.csv_path)
        self.assertEqual(data_file.encoding, "gbk")
        self.assertEqual(data_file.newline, "\r\n")
        self.assertEqual(len(data_file.tail_lines), 1)
        self.assertEqual(data_file.tail_lines[0], "," * (len(data_file.columns) + 2))

    def test_edit_changes_only_target_cell(self):
        data_file = TimeSeriesFile.read(self.csv_path)
        row_index, column_index = 5, 3
        before = data_file.rows[row_index].values[column_index]
        data_file.rows[row_index].set_value(column_index, float(before) + 10)

        old_lines = self.csv_path.read_bytes().decode("gbk").split("\r\n")
        new_lines = data_file.to_bytes().decode("gbk").split("\r\n")
        self.assertEqual(len(old_lines), len(new_lines))
        differing = [i for i, (a, b) in enumerate(zip(old_lines, new_lines)) if a != b]
        self.assertEqual(differing, [row_index + 1])  # 第 0 行是表头

        changes = data_file.changed_cells()
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].before, before)
        self.assertEqual(changes[0].after, format_number(float(before) + 10))

    def test_note_change_tracked(self):
        data_file = TimeSeriesFile.read(self.csv_path)
        data_file.rows[0].set_note("复核")
        changes = data_file.changed_cells()
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].field_index, -1)
        self.assertEqual(changes[0].field_name, "备注")
        self.assertEqual(changes[0].before, "")
        self.assertEqual(changes[0].after, "复核")

    def test_write_rejects_separator_in_note(self):
        data_file = TimeSeriesFile.read(self.csv_path)
        data_file.rows[0].set_note("a,b")
        with self.assertRaises(ValueError):
            data_file.to_bytes()

    def test_field_count_mismatch_raises(self):
        bad = self.directory / "bad.csv"
        bad.write_bytes("Time,x,备注\r\n2026-1-5 0:00:00,1\r\n".encode("gbk"))
        with self.assertRaises(ValueError):
            TimeSeriesFile.read(bad)

    def test_matches_real_layout_header_16_data_17_fields(self):
        """现场格式：表头 16 字段，数据行 17 字段（多一个未命名的空列）。"""
        lines = self.csv_path.read_bytes().decode("gbk").split("\r\n")
        column_count = len(self.result.data_file.columns)
        self.assertEqual(len(lines[0].split(",")), column_count + 2)
        self.assertEqual(len(lines[1].split(",")), column_count + 3)
        data_file = TimeSeriesFile.read(self.csv_path)
        self.assertEqual(data_file.rows[0].extra, ("",))
        self.assertEqual(len(data_file.rows[0].values), column_count)

    def test_roundtrip_without_unnamed_column(self):
        columns = self.result.data_file.columns
        rows = self.result.data_file.rows[:5]
        for row in rows:
            row.extra = ()
        data_file = TimeSeriesFile.from_rows(columns, rows, unnamed_trailing_columns=0)
        path = self.directory / "no_phantom.csv"
        data_file.write(path)
        reloaded = TimeSeriesFile.read(path)
        self.assertEqual(reloaded.to_bytes(), path.read_bytes())
        self.assertEqual(reloaded.rows[0].extra, ())

    def test_non_blank_unnamed_column_raises(self):
        bad = self.directory / "extra.csv"
        bad.write_bytes("Time,x,备注\r\n2026-1-5 0:00:00,1,,oops\r\n".encode("gbk"))
        with self.assertRaises(ValueError):
            TimeSeriesFile.read(bad)


if __name__ == "__main__":
    unittest.main()
