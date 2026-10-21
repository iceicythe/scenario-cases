# scenario-cases

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![CI](https://github.com/iceicythe/scenario-cases/actions/workflows/ci.yml/badge.svg)](https://github.com/iceicythe/scenario-cases/actions/workflows/ci.yml)

**智驾测试分析工具的"带标准答案的卷子"：运动学级仿真场景生成器 + 场景库 + 闭环回归。**

造一批带已知答案的测试场景 → 交给分析工具跑 → 与真值对比 → 输出检出率/误报率。
整条闭环零第三方依赖、同种子完全可复现，用来回答一个问题：
**你的减速事件检测工具，到底能检出多少、会误报多少、在边界样本上会不会错？**

> **诚实声明**：本项目生成的是运动学级数据（时间-速度序列 + 简单目标运动），
> **不是**物理级仿真、传感器仿真或 3D 仿真，也不用于评估车辆性能。
> 它的唯一目的是给测试分析工具提供带已知答案的评测数据。

## 它解决什么问题

- **效果没法量化**：检测工具"准不准"不能靠感觉，需要带已知答案的数据才能算检出率/误报率；
- **真实数据标注贵**：路采数据逐帧标注成本高，且急刹、贴边界样本可遇不可求；
- **指标不可比**：每次随手挑一批数据评测，下次换一批，数字对不上——需要固化的回归集；
- **循环论证**：很多自测工具按注入的参数造数据、再按同一套假设检测，"100% 检出"只证明两边代码没写岔。

本项目针对这四点分别给出：合成数据、对抗样本、回归集快照、独立重算的真值。

## 功能特性

- **参数化模板**：9 个车速曲线模板（4 个典型组 + 5 个对抗组），每条场景独立可复现
- **四路 CSV 日志**：车辆/目标/规划/控制，格式与 [case-analyzer](https://github.com/iceicythe/case-analyzer) 的输入约定一致
- **真值独立重算**：`truth.json` 的事件窗口由独立实现的口径计算器从生成数据现场算出，不记录"注入了什么"——不与被评测工具共享假设
- **单文件场景库**：SQLite 单文件存储，内容指纹去重、按模板/参数范围检索、字节级原样导出
- **回归集（suite）**：把一次挑选固化为命名快照，保证两次评测跑的是同一套卷子
- **闭环回归**：一条命令跑完 生成 → 导入分析工具 → 规则筛选 → 对真值 → 双层指标报告
- **固定案例对拍**：`--cases/--labels` 不现生成，直接对拍既有案例集（真值取外部人工标签），仓库自带困难案例回归集
- **门禁与元数据**：`--check` 七项判定、非零退出码；报告自带代码版本、数据指纹、阈值快照，可挂 CI

## 环境要求

- Python 3.11+；`generate` / `library` / `suite` **除标准库外零依赖**
- `regress` 子命令需要 [case-analyzer](https://github.com/iceicythe/case-analyzer) 的运行环境（其依赖 pandas/numpy）

## 快速开始

```shell
git clone https://github.com/iceicythe/scenario-cases.git
cd scenario-cases

# 在仓库根目录执行（包以目录形式被引用）
python -m scenario_cases generate --out ./generated/batch_001 --count 20 --seed 20261018
```

输出结构：

```text
generated/batch_001/
├── scn_000/
│   ├── vehicle_state.csv     # timestamp, speed_mps, acceleration_mps2, brake
│   ├── object_state.csv      # timestamp, object_id, distance_m, relative_speed_mps, confidence
│   ├── planning_state.csv    # timestamp, target_speed_mps, planned_acceleration_mps2, decision_reason
│   ├── control_state.csv     # timestamp, brake_command, throttle_command, requested_acceleration_mps2
│   ├── source.json           # 来源声明：合成数据，运动学级，非道路采集
│   └── truth.json            # 真值：口径参考实现算出的事件 + 场景参数 + 设计备注
├── scn_001/
└── ...
```

相同 `--seed` 结果完全可复现；`--profiles hard_brake,cruise` 可指定参与混合的模板（配比局部归一化）。

**批次命名空间**：场景 ID 是 `scn_<tag>_序号`。`tag` 默认由"生成器版本 + 种子 + 行数 +
模板配比"算短哈希——同配置可复现（重复入库按内容指纹去重），不同配置互不撞 ID、可同时入库；
`--tag brake-v1` 也可人工指定。刻意不含条数：多生成几条应与少生成几条共用命名空间，
让指纹去重识别重复，而不是换一批新 ID。

## 使用指南

### 场景库

生成的一批场景可以登记进一个单文件 SQLite 库：元数据、真值事件与文件内容一起入库，
按内容指纹去重（内容相同即重复，与目录位置无关），支持按模板、事件类型、参数范围检索，
也能原样导出（字节级一致）。没有来源声明（`source.json`）的场景不予入库。

```shell
# 入库：参数可以是批量目录（自动展开其中的场景子目录），也可以是单个场景目录
python -m scenario_cases library add ./generated/batch_001 --db ./library.db

# 检索：所有含急刹的场景（--profile 逗号分隔可多选）
python -m scenario_cases library list --profile hard_brake --db ./library.db

# 检索：减速度 ≥ 4 m/s² 的场景，JSON 输出便于脚本消费
python -m scenario_cases library list --min-decel 4.0 --json --db ./library.db

# 只要没有真值事件的负样本
python -m scenario_cases library list --with-events no --db ./library.db

# 导出、详情、统计
python -m scenario_cases library export scn_000 ./out/scn_000 --db ./library.db
python -m scenario_cases library show scn_000 --db ./library.db
python -m scenario_cases library stats --db ./library.db
```

### 回归集（suite）：把一次挑选固化成"同一套卷子"

评测要可比，两次跑的必须是同一批场景。`suite` 把一次查询结果固化为命名快照：

```shell
# 按查询条件创建（条件与 library list 相同）
python -m scenario_cases suite add brake-v1 --profile hard_brake --db ./library.db

# 一次选出整个对抗组
python -m scenario_cases suite add adversarial \
    --profile sub_threshold,short_dip,v_dip,jitter,double_dip --db ./library.db

# 列出、查看、导出
python -m scenario_cases suite list --db ./library.db
python -m scenario_cases suite show brake-v1 --db ./library.db
python -m scenario_cases suite export brake-v1 ./regression/brake-v1 --db ./library.db
```

导出的目录每个场景一个子目录（字节级一致），顶层一份 `manifest.json`：
suite 元信息、逐场景真值事件与参数、内容指纹——下游拿 manifest 即可对答案，
不必解析每份 `truth.json`。

**关键取舍：suite 存成员快照，不只存查询条件。** 只存条件的话，库里每新增一条
场景，回归集成员就悄悄变了——两次评测跑的其实不是同一套卷子，指标不可比。
查询条件只作为 provenance 记录在案（"这个集当初是怎么挑的"）。

### 闭环回归（与 case-analyzer 对答案）

一条命令跑完：生成场景 → 导入 case-analyzer → 规则筛选 → 与真值对比 → 检出率与双口径误报指标报告。
不需要 API Key、不需要人工标注，相同 `--seed` 完全可复现。

```shell
# 用 case-analyzer 的 Python 环境运行（它带 pandas/numpy）；
# 源码位置默认取同级目录，也可用 --analyzer 或 CASE_ANALYZER_ROOT 指定
python -m scenario_cases regress --count 60 --seed 20261019 --out ./regression_run
```

**门禁模式**：`--check` 让回归结果能拦住错误版本——导入失败、口径误判、实现不一致、
漏检、**确认漏检**、结构不一致任何一项未过即返回非零退出码（阈值可用
`--max-noise-candidates` 等参数逐项声明），可直接挂 CI：

```shell
python -m scenario_cases regress --count 20 --seed 20261019 --check --max-noise-candidates 0.3
```

宽筛噪声只设容忍上限（固有成本，不要求零误报）；口径误判与实现不一致是实现 bug，
默认必须为 0 / 100%。本仓库的 CI 就跑这条命令（含固定种子的回归门禁 job）。

**"漏检"与"确认漏检"是两层**：前者看候选是否覆盖真值，后者看确认区间是否覆盖真值。
候选覆盖正常而确认能力退化（区间收紧、匹配变严）时，前者全绿、后者报警——
只有分开设门禁才拦得住。用例见 `tests/test_regress.py::test_confirmed_missed_independent_of_candidate_missed`。

#### 固定案例模式：困难案例回归集

`--cases/--labels` 不现生成，直接对拍既有案例集，真值取外部人工标签——
用来把"工具在已知困难输入上的行为"钉住。仓库自带
[`suites/hard-cases-v1/`](suites/hard-cases-v1/)：三例来自 sim-batch-02 冻结集、
因车辆采样缺口未进事件指标分母的案例（含人机分歧案例 seg_023）。

```shell
# --out 必须是全新目录：runtime 已有同 ID 数据时导入判冲突（导入保护，不是回归）
python -m scenario_cases regress \
    --cases suites/hard-cases-v1/cases --labels suites/hard-cases-v1/labels \
    --out generated/hard-cases-run --keep --check \
    --min-agreement 0.6 --max-confirmed-missed 2 --max-noise-candidates 0.7 \
    --max-structural-mismatch 1
```

2026-10-21 基线七项全过：导入 3/3、口径误判 0、实现一致 2/3（seg_023 人机分歧是
已知差异）、漏检 0、确认漏检 2（缺口案例不自动确认的设计行为）、宽筛噪声 66.7%
（缺口案例候选无支撑的已知行为）、结构不一致 1（同上）。阈值含义与成员说明见
`suites/hard-cases-v1/manifest.json`——那是**已知行为包线**，不是通过线。

两种模式的报告带同样的元数据：`code_version`（两个仓库的 git HEAD）、
`data_fingerprint`（输入内容哈希）、协议、阈值快照与逐项门禁结果——
数字可以放在同一张表里读，也回答"这批数字是哪份代码对哪份数据按什么阈值跑的"。

#### 真值为什么不算循环论证

早期版本的真值是"注入了什么制动窗"——那和被评测工具共享同一套假设：
工具按同一口径检出了注入的事件，100% 只证明两边代码没写岔，不证明任何事。
现在真值由 `scenario_cases/protocol.py`——一份**照抄口径常量、独立实现**的
SD-WINDOW-1.1 计算器——从生成的车辆 CSV 现场算出。两个独立实现一致才算检出；
回归还会把"参考实现的事件集 vs 工具自带口径参考区间"逐条比对，不一致就报出来。

#### 两层口径，两层指标

case-analyzer 的有意设计：候选筛查门槛（0.6 m/s 降幅、3 个下降间隔、0.2 m/s 回升）
比事件口径（1.0 m/s、4 个间隔、0.1 m/s）宽——**宽筛为了不漏，噪声留给复核层**。
所以指标分两层读：

| 指标 | 定义 |
|---|---|
| 检出率（宽筛层） | 真值事件被候选区间集合（先合并）覆盖 ≥ 0.9。宽筛更松，理应 100% |
| 误报候选占比（宽筛层） | 无真值支撑的候选项 ÷ 总候选项。**复核工作量口径**——接力候选各算一次 |
| 误报簇占比（宽筛层） | 无真值支撑的候选簇 ÷ 总候选簇。**事件质量口径**——重叠/接力候选同簇只算一次 |
| 确认率（口径层） | 事件被"自判符合口径"（meets_numeric_rule）的候选覆盖 ≥ 0.9 |
| 口径误判 | 候选自判符合口径，却没有真值事件支撑——**这才是实现 bug** |
| 结构不一致 | 有真值事件但候选簇数 ≠ 事件数（过度合并/切碎） |
| 实现一致率 | 参考事件集 vs 工具自带口径参考区间，逐条比对 |

两个误报口径都**不是**经典假阳性率 FPR——FPR 的分母是 FP+TN，本工具没有负例单位，
套用这个名字只会引起歧义。为什么要两个：同一个错误簇里两个接力候选，按候选算是 2 个误报
（复核员要多看两条），按簇算是 1 个（实际就一处错）——工作量看前者，质量看后者。

一次 60 条实测（种子 20261019，含 23 条贴边界/对抗样本，生成器 0.2.1）：

```text
场景 60 条 · 导入 60 条成功 · 失败 0 条
真值事件 45 个 · 检出 45 个 · 检出率 100.0%（宽筛层）
候选 56 个 · 误报候选 11 个 · 误报簇 11 个 · 误报候选占比 19.6% · 误报簇占比 19.6%（宽筛层）
口径确认 45/45（确认率 100.0%） · 口径误判候选 0 个 · 宽筛噪声 11 个
结构不一致案例 0 个 · 口径实现一致 60/60（100.0%）
回归门禁（--check）：七项全部 PASS
```

读法：11 个误报全部来自亚门槛/短急减样本，且全部被工具自判 `below_numeric_rule`——
宽筛按设计捞它们出来，复核层按口径把它们放回去。**"捞出但自判合格却没有事件支撑"的
候选是 0 个**，这是本轮回归真正守的东西。本批没有接力候选，所以两个误报口径数值相同；
口径分叉的用例见 `tests/test_regress.py::test_false_positive_two_calibers_differ`。

> 生成器 0.2.1 修复了模板配比的排序口径（见下），同一种子生成的场景序列随之变化，
> 因此本数字与 0.2.0 时代的记录（误报 15.1%）不同。0.2.0 的两轮历史报告保留在
> `generated/regress-w5/` 与 `generated/regress-w6/` 存档，不随代码改动回溯改写。

`--out` 目录下留存 `regression_report.json`（逐案例明细：漏检事件、误报候选及判定、
分模板统计、贴边界样本、实现不一致清单），默认临时目录跑完即清，`--keep` 可留现场排查。

#### 已知边界（重要，勿过度解读数字）

- 100% 检出率是**可证明的**（宽筛门槛严格更松，协议事件必被候选覆盖），不是巧合；
  本轮的价值在于两个独立实现在 60 条对抗样本上逐条一致。
- 宽筛噪声 15.1% 量化了"宽筛→复核"这条设计的固有成本，**不代表工具在真实数据上会误报**——
  真实数据的噪声结构（传感器误差、采样抖动、合法驾驶行为）不在生成能力内。
- 参考实现是照抄口径常量的第二份实现，不是第三方标准实现；它和工具一致，证明的是
  "双方对口径的理解一致"，不是"口径本身正确"。

## 场景模板

| 模板 | 行为 | 真值事件（口径参考实现算出） |
|---|---|---|
| `cruise` | 匀速，无制动 | 无（负样本） |
| `mild_decel` | 轻减速（默认 2.5 m/s²，1 秒） | 有，边界比制动窗各多探 0.1 秒 |
| `hard_brake` | 急刹（默认 4 m/s²，3 秒） | 有，同上 |
| `lead_cut_in` | 前车切入 + 制动，目标额外接近 | 有，同上 |
| `sub_threshold` | **对抗**：亚门槛平台（1.4–1.9 m/s² 持续 2–5 秒） | 无——任意 0.5 秒窗口降幅 < 1.0 |
| `short_dip` | **对抗**：短急减 0.2–0.3 秒（仅 2–3 个下降间隔）后恢复 | 无——不够"持续" |
| `v_dip` | **对抗**：V 形急减 0.4 秒（4 个下降间隔）后恢复 | 有——`[at-0.1, at+dip]`，左边界比急减起点早 0.1 秒（窗口语义） |
| `jitter` | **对抗**：三角抖动（±0.3–0.6 m/s，周期 0.6–1.0 秒） | 无——大幅抖动贴近宽筛门槛时会产生筛查候选 |
| `double_dip` | **对抗**：双波段急减，间隔 0.2–0.8 秒匀速 | 间隔 0.2 秒合并为一个；≥0.3 秒保持两个（间隔内速度持平，不满足搭桥降幅） |

典型组的减速度下限 2.2 m/s²，保证 0.5 秒窗口降幅 ≥ 1.0 m/s，跨过 SD-WINDOW-1.1 门槛；
对抗组专门打口径的边边角角，参数范围按"结论必须成立"反推（如短急减的 dip 上限 0.34 秒，
取整到网格后最多 3 个下降间隔——再大样本就悄悄变成另一类，结论泄露）。
`--rows` 控制采样点数（0.1 秒网格，默认 601 ≈ 60 秒）。

造数据、读指标的方法论沉淀在 [docs/EVAL_CHECKLIST.md](docs/EVAL_CHECKLIST.md)：
真值怎么避免循环论证、对抗样本参数怎么反推、双层指标怎么分开读。

## 项目结构

```text
scenario-cases/
├── scenario_cases/
│   ├── templates.py    # 9 个模板的速度曲线定义、参数范围与联合约束
│   ├── generator.py    # 规格 → 四路 CSV + truth.json + source.json
│   ├── protocol.py     # SD-WINDOW-1.1 口径的独立参考实现（真值来源）
│   ├── store.py        # SQLite 场景库：入库/检索/导出 + 回归集快照
│   ├── regress.py      # 闭环回归：生成/固定案例两种模式 → 双层指标 + 门禁
│   └── cli.py          # 命令行入口
├── suites/
│   └── hard-cases-v1/  # 困难案例固定回归集（采样缺口三例 + 人工标签 + 阈值基线）
├── tests/              # 149 个单元/集成测试
├── .github/workflows/  # CI：单元测试 + 固定种子闭环回归门禁 + 困难案例对拍（--check）
└── docs/
    └── EVAL_CHECKLIST.md  # 评测方法论 checklist
```

## 设计取舍

- **单文件 SQLite**：零依赖、一个文件拷走就是整个库。不上 Postgres/MySQL 的理由：单机单人工具、要能一键拷走。
- **文件内容随元数据入库**：源目录删了也能原样导出。
- **常用检索轴建成列**（profile / v0 / decel / duration / at），完整参数另存 JSON：范围查询走列、信息不丢。
- **suite 存成员快照**：回归集定义不受库新增影响，指标可比；查询条件只作 provenance。
- **哈希与抽样共用排序口径**：批次命名空间按 `sorted(mix)` 算哈希，模板抽样也按名字序遍历——否则同配比不同书写顺序会算出同一批次名却抽出不同内容（同 ID 不同内容，入库必撞）。生成器版本号同样进哈希：修复这类"同配置不同内容"的问题时升 minor，新旧批次自动换命名空间，不与历史数据撞。
- **对抗参数按结论反推**：先定"这条样本必须得出什么结论"，再反推参数范围；时间参数对齐 0.1 秒采样网格，取整不允许把样本推进另一类。

## 测试

```shell
python -B -m unittest discover -s tests -v
```

覆盖模板数学性质（门槛/范围/单调性、对抗组形状保证）、口径参考实现（窗口三条件逐一隔离、
合并与搭桥、端到端事件）、生成器输出（表头、网格、真值一致性、来源声明）、
命令行（批量、可复现、参数校验、批次命名空间与配比排序口径、固定案例模式互斥）、
场景库（去重、检索、导出、持久化、CLI 往返）、
回归集（快照语义、查询创建、manifest 结构、导出字节级一致、CLI 往返、旧库补表迁移）、
闭环指标（覆盖合并、双口径误报、双层判定、结构差异、analyzer 定位、--check 七项门禁判定
与退出码、固定案例标签适配）。闭环回归的真跑用例需要 case-analyzer 源码及其 pandas/numpy 环境，
缺失时自动跳过（不报错）。

## Roadmap

已实现：场景生成器、场景库、回归集、闭环回归、口径参考实现与对抗样本。

可能的方向（不承诺排期）：更多对抗形状、适配其他分析工具、CI 流水线。
欢迎通过 Issue / PR 交流。

**非目标**：物理引擎、传感器建模、3D 渲染、CARLA/SUMO 集成、强化学习——
本项目刻意停在运动学级，把"带标准答案的测试数据"这一件事做透。

## 相关项目

- [case-analyzer](https://github.com/iceicythe/case-analyzer) —— 本项目的被评测对象：
  智驾测试 Case 分析助手（减速事件检测 + AI 归因 + 飞书通知）

## License

[MIT](LICENSE)
