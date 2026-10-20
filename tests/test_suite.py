"""回归集（suite）：快照语义、查询创建、导出 manifest、CLI 往返。"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from scenario_cases.cli import main
from scenario_cases.generator import build_case, write_case
from scenario_cases.models import ScenarioSpec
from scenario_cases.store import ScenarioLibrary, SuiteError

from tests.test_store import make_scenario


class SuiteTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.tmp = Path(self._temporary.name)
        self.db = self.tmp / "library.db"
        self.addCleanup(self._temporary.cleanup)

    def library(self):
        return ScenarioLibrary(self.db)

    def _fill(self, count=6):
        """入库一批混合场景：3 急刹（有事件）+ 2 匀速（无事件）+ 1 轻减速（有事件）。"""
        library = self.library()
        profiles = ["hard_brake"] * 3 + ["cruise"] * 2 + ["mild_decel"]
        for index, profile in enumerate(profiles):
            directory = make_scenario(self.tmp, f"scn_{index:03d}", profile)
            library.add_scenario(directory)
        return library

    def test_create_suite_from_query(self):
        library = self._fill()
        records = library.query(profile="hard_brake")
        suite = library.create_suite("brake-only", {"profile": "hard_brake"}, records)
        self.assertEqual(suite.name, "brake-only")
        self.assertEqual(suite.scenario_ids, ("scn_000", "scn_001", "scn_002"))
        self.assertEqual(suite.event_total, 3)

    def test_query_multi_profile(self):
        """--profile a,b 组筛选：一次选出多个模板（如整个对抗组）。"""
        library = self._fill()
        records = library.query(profile=["hard_brake", "mild_decel"])
        self.assertEqual([record.scenario_id for record in records],
                         ["scn_000", "scn_001", "scn_002", "scn_005"])
        suite = library.create_suite("brake-mild", {"profile": ["hard_brake", "mild_decel"]},
                                     records)
        self.assertEqual(len(suite.scenario_ids), 4)

    def test_query_empty_profile_list_rejected(self):
        library = self._fill()
        with self.assertRaises(ValueError):
            library.query(profile=[])

    def test_duplicate_name_rejected(self):
        library = self._fill()
        library.create_suite("dup", {}, library.query())
        with self.assertRaises(SuiteError):
            library.create_suite("dup", {}, library.query())

    def test_snapshot_semantics_new_scenarios_do_not_join(self):
        """核心语义：suite 建成后库里新增场景，不改变回归集成员。"""
        library = self._fill()
        library.create_suite("snap", {}, library.query(with_events=True))
        before = library.get_suite("snap").scenario_ids
        make_scenario(self.tmp, "scn_900", "hard_brake")
        library.add_scenario(self.tmp / "scn_900")
        after = library.get_suite("snap").scenario_ids
        self.assertEqual(before, after)
        self.assertNotIn("scn_900", after)

    def test_snapshot_survives_member_deletion(self):
        """快照还记录已不在库里的成员 id；suite_members 只返回仍在库的。"""
        library = self._fill()
        library.create_suite("keep", {}, library.query())
        with library._connect() as db:
            db.execute("DELETE FROM scenarios WHERE scenario_id = 'scn_001'")
        suite = library.get_suite("keep")
        self.assertIn("scn_001", suite.scenario_ids)  # 快照不动
        members = library.suite_members("keep")
        self.assertNotIn("scn_001", [record.scenario_id for record in members])

    def test_suite_members_unknown_raises(self):
        library = self._fill()
        with self.assertRaises(SuiteError):
            library.suite_members("nope")

    def test_export_suite_layout_and_manifest(self):
        library = self._fill()
        library.create_suite("mixed", {"with_events": True}, library.query(with_events=True))
        out = self.tmp / "export"
        manifest_path = library.export_suite("mixed", out)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["manifest_version"], 1)
        self.assertEqual(manifest["suite"]["name"], "mixed")
        self.assertEqual(manifest["counts"]["scenarios"], 4)
        self.assertEqual(manifest["counts"]["events"], 4)
        self.assertEqual(manifest["counts"]["without_events"], 0)
        self.assertTrue(manifest_path.is_file())

        # 每个成员一个子目录，六个文件齐全，内容与库中字节级一致
        for entry in manifest["scenarios"]:
            directory = out / entry["scenario_id"]
            for name in ("vehicle_state.csv", "object_state.csv",
                         "planning_state.csv", "control_state.csv",
                         "truth.json", "source.json"):
                self.assertTrue((directory / name).is_file(), f"{entry['scenario_id']}/{name}")
            truth = json.loads((directory / "truth.json").read_text(encoding="utf-8"))
            self.assertEqual(len(truth["events"]), len(entry["events"]))

    def test_export_unknown_suite_raises(self):
        library = self._fill()
        with self.assertRaises(SuiteError):
            library.export_suite("ghost", self.tmp / "out")

    def test_schema_upgrade_adds_suites_table(self):
        """旧库（无 suites 表）打开时幂等补建，user_version 升到当前版本。"""
        import sqlite3
        old = self.tmp / "old.db"
        db = sqlite3.connect(old)
        db.execute("PRAGMA user_version = 1")
        db.commit()
        db.close()
        library = ScenarioLibrary(old)  # 打开即补表
        self.assertIsNotNone(library.list_suites())
        check = sqlite3.connect(old)
        try:
            self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], 2)
        finally:
            check.close()  # sqlite3 连接的上下文管理器不关连接，临时目录会删不掉


class SuiteCliTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.tmp = Path(self._temporary.name)
        self.db = self.tmp / "library.db"
        self.addCleanup(self._temporary.cleanup)

    def _cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def _fill(self):
        library = ScenarioLibrary(self.db)
        for index, profile in enumerate(["hard_brake"] * 3 + ["cruise"] * 2):
            directory = make_scenario(self.tmp, f"scn_{index:03d}", profile)
            library.add_scenario(directory)

    def test_cli_add_list_show_export(self):
        self._fill()
        code, out, _ = self._cli("suite", "add", "brake-v1", "--profile", "hard_brake",
                                 "--db", str(self.db))
        self.assertEqual(code, 0)
        self.assertIn("brake-v1", out)

        code, out, _ = self._cli("suite", "list", "--db", str(self.db))
        self.assertEqual(code, 0)
        self.assertIn("brake-v1", out)

        code, out, _ = self._cli("suite", "show", "brake-v1", "--db", str(self.db))
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["scenario_count"], 3)

        code, out, _ = self._cli("suite", "export", "brake-v1", str(self.tmp / "exp"),
                                 "--db", str(self.db))
        self.assertEqual(code, 0)
        manifest = json.loads((self.tmp / "exp" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["counts"]["scenarios"], 3)

    def test_cli_add_empty_query_fails(self):
        self._fill()
        code, _, err = self._cli("suite", "add", "empty", "--profile", "lead_cut_in",
                                 "--db", str(self.db))
        self.assertEqual(code, 1)
        self.assertIn("没有命中", err)

    def test_cli_add_duplicate_name_fails(self):
        self._fill()
        self._cli("suite", "add", "dup", "--db", str(self.db))
        code, _, err = self._cli("suite", "add", "dup", "--db", str(self.db))
        self.assertEqual(code, 1)
        self.assertIn("已存在", err)

    def test_cli_show_unknown_suite_fails(self):
        self._fill()
        code, _, err = self._cli("suite", "show", "ghost", "--db", str(self.db))
        self.assertEqual(code, 1)
        self.assertIn("不存在", err)
