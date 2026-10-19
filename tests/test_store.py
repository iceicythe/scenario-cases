"""场景库：入库去重、检索、导出、持久化、CLI 往返。"""
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scenario_cases.generator import build_case, write_case
from scenario_cases.models import ScenarioSpec
from scenario_cases.store import (ScenarioError, ScenarioLibrary, fingerprint_of,
                                  read_scenario)

ROOT = Path(__file__).resolve().parents[1]


def make_scenario(root, case_id="scn_000", profile="hard_brake", params=None, rows=60):
    spec = ScenarioSpec(profile=profile, params=params or {})
    scenario = build_case(case_id, spec, rows)
    directory = Path(root) / case_id
    write_case(scenario, directory)
    return directory


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.tmp = Path(self._temporary.name)
        self.db = self.tmp / "library.db"
        self.addCleanup(self._temporary.cleanup)

    def library(self):
        return ScenarioLibrary(self.db)

    def test_add_then_get(self):
        directory = make_scenario(self.tmp)
        result = self.library().add_scenario(directory)
        self.assertEqual((result.scenario_id, result.status), ("scn_000", "added"))
        record = self.library().get("scn_000")
        self.assertIsNotNone(record)
        self.assertEqual(record.profile, "hard_brake")
        self.assertEqual(record.rows, 60)
        self.assertEqual(record.event_count, 1)
        # 真值由参考实现算出：左探窗口 [4.9,5.4] 合格，事件从 4.9 开始；
        # 数据只到 5.9 秒，制动窗 [5.0,8.0] 被末尾截断，事件到 5.9 结束
        self.assertEqual([(kind, start, end) for kind, start, end in record.events],
                         [("deceleration", 4.9, 5.9)])
        self.assertEqual(record.params["decel"], 4.0)

    def test_same_directory_twice_is_duplicate(self):
        directory = make_scenario(self.tmp)
        library = self.library()
        library.add_scenario(directory)
        again = library.add_scenario(directory)
        self.assertEqual(again.status, "duplicate")
        self.assertEqual(library.stats()["total"], 1)

    def test_same_content_elsewhere_is_duplicate(self):
        first = make_scenario(self.tmp)
        clone = self.tmp / "clone_of_scn_000"
        shutil.copytree(first, clone)
        library = self.library()
        library.add_scenario(first)
        result = library.add_scenario(clone)
        self.assertEqual(result.status, "duplicate")
        self.assertEqual(result.scenario_id, "scn_000")
        self.assertEqual(library.stats()["total"], 1)

    def test_modified_content_with_new_id_is_added(self):
        make_scenario(self.tmp)
        other = make_scenario(self.tmp, case_id="scn_100", params={"decel": 4.5})
        library = self.library()
        library.add_scenario(self.tmp / "scn_000")
        result = library.add_scenario(other)
        self.assertEqual(result.status, "added")
        self.assertEqual(library.stats()["total"], 2)

    def test_same_id_different_content_raises(self):
        directory = make_scenario(self.tmp)
        library = self.library()
        library.add_scenario(directory)
        vehicle = directory / "vehicle_state.csv"
        vehicle.write_text(vehicle.read_text(encoding="utf-8").replace("20.000", "19.500", 1),
                           encoding="utf-8", newline="\n")
        with self.assertRaises(ScenarioError):
            library.add_scenario(directory)
        self.assertEqual(library.stats()["total"], 1)  # 失败的入库不留残票

    def test_query_by_profile(self):
        library = self.library()
        library.add_many([make_scenario(self.tmp, case_id="scn_000", profile="hard_brake"),
                          make_scenario(self.tmp, case_id="scn_001", profile="cruise")])
        records = library.query(profile="hard_brake")
        self.assertEqual([record.scenario_id for record in records], ["scn_000"])

    def test_query_by_event_kind(self):
        library = self.library()
        library.add_many([make_scenario(self.tmp, case_id="scn_000", profile="mild_decel"),
                          make_scenario(self.tmp, case_id="scn_001", profile="cruise")])
        records = library.query(event_kind="deceleration")
        self.assertEqual([record.scenario_id for record in records], ["scn_000"])

    def test_query_by_decel_range(self):
        library = self.library()
        library.add_many([make_scenario(self.tmp, case_id="scn_000", params={"decel": 2.5}),
                          make_scenario(self.tmp, case_id="scn_001", params={"decel": 4.5})])
        records = library.query(min_decel=4.0, max_decel=5.0)
        self.assertEqual([record.scenario_id for record in records], ["scn_001"])

    def test_query_with_events_flag(self):
        make_scenario(self.tmp, case_id="scn_000", profile="hard_brake")
        make_scenario(self.tmp, case_id="scn_001", profile="cruise")
        library = self.library()
        library.add_many([self.tmp / "scn_000", self.tmp / "scn_001"])
        with_events = library.query(with_events=True)
        without = library.query(with_events=False)
        self.assertEqual([record.scenario_id for record in with_events], ["scn_000"])
        self.assertEqual([record.scenario_id for record in without], ["scn_001"])

    def test_missing_truth_raises(self):
        directory = make_scenario(self.tmp)
        (directory / "truth.json").unlink()
        with self.assertRaises(ScenarioError):
            read_scenario(directory)

    def test_missing_vehicle_csv_raises(self):
        directory = make_scenario(self.tmp)
        (directory / "vehicle_state.csv").unlink()
        with self.assertRaises(ScenarioError):
            self.library().add_scenario(directory)

    def test_missing_source_declaration_raises(self):
        # 没有来源声明的场景不予入库：评测数据的可信度一半在声明上
        directory = make_scenario(self.tmp)
        (directory / "source.json").unlink()
        with self.assertRaises(ScenarioError):
            self.library().add_scenario(directory)

    def test_corrupt_truth_raises(self):
        directory = make_scenario(self.tmp)
        (directory / "truth.json").write_text("{不是 JSON", encoding="utf-8")
        with self.assertRaises(ScenarioError):
            self.library().add_scenario(directory)

    def test_invalid_truth_structure_raises(self):
        directory = make_scenario(self.tmp)
        payload = json.loads((directory / "truth.json").read_text(encoding="utf-8"))
        payload["events"] = [{"start": 9.0, "end": 5.0, "kind": "deceleration"}]
        (directory / "truth.json").write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ScenarioError):
            self.library().add_scenario(directory)

    def test_export_roundtrip_is_byte_identical(self):
        directory = make_scenario(self.tmp)
        library = self.library()
        library.add_scenario(directory)
        out = self.tmp / "exported"
        written = library.export("scn_000", out)
        self.assertEqual(len(written), 6)
        for path in written:
            self.assertEqual(path.read_bytes(),
                             (directory / path.name).read_bytes(), path.name)

    def test_persistence_across_reopen(self):
        directory = make_scenario(self.tmp)
        self.library().add_scenario(directory)
        reopened = self.library()
        record = reopened.get("scn_000")
        self.assertIsNotNone(record)
        self.assertEqual(record.fingerprint, fingerprint_of(read_scenario(directory)[1]))

    def test_stats_counts(self):
        make_scenario(self.tmp, case_id="scn_000", profile="hard_brake")
        make_scenario(self.tmp, case_id="scn_001", profile="cruise")
        library = self.library()
        library.add_many([self.tmp / "scn_000", self.tmp / "scn_001"])
        summary = library.stats()
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["with_events"], 1)
        self.assertEqual(summary["without_events"], 1)
        self.assertEqual(summary["by_profile"], {"cruise": 1, "hard_brake": 1})

    def test_add_many_summary(self):
        good = make_scenario(self.tmp, case_id="scn_000")
        missing = self.tmp / "scn_404"
        summary = self.library().add_many([good, missing, good])
        self.assertEqual([result.scenario_id for result in summary["added"]], ["scn_000"])
        self.assertEqual(len(summary["duplicate"]), 1)
        self.assertEqual(len(summary["failed"]), 1)
        self.assertIn("目录不存在", summary["failed"][0][1])

    def test_fingerprint_is_stable_and_content_sensitive(self):
        first = make_scenario(self.tmp, case_id="scn_000")
        clone = self.tmp / "scn_000_copy"
        shutil.copytree(first, clone)
        self.assertEqual(fingerprint_of(read_scenario(first)[1]),
                         fingerprint_of(read_scenario(clone)[1]))
        vehicle = clone / "vehicle_state.csv"
        vehicle.write_text(vehicle.read_text(encoding="utf-8") + "60.0,19.000,0.000,0.0\n",
                           encoding="utf-8", newline="\n")
        self.assertNotEqual(fingerprint_of(read_scenario(first)[1]),
                            fingerprint_of(read_scenario(clone)[1]))


def run_cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "scenario_cases", *args],
                          cwd=ROOT, capture_output=True, text=True, encoding="utf-8")


class CliLibraryTests(unittest.TestCase):
    def test_cli_add_list_export_roundtrip(self):
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            db = tmp / "library.db"
            directory = make_scenario(tmp, case_id="scn_000", rows=40)
            added = run_cli("library", "add", str(directory), "--db", str(db))
            self.assertEqual(added.returncode, 0, added.stderr)
            self.assertIn("入库 scn_000", added.stdout)

            again = run_cli("library", "add", str(directory), "--db", str(db))
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertIn("跳过 scn_000", again.stdout)

            listed = run_cli("library", "list", "--db", str(db), "--json")
            self.assertEqual(listed.returncode, 0, listed.stderr)
            records = json.loads(listed.stdout)
            self.assertEqual([record["scenario_id"] for record in records], ["scn_000"])
            self.assertEqual(records[0]["rows"], 40)

            out = tmp / "exported"
            exported = run_cli("library", "export", "scn_000", str(out), "--db", str(db))
            self.assertEqual(exported.returncode, 0, exported.stderr)
            self.assertEqual((out / "truth.json").read_bytes(),
                             (directory / "truth.json").read_bytes())

    def test_cli_add_missing_directory_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc = run_cli("library", "add", str(Path(temporary) / "nope"),
                           "--db", str(Path(temporary) / "library.db"))
            self.assertEqual(proc.returncode, 1)
            self.assertIn("目录不存在", proc.stderr)

    def test_cli_add_batch_directory_expands_children(self):
        from scenario_cases.cli import generate_batch

        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            batch = tmp / "batch_001"
            generate_batch(batch, count=4, rows=60, seed=11)
            db = tmp / "library.db"
            proc = run_cli("library", "add", str(batch), "--db", str(db))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("新增 4", proc.stdout)
            again = run_cli("library", "add", str(batch), "--db", str(db))
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertIn("重复 4", again.stdout)
            self.assertIn("新增 0", again.stdout)

    def test_cli_show_unknown_id_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc = run_cli("library", "show", "scn_999",
                           "--db", str(Path(temporary) / "library.db"))
            self.assertEqual(proc.returncode, 1)
            self.assertIn("库中没有", proc.stderr)

    def test_cli_stats_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            tmp = Path(temporary)
            db = tmp / "library.db"
            run_cli("library", "add", str(make_scenario(tmp, case_id="scn_000")), "--db", str(db))
            proc = run_cli("library", "stats", "--db", str(db))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("共 1 条", proc.stdout)
            self.assertIn("hard_brake", proc.stdout)


if __name__ == "__main__":
    unittest.main()
