"""命令行：批量、可复现、参数校验。"""
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args):
    # PYTHONUTF8=1 必须显式传给子进程：-X utf8 是父进程的命令行参数，不会遗传；
    # Windows 默认代码页是 cp936，子进程按 GBK 打印中文，父进程按 utf-8 解码直接炸。
    # 不传这个变量，测试在中文 Windows 上会随机失败（取决于启动终端）。
    env = {**os.environ, "PYTHONUTF8": "1"}
    return subprocess.run([sys.executable, "-B", "-m", "scenario_cases", *args],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8", env=env)


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


class BatchNamespaceTest(unittest.TestCase):
    """批次命名空间：同配置可复现、不同配置可并存（评审反例的回归测试）。"""

    def _generate(self, temporary, name, seed, rows=60, count=2, extra=()):
        out = Path(temporary) / name
        proc = run_cli("generate", "--out", str(out), "--count", str(count),
                       "--rows", str(rows), "--seed", str(seed), *extra)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return out

    def test_same_config_same_namespace(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = sorted(p.name for p in self._generate(temporary, "a", 11).iterdir())
            second = sorted(p.name for p in self._generate(temporary, "b", 11).iterdir())
            self.assertEqual(first, second)
            self.assertTrue(first[0].startswith("scn_"), first[0])
            self.assertRegex(first[0], r"^scn_[0-9a-f]{6}_000$")

    def test_different_seed_different_namespace(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = sorted(p.name for p in self._generate(temporary, "a", 11).iterdir())
            second = sorted(p.name for p in self._generate(temporary, "b", 12).iterdir())
            self.assertNotEqual(first, second)

    def test_two_batches_coexist_in_one_library(self):
        # 评审实测场景：两批同 ID 不同内容，第二批全部入库失败。命名空间后应全部成功。
        with tempfile.TemporaryDirectory() as temporary:
            first = self._generate(temporary, "a", 11)
            second = self._generate(temporary, "b", 12)
            db = Path(temporary) / "library.db"
            for batch in (first, second):
                proc = run_cli("library", "add", str(batch), "--db", str(db))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("失败 0", proc.stdout)
            listed = run_cli("library", "list", "--db", str(db), "--json")
            records = json.loads(listed.stdout)
            self.assertEqual(len(records), 4)

    def test_explicit_tag_used_and_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            out = self._generate(temporary, "a", 11, extra=("--tag", "brake-v1"))
            self.assertTrue((out / "scn_brake-v1_000").is_dir())
            bad = run_cli("generate", "--out", str(Path(temporary) / "x"),
                          "--count", "1", "--rows", "20", "--seed", "1", "--tag", "../evil")
            self.assertEqual(bad.returncode, 2)
            self.assertIn("--tag", bad.stderr)

    def test_count_does_not_change_namespace(self):
        # 条数刻意不进哈希：多生成几条应与少生成几条共用命名空间，靠指纹去重
        with tempfile.TemporaryDirectory() as temporary:
            first = sorted(p.name for p in self._generate(temporary, "a", 11, count=2).iterdir())
            second = sorted(p.name for p in self._generate(temporary, "b", 11, count=4).iterdir())
            self.assertEqual(first, second[:2])

    def test_mix_writing_order_does_not_change_content(self):
        # 评审场景：同配比不同书写顺序 → batch_tag 排序后同 tag，
        # 但 pick_profile 按 dict 原序遍历 → 同 seed 抽出不同模板 → 同 ID 不同内容。
        # 修复后哈希与抽样共用 sorted 口径：namespace 与逐字节内容都必须一致。
        with tempfile.TemporaryDirectory() as temporary:
            first = self._generate(temporary, "a", 11, count=20,
                                   extra=("--profiles", "mild_decel,hard_brake,cruise"))
            second = self._generate(temporary, "b", 11, count=20,
                                    extra=("--profiles", "cruise,hard_brake,mild_decel"))
            self.assertEqual(sorted(p.name for p in first.iterdir()),
                             sorted(p.name for p in second.iterdir()))
            for case in sorted(first.iterdir()):
                twin = second / case.name
                self.assertTrue(twin.is_dir(), twin)
                for f in sorted(case.iterdir()):
                    self.assertEqual(f.read_bytes(), (twin / f.name).read_bytes(),
                                     f"{case.name}/{f.name} 内容不一致")

    def test_pick_profile_and_tag_use_sorted_order(self):
        # 单元级：batch_tag 用 sorted(mix) 算哈希，pick_profile 也必须按
        # sorted 遍历——否则 dict 书写顺序会改变同一 rng 的模板序列。
        from scenario_cases.cli import batch_tag, pick_profile
        mix_a = {"mild_decel": 0.5, "hard_brake": 0.5}
        mix_b = {"hard_brake": 0.5, "mild_decel": 0.5}
        self.assertEqual(batch_tag(11, 60, mix_a), batch_tag(11, 60, mix_b))
        rng_a, rng_b = random.Random(7), random.Random(7)
        seq_a = [pick_profile(rng_a, mix_a) for _ in range(50)]
        seq_b = [pick_profile(rng_b, mix_b) for _ in range(50)]
        self.assertEqual(seq_a, seq_b)


class FixedCasesModeTest(unittest.TestCase):
    """固定案例模式（--cases/--labels）：标签适配与参数互斥，不依赖 analyzer 环境。"""

    def _write_labels(self, temporary, files):
        labels = Path(temporary) / "labels"
        labels.mkdir(parents=True, exist_ok=True)
        for name, payload in files.items():
            (labels / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False),
                                                 encoding="utf-8")
        return labels

    def test_load_fixed_truth_reference_labels_style(self):
        # reference_labels 风格：events 是 [start, end] 列表
        from scenario_cases.regress import load_fixed_truth
        with tempfile.TemporaryDirectory() as temporary:
            cases = Path(temporary) / "cases"
            cases.mkdir()
            labels = self._write_labels(temporary, {
                "seg_023": {"case_id": "seg_023", "decision": "positive",
                            "events": [[54735.2, 54743.2]], "scene": "事件中段缺口",
                            "notes": "人工修正"}})
            truth = load_fixed_truth(cases, labels)
        self.assertEqual(truth["seg_023"]["events"],
                         [{"start": 54735.2, "end": 54743.2, "kind": "deceleration"}])
        self.assertEqual(truth["seg_023"]["profile"], "事件中段缺口")
        self.assertIn("人工修正", truth["seg_023"]["notes"])

    def test_load_fixed_truth_truth_json_style(self):
        # truth.json 风格：events 是 {"start","end","kind"} dict 列表
        from scenario_cases.regress import load_fixed_truth
        with tempfile.TemporaryDirectory() as temporary:
            cases = Path(temporary) / "cases"
            cases.mkdir()
            labels = self._write_labels(temporary, {
                "scn_x_000": {"case_id": "scn_x_000",
                              "events": [{"start": 10.0, "end": 16.0, "kind": "deceleration"}]}})
            truth = load_fixed_truth(cases, labels)
        self.assertEqual(truth["scn_x_000"]["events"],
                         [{"start": 10.0, "end": 16.0, "kind": "deceleration"}])

    def test_uncertain_label_has_no_events(self):
        # 人工结论"无法判断"：不计事件真值
        from scenario_cases.regress import load_fixed_truth
        with tempfile.TemporaryDirectory() as temporary:
            cases = Path(temporary) / "cases"
            cases.mkdir()
            labels = self._write_labels(temporary, {
                "seg_024": {"case_id": "seg_024", "decision": "uncertain",
                            "events": [], "notes": "缺口覆盖全部完整窗口，无法确认事件"}})
            truth = load_fixed_truth(cases, labels)
        self.assertEqual(truth["seg_024"]["events"], [])
        self.assertTrue(any("无法确认事件" in n for n in truth["seg_024"]["notes"]))

    def test_cases_without_labels_is_rejected(self):
        # 只给 --cases 不给 --labels：直接退出码 2，不进流水线
        with tempfile.TemporaryDirectory() as temporary:
            cases = Path(temporary) / "cases"
            cases.mkdir()
            proc = run_cli("regress", "--cases", str(cases))
            self.assertEqual(proc.returncode, 2)
            self.assertIn("--labels", proc.stderr)

    def test_fixed_mode_rejects_count_seed(self):
        # 固定案例模式与 --count/--seed 互斥（防误用：生成参数被静默忽略）
        with tempfile.TemporaryDirectory() as temporary:
            cases = Path(temporary) / "cases"
            labels = self._write_labels(temporary, {})
            cases.mkdir()
            proc = run_cli("regress", "--cases", str(cases), "--labels", str(labels),
                           "--count", "10")
            self.assertEqual(proc.returncode, 2)
            self.assertIn("不能与 --count/--seed 混用", proc.stderr)


if __name__ == "__main__":
    unittest.main()
