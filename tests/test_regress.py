"""闭环回归：覆盖合并、指标口径、analyzer 定位；真跑用例无环境时跳过。"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scenario_cases.regress import (COVERAGE_RULE, default_analyzer_root, format_report,
                                    locate_analyzer, merged_coverage, run_regression,
                                    score_cases)


def candidate(cid, start, end):
    return {"candidate_id": cid, "start_time": start, "end_time": end}


def event(start, end, kind="deceleration"):
    return {"start": start, "end": end, "kind": kind}


def fake_case(profile, events, candidates, notes=None):
    return {"profile": profile, "truth_events": events, "candidates": candidates,
            "notes": notes or []}


class MergedCoverageTest(unittest.TestCase):
    def test_single_interval_full_cover(self):
        self.assertAlmostEqual(merged_coverage([10, 20], [[10, 20]]), 1.0)

    def test_partial_cover(self):
        self.assertAlmostEqual(merged_coverage([10, 20], [[12, 20]]), 0.8)

    def test_overlapping_candidates_merge_before_measuring(self):
        # 两个候选各覆盖一半且互相重叠：合并后是 12~18 全覆盖 6/10
        self.assertAlmostEqual(merged_coverage([10, 20], [[12, 16], [14, 18]]), 0.6)

    def test_disjoint_candidates_sum(self):
        self.assertAlmostEqual(merged_coverage([0, 10], [[0, 3], [6, 10]]), 0.7)

    def test_candidate_outside_event_counts_zero(self):
        self.assertAlmostEqual(merged_coverage([10, 20], [[0, 5]]), 0.0)

    def test_empty_candidates_zero(self):
        self.assertAlmostEqual(merged_coverage([10, 20], []), 0.0)

    def test_touching_edges_counted(self):
        self.assertAlmostEqual(merged_coverage([10, 20], [[10, 15], [15, 20]]), 1.0)


class ScoreCasesTest(unittest.TestCase):
    def test_full_detection_no_false_positive(self):
        cases = {"s1": fake_case("hard_brake", [event(10, 16)], [candidate("C001", 10, 16)])}
        result = score_cases(cases)
        self.assertEqual(result["totals"]["detection_rate"], 1.0)
        self.assertEqual(result["totals"]["false_positive_rate"], 0.0)

    def test_missed_event_when_coverage_below_rule(self):
        # 候选只盖住真值的一半：6/10 = 0.6 < 0.9，算漏检，且候选本身算误报
        cases = {"s1": fake_case("hard_brake", [event(10, 20)], [candidate("C001", 12, 18)])}
        result = score_cases(cases)
        self.assertEqual(result["totals"]["detection_rate"], 0.0)
        self.assertEqual(result["totals"]["false_positive_candidates"], 1)
        missed = result["cases"][0]["missed_events"][0]
        self.assertAlmostEqual(missed["merged_coverage"], 0.6)

    def test_negative_sample_any_candidate_is_false_positive(self):
        cases = {"s1": fake_case("cruise", [], [candidate("C001", 5, 9)])}
        result = score_cases(cases)
        self.assertIsNone(result["totals"]["detection_rate"])  # 没有真值事件
        self.assertEqual(result["totals"]["false_positive_rate"], 1.0)

    def test_clean_negative_sample(self):
        cases = {"s1": fake_case("cruise", [], [])}
        result = score_cases(cases)
        # 没有任何候选时误报率无定义（None），不是 0——没有分母就没有率
        self.assertIsNone(result["totals"]["false_positive_rate"])

    def test_two_candidates_cover_one_event(self):
        # 真值 10~20 被 10~15 与 15~20 两个候选接力覆盖：检出，两个都不是误报
        cases = {"s1": fake_case("hard_brake", [event(10, 20)],
                                 [candidate("C001", 10, 15), candidate("C002", 15, 20)])}
        result = score_cases(cases)
        self.assertEqual(result["totals"]["detection_rate"], 1.0)
        self.assertEqual(result["totals"]["false_positive_candidates"], 0)

    def test_by_profile_breakdown(self):
        cases = {"s1": fake_case("hard_brake", [event(10, 16)], [candidate("C001", 10, 16)]),
                 "s2": fake_case("mild_decel", [event(10, 16)], [candidate("C001", 30, 36)])}
        result = score_cases(cases)
        self.assertEqual(result["by_profile"]["hard_brake"]["detected_events"], 1)
        self.assertEqual(result["by_profile"]["mild_decel"]["detected_events"], 0)
        self.assertEqual(result["by_profile"]["mild_decel"]["false_positive_candidates"], 1)

    def test_boundary_cases_tracked_separately(self):
        cases = {"s1": fake_case("hard_brake", [event(10, 16)], [candidate("C001", 10, 16)],
                                 notes=["贴边界样本"])}
        result = score_cases(cases)
        self.assertEqual(result["boundary_cases"]["cases"], 1)
        self.assertEqual(result["boundary_cases"]["detected_events"], 1)

    def test_coverage_rule_is_configurable(self):
        cases = {"s1": fake_case("hard_brake", [event(10, 20)], [candidate("C001", 12, 18)])}
        self.assertEqual(score_cases(cases, coverage_rule=0.5)["totals"]["detection_rate"], 1.0)


class LocateAnalyzerTest(unittest.TestCase):
    def test_default_root_points_at_sibling_repo(self):
        root = default_analyzer_root()
        self.assertEqual(root.name, "case-analyzer-oss")

    def test_missing_analyzer_raises_with_hint(self):
        with TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError) as ctx:
                locate_analyzer(Path(temporary) / "nowhere")
            # 显式路径无效必须说清是哪个路径、缺什么，而不是静默用默认路径
            self.assertIn("nowhere", str(ctx.exception))
            self.assertIn("app/cli_import.py", str(ctx.exception))

    def test_explicit_root_wins_over_env(self):
        import os

        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            (root / "app").mkdir(parents=True)
            (root / "app" / "cli_import.py").write_text("", encoding="utf-8")
            previous = os.environ.get("CASE_ANALYZER_ROOT")
            os.environ["CASE_ANALYZER_ROOT"] = str(Path(temporary) / "env-root")
            try:
                self.assertEqual(locate_analyzer(root), root)
            finally:
                if previous is None:
                    del os.environ["CASE_ANALYZER_ROOT"]
                else:
                    os.environ["CASE_ANALYZER_ROOT"] = previous


class FormatReportTest(unittest.TestCase):
    def test_summary_contains_key_numbers(self):
        report = {"import": {"imported": 2}, "import_failures": [],
                  "metrics": {"totals": {"cases": 2, "truth_events": 2, "detected_events": 1,
                                         "candidates": 3, "false_positive_candidates": 1,
                                         "detection_rate": 0.5, "false_positive_rate": 1 / 3},
                              "by_profile": {"hard_brake": {"cases": 1, "truth_events": 1,
                                                            "detected_events": 1, "candidates": 1,
                                                            "false_positive_candidates": 0}},
                              "boundary_cases": {"cases": 0, "truth_events": 0,
                                                 "detected_events": 0, "candidates": 0,
                                                 "false_positive_candidates": 0},
                              "cases": []},
                  "report_path": None}
        text = format_report(report)
        self.assertIn("检出率 50.0%", text)
        self.assertIn("误报率 33.3%", text)
        self.assertIn("hard_brake", text)
        self.assertIn("临时目录已清理", text)


def analyzer_available():
    try:
        root = default_analyzer_root()
    except ValueError:
        return False
    if not (root / "app" / "cli_import.py").is_file():
        return False
    try:
        import numpy  # noqa: F401
        import pandas  # noqa: F401
    except ImportError:
        return False
    return True


@unittest.skipUnless(analyzer_available(), "需要 case-analyzer 源码及其 pandas/numpy 环境")
class RunRegressionIntegrationTest(unittest.TestCase):
    def test_small_batch_end_to_end(self):
        with TemporaryDirectory() as temporary:
            out = Path(temporary) / "run"
            report = run_regression(count=6, rows=120, seed=42, out=out)
            self.assertEqual(report["count"], 6)
            self.assertEqual(report["import"]["imported"], 6)
            self.assertEqual(report["import"]["failed"], 0)
            # 每条场景都该有候选判定结果（含负样本：0 候选 0 真值）
            self.assertEqual(len(report["cases"]), 6)
            for case in report["cases"].values():
                self.assertIn(case["profile"], {"hard_brake", "mild_decel",
                                                "lead_cut_in", "cruise"})
            totals = report["metrics"]["totals"]
            self.assertGreater(totals["candidates"], 0)
            self.assertGreater(totals["truth_events"], 0)
            self.assertIsNotNone(totals["detection_rate"])
            # 报告落盘且可再解析
            saved = json.loads((out / "regression_report.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["metrics"]["totals"], totals)

    def test_report_declares_synthetic_source(self):
        with TemporaryDirectory() as temporary:
            report = run_regression(count=3, rows=60, seed=7,
                                    out=Path(temporary) / "run")
            self.assertEqual(report["source"]["type"], "synthetic")
            self.assertIn("不是道路采集", report["source"]["note"])


if __name__ == "__main__":
    unittest.main()
