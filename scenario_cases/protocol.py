"""SD-WINDOW-1.1 口径的独立参考实现：从车辆 CSV 文本算出参考事件。

为什么闭环回归需要它：真值如果直接记录"注入了什么参数"，就和被评测工具
共享同一套假设——工具按同一口径检出了注入的事件，100% 是循环论证。
参考实现从生成出来的数据出发，按公开口径（窗口三条件 + 1.1 版搭桥合并）
独立重算一遍：两个实现一致才算检出，不一致就是其中一边有 bug，
回归报告会把差异原样列出来。

口径来源：case-analyzer-oss 的 configs/annotation_protocol_v1.json（窗口规则）
与 configs/annotation_protocol_v1_1.json（合并规则）。此处不 import 那个仓库，
常量照抄——照抄本身就是一次跨仓库对齐，改口径时两边都要改。
"""
from __future__ import annotations

import csv
import io

PROTOCOL_ID = "SD-WINDOW-1.1"
WINDOW_SECONDS = 0.5
SAMPLE_PERIOD = 0.1
STEPS = 5  # 0.5 秒窗口 = 6 个采样点 = 5 个间隔
MIN_SPEED_DROP_MPS = 1.0
MIN_DECREASING_INTERVALS = 4
DECREASING_INTERVAL_MIN_DROP_MPS = 0.01
MAX_CUMULATIVE_RECOVERY_MPS = 0.1
EPS = 1e-6
MAX_BRIDGE_GAP_SECONDS = 0.5
MAX_PER_STEP_INCREASE_MPS = 0.0
MIN_BRIDGE_DROP_MPS = 0.01


def read_speed_series(csv_text: str) -> tuple[list[float], list[float]]:
    """从 vehicle_state.csv 文本读出 (时间, 速度) 两列。"""
    reader = csv.DictReader(io.StringIO(csv_text))
    times, speeds = [], []
    for row in reader:
        times.append(float(row["timestamp"]))
        speeds.append(float(row["speed_mps"]))
    return times, speeds


def qualifying_windows(times: list[float], speeds: list[float]) -> list[dict]:
    """滑过每一个 0.5 秒窗口，返回满足口径三条件的窗口。

    三条件（SD-WINDOW-1.0 窗口规则）：
    1. 窗口首尾降幅 ≥ 1.0 m/s；
    2. 5 个间隔里至少 4 个在下降（每段降幅 ≥ 0.01 m/s）；
    3. 窗口内累计回升 ≤ 0.1 m/s。
    """
    windows = []
    # 最后一个可用窗口从 len(speeds)-STEPS 开始：窗口需要 6 个采样点，
    # 起点再往后就没有完整 0.5 秒可看。数据不足时 range 直接为空。
    for i in range(max(0, len(speeds) - STEPS)):
        values = speeds[i:i + STEPS + 1]
        # 逐间隔差分：deltas[k] 是第 k 个 0.1 秒间隔的速度变化。
        deltas = [values[k + 1] - values[k] for k in range(STEPS)]
        drop = values[0] - values[-1]
        # EPS 统一放在比较侧：浮点车速序列可能带 1e-15 级误差，
        # 直接用 > / >= 会把数学上恰好贴门槛的样本判错方向。
        decreases = sum(1 for d in deltas if d <= -DECREASING_INTERVAL_MIN_DROP_MPS + EPS)
        recovery = sum(d for d in deltas if d > 0)
        if (drop >= MIN_SPEED_DROP_MPS - EPS
                and decreases >= MIN_DECREASING_INTERVALS
                and recovery <= MAX_CUMULATIVE_RECOVERY_MPS + EPS):
            windows.append({"start_time": times[i], "end_time": times[i + STEPS],
                            "speed_drop_mps": drop,
                            "decreasing_intervals": decreases,
                            "cumulative_recovery_mps": recovery})
    return windows


def merge_intervals(intervals, times: list[float],
                    speeds: list[float]) -> list[list[float]]:
    """SD-WINDOW-1.1 合并：重叠/相接直接并；间断 ≤ 0.5 秒、且间隔内速度非增、
    端点降幅 ≥ 0.01 m/s 才搭桥。数据完整性由生成器保证（完整 0.1 秒网格、
    无缺失），因此这里不重复做来源校验。"""
    merged: list[list[float]] = []
    for start, end in sorted((float(a), float(b)) for a, b in intervals):
        # 分支一：重叠或相接（start ≤ 上一簇右端）直接并入当前簇。
        # 这里不检查间隔内速度——两段窗口已经各自合格，拼接本身就是合法合并。
        if merged and start <= merged[-1][1] + EPS:
            merged[-1][1] = max(merged[-1][1], end)
            continue
        if merged:
            # 分支二：间隔 ≤ 0.5 秒时尝试“搭桥”。搭桥不看整段总降幅，
            # 只看三点：间隔内每个采样都不回升（d ≤ 0）、跨间隔总降幅
            # ≥ 0.01 m/s、且至少有两个采样点可检查。
            previous = merged[-1][1]
            if start - previous <= MAX_BRIDGE_GAP_SECONDS + EPS:
                gap = [(t, s) for t, s in zip(times, speeds)
                       if previous - EPS <= t <= start + EPS]
                if len(gap) >= 2:
                    gap_speeds = [s for _, s in gap]
                    gap_deltas = [gap_speeds[k + 1] - gap_speeds[k]
                                  for k in range(len(gap_speeds) - 1)]
                    if (all(d <= MAX_PER_STEP_INCREASE_MPS + EPS for d in gap_deltas)
                            and gap_speeds[0] - gap_speeds[-1] >= MIN_BRIDGE_DROP_MPS - EPS):
                        merged[-1][1] = max(merged[-1][1], end)
                        continue
        # 分支三：既不相接也不满足搭桥，开启新簇。
        merged.append([start, end])
    return merged


def reference_events(csv_text: str) -> list[dict]:
    """参考事件：合格窗口按 1.1 版规则合并后的区间列表。"""
    times, speeds = read_speed_series(csv_text)
    windows = qualifying_windows(times, speeds)
    merged = merge_intervals([(w["start_time"], w["end_time"]) for w in windows],
                             times, speeds)
    return [{"start": round(start, 3), "end": round(end, 3), "kind": "deceleration"}
            for start, end in merged]
