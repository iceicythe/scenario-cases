"""命令行：批量、可复现、参数校验。"""
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

    def test_jitter_keeps_min_speed_margin(self):
        # 联合约束：单参数合法 ≠ 组合合法；任何种子下末端车速都要保留余量。
        from scenario_cases.cli import jitter, pick_profile
        from scenario_cases.templates import min_speed, resolve_params

        for seed in range(200):
            rng = random.Random(seed)
            profile = pick_profile(rng)
            params = jitter(rng, profile)
            self.assertGreaterEqual(min_speed(profile, resolve_params(profile, params)), 3.0,
                                    f"seed={seed} {profile} {params}")

    def test_invalid_counts_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc = run_cli("generate", "--out", str(Path(temporary) / "x"), "--count", "0")
            self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
