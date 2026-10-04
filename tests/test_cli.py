"""命令行入口测试：参数解析与命令转发（不真正启动图形界面）。"""

import unittest

from sharingan import cli


class UiCommandTest(unittest.TestCase):
    def test_ui_accepts_path_and_rules(self):
        args = cli.build_parser().parse_args(["ui", "data.csv", "--rules", "rules.json"])
        self.assertEqual(args.path, "data.csv")
        self.assertEqual(args.rules, "rules.json")
        self.assertIs(args.func, cli._cmd_ui)

    def test_ui_without_arguments(self):
        args = cli.build_parser().parse_args(["ui"])
        self.assertIsNone(args.path)
        self.assertIsNone(args.rules)

    def test_cmd_ui_forwards_arguments_to_ui_main(self):
        import sharingan.ui as ui_package

        captured: dict[str, list[str]] = {}
        original = ui_package.main

        def fake_main(argv: list[str]) -> int:
            captured["argv"] = list(argv)
            return 0

        ui_package.main = fake_main  # type: ignore[assignment]
        try:
            args = cli.build_parser().parse_args(["ui", "data.csv", "--rules", "rules.json"])
            code = cli._cmd_ui(args)
        finally:
            ui_package.main = original  # type: ignore[assignment]

        self.assertEqual(code, 0)
        self.assertEqual(captured["argv"], ["data.csv", "--rules", "rules.json"])

    def test_cmd_ui_forwards_empty_arguments(self):
        import sharingan.ui as ui_package

        captured: dict[str, list[str]] = {}
        original = ui_package.main

        def fake_main(argv: list[str]) -> int:
            captured["argv"] = list(argv)
            return 0

        ui_package.main = fake_main  # type: ignore[assignment]
        try:
            code = cli._cmd_ui(cli.build_parser().parse_args(["ui"]))
        finally:
            ui_package.main = original  # type: ignore[assignment]
        self.assertEqual(code, 0)
        self.assertEqual(captured["argv"], [])


class InspectCommandTest(unittest.TestCase):
    def test_inspect_parses_rules_and_gap_minutes(self):
        args = cli.build_parser().parse_args(
            ["inspect", "data.csv", "--rules", "r.json", "--gap-minutes", "30"]
        )
        self.assertEqual(args.path, "data.csv")
        self.assertEqual(args.rules, "r.json")
        self.assertEqual(args.gap_minutes, 30)

    def test_rules_validate_draft_flag(self):
        args = cli.build_parser().parse_args(["rules", "validate", "r.json", "--draft"])
        self.assertTrue(args.draft)


if __name__ == "__main__":
    unittest.main()
