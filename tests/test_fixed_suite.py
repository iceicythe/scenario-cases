"""Regression guards for incomplete suites, uncertain labels and behavior drift."""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from scenario_cases.cli import main
from scenario_cases.fixed_suite import check_behavior, load_fixed_truth, load_manifest
from scenario_cases.regress import _agreement, check_report, run_fixed_regression, score_cases
from test_regress import analyzer_available

SUITE = Path(__file__).resolve().parents[1] / "suites" / "hard-cases-v1"
LIMITS = {"min_agreement": .5, "max_confirmed_missed": 2,
          "max_noise_candidates": .7, "max_structural_mismatch": 1}


class FixedInputValidationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cases, self.labels = self.root / "cases", self.root / "labels"
        self.cases.mkdir()
        self.labels.mkdir()
        (self.cases / "one").mkdir()
        self.label({"case_id": "one", "decision": "negative", "events": []})

    def label(self, value):
        (self.labels / "one.json").write_text(json.dumps(value), encoding="utf-8")

    def test_missing_case_is_rejected_before_runtime(self):
        (self.labels / "two.json").write_text('{"case_id":"two","events":[]}', encoding="utf-8")
        out = self.root / "output"
        with self.assertRaisesRegex(ValueError, "集合"):
            run_fixed_regression(self.cases, self.labels, out=out)
        self.assertFalse(out.exists())

    def test_unlabelled_case_is_rejected(self):
        (self.cases / "two").mkdir()
        with self.assertRaisesRegex(ValueError, "集合"):
            load_fixed_truth(self.cases, self.labels)

    def test_invalid_labels_are_rejected(self):
        for value in (
            {"case_id": "other", "events": []},
            {"decision": "positive", "events": []},
            {"decision": "negative", "events": [[1, 2]]},
            {"decision": [], "events": []},
            {"events": [[1, float("nan")]]},
            {"events": [[2, 1]]},
        ):
            with self.subTest(value=value):
                self.label(value)
                with self.assertRaises(ValueError):
                    load_fixed_truth(self.cases, self.labels)

    def test_uncertain_decision_survives_loading(self):
        self.label({"decision": "uncertain", "events": []})
        self.assertEqual(load_fixed_truth(self.cases, self.labels)["one"]["decision"], "uncertain")

    def test_manifest_requires_exact_members_and_valid_assertions(self):
        truth = load_fixed_truth(self.cases, self.labels)
        good = {"case_id": "one", "expected": {"candidate_assessments": []}}
        bad = {"case_id": "one", "expected": {"candidate_assessments": [{}]}}
        path = self.root / "manifest.json"
        for members in ([], [good, good], [{**good, "case_id": "other"}], [bad]):
            with self.subTest(members=members):
                path.write_text(json.dumps({"members": members}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_manifest(path, truth)


class UncertainScoringTest(unittest.TestCase):
    def test_uncertain_is_not_a_negative_and_numeric_confirmation_is_gated(self):
        candidate = {"start_time": 1, "end_time": 2, "event_assessment": "meets_numeric_rule"}
        base = {"profile": "external", "truth_events": [], "tool_reference_events": []}
        cases = {"unknown": dict(base, decision="uncertain", candidates=[candidate]),
                 "negative": dict(base, decision="negative", candidates=[candidate])}
        metrics, agreement = score_cases(cases), _agreement(cases)
        self.assertEqual(metrics["input_cases"], 2)
        self.assertEqual(metrics["totals"]["cases"], 1)
        self.assertEqual(metrics["totals"]["false_positive_candidates"], 1)
        self.assertEqual(agreement["cases"], 1)
        self.assertEqual(metrics["quality_cases"][0]["unexpected_numeric_confirmations"], 1)
        gates = check_report({"metrics": metrics, "agreement": agreement, "import_failures": []})
        quality = next(g for g in gates["gates"] if g["gate"] == "未知案例确认")
        self.assertFalse(quality["ok"])

    def test_all_uncertain_has_no_event_accuracy(self):
        cases = {"unknown": {"decision": "uncertain", "candidates": []}}
        metrics, agreement = score_cases(cases), _agreement(cases)
        self.assertIsNone(agreement["agreement_rate"])
        self.assertIsNone(metrics["totals"]["detection_rate"])
        self.assertTrue(check_report({"metrics": metrics, "agreement": agreement,
                                      "import_failures": []})["passed"])


@unittest.skipUnless(analyzer_available(), "需要 case-analyzer 源码及 pandas/numpy")
class FixedRegressionIntegrationTest(unittest.TestCase):
    def test_default_check_without_output_directory(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["regress", "--count", "3", "--rows", "60", "--seed", "7",
                         "--check", "--max-noise-candidates", "1"])
        self.assertEqual(code, 0, output.getvalue())
        self.assertIn("临时目录已清理", output.getvalue())

    def test_fixed_cli_default_cleanup_and_saved_gate_report(self):
        args = ["regress", "--cases", str(SUITE / "cases"), "--labels", str(SUITE / "labels"),
                "--check", "--min-agreement", ".5", "--max-confirmed-missed", "2",
                "--max-noise-candidates", ".7", "--max-structural-mismatch", "1"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 0)
            with tempfile.TemporaryDirectory() as temporary:
                self.assertEqual(main(args + ["--out", temporary]), 0)
                saved = json.loads((Path(temporary) / "regression_report.json").read_text(encoding="utf-8"))
                self.assertTrue(saved["check"]["passed"])
                self.assertEqual(len(saved["check"]["gates"]), 9)

    def test_hard_cases_and_confirmation_mutation(self):
        report = run_fixed_regression(SUITE / "cases", SUITE / "labels")
        self.assertIsNone(report["report_path"])
        self.assertEqual(report["metrics"]["totals"]["cases"], 2)
        self.assertEqual(report["metrics"]["excluded_uncertain_cases"], 1)
        self.assertEqual(report["agreement"]["agreement_rate"], .5)
        self.assertTrue(check_report(report, LIMITS)["passed"])
        expected = load_manifest(SUITE / "manifest.json", load_fixed_truth(SUITE / "cases", SUITE / "labels"))
        changed = copy.deepcopy(report)
        changed["cases"]["seg_022"]["candidates"][0]["event_assessment"] = "meets_numeric_rule"
        changed["metrics"] = score_cases(changed["cases"])
        changed["behavior"] = check_behavior(changed["cases"], expected)
        checks = check_report(changed, LIMITS)
        self.assertFalse(checks["passed"])
        self.assertEqual([g["gate"] for g in checks["gates"] if not g["ok"]], ["逐案例行为"])
        del changed["cases"]["seg_023"]
        self.assertFalse(check_behavior(changed["cases"], expected)["passed"])


if __name__ == "__main__":
    unittest.main()
