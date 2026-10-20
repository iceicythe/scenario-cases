# scenario-cases

**运动学级仿真场景生成器：造"带标准答案的卷子"，用来检验智驾测试分析工具的检出率与误报率。**

每条场景按参数化模板生成四路 CSV 日志（车辆/目标/规划/控制，格式与 case-analyzer 输入约定一致），
并同时产出一份真值 `truth.json`：**按 SD-WINDOW-1.1 口径从生成数据独立算出的事件窗口**、
场景参数与设计备注。
生成 → 交给分析工具跑 → 与真值对比 → 出指标，这条闭环就是本项目要支撑的东西。

**诚实声明**：本项目生成的是运动学级数据（时间-速度序列 + 简单目标运动），
**不是**物理级仿真、传感器仿真或 3D 仿真，也不用于评估车辆性能；
它的唯一目的是给测试分析工具提供带已知答案的评测数据。

## 快速开始

无第三方依赖，Python 3.11+：

```shell
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

相同 `--seed` 结果完全可复现；`--profiles hard_brake,cruise` 可指定参与混合的模板（局部归一化配比）。

## 场景库（SQLite 单文件）

生成的一批场景可以登记进一个单文件 SQLite 库：元数据、真值事件与文件内容一起入库，
按内容指纹去重（内容相同即重复，与目录位置无关），支持按模板、事件类型、参数范围检索，
也能原样导出（字节级一致）。没有来源声明（`source.json`）的场景不予入库。

```shell
# 入库：参数可以是批量目录（自动展开其中的场景子目录），也可以是单个场景目录
python -m scenario_cases library add ./generated/batch_001 --db ./library.db

# 检索：所有含急刹的场景
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

设计取舍：单文件 SQLite = 零依赖、一个文件拷走就是整个库（不上 Postgres/MySQL 的理由：
单机单人工具、要能一键拷走）；文件内容随元数据入库 = 源目录删了也能原样导出；
常用检索轴（profile / v0 / decel / duration / at）建成列，完整参数另存 JSON，范围查询走列、信息不丢。

## 回归集（suite）：把一次挑选固化成"同一套卷子"

评测要可比，两次跑的必须是同一批场景。`suite` 把一次查询结果固化为命名快照：

```shell
# 按查询条件创建（条件与 library list 相同）
python -m scenario_cases suite add brake-v1 --profile hard_brake --db ./library.db

# 列出、查看、导出
python -m scenario_cases suite list --db ./library.db
python -m scenario_cases suite show brake-v1 --db ./library.db
python -m scenario_cases suite export brake-v1 ./regression/brake-v1 --db ./library.db
```

导出的目录每个场景一个子目录（字节级一致），顶层一份 `manifest.json`：
suite 元信息、逐场景真值事件与参数、内容指纹——下游拿 manifest 即可对答案，
不必解析每份 truth.json。

**关键取舍：suite 存成员快照，不只存查询条件。** 只存条件的话，库里每新增一条
场景，回归集成员就悄悄变了——两次评测跑的其实不是同一套卷子，指标不可比。
查询条件只作为 provenance 记录在案（"这个集当初是怎么挑的"）。

造数据、读指标的方法论沉淀在 [docs/EVAL_CHECKLIST.md](docs/EVAL_CHECKLIST.md)：
真值怎么避免循环论证、对抗样本参数怎么反推、双层指标怎么分开读。

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
取整到网格后最多 3 个下降间隔）。`--rows` 控制采样点数（0.1 秒网格，默认 601 ≈ 60 秒）。

## 闭环回归（与 case-analyzer 对答案）

一条命令跑完：生成场景 → 导入 case-analyzer → 规则筛选 → 与真值对比 → 检出率/误报率报告。
不需要 API Key、不需要人工标注，相同 `--seed` 完全可复现。

```shell
# 需要 case-analyzer 的运行环境（pandas/numpy 是其依赖）
# 用它的虚拟环境跑；源码位置默认取同级目录，也可用 --analyzer 或 CASE_ANALYZER_ROOT 指定
../case-analyzer-oss/.venv/Scripts/python.exe -m scenario_cases regress \
    --count 60 --seed 20261019 --out ./regression_run
```

### 真值为什么不算循环论证

早期版本的真值是"注入了什么制动窗"——那和被评测工具共享同一套假设：
工具按同一口径检出了注入的事件，100% 只证明两边代码没写岔，不证明任何事。
现在真值由 `scenario_cases/protocol.py`——一份**照抄口径常量、独立实现**的
SD-WINDOW-1.1 计算器——从生成的车辆 CSV 现场算出。两个独立实现一致才算检出；
回归还会把"参考实现的事件集 vs 工具自带口径参考区间"逐条比对，不一致就报出来。

### 两层口径，两层指标

case-analyzer 的有意设计：候选筛查门槛（0.6 m/s 降幅、3 个下降间隔、0.2 m/s 回升）
比事件口径（1.0 m/s、4 个间隔、0.1 m/s）宽——**宽筛为了不漏，噪声留给复核层**。
所以指标分两层读：

| 指标 | 定义 |
|---|---|
| 检出率（宽筛层） | 真值事件被候选区间集合（先合并）覆盖 ≥ 0.9。宽筛更松，理应 100% |
| 误报率（宽筛层） | 候选簇对任何真值事件覆盖 < 0.9。亚门槛样本会被宽筛捞出，这是固有噪声 |
| 确认率（口径层） | 事件被"自判符合口径"（meets_numeric_rule）的候选覆盖 ≥ 0.9 |
| 口径误判 | 候选自判符合口径，却没有真值事件支撑——**这才是实现 bug** |
| 结构不一致 | 有真值事件但候选簇数 ≠ 事件数（过度合并/切碎） |
| 实现一致率 | 参考事件集 vs 工具自带口径参考区间，逐条比对 |

一次 60 条实测（种子 20261019，含 26 条贴边界/对抗样本）：

```text
场景 60 条 · 导入 60 条成功 · 失败 0 条
真值事件 45 个 · 检出 45 个 · 检出率 100.0%（宽筛层）
候选 53 个 · 误报候选 8 个 · 误报率 15.1%（宽筛层）
口径确认 45/45（确认率 100.0%） · 口径误判候选 0 个 · 宽筛噪声 8 个
结构不一致案例 0 个 · 口径实现一致 60/60（100.0%）
```

读法：8 个误报全部来自亚门槛平台/短急减样本，且全部被工具自判 `below_numeric_rule`——
宽筛按设计捞它们出来，复核层按口径把它们放回去。**"捞出但自判合格却没有事件支撑"的
候选是 0 个**，这是本轮回归真正守的东西。

`--out` 目录下留存 `regression_report.json`（逐案例明细：漏检事件、误报候选及判定、
分模板统计、贴边界样本、实现不一致清单），默认临时目录跑完即清，`--keep` 可留现场排查。

### 已知边界（重要，勿过度解读数字）

- 100% 检出率是**可证明的**（宽筛门槛严格更松，协议事件必被候选覆盖），不是巧合；
  本轮的价值在于两个独立实现在 60 条对抗样本上逐条一致。
- 宽筛噪声 15.1% 量化了"宽筛→复核"这条设计的固有成本，**不代表工具在真实数据上会误报**——
  真实数据的噪声结构（传感器误差、采样抖动、合法驾驶行为）不在生成能力内。
- 参考实现是照抄口径常量的第二份实现，不是第三方标准实现；它和工具一致，证明的是
  "双方对口径的理解一致"，不是"口径本身正确"。

## 测试

```shell
python -B -m unittest discover -s tests -v
```

覆盖模板数学性质（门槛/范围/单调性、对抗组形状保证）、口径参考实现（窗口三条件逐一隔离、
合并与搭桥、端到端事件）、生成器输出（表头、网格、真值一致性、来源声明）、
命令行（批量、可复现、参数校验）、场景库（去重、检索、导出、持久化、CLI 往返）、
回归集（快照语义、查询创建、manifest 结构、导出字节级一致、CLI 往返、旧库补表迁移）、
闭环指标（覆盖合并、双层判定、结构差异、analyzer 定位）。闭环回归的真跑用例需要
case-analyzer 源码及其 pandas/numpy 环境，缺失时自动跳过（不报错）。

## 路线图（按周推进，范围只缩不扩）

- [x] W1–W2：场景模板与生成器（四路 CSV + 真值 + 来源声明 + 测试）
- [x] W3：场景库入库（SQLite 单文件、sha256 指纹去重、按参数检索、原样导出）
- [x] W4：闭环回归（生成 → 调用 case-analyzer 全跑 → 对真值 → 检出率/误报率报告）
- [x] W5：口径参考实现 + 对抗样本（真值不再循环论证；双层指标；实现一致性比对）
- [x] W6：回归集导出（suite 快照 + manifest）+ 评测 checklist 沉淀
  （[docs/EVAL_CHECKLIST.md](docs/EVAL_CHECKLIST.md)）

明确不做：物理引擎、传感器建模、3D 渲染、CARLA/SUMO 集成、强化学习。

## License

MIT。
