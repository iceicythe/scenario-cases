"""命令行：批量、可复现、参数校验。"""
import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "scenario_cases", *args],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8")


class CliTests(unittest.TestCase):
    def test_generate_batch(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "batch"
            proc = run_cli("generate", "--out", str(out), "--count", "6", "--rows", "120", "--seed", "7")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            cases = sorted(p.name for p in out.iterdir() if p.is_dir())
            self.assertEqual(len(cases), 6)
            for case in cases:
                for name in ("vehicle_state.csv", "object_state.csv", "planning_state.csv",
                             "control_state.csv", "source.json", "truth.json"):
                    self.assertTrue((out / case / name).is_file(), f"{case}/{name}")

    def test_same_seed_is_reproducible(self):
        with tempfile.TemporaryDirectory() as temporary:
            first, second = Path(temporary) / "a", Path(temporary) / "b"
            run_cli("generate", "--out", str(first), "--count", "4", "--rows", "80", "--seed", "42")
            run_cli("generate", "--out", str(second), "--count", "4", "--rows", "80", "--seed", "42")
            for case in sorted(p.name for p in first.iterdir()):
                self.assertEqual((first / case / "vehicle_state.csv").read_text(encoding="utf-8"),
                                 (second / case / "vehicle_state.csv").read_text(encoding="utf-8"))

    def test_unknown_profile_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc = run_cli("generate", "--out", str(Path(temporary) / "x"),
                           "--profiles", "hard_brake,teleport")
            self.assertEqual(proc.returncode, 2)
            self.assertIn("未知模板", proc.stderr)

    def test_mix_weights_sum_to_one(self):
        from scenario_cases.cli import MIX
        self.assertAlmostEqual(sum(MIX.values()), 1.0)
        for name in ("sub_threshold", "short_dip", "v_dip", "jitter", "double_dip"):
            self.assertIn(name, MIX)

    def test_jitter_keeps_min_speed_margin(self):
        # 联合约束：单参数合法 ≠ 组合合法；任何种子下末端车速都要保留余量。
        from scenario_cases.cli import jitter, pick_profile
        from scenario_cases.templates import min_speed, resolve_params

        for seed in range(300):
            rng = random.Random(seed)
            profile = pick_profile(rng)
            params = jitter(rng, profile)
            self.assertGreaterEqual(min_speed(profile, resolve_params(profile, params)), 3.0,
                                    f"seed={seed} {profile} {params}")

    def test_jitter_snaps_time_params_to_grid(self):
        from scenario_cases.cli import jitter
        from scenario_cases.templates import DEFAULTS

        for seed in range(100):
            rng = random.Random(seed)
            for profile in DEFAULTS:
                params = jitter(rng, profile)
                for key in ("duration", "at", "dip", "recover", "gap"):
                    if key in params:
                        self.assertAlmostEqual(params[key] * 10,
                                               round(params[key] * 10), places=9,
                                               msg=f"seed={seed} {profile} {key}")

    def test_sub_threshold_batch_has_no_truth_events(self):
        # 对抗组单独成批：任何种子下都不该有口径事件，否则范围约束被破坏
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "batch"
            proc = run_cli("generate", "--out", str(out), "--count", "12", "--rows", "200",
                           "--seed", "5", "--profiles", "sub_threshold,short_dip,jitter")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            for case in out.iterdir():
                truth = json.loads((case / "truth.json").read_text(encoding="utf-8"))
                self.assertEqual(truth["events"], [], case.name)

    def test_invalid_counts_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc = run_cli("generate", "--out", str(Path(temporary) / "x"), "--count", "0")
            self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
