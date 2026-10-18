"""场景用例的数据模型：一份规格 → 一组 CSV + 一份真值。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScenarioSpec:
    """一条场景的生成规格。

    profile: 车速曲线模板名（见 templates.py）
    params: 模板参数（初速度、减速度、持续时长等），具体键由模板定义
    """

    profile: str
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TruthEvent:
    """注入的真值事件：相对时间轴上的闭区间 [start, end]（秒）。"""

    start: float
    end: float
    kind: str = "deceleration"

    def as_dict(self) -> dict:
        return {"start": round(self.start, 3), "end": round(self.end, 3), "kind": self.kind}


@dataclass
class Scenario:
    """生成结果：四路 CSV 文本 + 真值 + 来源声明。"""

    case_id: str
    spec: ScenarioSpec
    files: dict          # {文件名: CSV 文本}
    events: list         # [TruthEvent]
    notes: list = field(default_factory=list)  # 场景设计备注（如"贴边界样本"）
