"""闭环回归：生成 → 调用 case-analyzer 全跑 → 对真值 → 检出率/误报率报告。

与 case-analyzer 自带冻结评测的分工：
- 那边是"人工盲标 → 揭盲 → 算指标"，人当裁判；
- 这边是"生成器自带真值 → 工具全跑 → 对答案"，真值当裁判，全自动可复现。

指标口径与 case-analyzer 的 evaluation.py 对齐：真值事件被候选区间覆盖的
比例达到 candidate_coverage（默认 0.9）算检出；没有真值事件被它主要覆盖的
候选算误报。负样本场景（无真值事件）的任何候选都计入误报。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from scenario_cases.cli import generate_batch
from scenario_cases.generator import GENERATOR_VERSION

VERSION = "closed-loop-regress-1.0"
COVERAGE_RULE = 0.9
DEFAULT_COUNT = 20
DEFAULT_ROWS = 601
DEFAULT_SEED = 20261019
SOURCE_NAME = "scenario-cases 闭环回归批次"
ACCELERATION_SOURCE = "由车速有限差分得到，不是独立传感器测量"


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
            f"指定的 case-analyzer 目录无效：{candidate}（缺少 app/cli_import.py）")
    fallback = default_analyzer_root()
    if (fallback / "app" / "cli_import.py").is_file():
        return fallback
    raise ValueError(
        f"找不到 case-analyzer（默认位置：{fallback}）；"
        "用 --analyzer 或环境变量 CASE_ANALYZER_ROOT 指定其源码目录")


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
    """事件区间被候选区间集覆盖的比例；先合并重叠候选再计算。"""
    start, end = float(event[0]), float(event[1])
    if end <= start:
        return 0.0
    covered = 0.0
    for cluster_start, cluster_end in _merge(intervals):
        overlap = min(end, cluster_end) - max(start, cluster_start)
        if overlap > 0:
            covered += overlap
    return covered / (end - start)


def score_cases(cases: dict, coverage_rule: float = COVERAGE_RULE) -> dict:
    """逐案例对真值，再按模板汇总。cases 结构见 run_regression。"""
    COUNTER_KEYS = ("cases", "truth_events", "detected_events", "candidates",
                    "false_positive_candidates")
    by_profile: dict[str, dict] = {}
    boundary = dict.fromkeys(COUNTER_KEYS, 0)
    totals = dict.fromkeys(COUNTER_KEYS, 0)
    per_case = []
    for case_id, case in cases.items():
        truth = [[event["start"], event["end"]] for event in case["truth_events"]]
        intervals = [[candidate["start_time"], candidate["end_time"]]
                     for candidate in case["candidates"]]
        detected, missed = [], []
        for index, event in enumerate(truth):
            # 检出判定与 case-analyzer evaluation.py 同口径：候选区间集合先合并，
            # 再看对真值事件的覆盖比例——两个候选接力盖住一个事件也算检出。
            coverage = merged_coverage(event, intervals)
            record = {"index": index, "event": case["truth_events"][index],
                      "merged_coverage": round(coverage, 4)}
            (detected if coverage >= coverage_rule - 1e-9 else missed).append(record)
        # 误报判定按候选簇：先合并成簇，再看簇有没有主要覆盖某个真值事件。
        # 接力盖住同一个事件的两个候选不各算一次误报——那是切碎，不是编造。
        clusters = _merge(intervals)
        false_positives = []
        for candidate in case["candidates"]:
            cluster = next(item for item in clusters
                           if item[0] <= candidate["start_time"] + 1e-9
                           and candidate["end_time"] <= item[1] + 1e-9)
            best = max((merged_coverage(event, [cluster]) for event in truth), default=0.0)
            if best < coverage_rule - 1e-9:
                false_positives.append({"candidate_id": candidate.get("candidate_id"),
                                        "interval": [candidate["start_time"], candidate["end_time"]],
                                        "cluster": [round(cluster[0], 3), round(cluster[1], 3)],
                                        "cluster_coverage": round(best, 4)})
        entry = {"case_id": case_id, "profile": case["profile"], "notes": case.get("notes", []),
                 "truth_events": len(truth), "detected_events": len(detected),
                 "candidates": len(intervals), "false_positive_candidates": len(false_positives),
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
            "totals": dict(totals,
                           detection_rate=rate(totals["detected_events"], totals["truth_events"]),
                           false_positive_rate=rate(totals["false_positive_candidates"],
                                                    totals["candidates"])),
            "by_profile": by_profile,
            "boundary_cases": boundary,
            "cases": per_case}


def _load_analyzer_modules(analyzer_root: Path):
    """把 case-analyzer 加进 sys.path 并导入闭环需要的模块。

    失败信息说人话：最常见的坑是运行环境缺 pandas/numpy，
    那正是 case-analyzer 自己 requirements 里的依赖。
    """
    root = str(analyzer_root)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from app.candidate_review import build_candidate_case
        from app.cli_import import run_import
        from app.paths import DEFAULT_RUNTIME
        from app.server import WorkbenchContext
        from app.workspace import initialize
    except ImportError as error:
        raise ValueError(
            f"导入 case-analyzer 模块失败：{error}。"
            "闭环回归依赖 case-analyzer 的运行环境（pandas、numpy 等），"
            "请用它的虚拟环境运行："
            "case-analyzer-oss/.venv/Scripts/python.exe -m scenario_cases regress"
        ) from error
    return build_candidate_case, run_import, DEFAULT_RUNTIME, WorkbenchContext, initialize


def run_regression(*, count: int = DEFAULT_COUNT, seed: int = DEFAULT_SEED,
                   rows: int = DEFAULT_ROWS, out=None, analyzer=None,
                   keep: bool = False, coverage_rule: float = COVERAGE_RULE) -> dict:
    """一条命令跑完闭环：生成 → 导入 case-analyzer → 对真值 → 出报告。"""
    if count < 1 or rows < 10:
        raise ValueError("count 至少 1，rows 至少 10")
    analyzer_root = locate_analyzer(analyzer)
    build_candidate_case, run_import, DEFAULT_RUNTIME, WorkbenchContext, initialize = \
        _load_analyzer_modules(analyzer_root)

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
        summary = run_import(batch, context, "synthetic", SOURCE_NAME, ACCELERATION_SOURCE)
        imported_ids = [item["case_id"] for item in summary["cases"]
                        if item["status"] == "imported"]
        import_failures = [{"case_id": item["case_id"], "status": item["status"],
                            "message": item["message"]}
                           for item in summary["cases"] if item["status"] != "imported"]
        cases = {}
        for case_id in imported_ids:
            truth = json.loads((batch / case_id / "truth.json").read_text(encoding="utf-8"))
            built = build_candidate_case(context.data_root / case_id)
            cases[case_id] = {"profile": truth["spec"]["profile"],
                              "truth_events": truth["events"], "notes": truth.get("notes", []),
                              "candidates": [{"candidate_id": candidate["candidate_id"],
                                              "start_time": candidate["start_time"],
                                              "end_time": candidate["end_time"]}
                                             for candidate in built["candidates"]]}
        report = {"tool": VERSION, "started_at": started,
                  "generator": GENERATOR_VERSION, "analyzer_root": str(analyzer_root),
                  "seed": seed, "count": count, "rows_per_case": rows,
                  "source": {"type": "synthetic", "name": SOURCE_NAME,
                             "note": "公式合成的运动学级场景，不是道路采集或闭环仿真"},
                  "import": summary["totals"], "import_failures": import_failures,
                  "metrics": score_cases(cases, coverage_rule), "cases": cases}
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
            report["report_path"] = None


def format_report(report: dict) -> str:
    """人类可读摘要：总量指标 → 分模板 → 边界样本 → 失败与漏检明细。"""
    metrics = report["metrics"]
    totals = metrics["totals"]
    lines = [
        "闭环回归报告（真值 = 生成器注入的事件窗口，裁判全自动）",
        "-" * 64,
        f"场景 {totals['cases']} 条 · 导入 {report['import'].get('imported', 0)} 条成功 · "
        f"失败 {len(report['import_failures'])} 条",
        f"真值事件 {totals['truth_events']} 个 · 检出 {totals['detected_events']} 个 · "
        f"检出率 {_percent(totals['detection_rate'])}",
        f"候选 {totals['candidates']} 个 · 误报候选 {totals['false_positive_candidates']} 个 · "
        f"误报率 {_percent(totals['false_positive_rate'])}",
        "",
        "分模板：",
    ]
    for profile, bucket in sorted(metrics["by_profile"].items()):
        lines.append(
            f"  {profile:<12} 场景 {bucket['cases']:>3} · 事件 {bucket['truth_events']:>3} · "
            f"检出 {bucket['detected_events']:>3}（{_percent(_rate(bucket['detected_events'], bucket['truth_events']))}） · "
            f"候选 {bucket['candidates']:>3} · 误报 {bucket['false_positive_candidates']:>3} "
            f"（{_percent(_rate(bucket['false_positive_candidates'], bucket['candidates']))}）")
    boundary = metrics["boundary_cases"]
    if boundary["cases"]:
        lines += [
            "",
            f"贴边界样本（生成器标注）：{boundary['cases']} 条 · 检出率 "
            f"{_percent(_rate(boundary['detected_events'], boundary['truth_events']))} · "
            f"误报率 {_percent(_rate(boundary['false_positive_candidates'], boundary['candidates']))}",
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
                for item in entry["false_positives"]))
    lines += ["", "完整报告：" + (report.get("report_path")
                                 or "（临时目录已清理；用 --out 或 --keep 留存现场）")]
    return "\n".join(lines)


def _rate(numerator, denominator):
    return round(numerator / denominator, 4) if denominator else None


def _percent(value):
    return "n/a" if value is None else f"{value * 100:.1f}%"
