"""命令行入口：python -m scenario_cases <generate|library|suite|regress> ..."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import sys
from pathlib import Path

from scenario_cases.generator import GENERATOR_VERSION, build_case, write_case
from scenario_cases.models import ScenarioSpec
from scenario_cases.store import TRUTH_FILE, ScenarioError, ScenarioLibrary, SuiteError
from scenario_cases.templates import (DEFAULTS, PROFILE_NAMES, param_range)

# 批量时的模板配比：典型组占六成，对抗组占三成，负样本一成。
# 对抗组专门打口径边界——如果闭环还是 100% 检出 0 误报，说明样本造得太乖。
MIX = {"hard_brake": 0.24, "mild_decel": 0.20, "lead_cut_in": 0.16, "cruise": 0.10,
       "sub_threshold": 0.10, "short_dip": 0.08, "v_dip": 0.06, "jitter": 0.04,
       "double_dip": 0.02}

DEFAULT_DB = Path("scenario_library.db")

# 时间类参数抖动后对齐 0.1 秒采样网格：不对齐会让制动窗落在半个间隔上，
# 边界样本的"应有结论"变得没法手算复核。
GRID_KEYS = ("duration", "at", "dip", "recover", "gap")

# 人工指定批次名时的合法字符：会进目录名和数据库 ID，窄一点不惹事
TAG_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,24}")


def _snap(value: float) -> float:
    return round(value * 10) / 10


def batch_tag(seed: int, rows: int, mix: dict | None = None, tag: str | None = None) -> str:
    """批次命名空间：显式 tag 优先，否则由配置算短哈希。

    为什么需要它：每批都从 scn_000 编号时，第二批入库会和第一批撞 ID
    （同 ID 不同内容 → 入库失败），场景库没法跨批次持续积累。
    同一种子 + 行数 + 模板配比 → 同一标签（可复现，重复入库靠内容指纹去重）；
    任何一项不同 → 不同标签（两批互不撞 ID，可同时入库）。
    配比按名字排序后计入哈希，生成器版本也计入：修复抽样顺序这类
    “同配置不同内容”的问题时升 minor 版本，新旧批次自动换命名空间。
    刻意不含 count：条数不改变单条场景的内容，多生成几条应与少生成几条
    共用命名空间，让指纹去重去识别重复，而不是换一批新 ID。
    """
    if tag:
        return tag
    mix_key = json.dumps(sorted((mix or MIX).items()), ensure_ascii=False)
    digest = hashlib.sha1(f"{GENERATOR_VERSION}|{seed}|{rows}|{mix_key}".encode("utf-8"))
    return digest.hexdigest()[:6]


def pick_profile(rng: random.Random, mix: dict | None = None) -> str:
    # 必须按名字排序遍历：batch_tag 的哈希口径就是 sorted(mix)，这里若按
    # dict 原序遍历（--profiles 的书写顺序），同配比不同写法会算出同一 tag
    # 却抽出不同模板序列——同 ID 不同内容，入库必撞。两处口径保持一致。
    weights = MIX if mix is None else mix
    roll = rng.random()
    cumulative = 0.0
    for name, weight in sorted(weights.items()):
        cumulative += weight
        if roll <= cumulative:
            return name
    return sorted(weights)[-1]  # 权重和为 1 时不会走到；兜底返回排序最后一个


def jitter(rng: random.Random, profile: str) -> dict:
    """在模板范围内抖动参数；没有范围的参数保持默认值不动。"""
    params = {}
    for key, value in DEFAULTS[profile].items():
        bounds = param_range(profile, key)
        if bounds is None:
            params[key] = value
            continue
        low, high = bounds
        drawn = round(rng.uniform(low, high), 2)
        params[key] = _snap(drawn) if key in GRID_KEYS else drawn
    # 联合约束：单个参数合法不代表组合合法——末端车速至少保留 3 m/s 余量。
    # 只对减速度与时长都参与抖动的模板生效（典型组与 sub_threshold）；
    # double_dip 时长范围很窄、约束恒成立，jitter/v_dip 等没有这两个键。
    # 上限同样向下取到 0.1 秒网格：取整只会更保守，不会把末端余量吃掉，
    # 也不会把对齐网格的时长又抖成半个间隔。
    if "decel" in params and "duration" in params:
        max_duration = math.floor((params["v0"] - 3.0) / params["decel"] * 10) / 10
        params["duration"] = min(params["duration"], max(0.8, max_duration))
    return params


def generate_batch(out: Path, count: int, rows: int, seed: int, mix: dict | None = None,
                   tag: str | None = None) -> list:
    namespace = batch_tag(seed, rows, mix, tag)
    rng = random.Random(seed)
    written = []
    for index in range(count):
        profile = pick_profile(rng, mix)
        params = jitter(rng, profile)
        spec = ScenarioSpec(profile=profile, params=params)
        scenario = build_case(f"scn_{namespace}_{index:03d}", spec, rows)
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
    gen.add_argument("--tag", default=None,
                     help="批次命名空间（字母/数字/-/_，1-24 字符）；默认由配置自动生成短哈希。"
                          "场景 ID 为 scn_<tag>_序号，不同批次不会撞 ID")

    lib = sub.add_parser("library", help="场景库：入库、检索、导出")
    lib_sub = lib.add_subparsers(dest="library_command", required=True)

    add = lib_sub.add_parser("add", help="把场景目录登记入库（内容指纹去重）")
    add.add_argument("directories", nargs="+", type=Path,
                     help="场景目录（含四路 CSV、truth.json 与 source.json）")
    add.add_argument("--db", type=Path, default=DEFAULT_DB,
                     help="库文件路径，默认 scenario_library.db")

    listing = lib_sub.add_parser("list", help="按条件检索场景")
    listing.add_argument("--db", type=Path, default=DEFAULT_DB)
    listing.add_argument("--profile", help="按车速曲线模板筛选，如 hard_brake；"
                                           "逗号分隔可多选，如 sub_threshold,jitter")
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

    suite = sub.add_parser("suite", help="回归集：按查询条件固化命名快照，导出带标准答案的卷子")
    suite_sub = suite.add_subparsers(dest="suite_command", required=True)

    suite_add = suite_sub.add_parser("add", help="按查询条件创建回归集（成员存快照）")
    suite_add.add_argument("name", help="回归集名字，如 smoke-v1")
    suite_add.add_argument("--db", type=Path, default=DEFAULT_DB)
    suite_add.add_argument("--profile", help="按车速曲线模板筛选，如 hard_brake；"
                                             "逗号分隔可多选，如 sub_threshold,jitter")
    suite_add.add_argument("--event", help="按真值事件类型筛选，如 deceleration")
    suite_add.add_argument("--min-decel", type=float, help="减速度下限（m/s²）")
    suite_add.add_argument("--max-decel", type=float, help="减速度上限（m/s²）")
    suite_add.add_argument("--min-v0", type=float, help="初速度下限（m/s）")
    suite_add.add_argument("--max-v0", type=float, help="初速度上限（m/s）")
    suite_add.add_argument("--with-events", choices=["yes", "no"],
                           help="只要 有/没有 真值事件的场景")

    suite_list = suite_sub.add_parser("list", help="列出所有回归集")
    suite_list.add_argument("--db", type=Path, default=DEFAULT_DB)

    suite_show = suite_sub.add_parser("show", help="查看回归集详情（成员与真值事件数）")
    suite_show.add_argument("name")
    suite_show.add_argument("--db", type=Path, default=DEFAULT_DB)

    suite_export = suite_sub.add_parser("export", help="导出整个回归集（场景目录 + manifest.json）")
    suite_export.add_argument("name")
    suite_export.add_argument("out", type=Path, help="输出目录")
    suite_export.add_argument("--db", type=Path, default=DEFAULT_DB)

    regress = sub.add_parser("regress", help="闭环回归：生成 → case-analyzer 全跑 → 对真值 → 检出率/误报率")
    regress.add_argument("--count", type=int, default=None, help="场景条数，默认 20")
    regress.add_argument("--seed", type=int, default=None, help="随机种子，可复现，默认 20261019")
    regress.add_argument("--rows", type=int, default=601, help="每条采样点数，默认 601")
    regress.add_argument("--cases", type=Path, default=None,
                         help="固定案例模式：场景目录（每个子目录是一例，四路 CSV）。"
                              "与 --labels 同时提供，不再现生成")
    regress.add_argument("--labels", type=Path, default=None,
                         help="固定案例模式：外部标签目录（<case_id>.json，events 字段）。"
                              "与 --cases 同时提供")
    regress.add_argument("--manifest", type=Path, default=None,
                         help="固定集成员与逐案例行为断言；默认读取 cases/labels 共同上级的 manifest.json")
    regress.add_argument("--out", type=Path, default=None,
                         help="工作目录（场景、runtime、报告）；默认临时目录，跑完即清")
    regress.add_argument("--keep", action="store_true", help="保留默认临时目录中的现场")
    regress.add_argument("--analyzer", type=Path, default=None,
                         help="case-analyzer 源码目录；默认读 CASE_ANALYZER_ROOT 或同级目录")
    regress.add_argument("--coverage-rule", type=float, default=0.9,
                         help="真值事件被判检出的候选覆盖阈值，默认 0.9")
    regress.add_argument("--check", action="store_true",
                         help="门禁模式：导入失败/口径误判/实现不一致/漏检/确认漏检/"
                              "噪声占比/结构不一致任一未过即返回非零退出码"
                              "（供 CI 或本地发布前拦截）")
    regress.add_argument("--max-noise-candidates", type=float, default=0.25,
                         help="--check 的误报候选占比容忍上限，默认 0.25"
                              "（宽筛固有噪声，不要求零）")
    regress.add_argument("--max-missed", type=int, default=0,
                         help="--check 允许的漏检真值事件数（候选层），默认 0")
    regress.add_argument("--max-confirmed-missed", type=int, default=0,
                         help="--check 允许的确认漏检数：被候选覆盖但确认区间未覆盖的"
                              "真值事件（确认层），默认 0。候选正常而确认能力退化时"
                              "只有此项能暴露")
    regress.add_argument("--max-misjudged", type=int, default=0,
                         help="--check 允许的口径误判候选数，默认 0（这是实现 bug，应为 0）")
    regress.add_argument("--min-agreement", type=float, default=1.0,
                         help="--check 要求的实现一致率下限，默认 1.0")
    regress.add_argument("--max-import-failures", type=int, default=0,
                         help="--check 允许的导入失败条数，默认 0")
    regress.add_argument("--max-structural-mismatch", type=int, default=0,
                         help="--check 允许的结构不一致案例数，默认 0")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "generate":
        return _run_generate(args)
    if args.command == "library":
        return _run_library(args)
    if args.command == "suite":
        return _run_suite(args)
    if args.command == "regress":
        return _run_regress(args)
    return 2


def _run_regress(args) -> int:
    # 延迟导入：regress 依赖 case-analyzer 环境，且它反向依赖本模块的
    # generate_batch，顶层导入会形成循环
    from scenario_cases.regress import (DEFAULT_COUNT, DEFAULT_SEED, check_report,
                                        format_gates, format_report,
                                        run_fixed_regression, run_regression)

    fixed = bool(args.cases) or bool(args.labels)
    if args.manifest and not fixed:
        print("--manifest 需要 --cases 与 --labels", file=sys.stderr)
        return 2
    if fixed:
        if not (args.cases and args.labels):
            print("--cases 与 --labels 必须同时提供", file=sys.stderr)
            return 2
        if args.count is not None or args.seed is not None:
            print("固定案例模式（--cases/--labels）不能与 --count/--seed 混用", file=sys.stderr)
            return 2

    if not fixed and ((args.count is not None and args.count < 1) or args.rows < 10):
        print("count 至少 1，rows 至少 10", file=sys.stderr)
        return 2

    try:
        if fixed:
            report = run_fixed_regression(args.cases, args.labels, out=args.out,
                                          analyzer=args.analyzer, keep=args.keep,
                                          coverage_rule=args.coverage_rule, manifest=args.manifest)
        else:
            report = run_regression(
                count=args.count if args.count is not None else DEFAULT_COUNT,
                seed=args.seed if args.seed is not None else DEFAULT_SEED,
                rows=args.rows,
                out=args.out, analyzer=args.analyzer, keep=args.keep,
                coverage_rule=args.coverage_rule)
    except ValueError as error:
        print(f"回归未开始：{error}", file=sys.stderr)
        return 2
    print(format_report(report))
    if args.check:
        result = check_report(report, gates={
            "max_import_failures": args.max_import_failures,
            "max_misjudged": args.max_misjudged,
            "min_agreement": args.min_agreement,
            "max_missed": args.max_missed,
            "max_confirmed_missed": args.max_confirmed_missed,
            "max_noise_candidates": args.max_noise_candidates,
            "max_structural_mismatch": args.max_structural_mismatch})
        print(format_gates(result))
        # 阈值快照与逐项结果回写报告文件：报告从此自带"按什么阈值判定、
        # 判定结果如何"，脱离命令行也能读
        report["gates"] = result["limits"]
        report["check"] = {"passed": result["passed"],
                           "gates": [{k: item[k] for k in ("gate", "ok", "actual", "limit")}
                                     for item in result["gates"]]}
        report_path = report.get("report_path")
        if report_path:
            import json as _json
            from pathlib import Path as _Path
            _Path(report_path).write_text(
                _json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0 if result["passed"] else 1
    return 0


def _run_generate(args) -> int:
    if args.count < 1 or args.rows < 10:
        print("count 至少 1，rows 至少 10", file=sys.stderr)
        return 2
    unknown = [name for name in args.profiles.split(",") if name and name not in PROFILE_NAMES]
    if unknown:
        print(f"未知模板：{', '.join(unknown)}；可用：{', '.join(PROFILE_NAMES)}", file=sys.stderr)
        return 2
    if args.tag is not None and not TAG_PATTERN.fullmatch(args.tag):
        print("--tag 只能包含字母、数字、-、_（1-24 字符）", file=sys.stderr)
        return 2
    allowed = [name for name in args.profiles.split(",") if name]
    # 子集配比在局部归一化，不碰全局 MIX——原地改全局会让同进程内
    # 后续批次混进上一次的模板（roll 的上界变成权重和，直接错位）。
    # 归一化后按名字排序落库：让 mix 从构造起就有确定顺序，消费端
    # （pick_profile、batch_tag）即使漏排序也不会因书写顺序分叉。
    weights = {name: MIX.get(name, 0.0) for name in allowed}
    total = sum(weights.values()) or 1.0
    mix = {name: weight / total for name, weight in sorted(weights.items())}
    namespace = batch_tag(args.seed, args.rows, mix, args.tag)

    files = generate_batch(args.out, args.count, args.rows, args.seed, mix, namespace)
    print(f"已生成 {args.count} 条场景 → {args.out}（{len(files)} 个文件，种子 {args.seed}，"
          f"批次 {namespace}，生成器 {GENERATOR_VERSION}）")
    print("模板混合：" + "，".join(f"{name} {mix[name]:.0%}" for name in allowed))
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
        records = library.query(**_query_kwargs(args))
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


def _query_kwargs(args) -> dict:
    """library list 与 suite add 共用的查询条件解析（含 with-events 三元转换、
    profile 逗号分隔多模板拆分为列表）。"""
    profiles = [name for name in (args.profile or "").split(",") if name]
    return {"profile": profiles or None, "event_kind": args.event,
            "min_decel": args.min_decel, "max_decel": args.max_decel,
            "min_v0": args.min_v0, "max_v0": args.max_v0,
            "with_events": {"yes": True, "no": False}.get(args.with_events)}


def _run_suite(args) -> int:
    library = ScenarioLibrary(args.db)
    try:
        if args.suite_command == "add":
            records = library.query(**_query_kwargs(args))
            if not records:
                print("查询没有命中任何场景，回归集未创建", file=sys.stderr)
                return 1
            suite = library.create_suite(args.name, _query_kwargs(args), records)
            print(f"回归集 {suite.name!r} 已创建：{len(suite.scenario_ids)} 条场景、"
                  f"{suite.event_total} 个真值事件（成员为快照，库后续新增不影响）")
            return 0

        if args.suite_command == "list":
            suites = library.list_suites()
            for suite in suites:
                print(f"{suite.name}  {len(suite.scenario_ids)} 条 · "
                      f"{suite.event_total} 事件 · 建于 {suite.created_at}")
            print(f"共 {len(suites)} 个")
            return 0

        if args.suite_command == "show":
            suite = library.get_suite(args.name)
            if suite is None:
                print(f"回归集 {args.name!r} 不存在", file=sys.stderr)
                return 1
            print(json.dumps(suite.as_dict(), ensure_ascii=False, indent=2))
            return 0

        if args.suite_command == "export":
            manifest = library.export_suite(args.name, args.out)
            suite = library.get_suite(args.name)
            print(f"回归集 {args.name!r} 已导出 → {args.out}"
                  f"（{len(suite.scenario_ids)} 条场景 + manifest.json，字节级一致）")
            print(f"清单文件：{manifest}")
            return 0
    except (SuiteError, ScenarioError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
