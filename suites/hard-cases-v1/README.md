# hard-cases-v1：困难案例固定回归集

三例来自 sim-batch-02 冻结评测集、因车辆采样缺口未进入事件指标分母的案例。
它们进回归集不是为了刷指标，是为了**把工具在已知困难输入上的行为钉住**：
采样缺口案例不自动确认、不编造结论、人机分歧可追溯——行为一旦漂移，
门禁立刻报警，由人判断是改进还是退化。

## 目录

```
hard-cases-v1/
├── manifest.json    成员、困难点、预期行为与阈值基线（先读这个）
├── cases/           三例四路日志（与冻结集逐字节一致）
└── labels/          人工复核生效标签（human_verified，含 seg_023 修正记录）
```

## 跑法

```bash
# 用 case-analyzer 的虚拟环境（regress 依赖 pandas/numpy）
../case-analyzer-oss/.venv/Scripts/python.exe -m scenario_cases regress \
    --cases suites/hard-cases-v1/cases --labels suites/hard-cases-v1/labels \
    --out generated/hard-cases-run --keep --check \
    --max-import-failures 0 --max-misjudged 0 --min-agreement 0.6 \
    --max-missed 0 --max-confirmed-missed 2 --max-noise-candidates 0.7 \
    --max-structural-mismatch 1
```

2026-10-21 基线（七项全过）：导入 3/3、口径误判 0、实现一致 2/3、
漏检 0、确认漏检 2（缺口案例不自动确认的设计行为）、宽筛噪声 66.7%
（缺口案例候选无真值支撑的已知行为）、结构不一致 1（seg_023 人机分歧）。

阈值含义见 `manifest.json` 的 `gates_baseline`：这是**已知行为包线**，
不是通过线。工具将来若修好了采样缺口处理（例如能跨缺口桥接），
一致率会升、结构不一致会降——那是改进，届时上调包线即可；
反之任何一项跌破包线，先查是需求变了还是实现退了。

## 与主回归的分工

- 主回归（`regress` 默认生成模式）：合成场景全自动对拍，回答"工具稳不稳"；
- 本回归集：真实困难输入对拍，回答"工具的行为符不符合已知预期"。
  报告元数据（代码版本、数据指纹、阈值快照、逐项结果）两种模式完全一致，
  数字可以放在同一张表里读。
