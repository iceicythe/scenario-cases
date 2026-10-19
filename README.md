# scenario-cases

**运动学级仿真场景生成器：造"带标准答案的卷子"，用来检验智驾测试分析工具的检出率与误报率。**

每条场景按参数化模板生成四路 CSV 日志（车辆/目标/规划/控制，格式与 case-analyzer 输入约定一致），
并同时产出一份真值 `truth.json`：注入的事件窗口、场景参数与设计备注。
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
│   └── truth.json            # 真值：注入事件窗口 + 场景参数 + 备注
├── scn_001/
└── ...
```

相同 `--seed` 结果完全可复现；`--profiles hard_brake,cruise` 可指定参与混合的模板。

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

## 场景模板

| 模板 | 行为 | 真值事件 |
|---|---|---|
| `cruise` | 匀速，无制动 | 无（负样本） |
| `mild_decel` | 轻减速（默认 2.5 m/s²，1 秒） | 制动窗口 |
| `hard_brake` | 急刹（默认 4 m/s²，3 秒） | 制动窗口 |
| `lead_cut_in` | 前车切入 + 制动，目标额外接近 | 制动窗口 |

所有制动模板的持续减速度下限为 2.2 m/s²，保证任意 0.5 秒窗口降幅 ≥ 1.0 m/s，
跨过 SD-WINDOW-1.1 的减速事件门槛——否则"事件"过不了检测口径，真值没有意义。
`--rows` 控制采样点数（0.1 秒网格，默认 601 ≈ 60 秒）。

## 闭环回归（与 case-analyzer 对答案）

一条命令跑完：生成场景 → 导入 case-analyzer → 规则筛选 → 与生成器真值对比 → 检出率/误报率报告。
不需要 API Key、不需要人工标注，相同 `--seed` 完全可复现。

```shell
# 需要 case-analyzer 的运行环境（pandas/numpy 是其依赖）
# 用它的虚拟环境跑；源码位置默认取同级目录，也可用 --analyzer 或 CASE_ANALYZER_ROOT 指定
../case-analyzer-oss/.venv/Scripts/python.exe -m scenario_cases regress \
    --count 60 --seed 20261019 --out ./regression_run
```

输出（一次 60 条实测）：

```text
场景 60 条 · 导入 60 条成功 · 失败 0 条
真值事件 51 个 · 检出 51 个 · 检出率 100.0%
候选 51 个 · 误报候选 0 个 · 误报率 0.0%
```

指标口径（与 case-analyzer 的 `evaluation.py` 对齐，真值换成生成器注入的事件窗口）：

| 指标 | 定义 |
|---|---|
| 检出 | 真值事件被**候选区间集合**（先合并重叠/相接）覆盖的比例 ≥ 0.9；两个候选接力盖住一个事件算检出 |
| 漏检 | 覆盖不足 0.9 的真值事件，报告里给出合并覆盖值 |
| 误报候选 | 候选先合并成簇，簇对**任何**真值事件的覆盖都 < 0.9；切碎同一个事件的候选不各算一次 |
| 负样本 | 无真值事件的场景，出现的任何候选都计误报 |

`--out` 目录下留存 `regression_report.json`（逐案例明细：漏检事件、误报候选、分模板统计、
贴边界样本单独统计），默认临时目录跑完即清，`--keep` 可留现场排查。

### 已知边界（重要，勿过度解读数字）

当前模板的减速度下限（2.2 m/s²）是**按 SD-WINDOW-1.1 门槛（0.5 秒降 1.1 m/s）留余量设计的**，
负样本是纯匀速——用"保证能过规则"的数据测规则，100% 只证明**管道一致性和管内自洽**，
不证明工具在真实数据分布上的泛化能力。要测泛化，需要门槛边缘的对抗样本：
亚门槛减速、V 形（降后回升）、平台期（停住不动）、锯齿波动——已列入路线图，
在那之前，请把回归报告当作"回归防护网"（改动不过度、不过灵敏），而不是"性能证书"。

## 测试

```shell
python -B -m unittest discover -s tests -v
```

覆盖模板数学性质（门槛/范围/单调性）、生成器输出（表头、网格、真值一致性、来源声明）、
命令行（批量、可复现、参数校验）、场景库（去重、检索、导出、持久化、CLI 往返）、
闭环指标（覆盖合并、检出/误报口径、analyzer 定位）。闭环回归的真跑用例需要
case-analyzer 源码及其 pandas/numpy 环境，缺失时自动跳过（不报错）。

## 路线图（按周推进，范围只缩不扩）

- [x] W1–W2：场景模板与生成器（四路 CSV + 真值 + 来源声明 + 测试）
- [x] W3：场景库入库（SQLite 单文件、sha256 指纹去重、按参数检索、原样导出）
- [x] W4：闭环回归（生成 → 调用 case-analyzer 全跑 → 对真值 → 检出率/误报率报告）
- [ ] W4+：门槛边缘对抗样本（亚门槛减速、V 形恢复、平台期、锯齿）——测泛化，防循环论证
- [ ] W4+：回归集导出、checklist 沉淀（视进度取舍）

明确不做：物理引擎、传感器建模、3D 渲染、CARLA/SUMO 集成、强化学习。

## License

MIT。
