"""批量生成命令行：python -m scenario_cases generate ..."""
from __future__ import annotations

import argparse
import math
import random
import sys
from pathlib import Path

from scenario_cases.generator import GENERATOR_VERSION, build_case, write_case
from scenario_cases.models import ScenarioSpec
from scenario_cases.templates import DEFAULTS, PROFILE_NAMES, RANGES

# 批量时的模板配比：多数有事件，少量无事件（负样本）， cut_in 少量
MIX = {"hard_brake": 0.35, "mild_decel": 0.3, "lead_cut_in": 0.2, "cruise": 0.15}


def pick_profile(rng: random.Random) -> str:
    roll = rng.random()
    cumulative = 0.0
    for name, weight in MIX.items():
        cumulative += weight
        if roll <= cumulative:
            return name
    return "cruise"


def jitter(rng: random.Random, profile: str) -> dict:
    """在模板范围内抖动参数；未知参数键不动。"""
    params = {}
    for key, value in DEFAULTS[profile].items():
        if key not in RANGES:
            params[key] = value
            continue
        low, high = RANGES[key]
        if key == "decel":
            # 减速度下限保留门槛余量，抖动时同样遵守
            low = max(low, 2.2)
        params[key] = round(rng.uniform(low, high), 2)
    if profile != "cruise":
        # 联合约束：单个参数合法不代表组合合法——末端车速至少保留 3 m/s 余量。
        # 时长向下取整到 0.01 秒，取整只会更保守，不会把余量吃掉。
        max_duration = math.floor((params["v0"] - 3.0) / params["decel"] * 100) / 100
        params["duration"] = round(min(params["duration"], max(0.8, max_duration)), 2)
    return params


def generate_batch(out: Path, count: int, rows: int, seed: int) -> list:
    rng = random.Random(seed)
    written = []
    for index in range(count):
        profile = pick_profile(rng)
        params = jitter(rng, profile)
        spec = ScenarioSpec(profile=profile, params=params)
        scenario = build_case(f"scn_{index:03d}", spec, rows)
        written.extend(write_case(scenario, out / scenario.case_id))
    return written


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scenario_cases",
                                     description="批量生成运动学级仿真场景（四路 CSV + 真值）")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate", help="生成一批场景")
    gen.add_argument("--out", type=Path, required=True, help="输出目录（每个场景一个子目录）")
    gen.add_argument("--count", type=int, default=20, help="场景条数，默认 20")
    gen.add_argument("--rows", type=int, default=601, help="每条场景采样点数，默认 601（约 60 秒）")
    gen.add_argument("--seed", type=int, default=20261018, help="随机种子，相同种子结果可复现")
    gen.add_argument("--profiles", default=",".join(PROFILE_NAMES),
                     help="参与混合的模板，逗号分隔；默认全部")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "generate":
        return 2
    if args.count < 1 or args.rows < 10:
        print("count 至少 1，rows 至少 10", file=sys.stderr)
        return 2
    unknown = [name for name in args.profiles.split(",") if name and name not in PROFILE_NAMES]
    if unknown:
        print(f"未知模板：{', '.join(unknown)}；可用：{', '.join(PROFILE_NAMES)}", file=sys.stderr)
        return 2
    allowed = [name for name in args.profiles.split(",") if name]
    weights = {name: MIX.get(name, 0.0) for name in allowed}
    total = sum(weights.values()) or 1.0
    for name in allowed:
        MIX[name] = weights[name] / total

    files = generate_batch(args.out, args.count, args.rows, args.seed)
    print(f"已生成 {args.count} 条场景 → {args.out}（{len(files)} 个文件，种子 {args.seed}，"
          f"生成器 {GENERATOR_VERSION}）")
    print("模板混合：" + "，".join(f"{name} {MIX[name]:.0%}" for name in allowed))
    print("数据为公式合成的运动学级序列，不是道路采集或闭环仿真。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
