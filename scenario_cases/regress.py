"""闭环回归：生成 → 调用 case-analyzer 全跑 → 对真值 → 检出率/误报率报告。

与 case-analyzer 自带冻结评测的分工：
- 那边是"人工盲标 → 揭盲 → 算指标"，人当裁判；
- 这边是"参考实现从生成数据算真值 → 工具全跑 → 对答案"，两个独立实现互相当裁判，
  全自动可复现。真值不记注入参数——那会和工具共享假设，100% 是循环论证。

两层口径（case-analyzer 的有意设计）：
- 宽筛层：候选窗口门槛更低（0.6 m/s 降幅、3 个下降间隔、0.2 m/s 回升），
  目的是不漏，噪声留给复核层；
- 口径层：SD-WINDOW-1.1 事件定义（1.0 m/s、4 个下降间隔、0.1 m/s 回升）。
  参考真值按口径层算；每个候选自带 event_assessment 数值判定。

因此指标分两层读：
- 检出率/误报率：宽筛层——协议事件应被候选覆盖（宽筛更松，理应 100%），
  误报主要来自亚门槛样本，这是宽筛的固有噪声；
- 确认率/口径误判：口径层——事件被"自判符合口径"的候选覆盖才算确认；
  自判符合口径却没有真值事件支撑的候选，才是实现 bug。
- 实现一致率：参考实现算出的事件集 vs 工具自带口径参考区间，逐条比对。

误报指标按两个口径分别报（都**不是**经典假阳性率 FPR——FPR 的分母是
FP+TN，本工具没有负例单位，套用这个名字只会引起歧义）：
- 候选级"误报候选占比"：无真值支撑的候选项 ÷ 总候选项。衡量复核工作量；
- 簇级"误报簇占比"：无真值支撑的候选簇 ÷ 总候选簇。衡量事件质量——
  接力候选（两个候选接力盖住同一事件）在候选级会重复计数，簇级不会。

两种输入模式：
- 生成模式（默认）：现生成批次全自动跑，回答"工具对合成场景稳不稳"；
- 固定案例模式（--cases/--labels）：对拍既有案例集（如冻结评测集的困难
  案例），真值取外部人工标签，回答"工具对已知困难输入的行为是否符合预期"。
  两种模式的报告带同样的元数据（代码版本、数据指纹、协议、阈值），数字可同表比较。

报告元数据（回答"这批数字是什么时候、哪份代码、对哪份数据、按什么阈值跑的"）：
- code_version：scenario-cases 与 case-analyzer 两个仓库的 git HEAD；
- data_fingerprint：输入目录的内容哈希（生成模式对批次、固定模式对案例与标签）；
- --check 时另存 gates 阈值快照与逐项结果。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from scenario_cases.cli import generate_batch
from scenario_cases.generator import GENERATOR_VERSION
from scenario_cases.protocol import PROTOCOL_ID
from scenario_cases.fixed_suite import load_fixed_truth, load_manifest, check_behavior

VERSION = "closed-loop-regress-2.2"
COVERAGE_RULE = 0.9
DEFAULT_COUNT = 20
DEFAULT_ROWS = 601
DEFAULT_SEED = 20261019
SOURCE_NAME = "scenario-cases 闭环回归批次"
SOURCE_NAME_FIXED = "scenario-cases 固定案例集对拍"
ACCELERATION_SOURCE = "由车速有限差分得到，不是独立传感器测量"
MEETS = "meets_numeric_rule"

COUNTER_KEYS = ("cases", "truth_events", "detected_events", "confirmed_events",
                "candidates", "false_positive_candidates", "false_positive_clusters",
                "misjudged_candidates", "screening_noise_candidates",
                "candidate_clusters", "structural_mismatch_cases")


def default_analyzer_root() -> Path:
    """默认把同级目录的 case-analyzer-oss 当作被评测工具。"""
    return Path(__file__).resolve().parents[2] / "case-analyzer-oss"


def locate_analyzer(explicit=None) -> Path:
    """定位 case-analyzer 源码目录：显式参数 > 环境变量 > 默认同级路径。

    显式参数和环境变量只要给了就必须有效——静默回落到默认路径会让调用方
    以为跑在自己的目录上；两者都没给才用默认同级路径。
    """
    given = []
    if explicit:
        given.append(Path(explicit))
    env = os.environ.get("CASE_ANALYZER_ROOT")
    if env:
        given.append(Path(env))
    for candidate in given:
        if (candidate / "app" / "cli_import.py").is_file():
            return candidate
        raise ValueError(
            f"指定的 case-analyzer 目录无效：{candidate}（缺少 app/cli_import.py）。"
            f"请用 --analyzer 或环境变量 CASE_ANALYZER_ROOT 指定其源码目录")
    fallback = default_analyzer_root()
    if (fallback / "app" / "cli_import.py").is_file():
        return fallback
    raise ValueError(
        f"找不到 case-analyzer（默认位置：{fallback}）；"
        f"用 --analyzer 或环境变量 CASE_ANALYZER_ROOT 指定其源码目录")


def _merge(intervals):
    """把重叠或相接的区间合并成簇；不相邻的保持独立。"""
    parts = sorted((float(a), float(b)) for a, b in intervals if float(b) > float(a))
    merged: list[list[float]] = []
    for start, end in parts:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def merged_coverage(event, intervals) -> float:
    """事件区间被候选区间集覆盖的比例；先合并重叠候选再计算。

    输入 event 是 [start,end]，intervals 是候选区间列表；输出 0～1。
    边界处理：先丢掉零长度候选（end≤start 不参与覆盖），重叠候选先合并，
    避免同一段时间被重复累计；事件本身零长度直接返回 0。
    """
    start, end = float(event[0]), float(event[1])
    if end <= start:
        return 0.0
    covered = 0.0
    for cluster_start, cluster_end in _merge(intervals):
        # 只累加事件与候选簇的真实交集长度；两簇因已合并不会重叠。
        overlap = min(end, cluster_end) - max(start, cluster_start)
        if overlap > 0:
            covered += overlap
    return covered / (end - start)


def score_cases(cases: dict, coverage_rule: float = COVERAGE_RULE) -> dict:
    """逐案例对真值，再按模板汇总。cases 结构见 run_regression。"""
    by_profile: dict[str, dict] = {}
    boundary = dict.fromkeys(COUNTER_KEYS, 0)
    totals = dict.fromkeys(COUNTER_KEYS, 0)
    per_case = []
    quality_cases = []
    for case_id, case in cases.items():
        if case.get("decision") == "uncertain":
            quality_cases.append({"case_id": case_id, "decision": "uncertain",
                                  "reason": "参考标签无法判断，不计事件检出、误报及区间一致性",
                                  "candidate_assessments": [c["event_assessment"] for c in case["candidates"]],
                                  "unexpected_numeric_confirmations": sum(
                                      c["event_assessment"] == MEETS for c in case["candidates"])})
            continue
        truth = [[event["start"], event["end"]] for event in case["truth_events"]]
        intervals = [[candidate["start_time"], candidate["end_time"]]
                     for candidate in case["candidates"]]
        meets = [[candidate["start_time"], candidate["end_time"]]
                 for candidate in case["candidates"]
                 if candidate.get("event_assessment") == MEETS]
        detected, missed = [], []
        for index, event in enumerate(truth):
            # 检出判定与 case-analyzer evaluation.py 同口径：候选区间集合先合并，
            # 再看对真值事件的覆盖比例——两个候选接力盖住一个事件也算检出。
            coverage = merged_coverage(event, intervals)
            record = {"index": index, "event": case["truth_events"][index],
                      "merged_coverage": round(coverage, 4)}
            (detected if coverage >= coverage_rule - 1e-9 else missed).append(record)
        # 确认判定：只认"自判符合口径"的候选——工具自己说低于数值规则的候选
        # 盖住事件不算确认，那说明它没把握住这个事件。
        confirmed = sum(1 for event in truth
                        if merged_coverage(event, meets) >= coverage_rule - 1e-9)
        # 误报判定按候选簇：先合并成簇，再看簇有没有主要覆盖某个真值事件。
        # 接力盖住同一个事件的两个候选不各算一次误报——那是切碎，不是编造。
        # 簇级计数用于事件质量口径；下面逐候选的列表保留候选级工作量口径，
        # 每个候选标注所属簇，两个口径都能从同一份数据算出来。
        clusters = _merge(intervals)
        cluster_best = [max((merged_coverage(event, [cluster]) for event in truth),
                            default=0.0) for cluster in clusters]
        false_positive_clusters = sum(1 for best in cluster_best
                                      if best < coverage_rule - 1e-9)
        false_positives = []
        for candidate in case["candidates"]:
            # 找当前候选所属的簇：簇由 _merge 生成且候选端点必落在簇内。
            # 1e-9 容差吸收浮点边界，避免“数学上相接、浮点上差 1e-16”时找不到簇。
            cluster_index = next(i for i, item in enumerate(clusters)
                                 if item[0] <= candidate["start_time"] + 1e-9
                                 and candidate["end_time"] <= item[1] + 1e-9)
            cluster = clusters[cluster_index]
            best = cluster_best[cluster_index]
            # 误报按“簇没有主要覆盖任何真值事件”判定；候选本身只负责
            # 标注它属于哪个簇、自带什么数值判定，供两个口径分别统计。
            if best < coverage_rule - 1e-9:
                false_positives.append({"candidate_id": candidate.get("candidate_id"),
                                        "interval": [candidate["start_time"], candidate["end_time"]],
                                        "cluster": [round(cluster[0], 3), round(cluster[1], 3)],
                                        "cluster_coverage": round(best, 4),
                                        "event_assessment": candidate.get("event_assessment")})
        misjudged = sum(1 for item in false_positives
                        if item["event_assessment"] == MEETS)
        # 结构不一致只数有真值事件的案例：负样本多出候选已经被误报列表体现，
        # 这里要抓的是正样本上"两个事件被并成一个候选"或"一个事件被切成一堆"。
        entry = {"case_id": case_id, "profile": case["profile"], "notes": case.get("notes", []),
                 "truth_events": len(truth), "detected_events": len(detected),
                 "confirmed_events": confirmed,
                 "candidates": len(intervals), "false_positive_candidates": len(false_positives),
                 "false_positive_clusters": false_positive_clusters,
                 "misjudged_candidates": misjudged,
                 "screening_noise_candidates": len(false_positives) - misjudged,
                 "candidate_clusters": len(clusters),
                 "structural_mismatch_cases": 1 if truth and len(clusters) != len(truth) else 0,
                 "missed_events": missed, "false_positives": false_positives,
                 "cases": 1}
        per_case.append(entry)

        for bucket in (by_profile.setdefault(case["profile"], dict.fromkeys(COUNTER_KEYS, 0)),
                       totals):
            for key in COUNTER_KEYS:
                bucket[key] += entry[key]
        if case.get("notes"):
            for key in COUNTER_KEYS:
                boundary[key] += entry[key]

    def rate(numerator, denominator):
        return round(numerator / denominator, 4) if denominator else None

    return {"coverage_rule": coverage_rule,
            "input_cases": len(cases), "excluded_uncertain_cases": len(quality_cases),
            "quality_cases": quality_cases,
            "totals": dict(totals,
                           detection_rate=rate(totals["detected_events"], totals["truth_events"]),
                           confirmation_rate=rate(totals["confirmed_events"], totals["truth_events"]),
                           false_positive_candidate_ratio=rate(
                               totals["false_positive_candidates"], totals["candidates"]),
                           false_positive_cluster_ratio=rate(
                               totals["false_positive_clusters"], totals["candidate_clusters"]),
                           misjudged_candidate_ratio=rate(totals["misjudged_candidates"],
                                                          totals["candidates"]),
                           screening_noise_candidate_ratio=rate(
                               totals["screening_noise_candidates"], totals["candidates"])),
            "by_profile": by_profile,
            "boundary_cases": boundary,
            "cases": per_case}


def _agreement(cases: dict) -> dict:
    """参考实现（真值来源）与工具自带口径参考区间的一致性。"""
    matching, mismatches = 0, []
    assessed = 0
    for case_id, case in cases.items():
        if case.get("decision") == "uncertain":
            continue
        assessed += 1
        mine = sorted((event["start"], event["end"]) for event in case["truth_events"])
        tool = sorted((round(float(start), 3), round(float(end), 3))
                      for start, end in case.get("tool_reference_events", []))
        same = len(mine) == len(tool) and all(
            abs(a[0] - b[0]) < 5e-4 and abs(a[1] - b[1]) < 5e-4
            for a, b in zip(mine, tool))
        if same:
            matching += 1
        else:
            mismatches.append({"case_id": case_id, "profile": case["profile"],
                               "reference": mine, "tool": tool})
    return {"cases": assessed, "excluded_uncertain_cases": len(cases) - assessed, "matching": matching,
            "agreement_rate": round(matching / assessed, 4) if assessed else None,
            "mismatches": mismatches}


def _load_analyzer_modules(analyzer_root: Path):
    """把 case-analyzer 加进 sys.path 并导入闭环需要的模块。

    失败信息说人话：最常见的坑是运行环境缺 pandas/numpy，
    那正是 case-analyzer 自己 requirements 里的依赖。
    """
    root = str(analyzer_root)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from app.annotation_assist import build_annotation_assist
        from app.candidate_review import build_candidate_case
        from app.cli_import import run_import
        from app.paths import DEFAULT_RUNTIME
        from app.server import WorkbenchContext
        from app.workspace import initialize
    except ImportError as error:
        raise ValueError(
            f"导入 case-analyzer 模块失败：{error}。"
            f"闭环回归依赖 case-analyzer 的运行环境（pandas、numpy 等），"
            f"请用它的虚拟环境运行："
            f"case-analyzer-oss/.venv/Scripts/python.exe -m scenario_cases regress"
        ) from error
    return (build_candidate_case, build_annotation_assist, run_import,
            DEFAULT_RUNTIME, WorkbenchContext, initialize)


def _git_head(path) -> str | None:
    """取目录所在 git 仓库的当前 commit；不在仓库里或没有 git 时返回 None。

    报告要能回答“这批数字是哪份代码跑出来的”，所以代码版本进元数据；
    但发布包/解压目录里没有 .git，不能因此让回归跑不起来。
    """
    try:
        proc = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10)
        return proc.stdout.strip() if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _data_fingerprint(root) -> str | None:
    """目录内容指纹：所有文件（相对路径排序）的 SHA256 拼接后再取短哈希。

    回答“这批数字是对着哪份数据跑的”。文件很多时逐字节读取有成本，
    但回归批次最多几百个 CSV、单文件几百 KB，可接受。
    """
    root = Path(root)
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    count = 0
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
        count += 1
    return f"sha256:{digest.hexdigest()[:16]}:{count}files" if count else None


def _import_and_score(batch, *, modules, source_type, source_name, context,
                      coverage_rule, truth_events=None):
    """闭环流水线的公共部分：导入批次 → 读回候选与参考区间 → 评分。

    生成模式与固定案例模式共用；truth_events 为 None 时从批次的
    truth.json 读真值，固定模式则由调用方从外部标签目录提供。
    modules 是 _load_analyzer_modules 返回三元组的前三项。
    """
    build_candidate_case, build_annotation_assist, run_import = modules
    summary = run_import(batch, context, source_type, source_name, ACCELERATION_SOURCE)
    imported_ids = [item["case_id"] for item in summary["cases"]
                    if item["status"] == "imported"]
    import_failures = [{"case_id": item["case_id"], "status": item["status"],
                        "message": item["message"]}
                       for item in summary["cases"] if item["status"] != "imported"]
    cases = {}
    for case_id in imported_ids:
        if truth_events is None:
            truth = json.loads((batch / case_id / "truth.json").read_text(encoding="utf-8"))
            events, profile, notes = truth["events"], truth["spec"]["profile"], truth.get("notes", [])
        else:
            events = truth_events[case_id]["events"]
            profile = truth_events[case_id].get("profile", "external")
            notes = truth_events[case_id].get("notes", [])
        built = build_candidate_case(context.data_root / case_id)
        assist = build_annotation_assist(context.data_root / case_id)
        cases[case_id] = {"profile": profile,
                          "decision": truth_events[case_id]["decision"] if truth_events is not None else ("positive" if events else "negative"),
                          "truth_events": events, "notes": notes,
                          "candidates": [{"candidate_id": candidate["candidate_id"],
                                          "start_time": candidate["start_time"],
                                          "end_time": candidate["end_time"],
                                          "event_assessment": candidate["event_assessment"]}
                                         for candidate in built["candidates"]],
                          "tool_reference_events": [[event["start_time"], event["end_time"]]
                                                    for event in assist["reference_events"]]}
    return cases, summary, import_failures


def run_regression(*, count: int = DEFAULT_COUNT, seed: int = DEFAULT_SEED,
                   rows: int = DEFAULT_ROWS, out=None, analyzer=None,
                   keep: bool = False, coverage_rule: float = COVERAGE_RULE) -> dict:
    """一条命令跑完闭环：生成 → 导入 case-analyzer → 对真值 → 出报告。"""
    if count < 1 or rows < 10:
        raise ValueError("count 至少 1，rows 至少 10")
    analyzer_root = locate_analyzer(analyzer)
    (build_candidate_case, build_annotation_assist, run_import,
     DEFAULT_RUNTIME, WorkbenchContext, initialize) = _load_analyzer_modules(analyzer_root)

    started = datetime.now(timezone.utc).isoformat()
    workspace = Path(out) if out else Path(tempfile.mkdtemp(prefix="scenario-regress-"))
    workspace.mkdir(parents=True, exist_ok=True)
    report = None
    batch = workspace / "scenarios"
    runtime = workspace / "runtime"
    try:
        generate_batch(batch, count, rows, seed)
        initialize(runtime)
        context = WorkbenchContext(batch_dir=runtime / "index", data_root=runtime / "data",
                                   annotations_root=runtime / "annotations",
                                   evaluation_output=runtime / "evaluations",
                                   model_reviews_root=runtime / "model_reviews",
                                   automatic_review=False)
        cases, summary, import_failures = _import_and_score(
            batch, modules=(build_candidate_case, build_annotation_assist, run_import),
            source_type="synthetic", source_name=SOURCE_NAME,
            context=context, coverage_rule=coverage_rule)
        report = {"tool": VERSION, "started_at": started,
                  "generator": GENERATOR_VERSION, "analyzer_root": str(analyzer_root),
                  "code_version": {"scenario_cases": _git_head(Path(__file__).resolve().parents[1]),
                                   "case_analyzer": _git_head(analyzer_root)},
                  "data_fingerprint": _data_fingerprint(batch),
                  "seed": seed, "count": count, "rows_per_case": rows,
                  "protocol": PROTOCOL_ID,
                  "truth_basis": (f"{PROTOCOL_ID} 参考实现从生成数据计算"
                                  f"（非注入参数；与工具参考区间逐条比对）"),
                  "source": {"type": "synthetic", "name": SOURCE_NAME,
                             "note": "公式合成的运动学级场景，不是道路采集或闭环仿真"},
                  "import": summary["totals"], "import_failures": import_failures,
                  "metrics": score_cases(cases, coverage_rule),
                  "agreement": _agreement(cases), "cases": cases}
        report_path = workspace / "regression_report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
        report["report_path"] = str(report_path)
        return report
    finally:
        if not keep and not out:
            # 临时目录连同 runtime 一起清掉；报告已成型，需要留存现场就用
            # --keep 或 --out。路径一并置空，避免摘要指向已删除的文件。
            import shutil
            shutil.rmtree(workspace, ignore_errors=True)
            if report is not None:
                report["report_path"] = None


def run_fixed_regression(cases_dir, labels_dir, *, out=None, analyzer=None,
                         keep: bool = False, coverage_rule: float = COVERAGE_RULE, manifest=None) -> dict:
    """对拍固定案例集（困难案例/冻结集复用）：不现生成，直接导入给定目录。

    与 run_regression 的差别只在输入来源：真值来自 labels_dir 的人工标签，
    场景数据来自 cases_dir。报告同样带代码版本、数据指纹与全部口径字段，
    保证两种模式的数字可以放在同一张表里比较。
    """
    cases_dir, labels_dir = Path(cases_dir), Path(labels_dir)
    if not cases_dir.is_dir() or not labels_dir.is_dir():
        raise ValueError(f"固定案例模式需要 cases 与 labels 两个目录：{cases_dir} / {labels_dir}")
    truth = load_fixed_truth(cases_dir, labels_dir)
    if manifest is None and cases_dir.resolve().parent == labels_dir.resolve().parent:
        sibling = cases_dir.resolve().parent / "manifest.json"
        if sibling.is_file():
            manifest = sibling
    expectations = load_manifest(manifest, truth) if manifest is not None else None
    analyzer_root = locate_analyzer(analyzer)
    (build_candidate_case, build_annotation_assist, run_import,
     DEFAULT_RUNTIME, WorkbenchContext, initialize) = _load_analyzer_modules(analyzer_root)

    started = datetime.now(timezone.utc).isoformat()
    workspace = Path(out) if out else Path(tempfile.mkdtemp(prefix="scenario-regress-fixed-"))
    workspace.mkdir(parents=True, exist_ok=True)
    report = None
    runtime = workspace / "runtime"
    try:
        initialize(runtime)
        context = WorkbenchContext(batch_dir=runtime / "index", data_root=runtime / "data",
                                   annotations_root=runtime / "annotations",
                                   evaluation_output=runtime / "evaluations",
                                   model_reviews_root=runtime / "model_reviews",
                                   automatic_review=False)
        cases, summary, import_failures = _import_and_score(
            cases_dir, modules=(build_candidate_case, build_annotation_assist, run_import),
            source_type="synthetic", source_name=SOURCE_NAME_FIXED,
            context=context, coverage_rule=coverage_rule, truth_events=truth)
        report = {"tool": VERSION, "started_at": started,
                  "generator": GENERATOR_VERSION, "analyzer_root": str(analyzer_root),
                  "code_version": {"scenario_cases": _git_head(Path(__file__).resolve().parents[1]),
                                   "case_analyzer": _git_head(analyzer_root)},
                  "data_fingerprint": {"cases": _data_fingerprint(cases_dir),
                                       "labels": _data_fingerprint(labels_dir),
                                       "manifest_sha256": hashlib.sha256(Path(manifest).read_bytes()).hexdigest() if manifest else None},
                  "mode": "fixed-cases",
                  "manifest": str(manifest) if manifest else None,
                  "behavior": check_behavior(cases, expectations) if expectations is not None else None,
                  "cases_dir": str(cases_dir), "labels_dir": str(labels_dir),
                  "protocol": PROTOCOL_ID,
                  "truth_basis": (f"外部人工标签（{labels_dir}）；"
                                  f"{PROTOCOL_ID} 口径仅用于候选与参考区间比对"),
                  "source": {"type": "synthetic", "name": SOURCE_NAME_FIXED,
                             "note": "固定案例集对拍：数据来自既有冻结批次，"
                                     "不是道路采集或闭环仿真"},
                  "import": summary["totals"], "import_failures": import_failures,
                  "metrics": score_cases(cases, coverage_rule),
                  "agreement": _agreement(cases), "cases": cases}
        report_path = workspace / "regression_report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
        report["report_path"] = str(report_path)
        return report
    finally:
        if not keep and not out:
            import shutil
            shutil.rmtree(workspace, ignore_errors=True)
            if report is not None:
                report["report_path"] = None


def format_report(report: dict) -> str:
    """人类可读摘要：总量指标 → 分模板 → 边界样本 → 失败与漏检明细。"""
    metrics = report["metrics"]
    totals = metrics["totals"]
    agreement = report.get("agreement") or {"matching": 0, "cases": 0,
                                            "agreement_rate": None, "mismatches": []}
    lines = [
        ("固定案例回归（外部标签；unknown 单独评价；不调用 AI）" if report.get("mode") == "fixed-cases"
         else "闭环回归报告（真值 = SD-WINDOW-1.1 参考实现从生成数据算出，裁判全自动）"),
        "-" * 64,
        f"场景 {metrics.get('input_cases', totals['cases'])} 条 · 导入 {report['import'].get('imported', 0)} 条成功 · "
        f"失败 {len(report['import_failures'])} 条",
        f"真值事件 {totals['truth_events']} 个 · 检出 {totals['detected_events']} 个 · "
        f"检出率 {_percent(totals['detection_rate'])}（宽筛层）",
        f"候选 {totals['candidates']} 个 · 误报候选 {totals['false_positive_candidates']} 个 · "
        f"误报簇 {totals['false_positive_clusters']} 个 · "
        f"误报候选占比 {_percent(totals['false_positive_candidate_ratio'])} · "
        f"误报簇占比 {_percent(totals['false_positive_cluster_ratio'])}（宽筛层）",
        f"口径确认 {totals['confirmed_events']}/{totals['truth_events']}"
        f"（确认率 {_percent(totals['confirmation_rate'])}） · "
        f"口径误判候选 {totals['misjudged_candidates']} 个 · "
        f"宽筛噪声 {totals['screening_noise_candidates']} 个",
        f"结构不一致案例 {totals['structural_mismatch_cases']} 个"
        f"（有真值事件但候选簇数 ≠ 事件数） · "
        f"口径实现一致 {agreement['matching']}/{agreement['cases']}"
        f"（{_percent(agreement['agreement_rate'])}）",
        "",
        f"事件指标纳入 {totals['cases']} 条；无法判断标签排除 {metrics.get('excluded_uncertain_cases', 0)} 条",
        "分模板：",
    ]
    for profile, bucket in sorted(metrics["by_profile"].items()):
        lines.append(
            f"  {profile:<14} 场景 {bucket['cases']:>3} · 事件 {bucket['truth_events']:>3} · "
            f"检出 {bucket['detected_events']:>3}（{_percent(_rate(bucket['detected_events'], bucket['truth_events']))}） · "
            f"候选 {bucket['candidates']:>3} · 误报候选 {bucket['false_positive_candidates']:>3} · "
            f"误报簇 {bucket['false_positive_clusters']:>3} "
            f"（误判 {bucket['misjudged_candidates']} / 噪声 {bucket['screening_noise_candidates']}）")
    boundary = metrics["boundary_cases"]
    if boundary["cases"]:
        boundary_label = ("有场景备注的案例" if report.get("mode") == "fixed-cases"
                          else "贴边界/对抗样本（生成器标注）")
        lines += [
            "",
            f"{boundary_label}：{boundary['cases']} 条 · 检出率 "
            f"{_percent(_rate(boundary['detected_events'], boundary['truth_events']))} · "
            f"误报候选占比 {_percent(_rate(boundary['false_positive_candidates'], boundary['candidates']))} · "
            f"误报簇占比 {_percent(_rate(boundary['false_positive_clusters'], boundary['candidate_clusters']))} · "
            f"口径误判 {boundary['misjudged_candidates']} 个",
        ]
    for failure in report["import_failures"]:
        lines.append(f"导入失败 {failure['case_id']}：{failure['message']}")
    for entry in metrics["cases"]:
        if entry["missed_events"]:
            lines.append(f"漏检 {entry['case_id']}（{entry['profile']}）：" + "；".join(
                f"{item['event']['start']}～{item['event']['end']} 秒 "
                f"合并覆盖 {_percent(item['merged_coverage'])}"
                for item in entry["missed_events"]))
        if entry["false_positives"]:
            lines.append(f"误报 {entry['case_id']}（{entry['profile']}）：" + "；".join(
                f"{item['candidate_id']} {item['interval'][0]}～{item['interval'][1]} 秒"
                f"（{item['event_assessment']}）"
                for item in entry["false_positives"]))
        if entry["structural_mismatch_cases"]:
            lines.append(f"结构不一致 {entry['case_id']}（{entry['profile']}）："
                         f"候选簇 {entry['candidate_clusters']} 个 vs 真值事件 "
                         f"{entry['truth_events']} 个")
    for mismatch in agreement["mismatches"]:
        lines.append(f"口径实现不一致 {mismatch['case_id']}（{mismatch['profile']}）："
                     f"参考 {mismatch['reference']} vs 工具 {mismatch['tool']}")
    lines += ["", "完整报告：" + (report.get("report_path")
                                 or "（临时目录已清理；用 --out 或 --keep 留存现场）")]
    return "\n".join(lines)


def _rate(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


# --check 门禁的默认阈值。宽筛层噪声有固有成本（评审原话：不宜要求零误报），
# 所以噪声只设容忍上限；口径误判和实现不一致是实现 bug，默认必须为 0/100%。
DEFAULT_GATES = {"max_import_failures": 0, "max_misjudged": 0, "min_agreement": 1.0,
                 "max_missed": 0, "max_confirmed_missed": 0,
                 "max_noise_candidates": 0.25,
                 "max_structural_mismatch": 0}


def check_report(report: dict, gates: dict | None = None) -> dict:
    """对回归报告逐项过门禁，返回 {passed, gates:[{gate, actual, limit, ok, message}]}。

    设计成纯函数：CI 与本地共用同一判定，测试可以直接喂报告进来自动判别。
    噪声占比无定义（整批零候选）时不算未过——那种情况由漏检门禁兜底。
    “漏检事件”与“确认漏检”是两个层次：前者看候选是否覆盖真值，后者看
    确认区间是否覆盖真值。候选正常而确认退化时只有后者非零。
    """
    limits = {**DEFAULT_GATES, **(gates or {})}
    totals = report["metrics"]["totals"]
    agreement = report.get("agreement") or {}
    noise = totals.get("false_positive_candidate_ratio")
    missed = totals["truth_events"] - totals["detected_events"]
    # 确认层漏检：没有被数值确认区间覆盖的真值事件数（含候选漏检）。
    # 与“漏检事件”分开设门禁——候选覆盖正常但确认能力退化（区间收紧、
    # 匹配规则变严）时，漏检门禁全绿而确认率暗跌，必须单独拦。
    confirmed_missed = totals["truth_events"] - totals["confirmed_events"]

    def gate(name, ok, actual, limit, message):
        return {"gate": name, "ok": bool(ok), "actual": actual, "limit": limit,
                "message": message}

    results = [
        gate("导入完整性", len(report["import_failures"]) <= limits["max_import_failures"],
             len(report["import_failures"]), limits["max_import_failures"],
             f"导入失败 {len(report['import_failures'])} 条"),
        gate("口径误判", totals["misjudged_candidates"] <= limits["max_misjudged"],
             totals["misjudged_candidates"], limits["max_misjudged"],
             f"自判符合口径却无真值支撑的候选 {totals['misjudged_candidates']} 个（实现 bug）"),
        gate("实现一致", (agreement.get("agreement_rate") or 0.0) >= limits["min_agreement"] or (agreement.get("cases") == 0 and bool(report["metrics"].get("quality_cases"))),
             agreement.get("agreement_rate"), limits["min_agreement"],
             f"参考实现与工具口径区间一致 {agreement.get('matching', 0)}/"
             f"{agreement.get('cases', 0)}"),
        gate("漏检事件", missed <= limits["max_missed"], missed, limits["max_missed"],
             f"漏检真值事件 {missed} 个"),
        gate("确认漏检", confirmed_missed <= limits["max_confirmed_missed"],
             confirmed_missed, limits["max_confirmed_missed"],
             f"未被数值确认区间覆盖的真值事件 {confirmed_missed} 个"
             f"（含候选漏检；用于检查数值确认层遗漏）"),
        gate("宽筛噪声", noise is None or noise <= limits["max_noise_candidates"],
             noise, limits["max_noise_candidates"],
             f"误报候选占比 {_percent(noise)}"),
        gate("结构一致", totals["structural_mismatch_cases"] <= limits["max_structural_mismatch"],
             totals["structural_mismatch_cases"], limits["max_structural_mismatch"],
             f"候选簇数与真值事件数不一致的案例 {totals['structural_mismatch_cases']} 个"),
    ]
    quality = report["metrics"].get("quality_cases", [])
    if quality:
        unexpected = sum(case["unexpected_numeric_confirmations"] for case in quality)
        results.append(gate("未知案例确认", unexpected == 0, unexpected, 0,
                            f"无法判断案例中出现 {unexpected} 个数值确认候选"))
    behavior = report.get("behavior")
    if behavior is not None:
        failed = [item for item in behavior["checks"] if not item["ok"]]
        detail = "; ".join(f"{item['case_id']}:{item['field']}" for item in failed)
        results.append(gate("逐案例行为", not failed, len(failed), 0,
                            f"{len(failed)} 项行为偏离预期" + (f"：{detail}" if detail else "")))
    return {"passed": all(item["ok"] for item in results), "gates": results,
            "limits": limits}


def format_gates(result: dict) -> str:
    lines = ["回归门禁（--check）："]
    for item in result["gates"]:
        mark = "PASS" if item["ok"] else "FAIL"
        lines.append(f"  [{mark}] {item['gate']}：{item['message']}"
                     f"（阈值 {item['limit']}）")
    lines.append("结论：" + ("全部通过" if result["passed"] else "存在未过项，退出码非零"))
    return "\n".join(lines)


def _percent(value):
    return "n/a" if value is None else f"{value * 100:.1f}%"
