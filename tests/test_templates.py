"""车速模板的数学性质：门槛、范围、单调性、对抗组的形状保证。"""
import unittest

from scenario_cases.templates import (DEFAULTS, PROFILE_RANGES, design_notes,
                                      design_phases, min_speed, param_range,
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

    def test_design_phases(self):
        self.assertEqual(design_phases("cruise", resolve_params("cruise")), [])
        params = resolve_params("hard_brake", {"at": 4.0, "duration": 2.0})
        self.assertEqual(design_phases("hard_brake", params), [(4.0, 6.0)])
        params = resolve_params("double_dip", {"at": 5.0, "duration": 0.6, "gap": 0.4})
        self.assertEqual(design_phases("double_dip", params), [(5.0, 5.6), (6.0, 6.6)])
        params = resolve_params("v_dip", {"at": 5.0, "dip": 0.4})
        self.assertEqual(design_phases("v_dip", params), [(5.0, 5.4)])

    def test_min_speed_stays_positive(self):
        for profile in DEFAULTS:
            self.assertGreaterEqual(min_speed(profile, resolve_params(profile)), 1.0)

    def test_adversarial_profiles_present(self):
        for name in ("sub_threshold", "short_dip", "v_dip", "jitter", "double_dip"):
            self.assertIn(name, DEFAULTS)

    def test_sub_threshold_decel_stays_below_protocol_floor(self):
        # 模板范围必须保证任何 0.5 秒窗口降幅 < 1.0 m/s
        low, high = PROFILE_RANGES["sub_threshold"]["decel"]
        self.assertLess(0.5 * high, 1.0)
        with self.assertRaises(ValueError):
            resolve_params("sub_threshold", {"decel": 2.0})
        self.assertEqual(resolve_params("sub_threshold", {"decel": 1.9})["decel"], 1.9)

    def test_typical_group_decel_keeps_threshold_margin(self):
        # 典型组减速度低于 2.2 一律拒绝：0.5 秒降幅会不到 1.0
        with self.assertRaises(ValueError):
            resolve_params("mild_decel", {"decel": 2.0})
        self.assertEqual(param_range("mild_decel", "decel"), (2.2, 5.0))

    def test_param_range_falls_back_to_global(self):
        # 模板自己有的参数走覆盖或全局；模板没有的键返回 None
        self.assertEqual(param_range("jitter", "v0"), (10.0, 25.0))
        self.assertEqual(param_range("jitter", "amp"), (0.3, 0.6))
        self.assertEqual(param_range("sub_threshold", "decel"), (1.4, 1.9))
        self.assertIsNone(param_range("cruise", "amp"))
        self.assertIsNone(param_range("jitter", "decel"))


class AdversarialShapeTests(unittest.TestCase):
    def test_v_dip_dips_then_recovers_to_v0(self):
        params = resolve_params("v_dip", {"v0": 20.0, "decel": 4.0, "dip": 0.4,
                                          "recover": 0.4, "at": 5.0})
        self.assertEqual(speed_at("v_dip", 4.9, params)[0], 20.0)
        self.assertAlmostEqual(speed_at("v_dip", 5.2, params)[0], 19.2, places=6)
        self.assertAlmostEqual(speed_at("v_dip", 5.4, params)[0], 18.4, places=6)
        self.assertAlmostEqual(speed_at("v_dip", 5.8, params)[0], 20.0, places=6)  # 恢复完成

    def test_short_dip_is_shallower_than_v_dip(self):
        # 短急减只有 3 个下降间隔，V 形有 4 个——差一个间隔就是两种结论
        self.assertEqual(resolve_params("short_dip")["dip"], 0.3)
        self.assertEqual(resolve_params("v_dip")["dip"], 0.4)

    def test_jitter_stays_within_amplitude(self):
        params = resolve_params("jitter", {"v0": 20.0, "amp": 0.5, "period": 0.8})
        for i in range(200):
            speed, _ = speed_at("jitter", i * 0.1, params)
            self.assertGreaterEqual(speed, params["v0"] - params["amp"] - 1e-9)
            self.assertLessEqual(speed, params["v0"] + 1e-9)

    def test_double_dip_bottom_is_flat_between_dips(self):
        params = resolve_params("double_dip", {"v0": 20.0, "decel": 4.0,
                                               "duration": 0.6, "gap": 0.4, "at": 5.0})
        # 两段之间的匀速段（5.6～6.0）速度持平，不满足搭桥需要的 0.01 m/s 降幅
        for t in (5.6, 5.7, 5.8, 5.9, 6.0):
            self.assertEqual(speed_at("double_dip", t, params)[0], 17.6)
        self.assertAlmostEqual(speed_at("double_dip", 6.3, params)[0], 16.4, places=6)


class DesignNotesTests(unittest.TestCase):
    def test_typical_group_quiet_unless_near_edge(self):
        self.assertEqual(design_notes("hard_brake", resolve_params("hard_brake")), [])
        self.assertEqual(design_notes("cruise", resolve_params("cruise")), [])

    def test_edge_decel_gets_boundary_note(self):
        # 2.5 m/s²：左探窗口降幅正好 1.0，事件左边界由它决定
        notes = design_notes("mild_decel", resolve_params("mild_decel", {"decel": 2.5}))
        self.assertTrue(any("贴边界" in note for note in notes))

    def test_adversarial_profiles_always_annotated(self):
        for profile in ("sub_threshold", "short_dip", "v_dip", "double_dip"):
            self.assertTrue(design_notes(profile, resolve_params(profile)), profile)

    def test_small_jitter_is_quiet_negative(self):
        self.assertEqual(design_notes("jitter", resolve_params("jitter", {"amp": 0.4})), [])


if __name__ == "__main__":
    unittest.main()
