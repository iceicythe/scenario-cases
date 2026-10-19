"""命令行入口：python -m scenario_cases <generate|library> ..."""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

from scenario_cases.generator import GENERATOR_VERSION, build_case, write_case
from scenario_cases.models import ScenarioSpec
from scenario_cases.store import TRUTH_FILE, ScenarioLibrary
from scenario_cases.templates import DEFAULTS, PROFILE_NAMES, RANGES

# 批量时的模板配比：多数有事件，少量无事件（负样本）， cut_in 少量
MIX = {"hard_brake": 0.35, "mild_decel": 0.3, "lead_cut_in": 0.2, "cruise": 0.15}

DEFAULT_DB = Path("scenario_library.db")


def pick_profile(rng: random.Random) -> str:
    roll = rng.random()
    cumulative = 0.0
    for name, weight in MIX.items():
        cumulative += weight
        if roll <= cumulative:
            return name
    return "cruise"


def jitter(rng: random.Random, profile: str) -> dict:
    """在模板范围内抖动参数；未知参数键不动。"""
    params = {}
    for key, value in DEFAULTS[profile].items():
        if key not in RANGES:
            params[key] = value
            continue
        low, high = RANGES[key]
        if key == "decel":
            # 减速度下限保留门槛余量，抖动时同样遵守
            low = max(low, 2.2)
        params[key] = round(rng.uniform(low, high), 2)
    if profile != "cruise":
        # 联合约束：单个参数合法不代表组合合法——末端车速至少保留 3 m/s 余量。
        # 时长向下取整到 0.01 秒，取整只会更保守，不会把余量吃掉。
        max_duration = math.floor((params["v0"] - 3.0) / params["decel"] * 100) / 100
        params["duration"] = round(min(params["duration"], max(0.8, max_duration)), 2)
    return params


def generate_batch(out: Path, count: int, rows: int, seed: int) -> list:
    rng = random.Random(seed)
    written = []
    for index in range(count):
        profile = pick_profile(rng)
        params = jitter(rng, profile)
        spec = ScenarioSpec(profile=profile, params=params)
        scenario = build_case(f"scn_{index:03d}", spec, rows)
        written.extend(write_case(scenario, out / scenario.case_id))
    return written


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scenario_cases",
        description="运动学级仿真场景：生成带真值的测试数据，并入库管理")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="生成一批场景（四路 CSV + 真值）")
    gen.add_argument("--out", type=Path, required=True, help="输出目录（每个场景一个子目录）")
    gen.add_argument("--count", type=int, default=20, help="场景条数，默认 20")
    gen.add_argument("--rows", type=int, default=601, help="每条场景采样点数，默认 601（约 60 秒）")
    gen.add_argument("--seed", type=int, default=20261018, help="随机种子，相同种子结果可复现")
    gen.add_argument("--profiles", default=",".join(PROFILE_NAMES),
                     help="参与混合的模板，逗号分隔；默认全部")

    lib = sub.add_parser("library", help="场景库：入库、检索、导出")
    lib_sub = lib.add_subparsers(dest="library_command", required=True)

    add = lib_sub.add_parser("add", help="把场景目录登记入库（内容指纹去重）")
    add.add_argument("directories", nargs="+", type=Path,
                     help="场景目录（含四路 CSV、truth.json 与 source.json）")
    add.add_argument("--db", type=Path, default=DEFAULT_DB,
                     help="库文件路径，默认 scenario_library.db")

    listing = lib_sub.add_parser("list", help="按条件检索场景")
    listing.add_argument("--db", type=Path, default=DEFAULT_DB)
    listing.add_argument("--profile", help="按车速曲线模板筛选，如 hard_brake")
    listing.add_argument("--event", help="按真值事件类型筛选，如 deceleration")
    listing.add_argument("--min-decel", type=float, help="减速度下限（m/s²）")
    listing.add_argument("--max-decel", type=float, help="减速度上限（m/s²）")
    listing.add_argument("--min-v0", type=float, help="初速度下限（m/s）")
    listing.add_argument("--max-v0", type=float, help="初速度上限（m/s）")
    listing.add_argument("--with-events", choices=["yes", "no"],
                         help="只要 有/没有 真值事件的场景")
    listing.add_argument("--json", action="store_true", help="以 JSON 输出，便于脚本消费")

    show = lib_sub.add_parser("show", help="查看单条场景的详情与真值")
    show.add_argument("scenario_id")
    show.add_argument("--db", type=Path, default=DEFAULT_DB)

    export = lib_sub.add_parser("export", help="把库中场景原样导出到目录")
    export.add_argument("scenario_id")
    export.add_argument("out", type=Path, help="输出目录")
    export.add_argument("--db", type=Path, default=DEFAULT_DB)

    stats = lib_sub.add_parser("stats", help="库整体统计")
    stats.add_argument("--db", type=Path, default=DEFAULT_DB)

    regress = sub.add_parser("regress", help="闭环回归：生成 → case-analyzer 全跑 → 对真值 → 检出率/误报率")
    regress.add_argument("--count", type=int, default=20, help="场景条数，默认 20")
    regress.add_argument("--seed", type=int, default=20261019, help="随机种子，可复现")
    regress.add_argument("--rows", type=int, default=601, help="每条采样点数，默认 601")
    regress.add_argument("--out", type=Path, default=None,
                         help="工作目录（场景、runtime、报告）；默认临时目录，跑完即清")
    regress.add_argument("--keep", action="store_true", help="保留默认临时目录中的现场")
    regress.add_argument("--analyzer", type=Path, default=None,
                         help="case-analyzer 源码目录；默认读 CASE_ANALYZER_ROOT 或同级目录")
    regress.add_argument("--coverage-rule", type=float, default=0.9,
                         help="真值事件被判检出的候选覆盖阈值，默认 0.9")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "generate":
        return _run_generate(args)
    if args.command == "library":
        return _run_library(args)
    if args.command == "regress":
        return _run_regress(args)
    return 2


def _run_regress(args) -> int:
    # 延迟导入：regress 依赖 case-analyzer 环境，generate/library 不依赖
    from scenario_cases.regress import format_report, run_regression

    try:
        report = run_regression(count=args.count, seed=args.seed, rows=args.rows,
                                out=args.out, analyzer=args.analyzer, keep=args.keep,
                                coverage_rule=args.coverage_rule)
    except ValueError as error:
        print(f"回归未开始：{error}", file=sys.stderr)
        return 2
    print(format_report(report))
    return 0


def _run_generate(args) -> int:
    if args.count < 1 or args.rows < 10:
        print("count 至少 1，rows 至少 10", file=sys.stderr)
        return 2
    unknown = [name for name in args.profiles.split(",") if name and name not in PROFILE_NAMES]
    if unknown:
        print(f"未知模板：{', '.join(unknown)}；可用：{', '.join(PROFILE_NAMES)}", file=sys.stderr)
        return 2
    allowed = [name for name in args.profiles.split(",") if name]
    weights = {name: MIX.get(name, 0.0) for name in allowed}
    total = sum(weights.values()) or 1.0
    for name in allowed:
        MIX[name] = weights[name] / total

    files = generate_batch(args.out, args.count, args.rows, args.seed)
    print(f"已生成 {args.count} 条场景 → {args.out}（{len(files)} 个文件，种子 {args.seed}，"
          f"生成器 {GENERATOR_VERSION}）")
    print("模板混合：" + "，".join(f"{name} {MIX[name]:.0%}" for name in allowed))
    print("数据为公式合成的运动学级序列，不是道路采集或闭环仿真。")
    return 0


def _format_record(record) -> str:
    params = " ".join(f"{key}={value}" for key, value in sorted(record.params.items()))
    events = f"{record.event_count} 个事件" if record.event_count else "无事件（负样本）"
    return f"{record.scenario_id}  {record.profile}  {params}  {record.rows} 行  {events}"


def _expand_targets(paths) -> list:
    """add 的参数既可以是单个场景目录，也可以是批量目录（自动展开其中的场景子目录）。"""
    targets = []
    for path in paths:
        path = Path(path)
        if not path.is_dir() or (path / TRUTH_FILE).is_file():
            targets.append(path)
            continue
        children = sorted(child for child in path.iterdir()
                          if child.is_dir() and (child / TRUTH_FILE).is_file())
        targets.extend(children or [path])
    return targets


def _run_library(args) -> int:
    library = ScenarioLibrary(args.db)
    if args.library_command == "add":
        summary = library.add_many(_expand_targets(args.directories))
        for result in summary["added"]:
            print(f"入库 {result.scenario_id}（{result.detail}）")
        for result in summary["duplicate"]:
            print(f"跳过 {result.scenario_id}（{result.detail}）")
        for directory, message in summary["failed"]:
            print(f"失败 {directory}：{message}", file=sys.stderr)
        print(f"合计：新增 {len(summary['added'])}，重复 {len(summary['duplicate'])}，"
              f"失败 {len(summary['failed'])}")
        return 1 if summary["failed"] else 0

    if args.library_command == "list":
        with_events = {"yes": True, "no": False}.get(args.with_events)
        records = library.query(profile=args.profile, event_kind=args.event,
                                min_decel=args.min_decel, max_decel=args.max_decel,
                                min_v0=args.min_v0, max_v0=args.max_v0,
                                with_events=with_events)
        if args.json:
            print(json.dumps([record.as_dict() for record in records],
                             ensure_ascii=False, indent=2))
        else:
            for record in records:
                print(_format_record(record))
            print(f"共 {len(records)} 条")
        return 0

    if args.library_command == "show":
        record = library.get(args.scenario_id)
        if record is None:
            print(f"库中没有 {args.scenario_id}", file=sys.stderr)
            return 1
        print(json.dumps(record.as_dict(), ensure_ascii=False, indent=2))
        return 0

    if args.library_command == "export":
        written = library.export(args.scenario_id, args.out)
        if not written:
            print(f"库中没有 {args.scenario_id}", file=sys.stderr)
            return 1
        print(f"已导出 {args.scenario_id} → {args.out}（{len(written)} 个文件，字节级一致）")
        return 0

    if args.library_command == "stats":
        summary = library.stats()
        print(f"共 {summary['total']} 条 · 有事件 {summary['with_events']} · "
              f"无事件 {summary['without_events']} · 合计 {summary['rows_total']} 行")
        for profile, count in summary["by_profile"].items():
            print(f"  {profile}: {count}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
