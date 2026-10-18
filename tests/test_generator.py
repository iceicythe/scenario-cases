"""生成器输出：格式、网格、真值与来源声明。"""
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from scenario_cases.generator import (GENERATOR_VERSION, build_case, source_json,
                                      truth_json, write_case)
from scenario_cases.models import ScenarioSpec
from scenario_cases.templates import speed_at, resolve_params


def rows_of(text):
    return list(csv.reader(io.StringIO(text)))


class GeneratorTests(unittest.TestCase):
    def test_case_shape(self):
        case = build_case("scn_000", ScenarioSpec("hard_brake"), rows=61)
        self.assertEqual(set(case.files), {"vehicle_state.csv", "object_state.csv",
                                           "planning_state.csv", "control_state.csv"})
        for name, text in case.files.items():
            table = rows_of(text)
            self.assertEqual(len(table), 62, name)  # 表头 + 61 行
        vehicle = rows_of(case.files["vehicle_state.csv"])
        self.assertEqual(vehicle[0], ["timestamp", "speed_mps", "acceleration_mps2", "brake"])
        timestamps = [float(row[0]) for row in vehicle[1:]]
        self.assertEqual(timestamps[0], 0.0)
        for earlier, later in zip(timestamps, timestamps[1:]):
            self.assertLess(earlier, later)  # 严格递增
        for earlier, later in zip(timestamps, timestamps[1:]):
            self.assertAlmostEqual(later - earlier, 0.1, places=6)  # 固定 0.1 网格

    def test_speed_column_matches_template(self):
        spec = ScenarioSpec("mild_decel", {"v0": 20.0, "decel": 3.0, "duration": 2.0, "at": 3.0})
        case = build_case("scn_001", spec, rows=101)
        params = resolve_params(spec.profile, spec.params)
        table = rows_of(case.files["vehicle_state.csv"])[1:]
        for index, row in enumerate(table):
            expected, _ = speed_at(spec.profile, index * 0.1, params)
            self.assertAlmostEqual(float(row[1]), expected, places=3)

    def test_truth_window_matches_injection(self):
        spec = ScenarioSpec("hard_brake", {"at": 5.0, "duration": 3.0})
        case = build_case("scn_002", spec, rows=200)
        self.assertEqual([(e.start, e.end) for e in case.events], [(5.0, 8.0)])
        truth = json.loads(truth_json(case))
        self.assertEqual(truth["events"], [{"start": 5.0, "end": 8.0, "kind": "deceleration"}])
        self.assertEqual(truth["generator"], GENERATOR_VERSION)

    def test_cruise_has_no_events(self):
        case = build_case("scn_003", ScenarioSpec("cruise"), rows=50)
        self.assertEqual(case.events, [])
        self.assertEqual(json.loads(truth_json(case))["events"], [])

    def test_borderline_note(self):
        spec = ScenarioSpec("mild_decel", {"decel": 2.2, "duration": 1.0, "at": 4.0})
        case = build_case("scn_004", spec, rows=100)
        self.assertTrue(any("贴边界" in note for note in case.notes))

    def test_source_declares_synthetic(self):
        case = build_case("scn_005", ScenarioSpec("hard_brake"), rows=30)
        payload = json.loads(source_json(case))
        self.assertEqual(payload["source_type"], "synthetic")
        self.assertIn("不是道路采集", payload["source_name"])

    def test_spec_that_stops_the_vehicle_is_rejected(self):
        with self.assertRaises(ValueError):
            build_case("scn_006", ScenarioSpec("hard_brake", {"v0": 12.0, "decel": 4.0, "duration": 4.0}), rows=100)

    def test_write_case_outputs_six_files(self):
        case = build_case("scn_007", ScenarioSpec("lead_cut_in"), rows=30)
        with tempfile.TemporaryDirectory() as temporary:
            written = write_case(case, Path(temporary) / "scn_007")
            self.assertEqual(len(written), 6)
            for name in written:
                self.assertTrue((Path(temporary) / "scn_007" / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
