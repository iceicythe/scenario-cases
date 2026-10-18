"""车速模板的数学性质：门槛、范围、单调性。"""
import unittest

from scenario_cases.templates import (DEFAULTS, braking_window, min_speed,
                                      resolve_params, speed_at)


class TemplateTests(unittest.TestCase):
    def test_cruise_is_constant(self):
        params = resolve_params("cruise")
        for t in (0, 1.5, 30, 59.9):
            speed, acceleration = speed_at("cruise", t, params)
            self.assertEqual(speed, params["v0"])
            self.assertEqual(acceleration, 0.0)

    def test_mild_decel_crosses_sd_window_threshold(self):
        # SD-WINDOW-1.1：0.5 秒内下降 ≥ 1.0 m/s，即持续减速度 ≥ 2.0 m/s²。
        params = resolve_params("mild_decel")
        start = params["at"]
        early = speed_at("mild_decel", start, params)[0]
        half = speed_at("mild_decel", start + 0.5, params)[0]
        self.assertGreaterEqual(early - half, 1.0)

    def test_hard_brake_window_values(self):
        params = resolve_params("hard_brake", {"v0": 20.0, "decel": 4.0, "duration": 3.0, "at": 5.0})
        self.assertEqual(speed_at("hard_brake", 5.0, params)[0], 20.0)
        self.assertAlmostEqual(speed_at("hard_brake", 8.0, params)[0], 8.0, places=6)
        self.assertEqual(speed_at("hard_brake", 9.0, params)[0], 8.0)  # 窗外保持末端速度
        self.assertEqual(min_speed("hard_brake", params), 8.0)

    def test_resolve_params_fills_defaults(self):
        params = resolve_params("mild_decel", {"decel": 3.0})
        self.assertEqual(params["decel"], 3.0)
        self.assertEqual(params["v0"], DEFAULTS["mild_decel"]["v0"])

    def test_resolve_params_rejects_unknown_key(self):
        with self.assertRaises(ValueError):
            resolve_params("hard_brake", {"nope": 1})

    def test_resolve_params_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            resolve_params("hard_brake", {"decel": 9.0})

    def test_resolve_params_rejects_unknown_profile(self):
        with self.assertRaises(ValueError):
            resolve_params("teleport")

    def test_braking_window(self):
        self.assertIsNone(braking_window("cruise", resolve_params("cruise")))
        params = resolve_params("hard_brake", {"at": 4.0, "duration": 2.0})
        self.assertEqual(braking_window("hard_brake", params), (4.0, 6.0))

    def test_min_speed_stays_positive(self):
        for profile in DEFAULTS:
            self.assertGreaterEqual(min_speed(profile, resolve_params(profile)), 1.0)


if __name__ == "__main__":
    unittest.main()
