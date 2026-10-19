"""口径参考实现：窗口三条件、合并与搭桥、端到端参考事件。

这些用例把 SD-WINDOW-1.1 的每条规则单独隔离验证——参考实现是真值的
唯一来源，它错了，闭环回归的每个数字都不可信。
"""
import unittest

from scenario_cases.protocol import (merge_intervals, qualifying_windows,
                                     read_speed_series, reference_events)


def series(speeds):
    times = [round(i * 0.1, 1) for i in range(len(speeds))]
    return times, [float(s) for s in speeds]


def csv_of(speeds):
    rows = ["timestamp,speed_mps"]
    rows += [f"{i * 0.1:.1f},{s:.3f}" for i, s in enumerate(speeds)]
    return "\n".join(rows) + "\n"


def steady(v0=20.0, decel=2.5, at=5.0, duration=1.0, count=120):
    speeds = []
    for i in range(count):
        t = i * 0.1
        if t < at:
            speeds.append(v0)
        elif t < at + duration:
            speeds.append(v0 - decel * (t - at))
        else:
            speeds.append(v0 - decel * duration)
    return speeds


class WindowRuleTests(unittest.TestCase):
    def test_steady_decel_qualifies(self):
        # 2.5 m/s²：0.5 秒降 1.25 ≥ 1.0，5 个间隔全在降
        times, speeds = series([20, 19.75, 19.5, 19.25, 19.0, 18.75])
        windows = qualifying_windows(times, speeds)
        self.assertEqual(len(windows), 1)
        self.assertAlmostEqual(windows[0]["speed_drop_mps"], 1.25)
        self.assertEqual(windows[0]["decreasing_intervals"], 5)

    def test_drop_exactly_at_threshold_qualifies(self):
        # 2.0 m/s²：0.5 秒降幅正好 1.0，压线合格（>= 1.0 - eps）
        times, speeds = series([20, 19.8, 19.6, 19.4, 19.2, 19.0])
        self.assertEqual(len(qualifying_windows(times, speeds)), 1)

    def test_drop_below_threshold_fails(self):
        # 1.9 m/s²：0.5 秒降幅 0.95 < 1.0
        times, speeds = series([20, 19.81, 19.62, 19.43, 19.24, 19.05])
        self.assertEqual(qualifying_windows(times, speeds), [])

    def test_only_three_decreasing_intervals_fails(self):
        # 3 个间隔急降共 1.5，后 2 个间隔各回升 0.05（累计 0.1 不超限）：
        # 单独隔离"至少 4 个下降间隔"这条
        times, speeds = series([20, 19.5, 19.0, 18.5, 18.55, 18.6])
        self.assertEqual(qualifying_windows(times, speeds), [])

    def test_recovery_over_limit_fails(self):
        # 4 个间隔下降共 1.2，最后 1 个间隔回升 0.4 > 0.1：单独隔离回升上限
        times, speeds = series([20, 19.6, 19.2, 18.8, 18.4, 18.8])
        self.assertEqual(qualifying_windows(times, speeds), [])


class MergeTests(unittest.TestCase):
    def test_overlapping_union(self):
        times, speeds = series([20.0] * 10)
        self.assertEqual(merge_intervals([(1.0, 2.0), (1.5, 2.5)], times, speeds),
                         [[1.0, 2.5]])

    def test_touching_union(self):
        times, speeds = series([20.0] * 10)
        self.assertEqual(merge_intervals([(1.0, 1.5), (1.5, 2.0)], times, speeds),
                         [[1.0, 2.0]])

    def test_flat_gap_not_bridged(self):
        # 间隔内速度持平：端点降幅 0 < 0.01，不搭桥
        times, speeds = series([20.0] * 16)
        self.assertEqual(merge_intervals([(1.0, 1.5), (2.0, 2.5)], times, speeds),
                         [[1.0, 1.5], [2.0, 2.5]])

    def test_slow_drop_gap_bridged(self):
        # 间隔 0.5 秒内速度非增且共降 0.25 ≥ 0.01：搭桥合并
        times = [round(1.0 + 0.1 * i, 1) for i in range(16)]
        speeds = ([20, 19, 18, 17, 16, 16, 15.95, 15.9, 15.85, 15.8, 15.75,
                   15.7, 15.65, 15.6, 15.55, 15.5])
        self.assertEqual(merge_intervals([(1.0, 1.5), (2.0, 2.5)], times, speeds),
                         [[1.0, 2.5]])

    def test_long_gap_not_bridged(self):
        # 间隔 0.6 > 0.5：长度就不够，哪怕一直在降也不搭桥
        times = [round(1.0 + 0.1 * i, 1) for i in range(19)]
        speeds = [20 - 0.05 * i for i in range(19)]
        self.assertEqual(merge_intervals([(1.0, 1.5), (2.1, 2.6)], times, speeds),
                         [[1.0, 1.5], [2.1, 2.6]])


class ReferenceEventTests(unittest.TestCase):
    def test_steady_decel_event_uses_window_semantics(self):
        # 2.5 m/s²、5.0 秒起持续 1.0 秒：左探窗口 [4.9,5.4] 与右探窗口
        # [5.6,6.1] 都合格，事件边界比设计制动窗各多探 0.1 秒
        events = reference_events(csv_of(steady()))
        self.assertEqual([(e["start"], e["end"]) for e in events], [(4.9, 6.1)])

    def test_slower_decel_starts_exactly_at_brake(self):
        # 2.2 m/s²：左探窗口降幅 0.88 < 1.0 不合格，事件从 at 才开始
        events = reference_events(csv_of(steady(decel=2.2)))
        self.assertEqual([(e["start"], e["end"]) for e in events], [(5.0, 6.0)])

    def test_sub_threshold_has_no_event(self):
        events = reference_events(csv_of(steady(decel=1.6, duration=4.0)))
        self.assertEqual(events, [])

    def test_cruise_has_no_event(self):
        self.assertEqual(reference_events(csv_of([20.0] * 100)), [])

    def test_read_speed_series_round_trip(self):
        times, speeds = read_speed_series(csv_of([10.0, 9.5, 9.0]))
        self.assertEqual(times, [0.0, 0.1, 0.2])
        self.assertEqual(speeds, [10.0, 9.5, 9.0])


if __name__ == "__main__":
    unittest.main()
