"""夹具生成器测试：确定性、异常剧本与真值清单一致、不产生意外超限。"""

import unittest

from sharingan.fixtures import NO_DATA_NOTE, SIGNALS, generate


class FixtureGeneratorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.result = generate()
        self.manifest = self.result.manifest
        self.data_file = self.result.data_file
        self.rows = self.data_file.rows
        self.columns = self.data_file.columns

    # ---------- 基本属性 ----------

    def test_row_count_equals_full_grid_minus_gap(self):
        expected = 7 * 24 * 6 - 84
        self.assertEqual(self.manifest["row_count"], expected)
        self.assertEqual(len(self.rows), expected)

    def test_manifest_declares_synthetic(self):
        self.assertTrue(self.manifest["synthetic"])
        self.assertIn("禁止用真实数据替换", self.manifest["警告"])

    def test_deterministic_for_same_seed(self):
        first = generate(seed=7)
        second = generate(seed=7)
        third = generate(seed=8)
        self.assertEqual(first.data_file.to_bytes(), second.data_file.to_bytes())
        self.assertEqual(first.manifest, second.manifest)
        self.assertNotEqual(first.data_file.to_bytes(), third.data_file.to_bytes())

    def test_no_change_records_in_fresh_fixture(self):
        self.assertEqual(self.data_file.changed_cells(), [])

    # ---------- 断档 ----------

    def test_single_gap_of_850_minutes(self):
        deltas = [
            (current.time - previous.time).total_seconds() / 60
            for previous, current in zip(self.rows, self.rows[1:])
        ]
        big_gaps = [delta for delta in deltas if delta > 10]
        self.assertEqual(len(big_gaps), 1)
        self.assertEqual(big_gaps[0], 850.0)

    # ---------- 陈旧值 ----------

    def test_stale_runs_match_manifest(self):
        anomalies = [a for a in self.manifest["anomalies"] if a["kind"] == "stale_run"]
        self.assertEqual(len(anomalies), 3)
        expected_notes = sum(a["length"] for a in anomalies)
        self.assertEqual(sum(1 for row in self.rows if row.note == NO_DATA_NOTE), expected_notes)
        for anomaly in anomalies:
            start = anomaly["start_index"]
            source_values = self.rows[start - 1].values
            self.assertEqual(anomaly["source_row_index"], start - 1)
            for offset in range(anomaly["length"]):
                row = self.rows[start + offset]
                self.assertEqual(row.values, source_values)
                self.assertEqual(row.note, NO_DATA_NOTE)

    def test_notes_only_come_from_stale_runs(self):
        notes = {row.note for row in self.rows if row.note.strip()}
        self.assertEqual(notes, {NO_DATA_NOTE})

    # ---------- 毛刺与超限 ----------

    def test_spikes_match_manifest(self):
        anomalies = [a for a in self.manifest["anomalies"] if a["kind"] == "spike"]
        self.assertEqual(len(anomalies), 3)
        for anomaly in anomalies:
            row = self.rows[anomaly["index"]]
            column = self.columns.index(anomaly["column"])
            self.assertEqual(row.value(column), anomaly["after"])

    def test_breach_counts_are_exactly_as_designed(self):
        """原始测点只有两处设计好的超限，其余列不应意外越线。"""
        breach_counts = {}
        for index, name in enumerate(self.columns):
            limit = SIGNALS[index].limit
            if limit is None:
                continue
            breach_counts[name] = sum(
                1
                for row in self.rows
                if row.value(index) is not None and row.value(index) >= limit
            )
        expected_nonzero = {
            "gearbox_temp_input_shaft1": 1,
            "generator_bearing_temp_b": 1,
        }
        for name, count in breach_counts.items():
            self.assertEqual(count, expected_nonzero.get(name, 0), name)

    # ---------- 派生列超限 ----------

    def test_derived_overflow_run(self):
        anomaly = next(
            a for a in self.manifest["anomalies"] if a["kind"] == "derived_overflow"
        )
        index_a = self.columns.index("generator_bearing_temp_a")
        index_b = self.columns.index("generator_bearing_temp_b")
        diffs = [
            abs(self.rows[k].value(index_b) - self.rows[k].value(index_a))
            for k in range(anomaly["start_index"], anomaly["start_index"] + anomaly["length"])
        ]
        self.assertTrue(all(diff >= anomaly["limit"] for diff in diffs))
        self.assertAlmostEqual(max(diffs), anomaly["max_abs_diff"], places=1)

    # ---------- 浮点尾数 ----------

    def test_float_artifacts_have_long_decimals(self):
        anomalies = [a for a in self.manifest["anomalies"] if a["kind"] == "float_artifact"]
        self.assertEqual(len(anomalies), 2)
        for anomaly in anomalies:
            row = self.rows[anomaly["index"]]
            text = row.values[self.columns.index(anomaly["column"])]
            self.assertIn(".", text)
            self.assertGreaterEqual(len(text.split(".")[1]), 4, text)


if __name__ == "__main__":
    unittest.main()
