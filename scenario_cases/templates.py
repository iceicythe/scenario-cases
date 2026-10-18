"""车速曲线模板：运动学级，只生成时间-速度序列，不做物理与传感器建模。

每个模板是一个纯函数：给定参数与时间点，返回 (speed, acceleration)。
制动窗口内减速度必须足以跨过 SD-WINDOW-1.1 的门槛（0.5 秒内下降 ≥ 1.0 m/s，
即持续减速度 ≥ 2.0 m/s²），否则造出来的"事件"过不了检测口径，真值就失去意义。
"""
from __future__ import annotations

# 各模板的默认参数与取值范围（秒、m/s、m/s²）
DEFAULTS = {
    "cruise": {"v0": 20.0},
    "mild_decel": {"v0": 20.0, "decel": 2.5, "duration": 1.0, "at": 5.0},
    "hard_brake": {"v0": 20.0, "decel": 4.0, "duration": 3.0, "at": 5.0},
    "lead_cut_in": {"v0": 18.0, "decel": 3.5, "duration": 2.0, "at": 4.0},
}

RANGES = {
    "v0": (10.0, 25.0),
    "decel": (2.2, 5.0),        # 下限 2.2：留出余量确保 0.5s 窗口降幅 ≥ 1.0 m/s
    "duration": (0.8, 4.0),
    "at": (2.0, 8.0),
}

PROFILE_NAMES = tuple(DEFAULTS)


def resolve_params(profile: str, params: dict | None = None) -> dict:
    """补齐缺省参数；只接受已知键，未知键直接报错（防止规格悄悄失真）。"""
    if profile not in DEFAULTS:
        raise ValueError(f"未知模板：{profile}；可用：{', '.join(PROFILE_NAMES)}")
    merged = dict(DEFAULTS[profile])
    for key, value in (params or {}).items():
        if key not in merged:
            raise ValueError(f"模板 {profile} 不接受参数 {key}")
        if key in RANGES:
            low, high = RANGES[key]
            if not low <= value <= high:
                raise ValueError(f"参数 {key}={value} 超出范围 [{low}, {high}]")
        merged[key] = value
    return merged


def speed_at(profile: str, t: float, params: dict) -> tuple[float, float]:
    """返回 t 时刻的 (speed, acceleration)。所有模板在制动窗外均为匀速。"""
    if profile == "cruise":
        return params["v0"], 0.0
    at, duration, decel = params["at"], params["duration"], params["decel"]
    if t < at:
        return params["v0"], 0.0
    if t < at + duration:
        return params["v0"] - decel * (t - at), -decel
    return params["v0"] - decel * duration, 0.0


def braking_window(profile: str, params: dict) -> tuple[float, float] | None:
    """制动窗口 [start, end]（秒）；无事件模板返回 None。"""
    if profile == "cruise":
        return None
    return params["at"], params["at"] + params["duration"]


def min_speed(profile: str, params: dict) -> float:
    """全程最低车速——用于校验不会生成负车速。"""
    if profile == "cruise":
        return params["v0"]
    return params["v0"] - params["decel"] * params["duration"]
