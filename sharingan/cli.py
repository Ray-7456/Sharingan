"""命令行入口：``python -m sharingan <命令>``。

命令：

- ``fixtures make``    生成合成测试数据与真值清单
- ``rules validate``   校验 rules.json（默认按生产标准，``--draft`` 放宽 source 要求）
- ``inspect``          查看数据文件概况（编码、时间范围、断档、备注、逐列统计），
                       配合 ``--rules`` 可按规则统计超限
- ``plan``             dry-run：算出"该改哪些单元格、改成什么"，只读不写
- ``apply``            执行写入：先备份、再改数据、最后落变更日志（需 ``--yes``）
- ``ui``               启动跨平台图形界面（需要 PySide6，见 docs/design.md §7）

统计口径全部来自 :mod:`sharingan.analysis`，与图形界面共用同一份逻辑。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .analysis import Summary, apply_repairs, build_summary, plan_repairs
from .fixtures import generate
from .formats import TimeSeriesFile
from .rules import load_rules


def _render_summary(summary: Summary) -> None:
    facts = summary.facts
    print(f"文件：{facts.path}")
    print(
        f"编码：{facts.encoding}  换行：{facts.newline!r}  "
        f"数值列：{facts.column_count}  数据行：{facts.row_count}"
    )
    if facts.row_count == 0:
        print("没有任何数据行")
        return
    print(f"时间范围：{facts.time_range[0]} → {facts.time_range[1]}")
    if facts.tail_line_count:
        print(f"尾部保留行：{facts.tail_line_count} 行（原样写回）")
    if facts.unnamed_trailing_columns:
        print(f"行尾未命名列：{facts.unnamed_trailing_columns} 个（原样保留）")
    if facts.parse_failures:
        print(f"时间无法解析的行：{facts.parse_failures}")
    if summary.notes:
        print("备注：" + "，".join(f"{note.text}×{note.count}" for note in summary.notes))
    if summary.duplicate_rows:
        print(f"与上一行完全重复的行：{summary.duplicate_rows}")
    if summary.gaps:
        print(f"时间断档：{len(summary.gaps)} 处")
        for gap in summary.gaps[:5]:
            print(f"  {gap.before} → {gap.after}（{gap.hours:.1f} 小时）")

    if summary.rule_path:
        for warning in summary.rule_warnings:
            print(f"提示：{warning}")
        for error in summary.rule_errors:
            print(f"错误：{error}")

    print("逐列统计：")
    for stat in summary.columns:
        if stat.count == 0 or stat.minimum is None or stat.maximum is None:
            continue
        line = (
            f"  {stat.letter} {stat.name}: n={stat.count} "
            f"min={stat.minimum:.1f} max={stat.maximum:.1f}"
        )
        if stat.limit is not None:
            line += f"  阈值={stat.limit} 超限={stat.breaches}"
        print(line)
    if summary.rule_path and summary.rule_ok:
        print(f"合计超限：{summary.breach_total}")


def _cmd_fixtures_make(args: argparse.Namespace) -> int:
    result = generate(seed=args.seed, days=args.days)
    csv_path, manifest_path = result.write(args.out, stem=args.stem)
    manifest = result.manifest
    print(f"合成数据：{csv_path}")
    print(f"真值清单：{manifest_path}")
    print(
        f"数据行：{manifest['row_count']}，"
        f"时间范围：{manifest['time_range'][0]} → {manifest['time_range'][1]}"
    )
    kinds: dict[str, int] = {}
    for anomaly in manifest["anomalies"]:
        kinds[anomaly["kind"]] = kinds.get(anomaly["kind"], 0) + 1
    print("注入异常：" + "，".join(f"{kind}×{count}" for kind, count in sorted(kinds.items())))
    return 0


def _cmd_rules_validate(args: argparse.Namespace) -> int:
    data, result = load_rules(args.path, production=not args.draft)
    print(result.format())
    if data is None:
        return 2
    print(f"错误 {len(result.errors)} 条，告警 {len(result.warnings)} 条")
    return 0 if result.ok else 1


def _cmd_inspect(args: argparse.Namespace) -> int:
    summary = build_summary(
        args.path, rules_path=args.rules, gap_minutes=args.gap_minutes
    )
    if summary.rule_path and not summary.rule_loaded:
        print("规则文件不可用：")
        for error in summary.rule_errors:
            print(f"  {error}")
        return 2
    _render_summary(summary)
    return 0


def _load_rules_or_report(path: str, *, draft: bool):
    """读规则并在出错时打印；返回 (rules_data, 退出码或 None)。"""
    rules_data, result = load_rules(path, production=not draft)
    if rules_data is None or not result.ok:
        print(result.format())
        return None, 2
    for warning in result.warnings:
        print(f"提示：{warning}")
    return rules_data, None


def _cmd_plan(args: argparse.Namespace) -> int:
    rules_data, code = _load_rules_or_report(args.rules, draft=args.draft)
    if rules_data is None:
        return code
    data_file = TimeSeriesFile.read(args.path)
    run = plan_repairs(data_file, rules_data)

    print(f"数据文件：{args.path}（{len(data_file.rows)} 行）")
    print(f"规则文件：{args.rules}（version {rules_data.get('version')}）")
    print(run.summary())
    for reason in run.halt_reasons:
        print(f"  停机原因：{reason}")
    for action in run.actions[: args.limit]:
        print(f"  {action.column_letter} {action.describe()}")
        print(f"      规则 {action.rule_key}｜来源：{action.rule_source or '（未注明）'}")
    if len(run.actions) > args.limit:
        print(f"  …… 其余 {len(run.actions) - args.limit} 处（用 --limit 调整显示条数）")
    for item in run.skipped[: args.limit]:
        print(f"  待人工判断：{item}")

    if args.json:
        payload = {
            "data": str(args.path),
            "rules": str(args.rules),
            "rules_version": run.rules_version,
            "halted": run.halted,
            "halt_reasons": run.halt_reasons,
            "action_count": run.action_count,
            "counts_by_column": run.counts_by_column(),
            "skipped": run.skipped,
            "actions": [
                {
                    "row_index": a.row_index,
                    "time": a.time_text,
                    "column": a.column_letter,
                    "name": a.column_name,
                    "before": a.before,
                    "after": a.after,
                    "rule": a.rule_key,
                    "rule_source": a.rule_source,
                    "reason": a.reason,
                }
                for a in run.actions
            ],
        }
        Path(args.json).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"清单已写入：{args.json}")
    return 1 if run.halted else 0


def _cmd_apply(args: argparse.Namespace) -> int:
    rules_data, code = _load_rules_or_report(args.rules, draft=args.draft)
    if rules_data is None:
        return code
    data_file = TimeSeriesFile.read(args.path)
    run = plan_repairs(data_file, rules_data)

    if run.halted:
        print("拒绝写盘：变更计划已停机。")
        for reason in run.halt_reasons:
            print(f"  停机原因：{reason}")
        print("请先补全规则（repair 段）后重试。")
        return 1
    if not run.actions:
        print("没有需要修改的单元格，未写盘。")
        return 0
    if not args.yes:
        print(f"这是写盘操作，当前计划：{run.summary()}")
        print("请先用 `sharingan plan` 复核清单，确认后加 --yes 执行（会自动备份原文件）。")
        return 2

    writeback = rules_data.get("writeback") or {}
    result = apply_repairs(
        data_file,
        run,
        args.path,
        backup=bool(writeback.get("backup", True)),
        log_dir=args.log_dir,
    )
    print(f"已写入：{result.written_path}（修改 {result.change_count} 处）")
    print(f"备份：{result.backup_path or '（未备份）'}")
    print(f"变更日志：{result.log_path}")
    return 0


def _cmd_ui(args: argparse.Namespace) -> int:
    try:
        from .ui import main as ui_main
    except ImportError as exc:  # 理论上不会发生：ui 模块内部处理缺失依赖
        print(f"无法加载图形界面模块：{exc}")
        return 2
    argv: list[str] = []
    if args.path:
        argv.append(args.path)
    if args.rules:
        argv.extend(["--rules", args.rules])
    return ui_main(argv)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sharingan", description="读懂规则、复制操作、生成自动化工具"
    )
    parser.add_argument("--version", action="version", version=f"sharingan {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    fixtures = subparsers.add_parser("fixtures", help="合成测试数据")
    fixtures_sub = fixtures.add_subparsers(dest="fixtures_command", required=True)
    make = fixtures_sub.add_parser("make", help="生成合成数据与真值清单")
    make.add_argument("--out", default="fixtures", help="输出目录（默认 fixtures/）")
    make.add_argument("--stem", default="synthetic_week", help="文件名前缀")
    make.add_argument("--seed", type=int, default=20260105, help="随机种子（决定噪声）")
    make.add_argument("--days", type=int, default=7, help="天数（默认 7）")
    make.set_defaults(func=_cmd_fixtures_make)

    rules = subparsers.add_parser("rules", help="规则文件")
    rules_sub = rules.add_subparsers(dest="rules_command", required=True)
    validate_cmd = rules_sub.add_parser("validate", help="校验 rules.json")
    validate_cmd.add_argument("path", help="rules.json 路径")
    validate_cmd.add_argument(
        "--draft", action="store_true", help="按草稿标准校验（缺少 source 只告警）"
    )
    validate_cmd.set_defaults(func=_cmd_rules_validate)

    inspect_cmd = subparsers.add_parser("inspect", help="查看数据文件概况")
    inspect_cmd.add_argument("path", help="数据文件路径")
    inspect_cmd.add_argument("--rules", help="可选：按 rules.json 统计超限")
    inspect_cmd.add_argument(
        "--gap-minutes", type=int, default=10, help="断档判定阈值（分钟，默认 10）"
    )
    inspect_cmd.set_defaults(func=_cmd_inspect)

    plan_cmd = subparsers.add_parser("plan", help="dry-run：算出变更清单但不写盘")
    plan_cmd.add_argument("path", help="数据文件路径")
    plan_cmd.add_argument("--rules", required=True, help="rules.json 路径")
    plan_cmd.add_argument("--json", help="把清单另存为 JSON 文件")
    plan_cmd.add_argument("--limit", type=int, default=20, help="最多显示多少条（默认 20）")
    plan_cmd.add_argument("--draft", action="store_true", help="按草稿标准校验规则")
    plan_cmd.set_defaults(func=_cmd_plan)

    apply_cmd = subparsers.add_parser(
        "apply", help="执行写入（先备份并落变更日志，需 --yes 确认）"
    )
    apply_cmd.add_argument("path", help="数据文件路径（就地修改，会先备份）")
    apply_cmd.add_argument("--rules", required=True, help="rules.json 路径")
    apply_cmd.add_argument("--yes", action="store_true", help="确认执行写盘")
    apply_cmd.add_argument("--log-dir", help="变更日志目录（默认与数据文件同目录）")
    apply_cmd.add_argument("--draft", action="store_true", help="按草稿标准校验规则")
    apply_cmd.set_defaults(func=_cmd_apply)

    ui_cmd = subparsers.add_parser("ui", help="启动图形界面（需要 PySide6）")
    ui_cmd.add_argument("path", nargs="?", help="可选：启动时打开的数据文件")
    ui_cmd.add_argument("--rules", help="可选：启动时加载的规则文件")
    ui_cmd.set_defaults(func=_cmd_ui)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
