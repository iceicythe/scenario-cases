"""生成器输出：格式、网格、参考真值与来源声明。"""
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from scenario_cases.generator import (GENERATOR_VERSION, build_case, source_json,
                                      truth_json, write_case)
from scenario_cases.models import ScenarioSpec
from scenario_cases.templates import resolve_params, speed_at


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
        for profile, params in (("mild_decel", {"v0": 20.0, "decel": 3.0, "duration": 2.0, "at": 3.0}),
                                ("v_dip", {"v0": 20.0, "decel": 4.0, "dip": 0.4, "recover": 0.4, "at": 3.0}),
                                ("jitter", {"v0": 20.0, "amp": 0.5, "period": 0.8, "at": 3.0})):
            spec = ScenarioSpec(profile, params)
            case = build_case("scn_speed", spec, rows=101)
            resolved = resolve_params(spec.profile, spec.params)
            table = rows_of(case.files["vehicle_state.csv"])[1:]
            for index, row in enumerate(table):
                expected, _ = speed_at(spec.profile, index * 0.1, resolved)
                self.assertAlmostEqual(float(row[1]), expected, places=3, msg=profile)

    def test_truth_events_come_from_protocol_reference(self):
        # 真值不再记录注入的制动窗：4.0 m/s²、5.0 秒起 3 秒，
        # 参考实现算出的事件是 [4.9, 8.1]（两端各多探一个采样）
        spec = ScenarioSpec("hard_brake", {"at": 5.0, "duration": 3.0})
        case = build_case("scn_002", spec, rows=200)
        self.assertEqual([(e.start, e.end) for e in case.events], [(4.9, 8.1)])
        truth = json.loads(truth_json(case))
        self.assertEqual(truth["events"], [{"start": 4.9, "end": 8.1, "kind": "deceleration"}])
        self.assertEqual(truth["generator"], GENERATOR_VERSION)
        self.assertIn("参考实现", truth["truth_basis"])
        self.assertIn("非注入参数", truth["truth_basis"])

    def test_slower_decel_event_starts_at_brake(self):
        # 2.2 m/s²：左探窗口降幅 0.88 不够门槛，事件从 at 才开始
        spec = ScenarioSpec("mild_decel", {"decel": 2.2, "duration": 1.0, "at": 4.0})
        case = build_case("scn_003", spec, rows=100)
        self.assertEqual([(e.start, e.end) for e in case.events], [(4.0, 5.0)])

    def test_cruise_has_no_events(self):
        case = build_case("scn_004", ScenarioSpec("cruise"), rows=50)
        self.assertEqual(case.events, [])
        self.assertEqual(json.loads(truth_json(case))["events"], [])

    def test_borderline_note(self):
        # 2.5 m/s² 正好压在左探窗口门槛上，生成器应标注贴边界
        spec = ScenarioSpec("mild_decel", {"decel": 2.5, "duration": 1.0, "at": 4.0})
        case = build_case("scn_005", spec, rows=100)
        self.assertTrue(any("贴边界" in note for note in case.notes))

    def test_sub_threshold_has_no_events(self):
        case = build_case("scn_006", ScenarioSpec("sub_threshold"), rows=200)
        self.assertEqual(case.events, [])
        self.assertTrue(any("亚门槛" in note for note in case.notes))

    def test_short_dip_has_no_events(self):
        # 0.3 秒急减只有 3 个下降间隔，不够口径的 4 个
        case = build_case("scn_007", ScenarioSpec("short_dip"), rows=200)
        self.assertEqual(case.events, [])

    def test_v_dip_event_starts_before_dip(self):
        # 0.4 秒急减：合格窗口 [at-0.1, at+dip] 含 1 巡航 + 4 下降间隔
        spec = ScenarioSpec("v_dip", {"at": 5.0})
        case = build_case("scn_008", spec, rows=200)
        self.assertEqual([(e.start, e.end) for e in case.events], [(4.9, 5.4)])

    def test_double_dip_gap_decides_merge(self):
        # 间隔 0.2 秒：合格窗口相接，一个事件；间隔 0.4 秒：两个
        # （间隔内速度持平，搭桥需要的 0.01 m/s 降幅不满足）
        merged = build_case("scn_009", ScenarioSpec("double_dip", {"gap": 0.2}), rows=200)
        self.assertEqual([(e.start, e.end) for e in merged.events], [(4.9, 6.5)])
        split = build_case("scn_010", ScenarioSpec("double_dip", {"gap": 0.4}), rows=200)
        self.assertEqual([(e.start, e.end) for e in split.events], [(4.9, 5.7), (5.9, 6.7)])

    def test_jitter_has_no_events(self):
        case = build_case("scn_011", ScenarioSpec("jitter"), rows=200)
        self.assertEqual(case.events, [])

    def test_source_declares_synthetic(self):
        case = build_case("scn_012", ScenarioSpec("hard_brake"), rows=30)
        payload = json.loads(source_json(case))
        self.assertEqual(payload["source_type"], "synthetic")
        self.assertIn("不是道路采集", payload["source_name"])

    def test_spec_that_stops_the_vehicle_is_rejected(self):
        with self.assertRaises(ValueError):
            build_case("scn_013", ScenarioSpec("hard_brake", {"v0": 12.0, "decel": 4.0, "duration": 4.0}), rows=100)

    def test_object_distance_stays_positive_and_consistent(self):
        # 目标间距永不为负；relative_speed 列与 distance 列的差分严格自洽。
        for profile in ("cruise", "mild_decel", "hard_brake", "lead_cut_in", "sub_threshold",
                        "short_dip", "v_dip", "jitter", "double_dip"):
            case = build_case(f"scn_obj_{profile}", ScenarioSpec(profile), rows=601)
            table = rows_of(case.files["object_state.csv"])[1:]
            distances = [float(row[2]) for row in table]
            relatives = [float(row[3]) for row in table]
            self.assertGreaterEqual(min(distances), 5.0, profile)
            for index in range(1, len(distances)):
                expected = (distances[index] - distances[index - 1]) / 0.1
                self.assertAlmostEqual(relatives[index], expected, delta=0.02, msg=profile)

    def test_write_case_outputs_six_files(self):
        case = build_case("scn_014", ScenarioSpec("lead_cut_in"), rows=30)
        with tempfile.TemporaryDirectory() as temporary:
            written = write_case(case, Path(temporary) / "scn_014")
            self.assertEqual(len(written), 6)
            for name in written:
                self.assertTrue((Path(temporary) / "scn_014" / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
