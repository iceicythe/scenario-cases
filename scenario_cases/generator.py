"""由规格生成四路 CSV + 真值，格式与 case-analyzer 的输入约定一致。

真值怎么定：不记录"注入了什么制动窗"——那是和被评测工具共享假设的循环论证。
真值由 protocol.py 的参考实现从生成出来的车辆数据按 SD-WINDOW-1.1 算出，
两个独立实现一致才算检出。设计意图（注入了什么）另存于 spec 与 notes。
"""
from __future__ import annotations

import csv
import io
import json
from dataclasses import replace

from scenario_cases.models import Scenario, ScenarioSpec, TruthEvent
from scenario_cases.protocol import PROTOCOL_ID, reference_events
from scenario_cases.templates import GRID, design_notes, min_speed, resolve_params, speed_at

GENERATOR_VERSION = "scenario-cases-0.2.0"

VEHICLE_HEADERS = ["timestamp", "speed_mps", "acceleration_mps2", "brake"]
OBJECT_HEADERS = ["timestamp", "object_id", "distance_m", "relative_speed_mps", "confidence"]
PLANNING_HEADERS = ["timestamp", "target_speed_mps", "planned_acceleration_mps2", "decision_reason"]
CONTROL_HEADERS = ["timestamp", "brake_command", "throttle_command", "requested_acceleration_mps2"]


def _table(headers, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return stream.getvalue()


def follow_relative(distance: float, speed: float) -> float:
    """简易跟车模型：相对速度与间距误差成正比，间距收敛到目标车距。

    目标车距随车速变化（快车距大），因此减速时间距自然缩短、目标表现"接近"，
    与减速事件的证据方向一致；间距永远为正，不会穿模。
    """
    desired = max(10.0, speed)
    return max(-12.0, min(6.0, (desired - distance) * 0.2))


def build_case(case_id: str, spec: ScenarioSpec, rows: int = 601) -> Scenario:
    """生成一条场景。rows 为采样点数（默认 601 ≈ 60 秒）。"""
    if rows < 10:
        raise ValueError("采样点至少 10 个（case-analyzer 要求车辆至少 6 点，留足余量）")
    params = resolve_params(spec.profile, spec.params)
    if min_speed(spec.profile, params) < 1.0:
        raise ValueError(f"规格会生成接近停止的车速：{spec.profile} {params}")
    # 真值记录补齐后的完整参数：默认值填上的才是真正生成数据的参数
    spec = replace(spec, params=params)

    vehicle, objects, planning, control = [], [], [], []
    distance = 60.0
    for i in range(rows):
        t = i * GRID
        speed, acceleration = speed_at(spec.profile, t, params)
        braking = acceleration < 0
        brake = 0.6 if braking else 0.0
        vehicle.append([f"{t:.1f}", f"{speed:.3f}", f"{acceleration:.3f}", brake])

        relative = follow_relative(distance, speed)
        if spec.profile == "lead_cut_in" and braking:
            relative = max(relative - 4.0, -14.0)  # 切入目标额外接近
        if i:
            step = relative * GRID
            if distance + step < 5.0:  # 触底保护：本步不再接近，保持相对速度列与距离列自洽
                step = 5.0 - distance
                relative = step / GRID
            distance += step
        objects.append([f"{t:.1f}", "target_a", f"{distance:.3f}", f"{relative:.3f}", 0.95])

        reason = "CRUISE"
        if braking:
            reason = "EMERGENCY_BRAKE" if spec.profile == "lead_cut_in" else "FOLLOW_LEAD_VEHICLE"
        planning.append([f"{t:.1f}", f"{max(0.0, speed + acceleration * GRID):.3f}",
                         acceleration, reason])
        control.append([f"{t:.1f}", brake, 0.15 if not braking else 0.0, acceleration])

    files = {
        "vehicle_state.csv": _table(VEHICLE_HEADERS, vehicle),
        "object_state.csv": _table(OBJECT_HEADERS, objects),
        "planning_state.csv": _table(PLANNING_HEADERS, planning),
        "control_state.csv": _table(CONTROL_HEADERS, control),
    }

    # 真值：按 SD-WINDOW-1.1 参考实现从刚生成的车辆数据计算。
    # V 形样本的设计制动窗是 [at, at+dip]，而口径事件是 [at-0.1, at+dip]——
    # 窗口向左多探一个采样。记录注入参数就等于把工具的假设当真值，会循环论证。
    events = [TruthEvent(item["start"], item["end"], item["kind"])
              for item in reference_events(files["vehicle_state.csv"])]
    notes = design_notes(spec.profile, params)

    return Scenario(case_id=case_id, spec=spec, files=files, events=events, notes=notes)


def source_json(scenario: Scenario) -> str:
    """来源声明：合成数据，运动学级，不是道路采集或闭环仿真。"""
    payload = {
        "case_id": scenario.case_id,
        "source_type": "synthetic",
        "source_name": "公式生成的仿真场景；运动学级时间序列，不是道路采集或闭环仿真",
        "acceleration_source": "由车速有限差分得到，不是独立传感器测量",
        "generator": GENERATOR_VERSION,
        "spec": {"profile": scenario.spec.profile, "params": scenario.spec.params},
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def truth_json(scenario: Scenario) -> str:
    """真值：参考实现算出的事件窗口 + 设计备注，供闭环回归对答案用。"""
    payload = {
        "generator": GENERATOR_VERSION,
        "case_id": scenario.case_id,
        "sampling_grid_s": GRID,
        "truth_basis": (f"{PROTOCOL_ID} 参考实现（scenario_cases/protocol.py）"
                        f"从生成的车辆数据计算，非注入参数"),
        "spec": {"profile": scenario.spec.profile, "params": scenario.spec.params},
        "events": [event.as_dict() for event in scenario.events],
        "notes": scenario.notes,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def write_case(scenario: Scenario, directory) -> list:
    """把一条场景落盘：四路 CSV + source.json + truth.json，返回写出的文件名。"""
    from pathlib import Path

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in scenario.files.items():
        path = directory / name
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(name)
    (directory / "source.json").write_text(source_json(scenario), encoding="utf-8", newline="\n")
    (directory / "truth.json").write_text(truth_json(scenario), encoding="utf-8", newline="\n")
    written += ["source.json", "truth.json"]
    return written
