"""车速曲线模板：运动学级，只生成时间-速度序列，不做物理与传感器建模。

每个模板是一个纯函数：给定参数与时间点，返回 (speed, acceleration)。

两组模板：
- 典型组（cruise / mild_decel / hard_brake / lead_cut_in）：持续减速跨过
  SD-WINDOW-1.1 门槛（0.5 秒内下降 ≥ 1.0 m/s，即持续减速度 ≥ 2.0 m/s²）。
- 对抗组（sub_threshold / short_dip / v_dip / jitter / double_dip）：
  专门打口径的边边角角——亚门槛平台、短急减、V 形回升、三角抖动、
  双波段合并。对抗样本的"应有结论"不在这里拍脑袋，由 protocol.py 的
  参考实现从生成数据算出；这里只负责把曲线造出来、把设计意图写清楚。
"""
from __future__ import annotations

from scenario_cases.protocol import (DECREASING_INTERVAL_MIN_DROP_MPS,
                                     MIN_BRIDGE_DROP_MPS,
                                     MIN_DECREASING_INTERVALS,
                                     MIN_SPEED_DROP_MPS)

GRID = 0.1  # 秒，车辆采样网格，与 case-analyzer 的输入约定一致

# 各模板的默认参数（秒、m/s、m/s²）
DEFAULTS = {
    # 典型组
    "cruise": {"v0": 20.0},
    "mild_decel": {"v0": 20.0, "decel": 2.5, "duration": 1.0, "at": 5.0},
    "hard_brake": {"v0": 20.0, "decel": 4.0, "duration": 3.0, "at": 5.0},
    "lead_cut_in": {"v0": 18.0, "decel": 3.5, "duration": 2.0, "at": 4.0},
    # 对抗组
    "sub_threshold": {"v0": 20.0, "decel": 1.6, "duration": 4.0, "at": 5.0},
    "short_dip": {"v0": 20.0, "decel": 4.0, "dip": 0.3, "recover": 0.3, "at": 5.0},
    "v_dip": {"v0": 20.0, "decel": 4.0, "dip": 0.4, "recover": 0.4, "at": 5.0},
    "jitter": {"v0": 20.0, "amp": 0.4, "period": 0.8, "at": 5.0},
    "double_dip": {"v0": 20.0, "decel": 4.0, "duration": 0.6, "gap": 0.4, "at": 5.0},
}

# 全局参数范围；个别模板用 PROFILE_RANGES 覆盖（见下）
RANGES = {
    "v0": (10.0, 25.0),
    "decel": (2.2, 5.0),   # 典型组下限 2.2：留出余量确保 0.5s 窗口降幅 ≥ 1.0 m/s
    "duration": (0.8, 4.0),
    "at": (2.0, 8.0),
    "dip": (0.2, 0.45),
    "recover": (0.2, 0.6),
    "amp": (0.3, 0.6),
    "period": (0.6, 1.0),
    "gap": (0.2, 0.8),
}

PROFILE_RANGES = {
    # 亚门槛：减速度必须 < 2.0 m/s²，任何 0.5 秒窗口降幅都 < 1.0 m/s
    "sub_threshold": {"decel": (1.4, 1.9), "duration": (2.0, 5.0)},
    # 短急减的 dip 上限 0.34：取整到网格后最多 0.3 秒 = 3 个下降间隔，
    # 结论（无事件）不会漏；0.4 秒及以上的行为归 v_dip 管
    "short_dip": {"decel": (3.5, 5.0), "dip": (0.2, 0.34)},
    "v_dip": {"decel": (3.0, 5.0), "dip": (0.4, 0.45)},
    "double_dip": {"duration": (0.5, 0.6)},
}

PROFILE_NAMES = tuple(DEFAULTS)


def param_range(profile: str, key: str):
    """参数的有效范围：先看模板覆盖，再看全局；模板没有这个键则返回 None。

    全局回退只对模板实际拥有的键生效——否则"jitter 有没有 decel 范围"
    这种问题会拿到一个看似合法的答案，调用方就无法据此判断键是否存在。
    """
    if profile not in DEFAULTS or key not in DEFAULTS[profile]:
        return None
    return PROFILE_RANGES.get(profile, {}).get(key, RANGES.get(key))


def resolve_params(profile: str, params: dict | None = None) -> dict:
    """补齐缺省参数；只接受已知键，未知键直接报错（防止规格悄悄失真）。"""
    if profile not in DEFAULTS:
        raise ValueError(f"未知模板：{profile}；可用：{', '.join(PROFILE_NAMES)}")
    merged = dict(DEFAULTS[profile])
    for key, value in (params or {}).items():
        if key not in merged:
            raise ValueError(f"模板 {profile} 不接受参数 {key}")
        bounds = param_range(profile, key)
        if bounds:
            low, high = bounds
            if not low <= value <= high:
                raise ValueError(f"参数 {key}={value} 超出范围 [{low}, {high}]")
        merged[key] = value
    return merged


def _jitter_speed(t: float, params: dict) -> tuple[float, float]:
    """三角抖动：at 之前匀速，之后以 period 为周期在 v0 与 v0-amp 间波动。

    输出 (speed, acceleration)：加速度取三角波两条斜边的解析斜率，
    供生成器写入 acceleration_mps2；边界点上左右斜率不同，这里按所在
    分支的斜率返回，采样点不会停在理论边界之外。
    """
    v0, amp, period, at = params["v0"], params["amp"], params["period"], params["at"]
    if t < at:
        # 抖动开始前保持巡航：速度 v0、加速度 0。
        return v0, 0.0
    # phase ∈ [0, period)：把任意 t 折回一个周期内再分上/下斜边。
    phase = (t - at) % period
    half = period / 2.0
    if phase <= half:
        # 下斜边：v0 线性降到 v0-amp，斜率恒为 -amp/half。
        return v0 - amp * (phase / half), -amp / half
    # 上斜边：从底部线性回 v0。2.0-phase/half 在 (0,1] 之间。
    return v0 - amp * (2.0 - phase / half), amp / half


def _dip_speed(t: float, params: dict) -> tuple[float, float]:
    """V 形：急减 dip 秒后以同样速率恢复——考察「急减但不够持续」的口径边界。

    边界处理：三段用半开区间 [at, at+dip)、[at+dip, at+dip+recover)、
    之后保持 v0——恰好落在切换点时进入下一段，保证任意时刻只属于一段。
    """
    v0, at, dip = params["v0"], params["at"], params["dip"]
    decel, recover = params["decel"], params["recover"]
    if t < at:
        # 减速前巡航。
        return v0, 0.0
    if t < at + dip:
        # 急减段：速度线性下降，斜率 -decel。
        return v0 - decel * (t - at), -decel
    if t < at + dip + recover:
        # 恢复段：从谷底按同一减速度大小回升到 v0。
        return v0 - decel * dip + decel * (t - at - dip), decel
    # 恢复完成后回到巡航速度。
    return v0, 0.0


def _double_dip_speed(t: float, params: dict) -> tuple[float, float]:
    """双波段：两段同深度急减，中间匀速持平——考察合并与搭桥规则。

    两段各从当前速度降 decel×duration：第一段后速度持平走完间隔，
    第二段后保持末端底部速度（不像 V 形那样恢复）。
    """
    v0, at = params["v0"], params["at"]
    duration, decel, gap = params["duration"], params["decel"], params["gap"]
    second = at + duration + gap
    end = second + duration
    if t < at:
        return v0, 0.0
    if t < at + duration:
        return v0 - decel * (t - at), -decel
    if t < second:
        return v0 - decel * duration, 0.0
    if t < end:
        return v0 - decel * duration - decel * (t - second), -decel
    return v0 - 2 * decel * duration, 0.0


def speed_at(profile: str, t: float, params: dict) -> tuple[float, float]:
    """返回 t 时刻的 (speed, acceleration)。

    分派规则按“曲线形状”而不是典型/对抗分组：需要复合形状的模板
    各自有专用函数；其余共享“单一持续减速窗 + 窗外保持末端速度”的
    通用分支（典型组与 sub_threshold）。所有分支的切换点都用半开区间，
    同一时刻只会命中一段。
    """
    if profile == "cruise":
        # 全程匀速：速度 v0、加速度 0。
        return params["v0"], 0.0
    if profile == "jitter":
        return _jitter_speed(t, params)
    if profile in ("short_dip", "v_dip"):
        # 两类 dip 共用 V 形实现；差别在 dip 范围（≤0.34 / ≥0.4），
        # 由 PROFILE_RANGES 保证各自落在“无事件 / 有事件”一侧。
        return _dip_speed(t, params)
    if profile == "double_dip":
        return _double_dip_speed(t, params)
    # 典型组与 sub_threshold：单一持续减速窗，窗外保持末端速度
    at, duration, decel = params["at"], params["duration"], params["decel"]
    if t < at:
        return params["v0"], 0.0
    if t < at + duration:
        return params["v0"] - decel * (t - at), -decel
    return params["v0"] - decel * duration, 0.0


def design_phases(profile: str, params: dict) -> list[tuple[float, float]]:
    """设计上的制动阶段列表（秒）；无制动模板返回空列表。

    注意这是「设计意图」，不是真值：V 形急减的设计阶段是 [at, at+dip]，
    而口径事件是 [at-0.1, at+dip]——窗口向左多探一个采样，两者故意不一致，
    参考实现会把这个差异算出来。
    """
    if profile in ("cruise", "jitter"):
        return []
    if profile in ("short_dip", "v_dip"):
        return [(params["at"], params["at"] + params["dip"])]
    if profile == "double_dip":
        second = params["at"] + params["duration"] + params["gap"]
        return [(params["at"], params["at"] + params["duration"]),
                (second, second + params["duration"])]
    return [(params["at"], params["at"] + params["duration"])]


def min_speed(profile: str, params: dict) -> float:
    """全程最低车速——用于校验不会生成接近停止的车速。"""
    if profile == "cruise":
        return params["v0"]
    if profile == "jitter":
        return params["v0"] - params["amp"]
    if profile in ("short_dip", "v_dip"):
        return params["v0"] - params["decel"] * params["dip"]
    if profile == "double_dip":
        return params["v0"] - 2 * params["decel"] * params["duration"]
    return params["v0"] - params["decel"] * params["duration"]


def design_notes(profile: str, params: dict) -> list[str]:
    """场景设计备注：写清这条样本在打口径的哪个位置。

    只有「贴边界」或「对抗」样本才写备注——备注是回归报告边界桶的入桶条件，
    人人有份就等于没有。
    """
    if profile in ("mild_decel", "hard_brake", "lead_cut_in"):
        # 左探窗口 [at-0.1, at+0.4] 的降幅决定事件左边界是 at-0.1 还是 at
        edge = 0.4 * params["decel"]
        if 0.9 <= edge <= 1.1:
            return [f"贴边界样本：左探窗口降幅 0.4×{params['decel']:.2f}={edge:.2f} m/s，"
                    f"贴近 {MIN_SPEED_DROP_MPS} m/s 门槛，事件左边界是 at-0.1 还是 at 由它决定"]
        return []
    if profile == "sub_threshold":
        worst = 0.5 * params["decel"]
        return [f"对抗样本·亚门槛平台：持续 {params['decel']:.2f} m/s² × "
                f"{params['duration']:.1f} 秒，任意 0.5 秒窗口降幅 ≤ {worst:.2f} m/s "
                f"< {MIN_SPEED_DROP_MPS}，按口径无事件；考察宽筛是否捞出、"
                f"工具是否自判 below_numeric_rule"]
    if profile == "short_dip":
        intervals = round(params["dip"] / GRID)
        return [f"对抗样本·短急减：{params['decel']:.2f} m/s² 仅 {params['dip']:.1f} 秒"
                f"（{intervals} 个下降间隔 < {MIN_DECREASING_INTERVALS}），随后等速恢复；"
                f"按口径无事件，考察「够不够持续」的判定"]
    if profile == "v_dip":
        return [f"对抗样本·V 形急减：{params['decel']:.2f} m/s² × {params['dip']:.1f} 秒后"
                f"等速恢复；合格窗口 [{params['at'] - 0.1:.1f}, {params['at'] + params['dip']:.1f}] "
                f"含 1 个巡航间隔 + 4 个下降间隔，事件左边界比急减起点早 0.1 秒（窗口语义）"]
    if profile == "jitter":
        if params["amp"] >= 0.55:
            return [f"对抗样本·大幅抖动：三角波 ±{params['amp']:.2f} m/s、"
                    f"周期 {params['period']:.1f} 秒；降幅峰值贴近宽筛门槛 0.6，"
                    f"可能产生筛查候选，但口径降幅 < {MIN_SPEED_DROP_MPS} 无事件"]
        return []
    if profile == "double_dip":
        gap = params["gap"]
        base = (f"对抗样本·双波段：两段 {params['duration']:.1f} 秒 @ "
                f"{params['decel']:.2f} m/s²，间隔 {gap:.1f} 秒匀速")
        if gap <= 0.2:
            return [base + "；两段合格窗口相接，协议合并为一个事件"]
        if gap <= 0.4:
            return [base + "；宽筛候选先相接合并、协议事件保持两个——结构性差异案例"]
        return [base + f"；候选与事件均为两个（间隔内速度持平，搭桥需要的 "
                       f"{MIN_BRIDGE_DROP_MPS} m/s 降幅不满足）"]
    return []
