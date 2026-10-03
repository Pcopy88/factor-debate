# 多智能体因子研究第 1、2、4 项修改实施计划

> **For agentic workers:** 后续实施时使用 `superpowers:executing-plans`，逐项执行本文件中的复选框并记录验收证据。2026-09-30 用户已授权实施，任务 1—9 已执行；实际证据与差异见 [实施验收记录](../../优先项124实施验收.md)。

**Goal：**让两个研究 Agent 根据完整的开发与验证结果、已有因子相似性证据，交替批判和改写同一条因子，并留下可恢复、可核查的研究过程。

**Architecture：**沿用 Python、FastAPI、LangGraph、SQLite、Parquet/DuckDB 和 React。增加结构化评估反馈、研究参考集、相似性计算及实证轮次调度；固定程序负责计算与筛选，Agent 负责解释、批判及提出新定义。

**Tech Stack：**Python 3.12、现有 Pydantic 模型、Pandas/NumPy、现有后端与前端依赖。本次不新增向量数据库、不更换模型供应商、不引入新的 Agent 平台。

**Spec：**本文件第 1—8 节为此次修改的设计规格，第 9—12 节为执行与验收计划。用户已确认优先修改上轮清单中的第 1、2、4 项；原项目边界以对话中已确认的《A 股多智能体因子研究系统：第一版设计》为准。仓库根目录的 `需求与研究方案.md` 是较早讨论稿，其中“尚未确认”的描述不能覆盖后续用户决定。

**日期与状态：**2026-09-30，已实施并完成软件验收。下文设计正文保留批准时的措辞，“当前代码”指修改前基线；实际完成情况以 [实施验收记录](../../优先项124实施验收.md) 为准。文档中的数值示例不是真实回测结果。

## 1 修改目标与范围

本次的“1、2、4”指上一轮改进清单编号，不是人工审核环节编号。

| 改进项 | 当前缺口 | 本次交付 | 修改后的预期结果 |
| --- | --- | --- | --- |
| 第 1 项 回测后继续交叉辩论 | 前置辩论结束后，实证阶段主要由各角色修改自己的因子 | 在第 5 环节批准后，按因子谱系交替进行实证批判、改写和重测 | 可以追踪“谁根据哪组结果指出问题，下一版怎样处理，重测是否支持改动” |
| 第 2 项 完整评估反馈 | 已计算不少指标，但实证改写主要收到平均 IC 和错误信息；计算成功与研究资格未区分 | 固定反馈包、逐指标前后比较、预设候选资格检查 | Agent 能同时看到预测力、扣费收益、覆盖、稳定性和风险；不再把计算成功解释为因子合格 |
| 第 4 项 生成过程中的相似性提醒 | 主要与三个基础因子比较，未形成历史参考集及可行动的相似性反馈 | 冻结研究参考集、截面相关性比较、相似因子定义与解释、批判引用和回应记录 | 高度重复的改写会被明确指出，最终候选按固定规则去重 |

这三项作为一个功能闭环实施。仅增加提示词而不接入数据和评估，无法完成目标；一次全面重写现有系统也没有必要。本方案采用局部新增模块，并保留现有数据、模型、后台任务及页面框架。

本次不包含种子概率选择、分阶段探索策略、通用长期经验库、财务字段扩充、专用纠错 Agent、供应商购买或真实模型选型。为落实第 1 项，只增加与实证批判有关的角色要求，不顺带重做全部角色体系。

## 2 全局约束

- 股票范围保持沪深非金融 A 股；动量仍指财务基本面变化。行情用于收益、交易和估值计算，不新增股价动量研究。
- 主要预测周期为 60 个交易日，20、120 日作为辅助诊断；组合按月调仓。
- 开发区为 2014—2019 年，验证区为 2020—2022 年，最终测试区为 2023—2025 年；2026 年已有数据及前瞻观察单列。
- 研究资料的发表日期与实际版本日期不得晚于 2022-12-31。2026 年中金报告只作系统方法参考，不能自动进入历史研究资料库。
- 研究 Agent 只能获得开发、验证信息。最终、近期及前瞻结果，以及依赖这些结果的选择理由，都不得经提示词、相似性参考集或摘要回流。
- 你仍参与第 1、5、10 环节；第 5 环节批准范围内自动执行，不增加逐轮人工审批。
- 默认每位研究 Agent 提 3 个假设，前置辩论最多 2 轮，实证改写最多 2 轮，每批最多 20 个不同公式与方向的实验定义。
- 三个固定基准不占候选额度；同一公式的开发、验证计算不重复计为两个定义。数值计算失败的已登记定义仍计入已尝试数量。
- 模型费用沿用本批上限与调用前预留机制；不因新增辩论自动提高预算。网络结果不确定时不自动重发请求。
- 公式仍使用受限算子；模型不能改变评分规则、数据分区、股票池或执行任意系统命令。
- 所有原始提案、批判、反馈、改写、失败及淘汰记录均保留。允许最后没有合格候选。
- 文档批准后实施；已先在隔离目录和旧档案副本验收，未使用日常运行档案做迁移试验。

## 3 当前代码与目标结构

### 3.1 已核对的实现

| 位置 | 当前行为 | 修改重点 |
| --- | --- | --- |
| `research/workflow.py::debate` | 两方批判对方，再改写自己的提案，发生在第 5 环节之前 | 保留前置逻辑与轮次含义；新闭环发生在批准之后 |
| `research/workflow.py::experiments` | 基线评估、工程检查、开发与验证评估，然后各自实证改写 | 拆出有持久化步骤的实证循环与冻结筛选 |
| `research/workflow.py::experiments` 的结果摘要 | 提供假设 ID、分区、状态、60 日平均 IC 和错误 | 改为固定、版本化的完整反馈包 |
| `research/evaluation.py::Evaluator.run` | 已有成本、回撤、年度、行业、分组及基准诊断 | 复用计算，补充缺失口径和信号导出 |
| `research/evaluation.py::run_cached` | 按公式、方向、数据、配置和引擎复用计算 | 增加假设版本与复用结果的显式绑定 |
| `research/agents.py::run_agent` | 统一模型调用与档案，自动读取研究记忆 | 给新阶段传入明确白名单上下文，校验所引用的实验与指标 |
| `research/literature.py::research_memory` | 按日期过滤，返回近期实验摘要 | 新流程限制可见谱系、分区及参考集，防止绕过相似性隔离 |
| `research/workflow.py::admission` | 第 10 环节人工决定，整批写入 `factors` | 保留人工档案功能，但不把该表直接作为研究参考集 |
| `research/api.py` 与 `frontend/src/main.tsx` | 展示任务、版本和实验，支持导出 | 增加轮次、反馈、相似性、筛选理由及父子版本比较 |

### 3.2 文件责任划分

以下路径均相对于项目根目录 `D:/Desktop/金融分析系统`。

| 文件 | 操作 | 责任 |
| --- | --- | --- |
| `research/research_types.py` | 新建 | 研究策略、反馈、相似性、谱系与筛选结果的数据模型；只依赖标准库和 Pydantic |
| `research/feedback.py` | 新建 | 从已归档开发与验证结果构建反馈，计算父子差异，运行固定资格检查 |
| `research/similarity.py` | 新建 | 参考集快照、因子信号缓存、配对相关性及相似性警告 |
| `research/research_loop.py` | 新建 | 一轮一轮推进实证研究、登记步骤、检查预算、关联批判与改写 |
| `research/selection.py` | 新建 | 全部已评估版本的资格汇总、确定性去重、冻结清单 |
| `research/schemas.py` | 修改 | 配置新研究策略；增加实证改写输出及批判中的指标引用 |
| `research/db.py` | 修改 | 追加表结构与版本迁移，不删除旧表和档案 |
| `research/evaluation.py` | 修改 | 月度信号缓存入口、覆盖口径、ICIR、双倍成本基准及实验绑定 |
| `research/agents.py`、`research/models.py`、`research/literature.py` | 修改 | 新动作、结构化输出、提示版本及可见上下文；保留现有费用预留 |
| `research/workflow.py`、`research/worker.py` | 修改 | 接入新循环、冻结节点、版本兼容和恢复边界 |
| `research/api.py` | 修改 | 返回研究过程摘要，扩展导出，保留原有接口 |
| `frontend/src/components/ResearchProgress.tsx` | 新建 | 展示轮次、角色、谱系、停止原因 |
| `frontend/src/components/ResearchFeedback.tsx` | 新建 | 展示完整指标、前后变化、相似因子和筛选理由 |
| `frontend/src/main.tsx`、`frontend/src/styles.css` | 修改 | 在原六个页面内接入新组件，不新增顶层页面 |
| `tests/test_research_policy.py`、`tests/test_research_feedback.py` | 新建 | 配置、迁移、指标和资格规则测试 |
| `tests/test_factor_similarity.py`、`tests/test_empirical_debate.py` | 新建 | 参考集隔离、相关性、交替改写、恢复和额度测试 |
| `tests/test_research_review_api.py` | 新建 | 接口、导出与可读筛选理由测试 |
| `tests/conftest.py`、现有相关测试 | 修改 | 增加独立夹具，保持原有时点与恢复测试 |
| `README.md`、`docs/验收记录.md` | 修改 | 在实际实现后更新用法和新验收证据，不提前写“已通过” |

## 4 第 1 项如何修改实证辩论

### 4.1 新流程

```text
第 1 环节确认目标、研究规则和预算
  → 数据验收与研究参考集快照
  → 两位 Agent 独立提案
  → 原有前置交叉辩论
  → 第 5 环节批准候选、资格规则、相似性规则与修改范围
  → 第 0 轮：工程检查 + 开发评估 + 验证评估
  → 构建完整反馈与相似性警告
  → 第 1 轮：对方批判 + 接手改写 + 重测
  → 构建新反馈，并检查上一轮预期是否得到支持
  → 第 2 轮：角色交换，再批判 + 改写 + 重测
  → 汇总全部版本，资格检查与去重，冻结候选
  → 统一最终测试，结果仅供人工查看
  → 第 10 环节决定观察、保留或淘汰
```

“第 0 轮”仅是批准候选的首次数值评估，不消耗一次改写机会。`revision_rounds=2` 对应最多产生两代改写版本；`debate_rounds` 仍控制批准之前的前置辩论，不与实证轮次重复相乘。

### 4.2 同一条因子的接手规则

一个在第 5 环节批准的候选对应一条 `lineage_id`。该 ID 固定，后续版本只追加，不覆盖父版本。

例如价值 Agent 的候选 V0：

1. 程序评估 V0，归档结果、反馈包与相似性警告。
2. 成长 Agent 阅读 V0 的定义、理由、结果及批判历史，先提出实证批判，再据此生成 V1。
3. 工程 Agent 检查 V1，程序重测。
4. 价值 Agent 阅读 V1 和相对于 V0 的变化，判断此前问题是否解决，再批判并形成 V2。
5. 程序重测 V2；到达轮次上限后进入汇总，不要求双方同意。

成长 Agent 起源的候选执行相反顺序。每轮由上一版作者的另一角色接手，保留 `origin_role` 与 `current_author` 两个字段。

同一轮内，先完成所有当前候选的评估，再冻结这一轮反馈，然后调用研究 Agent。不得把本轮刚完成的改写结果提前交给其他候选，防止执行顺序改变信息范围。

每位角色每轮最多使用一次批判调用和一次改写调用，按谱系批量处理。改写必须逐条带 `parent_id`，不能再按模型返回数组的位置推断父版本。

### 4.3 每次批判与改写必须包含什么

批判需要绑定：目标版本、问题类型、具体问题、实验及指标引用、可执行的修改或检验建议。问题类型限定为经济机制、样本覆盖、成本风险、稳定性、已有信号重复、实现异常。

实证改写输出 `EmpiricalRevision`：

| 字段 | 约束 |
| --- | --- |
| `parent_id` | 必须是本次调用允许处理的当前版本 |
| `action` | `revise`、`keep` 或 `drop` |
| `hypothesis` | `revise` 时提供完整 `Hypothesis`；其余为 `null` |
| `addressed_critique_ids` | 必须逐条覆盖本次针对该父版本的批判，回应可为接受或有理由地保留分歧 |
| `addressed_warning_ids` | 对每一条高相似性警告说明如何处理；引用由系统分配的 ID |
| `rationale` | 为什么修改、保留或停止；不得捏造指标 |
| `expected_effects` | 修改前写出的可检查预期，例如降低最大相关性、改善现金流覆盖 |
| `unresolved_issues` | 仍无法回答的问题；允许非空 |

`keep` 不产生新的数值实验，当前谱系停止继续改写；`drop` 停止该谱系并记录建议淘汰。两种动作都不删除已有版本。最终资格判定仍由固定程序和人工审核负责，Agent 的评价不能抹去一个已完成版本。

若模型输出缺少目标、重复目标或引用了别的谱系，记为结构校验失败，进入 `needs_attention`；不静默丢弃、不自动补编回应。本次不实现额外付费纠错循环。

### 4.4 停止与额度规则

| 条件 | 必须发生的行为 |
| --- | --- |
| 达到两轮实证改写 | 汇总已完成版本并进入冻结筛选 |
| 达到 20 个已登记不同定义 | 停止登记新定义，保留已生成但未执行的版本及原因；已登记版本可完成本轮计算 |
| 所有谱系选择保留、停止或失败 | 不再调用研究 Agent，汇总已经完成的实验 |
| 公式规范化后与旧定义一致 | 绑定原实验，登记“复用”，停止该谱系的重复改写 |
| 数值实验失败 | 保存错误；剩余改写轮次内可把错误作为证据交给接手者，不当作收益为零 |
| 模型预算不足 | 保留既有结果并以 `budget_exhausted` 结束，不自动调用最终测试；需要继续时另建批次 |
| 模型网络请求结果不确定 | 沿用现有停止和费用预留规则，不自动重复调用 |
| 用户暂停或进程重启 | 在已落盘步骤之后恢复，不能重新扣费或重复登记实验 |
| 研究目标、时间区间或评价规则需变化 | 当前批次不能自行修改；结束或退回适用决策节点，由新定义承接 |

默认六条谱系全部完成两次改写且定义均不重复时，共评估 18 个候选定义，另有三个固定基准。未发生人工退回时，模型调用上界示例为 36 次：初始提案 2 次、前置辩论 8 次、实证批判与改写 8 次、工程检查 18 次。实际次数受保留、淘汰、复用和预算停止影响；人民币费用由所选模型与实际输入长度决定。

## 5 第 2 项如何修改评估反馈与候选资格

### 5.1 固定反馈包

`ResearchFeedback` 由程序从已归档结果构建，禁止让模型生成其中的数值。每包包含：`feedback_version`、任务与假设 ID、谱系、轮次、父版本、公式与方向、数据版本、引擎签名、策略哈希、实验绑定、开发与验证分区、父子变化、相似性结果及资格原因。

`development` 与 `validation` 分开保存，不合并成一个“综合成绩”。失败或缺失的指标使用 `null` 并提供原因，不能替换为 0。

| 反馈类别 | 具体字段及处理 | Agent 能据此判断什么 |
| --- | --- | --- |
| 预测能力 | 20/60/120 日 Rank IC、有效月份、正 IC 月份比例、HAC 统计量与已调整 p 值；60 日为主 | 是否有排序信息，样本是否足够 |
| IC 稳定程度 | 增加非年化 ICIR，等于月度 IC 均值除以样本标准差，标准差 `ddof=1` | 平均效果是否依赖剧烈波动 |
| 数据覆盖 | 因子值覆盖、收益标签覆盖、缺失标签数、有效信号月数与计划月数 | 结果是否仅代表很小或不完整的样本 |
| 组合结果 | 扣费年化收益、基准年化收益、两者之差、最大回撤、换手、成本金额、受限订单次数 | 可执行结果与风险是什么 |
| 稳健性 | 双倍成本组合与双倍成本基准、季度调仓结果、逐年组合与基准收益 | 是否过度依赖成本或某段行情 |
| 行业与已有因子 | 行业 IC 及月份、三个基准的相关性、控制基础暴露后的 IC | 是否主要来自已知暴露或少数行业 |
| 相似性 | 第 6 节定义的相似因子、相关程度、样本充分性与警告 | 是否只是重复已有信号 |
| 父子比较 | 对同一分区逐项计算子版本减父版本的变化；同时显示原值、新值与单位 | 修改改善了什么，又牺牲了什么 |

非年化 ICIR 在样本少于两个月或标准差为零时为 `null`。该统计量不能替代 HAC 统计量，也不能把重叠的 60 日标签当成互相独立的月度收益。

`annual_excess` 延续现有定义：组合扣费年化收益减基准扣费年化收益，单位为年化百分点；不冒称相对净值的复合年化收益。新增 `double_cost_annual_excess` 使用双倍成本组合减双倍成本基准，避免只增加一边成本造成口径不一致。

年度超额采用每年组合收益减同年基准收益，记录正超额年份数与有数据年份数。行业 IC 继续作为诊断，不临时按表现好的行业缩小股票池。

### 5.2 修正覆盖率的分母

当前 `signals()` 只返回达到最低截面数量的月份。如果只在这些月份上计算覆盖率，会漏掉整月无法计算因子的情况。

新增月度信号面板应保留区间内全部计划信号月：

- `factor_coverage = 有限因子值的股票月数量 / 全部计划信号月中符合股票池要求的股票月数量`。
- 无足够因子值的月份照常进入覆盖分母，并记为无有效信号月。
- 每个收益周期继续记录已到达标签终点的有效信号股票中，收益可观测的比例；超出分区结束日期的未来标签不计入可观察样本。
- `signal_month_ratio = 达到最低截面数量的月份 / 全部计划信号月数量`，与因子值覆盖率分别显示。

保留既有返回键 `coverage`，使其指向修正后的因子覆盖率，并增加口径版本。不得混用新旧引擎结果进行父子变化计算。

### 5.3 候选资格的默认建议

以下数值是本次设计的保守起始规则，尚未证明能筛出有效因子，也不是照搬文章的效果结论。写入 `ResearchPolicy`，在第 1 环节可查看和设置，第 5 环节展示并绑定到批准记录；批次开始后不可由 Agent 调整。

| 配置字段 | 建议默认值 | 判定方式 |
| --- | --- | --- |
| `min_ic_months` | 24 | 开发、验证各自的 60 日有效 IC 月份至少达到此数 |
| `min_factor_coverage` | 0.70 | 两个分区各自不低于该值 |
| `min_label_coverage` | 0.90 | 两个分区 60 日收益标签覆盖各自不低于该值 |
| `min_rank_ic` | 0.00 | 两个分区 60 日平均 Rank IC 都严格大于该值 |
| `min_annual_excess` | 0.00 | 两个分区扣费年化收益差都严格大于该值 |
| `similarity.similarity_threshold` | 0.75 | 达到或超过该值视为高相似；计算口径见第 6 节 |

公式检查、开发和验证计算均成功，以及上述必要指标均为有限数值，是资格检查前提。所有门槛必须同时满足。

回撤、双倍成本、年度与行业差异、ICIR 和调整 p 值本次作为完整反馈与风险提示，不新增未经讨论的硬门槛。通过资格检查也不等于统计显著或已经发现可投资 Alpha。

每项规则返回实际值、阈值、比较方向和通过与否。例如“验证区 60 日 IC 为 -0.01，要求大于 0”，不能只返回“未通过”。

### 5.4 计算状态与研究结论分开

- `execution_status` 为 `complete`、`failed` 或 `not_evaluated`，只表示该定义是否完成计算。原 `experiments.status` 仍保留现有取值；尚未执行的定义由汇总记录表达。
- `quality_status` 为 `passed`、`failed` 或 `insufficient`，表示数据和效果是否达到上述预设要求。
- `selection_status` 为 `pending`、`selected`、`quality_rejected`、`similarity_rejected`、`insufficient` 或 `not_evaluated`，表示是否进入最终冻结集合。质量合格但尚未完成去重时为 `pending`，不能提前称为已入选。
- `factors.status` 仍是第 10 环节的人工决定，不能替代前面三个状态。

筛选覆盖本批全部已评估的独立定义，包括中途被替代的父版本。Agent 提出 `drop` 不会让父版本从档案或统计中消失。进入最终测试的仅为程序按第 7 节选出的集合；报告同时列出所有未入选版本和原因。

## 6 第 4 项如何修改相似性比较

### 6.1 建立与人工因子库分开的研究参考集

当前 `factors` 表里的人工决定可能参考最终测试。因此不能简单查询 `status='retained'` 再把结果送回研究 Agent。

新增 `research_reference_factors`，只在某批候选尚未调用最终测试时发布。发布对象是该批通过开发与验证资格检查的全部独立定义；不根据最终成绩或后来的人工决定增删成员。保存公式、方向、机制、失效条件、来源版本、开发/验证实验 ID、数据与引擎版本、策略、发布顺序及不可变内容哈希。

新任务在提案前创建 `reference_snapshot`，普通任务成员截止时刻使用任务创建时间；三组对照任务使用创建该组之前记录的同一截止时刻。快照一经生成，本批不随别的任务完成而更新。

准入参考项须同时满足：

1. 发布于本任务参考截止时刻之前，且发布时源批次尚未进入最终测试。源批次后来进入最终测试，不会改写已经签名的发布记录。
2. 来源具有可核验的新版研究上下文记录，资料与结果只使用开发/验证边界内的信息。
3. `demo/live` 模式一致，数据版本、股票池、分区和计算口径与本批兼容；第一版要求精确兼容，不自动重算陌生数据版本。
4. 不读取人工保留状态、审核理由、最终结果、近期结果或前瞻结果来决定参考成员。

旧版因子默认不自动导入，原因是没有完整的新式来源证明；三个固定基准照常可用。空参考集不是错误，应明确显示“暂无兼容历史参考项”。

相似性比较对象还包括三个基准与本批此前完整轮次中的可计算定义。本批父辈单独标为 `ancestor`，用于判断改写幅度，不因与父版本相似就立即拒绝子版本。最终冻结时，再对本批所有候选统一去重。

### 6.2 计算口径必须固定

比较的是同一历史月末、同一股票上的因子排序，不是把不同时点混成一列，也不是比较收益曲线的相关性。

对开发与验证分别计算：

1. 使用同一数据版本、当时股票池、月末信号日及既有安全公式求值器，生成带方向的因子值。
2. 每月按 `symbol` 对齐，取双方有限值的交集。至少有 `max(20, min_cross_section)` 只共同股票。
3. 共同股票数除以两边有限值股票并集数，得到 `overlap_ratio`；默认不低于 0.50。
4. 对该月共同股票计算 Spearman 相关系数 `rho_m`；常数因子或方差不足的月份记为不可计算。
5. 至少 12 个有效月份，且有效月份占该分区计划信号月数至少 50%，才报告可用于筛选的结论。
6. 主相似度为 `mean(abs(rho_m))`，同时报告 `mean(rho_m)`、有效月份和重叠情况。按月取绝对值后再平均，避免正负抵消。
7. 任一分区的主相似度达到 0.75 即产生高相似警告；两个分区不能先平均后消除警告。

这些口径是本项目的明确设计选择。文章未充分披露其相关性细节，不能把本实现宣称为对文章数值的精确复现。

`SimilarityPolicy` 额外配置为：`min_pair_symbols=20`、`min_overlap_ratio=0.50`、`min_valid_months=12`、`min_month_ratio=0.50`、`top_k=3`。参数随研究策略在实验前固定。

### 6.3 缺失、相反方向与祖先版本

- `F` 与 `-F` 的主相似度应为 1，不能通过简单改符号逃避重复检查；符号差异保留在有符号均值里。
- 样本不足、恒定值、字段缺失或缓存损坏不能返回“相关性 0”，应返回 `insufficient` 或明确错误。
- 每个必需参考对照都记录状态。外部参考、基准或最终去重比较存在不可判断项时，候选不得被自动标为“已确认具有差异”；此次保守地归为 `insufficient`，不进入最终测试。
- 与祖先的相似度作为解释证据，不单独触发对历史参考库的拒绝；若祖先本身也与一个外部参考重复，仍按外部参考结果处理。
- 公式完全相同且方向相同的定义直接标记重复并复用数值结果；不要增加一次模型“重新发现”实验。

### 6.4 发送给 Agent 的相似性证据

每个反馈包保留全部配对统计，提示词只携带按最大主相似度排序的前三个参考定义，以及警告总数；相似度相同时按参考 ID 排序。祖先比较单列，不挤占三个外部近邻位置。被截断的近邻清单仍可在档案与网页查看。

每条警告包含：稳定的 `warning_id`、目标版本、参考 ID、参考类型、两个分区的相关性与样本数、规范公式、可查看的受限计算代码、经济机制、允许使用的开发/验证指标及来源哈希。

程序只认定数值上的相似，经济机制是否重复由研究 Agent 解释。提示要求：说明重叠发生在哪里、准备改动哪个机制或保留相似的理由、希望在哪个指标上产生增益；不能仅改名、换符号或添加无解释参数。

高相似警告在改写阶段用于提醒，不直接终止该谱系。是否最终排除，由冻结时的固定筛选规则决定。

## 7 冻结筛选与人工审核

### 7.1 固定筛选顺序

1. 汇总所有已登记版本和实验绑定，列出工程失败、计算失败、未执行及复用记录。
2. 对完成开发与验证计算的独立定义，运行第 5 节资格规则。
3. 在最终测试之前发布通过质量门槛的研究参考项；该记录只包含开发/验证来源，不附带最终选择或人工入库结论。新发布项供未来任务使用，不并入本批已经固定的历史参考快照，避免与自己比较而被全部排除。
4. 对有资格的定义检查与历史参考集和三个基准的相似性。高相似者标为 `similarity_rejected`；信息不足者标为 `insufficient`。
5. 对剩余定义按“验证区 60 日平均 IC 降序、开发区 60 日平均 IC 降序、规范公式与方向哈希升序”排序。公式哈希保证相同数据下的确定性，不使用随机 UUID 排序。
6. 依次加入冻结集合；若与已选候选任一分区主相似度达到阈值，则记录被哪个已选定义覆盖并排除。必要配对统计不足时标为 `insufficient`。
7. 保存全部筛选决定、策略版本、参考快照哈希、代码签名和最终候选列表，再生成 `frozen_hash`。
8. 无候选时设置 `final_status='no_candidates'`，不打开最终测试数据库的样本区间；保存可读的结束报告并正常结束。

排序只用于选择低相关集合，不宣称最高 IC 的候选一定最好；成本、回撤与稳定性仍完整呈现。该规则在批次前固定，不在看到最终成绩后改变。

### 7.2 第 5 环节展示什么

- 本批批准的因子与主要争议。
- 初次评估及最多两轮接手改写的流程说明。
- 剩余模型预算、不同定义的额度、参考集数量及快照时间。
- 第 5.3 节门槛和第 6.2 节相似性口径，明确这些是筛选规则而非盈利保证。
- 不同 Agent、轮次和工程检查可能带来的调用数量，费用按实际模型报价预留。
- 批准记录绑定候选清单、策略哈希、参考快照哈希及任务版本。

### 7.3 第 10 环节和报告展示什么

展示每条谱系的公式演化、批判与回应、前后指标变化、相似因子及全部筛选理由。冻结入选、未入选、失败、未执行分别呈现；最终测试成绩与开发/验证反馈分区展示。

沿用本版“对冻结集合整批观察、保留或淘汰”的人工决定方式，不在本次扩大为逐个候选审批。未入选版本保留为研究档案，不因为整批“保留”进入人工因子库。没有候选的任务不显示可保留数量大于零。

## 8 数据结构与接口约定

### 8.1 研究数据模型

`research/research_types.py` 内的 Pydantic 模型禁止额外字段。`schemas.py` 可以导入该文件，该文件不得反向导入 `schemas.py`，避免循环依赖。

| 模型 | 必需内容 |
| --- | --- |
| `ResearchPolicy` | `version='research-policy-2.0'`，第 5.3 节质量字段，以及类型为 `SimilarityPolicy` 的 `similarity` 字段 |
| `SimilarityPolicy` | `similarity_threshold=0.75`，第 6.2 节配对样本要求和提示词近邻数 |
| `MetricReference` | `experiment_id`、`split`、`metric_path`；`split` 只能为 `development` 或 `validation` |
| `SplitFeedback` | 实验 ID、执行状态、时间区间、指标白名单、缺失原因与数据/引擎版本 |
| `ResearchFeedback` | 第 5.1 节元信息，两份 `SplitFeedback`、父子差异、相似性和质量检查 |
| `PairSimilarity` | 目标与参考定义 ID、关系类型、分区、统计状态、主相似度、有符号均值、月份与重叠统计 |
| `SimilarityReport` | 参考快照哈希、全部 `PairSimilarity`、警告列表、待判断原因 |
| `LineageState` | `lineage_id`、`origin_role`、`current_author`、`head_id`、当前轮次、活动状态、停止原因 |
| `RoundManifest` | 任务、轮次、固定输入版本、固定参考与反馈 ID、角色分组、输入哈希和阶段完成标记 |
| `CandidateDecision` | 定义 ID、关联假设 ID、计算状态、质量状态、筛选状态、逐项理由及覆盖它的候选 ID |
| `SelectionResult` | 全部 `CandidateDecision`、`selected_ids`、策略与参考快照哈希、停止原因 |

在 `schemas.py` 增加 `EmpiricalRevision`，其 `hypothesis` 复用现有 `Hypothesis`。`AgentOutput` 新增 `revisions`，最多 6 条；原有提案和批判结构保留。`Critique` 新增 `issue_type` 与 `metric_refs`；旧档案缺失新字段时允许读取默认值，新版实证阶段则做严格语义校验。

配置增加 `workflow_version=2` 和 `research_policy`。旧任务原始 JSON 中没有 `workflow_version` 时，必须显式识别为版本 1，不能因为 Pydantic 默认值而误当作新版任务。

### 8.2 新增持久化记录

数据库迁移版本由 1 升到 2。迁移幂等、追加式执行，保留原表和已有数据。

| 记录 | 关键字段与唯一约束 | 用途 |
| --- | --- | --- |
| `evaluation_bindings` 表 | `task_id,hypothesis_id,split` 唯一；保存实际 `experiment_id` | 多个假设版本复用同一数值实验时仍能正确取得反馈 |
| `research_steps` 表 | `task_id,step_key` 唯一；输入哈希、状态、输出引用、错误、时间 | 恢复到具体轮次动作，而非重新执行整个大循环 |
| `research_reference_factors` 表 | `reference_id` 主键；源任务、定义哈希、开发/验证实验、来源快照与发布时间；源任务与定义哈希唯一 | 在最终测试前建立研究参考集 |
| `artifacts` 新种类 | `approved_batch`、`reference_snapshot`、`lineage`、`round_manifest`、`research_feedback`、`similarity_report`、`candidate_selection`、`research_source` | 追加保存批准范围、过程和冻结依据 |
| Parquet 信号缓存与清单 | 数据版本、信号口径、规范公式、方向、分区、区间、股票池和代码签名共同构成键 | 复用相似性所需信号；以哈希检查内容，临时文件写完后原子替换 |

缓存只在本机使用，不把股票级全量数值发给模型。对缺失或损坏缓存可从固定数据重新计算；不能悄悄换数据版本。最终/近期/前瞻信号不进入研究缓存目录或研究读取函数。

`step_key` 规则固定为 `v2:{stage}:r{round}:{role_or_lineage}:{scope_hash}`，其中 `scope_hash` 来自排序后的输入版本清单。恢复时发现同一步骤输入哈希变化，停止并说明冲突，不能复用旧回答。

### 8.3 关键函数接口

以下是后续实施必须保持一致的接口约定；内部辅助函数可按代码需要补充。

```python
# research/feedback.py
def build_feedback(task_id: str, hypothesis_id: str, round_no: int,
                   similarity: SimilarityReport) -> ResearchFeedback: ...
def compare_feedback(parent: ResearchFeedback,
                     child: ResearchFeedback) -> dict: ...
def assess_quality(feedback: ResearchFeedback,
                   policy: ResearchPolicy) -> CandidateDecision: ...

# research/similarity.py
def freeze_reference_snapshot(task_id: str) -> str: ...  # 返回 artifact ID
def load_research_signals(task_id: str, hypothesis: dict,
                          split: str) -> pd.DataFrame: ...
def compare_signal_pair(left: pd.DataFrame, right: pd.DataFrame,
                        policy: SimilarityPolicy,
                        min_cross_section: int) -> dict: ...
def assess_similarity(task_id: str, hypothesis_id: str, round_no: int,
                      snapshot_id: str) -> SimilarityReport: ...
def publish_research_references(task_id: str,
                                qualified_ids: list[str]) -> list[str]: ...

# research/research_loop.py
def evaluate_round(task_id: str, round_no: int,
                    lineage_ids: list[str]) -> str: ...  # 返回 RoundManifest ID
def revise_round(task_id: str, manifest_id: str) -> list[str]: ...
def run_empirical_loop(task_id: str,
                       approved_ids: list[str]) -> list[str]: ...
def research_summary(task_id: str) -> dict: ...

# research/selection.py
def select_candidates(task_id: str,
                       hypothesis_ids: list[str]) -> SelectionResult: ...

# research/db.py
def bind_evaluation(task_id: str, hypothesis_id: str,
                    split: str, experiment_id: str) -> None: ...
```

`load_research_signals()` 只接受开发/验证分区，返回长表：`signal_date,symbol,value,eligible`，其中缺失值保留。该函数内部创建具有分区读取上界的数据访问器；不接受调用方传入任意 SQL、路径或最终区间。

`build_feedback()` 通过 `evaluation_bindings` 查实际实验 ID，验证任务、分区、区间、数据和引擎一致性。`compare_feedback()` 在口径不一致时返回不可比较原因，不给出差值。缺失父版本首次反馈返回空差异并标记 `initial_version`。

`revise_round()` 返回这一轮仍需评估的新版本 ID；谱系关联从已保存记录读取。`run_empirical_loop()` 返回本批所有已登记版本 ID，不只返回最后一代，便于完整筛选与导出。

### 8.4 对外接口和权限

新增 `GET /api/tasks/{task_id}/research-summary`，返回流程版本、轮次、谱系、调用与定义计数、参考集概况、反馈、筛选决定、停止原因。该接口只含开发/验证信息；前端继续从原任务详情接口获取人工可见的最终成绩。

原 `GET /api/tasks/{task_id}` 和导出接口保留。JSON 导出追加新档案及绑定；Markdown 导出增加每条谱系的批判、改写预期、实际变化和未入选理由。禁止将包含 `final_results` 的整个任务详情对象直接传入任何 Agent。

`research_memory()` 对版本 2 执行显式可见范围过滤：独立提案阶段不读取同批另一角色输出；实证阶段只提供调度器授权的谱系与已冻结公共参考。版本 2 停用版本 1 的“直接读取最近实验”路径；历史实验只能通过参考快照授权，当批实验只能通过当前阶段允许的假设和绑定读取，避免旧检索入口绕过隔离。

三组对照任务的 `comparison` 档案保存统一的 `reference_cutoff`，由创建接口写入，不能由模型设置；`freeze_reference_snapshot()` 对这三组采用该时间，其余任务采用创建时间。`independent` 与 `single` 在研究中不获得同批其他角色的候选或相似性结果，批次结束后的程序去重仍可统一比较。所有提示词使用专门的白名单投影；全量 `SimilarityReport` 留在档案，仅将第 6.4 节规定的近邻、警告总数及祖先对照放入模型输入。

## 9 分步实施计划

执行顺序为规则与记录 → 完整反馈 → 相似性 → 实证交替 → 冻结与界面。这样每一步都有可独立验证的输入和输出。

下列测试命令以项目根目录为工作目录。执行任何产品代码修改前，先完成任务 9.1 的现有测试基线记录，再依次执行任务 1—8，最后完成任务 9 的其他验收。新测试先运行并确认因缺少目标行为而失败，再实现，再运行通过；不能把安装或导入环境错误当成有效的失败测试。当前仓库没有 Git 元数据，本计划不包含强行初始化仓库或提交现有全部文件的操作。

### 任务 1 固定配置与安全迁移

**文件：**新增 `research/research_types.py`、`tests/test_research_policy.py`；修改 `research/schemas.py`、`research/db.py`、`research/api.py`、`research/workflow.py`。

**接口：**产出 `ResearchPolicy`、`SimilarityPolicy`、版本识别、新表及 `bind_evaluation()`，供后续任务使用。

- [x] 1.1 编写 `test_policy_defaults_are_explicit`：断言门槛为 24、0.70、0.90、0、0、0.75；相似性样本要求与第 6.2 节一致；非法比例和负样本数被拒绝。
- [x] 1.2 编写 `test_schema_upgrade_preserves_old_archives`：创建版本 1 的临时数据库，连续迁移两次，旧任务/实验/决定数量与内容不变，新表存在且结构版本为 2。
- [x] 1.3 编写 `test_legacy_config_is_not_silently_upgraded`：缺少流程版本的旧任务被标为 1；新建任务存储 2；旧检查点不能误用新轮次语义。
- [x] 1.4 运行 `.venv\Scripts\python.exe -m pytest tests/test_research_policy.py -q`，确认上述新增行为尚未实现。
- [x] 1.5 实现第 8 节配置和迁移；修改任务创建及读取路径，在开始研究后拒绝覆盖策略；保留现有第 1 环节可修改机制。
- [x] 1.6 再运行同一命令，要求全部通过；记录数据库前后计数与迁移结果。

**预期结果：**新旧档案可以区分，所有新规则有确定值，迁移不会重置历史数据。

### 任务 2 形成可信的完整评估反馈

**文件：**新增 `research/feedback.py`、`tests/test_research_feedback.py`；修改 `research/evaluation.py`、`research/db.py`、`tests/conftest.py`。

**接口：**产出 `build_feedback()`、`compare_feedback()`、`assess_quality()`；实验统一通过 `bind_evaluation()` 关联假设。

- [x] 2.1 编写 `test_feedback_contains_both_splits_and_all_required_metrics`，断言第 5.1 节所有类别存在且开发/验证分离；费用与相似性缺失时有原因。
- [x] 2.2 编写 `test_factor_coverage_includes_empty_months`：两个月各 20 只合格股票，首月有 20 个有效值、第二月全缺失，覆盖率必须为 0.50，不能为 1。
- [x] 2.3 编写 `test_icir_and_excess_have_fixed_units`：IC 序列 `[0.01,0.02,0.03]` 的非年化 ICIR 为 2；组合年化 0.08、基准 0.05 时年化差为 0.03；双倍成本两边都使用对应成本结果。
- [x] 2.4 编写 `test_cached_result_is_bound_to_every_version`：两个版本引用同一公式，数值计算只执行一次，两个版本都取得同一实际实验 ID；失败结果也可绑定。
- [x] 2.5 编写 `test_quality_rules_distinguish_failed_and_insufficient`：负 IC 判失败，少于 24 个月或必要字段缺失判证据不足，计算完成不得自动判合格。
- [x] 2.6 运行 `.venv\Scripts\python.exe -m pytest tests/test_research_feedback.py -q` 确认失败，再实现反馈白名单、分母修正、ICIR、双倍成本基准、逐年差异及父子比较。
- [x] 2.7 再运行该文件及 `tests/test_execution.py`，要求通过；检查父子不同引擎签名返回不可比较，而不是生成误导差值。

**预期结果：**同一组归档结果能稳定产生完整、口径明确的反馈；失效和缺失不会被美化成零值。

### 任务 3 建立无最终结果污染的参考快照

**文件：**新增 `research/similarity.py` 的参考集部分、`tests/test_factor_similarity.py`；修改 `research/db.py`、`research/literature.py`、`research/api.py`。

**接口：**产出 `freeze_reference_snapshot()`、`publish_research_references()` 和明确的参考成员清单。

- [x] 3.1 编写 `test_reference_snapshot_is_prefinal_and_immutable`：快照只含创建时刻以前发布的兼容参考项；后续插入或改变人工保留决定不改变该快照。
- [x] 3.2 编写 `test_final_based_membership_cannot_enter_reference_pool`：把“最终收益 99%”和“因最终表现好而保留”的诱导记录放入人工档案，参考集、研究记忆和反馈都不得出现这些内容，也不能据此选成员。
- [x] 3.3 编写 `test_reference_compatibility_and_control_groups`：演示与真实隔离；数据、股票池或分区不兼容时排除并解释；三个对照任务的外部参考快照成员一致。
- [x] 3.4 运行 `.venv\Scripts\python.exe -m pytest tests/test_factor_similarity.py -k reference -q` 确认失败。
- [x] 3.5 实现来源校验、追加发布、快照内容哈希和旧因子排除规则；把公共参考元信息接入第 5 环节摘要。
- [x] 3.6 再运行测试并在测试中拦截 `db.connect(final=True)`：参考构建与研究检索若尝试访问最终库即失败。

**预期结果：**新任务使用一个可复查且固定的研究参考集，不会通过“因子是否被保留”间接获知最终测试结论。

### 任务 4 计算相似性并生成可行动警告

**文件：**完善 `research/similarity.py`、`tests/test_factor_similarity.py`；修改 `research/evaluation.py`。

**接口：**产出 `load_research_signals()`、`compare_signal_pair()`、`assess_similarity()`，由反馈与最终去重共用。

- [x] 4.1 编写 `test_pair_similarity_matches_symbols_not_row_order`：打乱行序仍得到相同结果；相同排序得 1，反向排序主相似度仍为 1，有符号均值为 -1。
- [x] 4.2 编写 `test_insufficient_overlap_is_not_zero_correlation`：少于 20 只共同股票、少于 12 个月、重叠比例不足或常数列返回 `insufficient`，数值为 `null`。
- [x] 4.3 编写 `test_warning_threshold_and_top_k_are_deterministic`：0.75 触发警告，0.749 不触发；按任一分区最大相似度排序，近邻相同分数按稳定参考 ID；父版本单列。
- [x] 4.4 编写 `test_signal_cache_reuses_and_rejects_contamination`：同数据口径复用，公式方向或代码签名变化不复用，最终分区被拒绝，缓存损坏在固定数据上重算。
- [x] 4.5 运行 `.venv\Scripts\python.exe -m pytest tests/test_factor_similarity.py -q` 确认新增行为失败，然后实现分区月度配对、统计、信号缓存及警告负载。
- [x] 4.6 再运行全部相似性测试，检查警告确实附带原公式、机制、可查看代码和来源，而不只是一个相关系数。

**预期结果：**重复因子、反号因子和样本不足均能被正确区分；Agent 得到可据此修改的具体参考。

### 任务 5 接入交替实证批判与改写

**文件：**新增 `research/research_loop.py`、`tests/test_empirical_debate.py`；修改 `research/agents.py`、`research/models.py`、`research/schemas.py`、`research/workflow.py`。

**接口：**产出 `evaluate_round()`、`revise_round()`、`run_empirical_loop()`；调用前四个任务的类型与数据接口，不另算一套指标。

- [x] 5.1 编写 `test_roles_alternate_on_the_same_lineage`：价值 V0 经成长生成 V1，再经价值生成 V2；成长起源相反；每个子版本父 ID 正确。
- [x] 5.2 编写 `test_round_uses_fixed_evidence_before_any_revision`：同轮两个角色使用已冻结的旧版本结果，不能看到本轮先完成的改写结果；改写前确实发生了数值评估。
- [x] 5.3 编写 `test_reordered_model_output_keeps_correct_parents`：模型倒序输出不改变父子归属，重复或越权父 ID 被拒绝。
- [x] 5.4 编写 `test_critique_and_revision_cite_real_evidence`：批判引用存在且属于允许分区的实验与字段；改写回应所有目标批判和高相似警告；伪造指标路径或最终实验 ID 被拒绝。
- [x] 5.5 编写 `test_keep_drop_and_unchanged_formula_stop_the_lineage`：相应动作不再追加数值定义，旧版本继续保留在总档案。
- [x] 5.6 编写 `test_independent_mode_does_not_receive_peer_lineages`：独立双 Agent 仅自行改写；单 Agent 仅一个作者；公共历史参考可共用，同批对方信息不可见。
- [x] 5.7 运行 `.venv\Scripts\python.exe -m pytest tests/test_empirical_debate.py -q` 确认失败，再实现动作、轮次屏障、显式父版本关联和新的提示词版本 `fundamental-research-2.0`。
- [x] 5.8 给演示模式增加明确标为脚本的批判与回应样例；重跑测试，要求演示无需 API 密钥且不会声称具备真实推理能力。

**预期结果：**“结果 → 交叉批判 → 改写 → 重测”成为实际执行流程，每次修改有可追踪的证据和预期。

### 任务 6 验证恢复与预算不会因多轮调用失控

**文件：**完善 `research/research_loop.py`、`research/db.py`；修改 `research/workflow.py`、`research/worker.py`；扩展 `tests/test_empirical_debate.py` 和现有恢复测试。

**接口：**完善步骤账本、定义登记、调用缓存与输入哈希检查；保持现有模型费用 API。

- [x] 6.1 编写 `test_resume_after_feedback_and_after_model_response`：分别在反馈落盘后、模型回答落盘后注入崩溃；恢复后的实验数、调用数、费用及子版本数不增加重复项。
- [x] 6.2 编写 `test_twenty_definition_limit_survives_restart`：额度来自持久化记录；模拟重启后第 21 个新定义仍被拒绝，旧定义复用不占新额度。
- [x] 6.3 编写 `test_same_step_different_input_is_rejected`：同一 `step_key` 收到不同输入哈希时停止，而非使用缓存回答。
- [x] 6.4 编写 `test_budget_exhaustion_does_not_open_final_test`：下一调用预算不足时保存结果、终止新研究调用，最终区间仍未打开。
- [x] 6.5 运行新增测试确认失败，再实现按步骤恢复、登记前额度检查和幂等事务。数值计算遇进程中断可重算未提交项；已提交项必须复用。
- [x] 6.6 运行 `.venv\Scripts\python.exe -m pytest tests/test_empirical_debate.py tests/test_models_and_workflow.py tests/test_recovery_and_observation.py -q`，要求新旧恢复与费用测试全部通过。

**预期结果：**关闭网页、暂停或重启不会重复执行已经完成的步骤，也不会重置研究额度。

### 任务 7 形成可解释的冻结集合

**文件：**新增 `research/selection.py`；修改 `research/workflow.py`；扩展 `tests/test_research_feedback.py`、`tests/test_factor_similarity.py`、`tests/test_empirical_debate.py`。

**接口：**产出 `select_candidates()`；结果接入 `frozen_batch` 和后续最终测试。

- [x] 7.1 编写 `test_completed_calculation_is_not_automatically_selected`：计算成功但验证 IC 为负的候选进入完整报告，不能进入最终测试。
- [x] 7.2 编写 `test_selection_is_deterministic_and_records_covering_factor`：对同一候选集合改变输入顺序，冻结清单不变；高相关候选记录具体覆盖它的参考或已选版本。
- [x] 7.3 编写 `test_all_versions_remain_visible_after_selection`：父版本、模型建议停止的版本、复用版本、失败版本都有档案与明确状态。
- [x] 7.4 编写 `test_no_qualified_candidate_finishes_without_opening_holdout`：无人通过时正常结束，最终库不新增窗口登记或测试结果。
- [x] 7.5 运行对应测试确认失败，按第 7.1 节实现资格检查、参考发布、去重排序和冻结；增加研究逻辑源文件快照及其签名，不只冻结评估引擎。
- [x] 7.6 重跑上述文件及 `tests/test_isolation_and_api.py`，要求通过；验证研究工具无法读取冻结后的最终结果。

**预期结果：**最终测试针对一个事先定义、可重现的候选集合，失败与未入选版本仍完整公开。

### 任务 8 页面和导出说明完整研究过程

**文件：**新增两个 React 组件和 `tests/test_research_review_api.py`；修改 `research/api.py`、`frontend/src/main.tsx`、`frontend/src/styles.css`。

**接口：**实现 `/api/tasks/{task_id}/research-summary`，复用 `research_summary()`；原接口保持可用。

- [x] 8.1 编写 `test_research_summary_preserves_metrics_and_reasons`：摘要包含轮次、谱系、反馈、近邻和未入选原因，不包含最终数值；旧任务返回明确的 `legacy` 标记。
- [x] 8.2 编写 `test_exports_include_failed_and_reused_versions`：Markdown 与 JSON 都保留失败、复用、批判和父子关系；仅 JSON 保留全量逐月数据也必须在 Markdown 中明确说明。
- [x] 8.3 运行 `.venv\Scripts\python.exe -m pytest tests/test_research_review_api.py -q` 确认失败，再实现接口和导出。
- [x] 8.4 实现辩论页时间线、结果页反馈与近邻卡、设置页固定规则说明，以及第 5/10 环节审核信息；“计算完成”“达到研究门槛”“人工保留”使用不同文案。
- [x] 8.5 再运行 API 测试；在 `frontend` 目录执行 `node ../.tools/npm/package/bin/npm-cli.js run build`，要求 TypeScript 与构建成功。
- [x] 8.6 在真实浏览器用隔离演示任务检查初始评估、两轮改写、缺失指标、高相似警告、无候选结束和导出；长公式与长中文理由不能撑破布局。

**预期结果：**用户不读代码，也能看懂改了什么、为什么改、有没有改善以及为什么某候选未入选。

### 任务 9 完成端到端验收和使用说明

**文件：**扩展现有集成测试；更新 `README.md`、`docs/验收记录.md`，新增实际验收档案到独立运行目录。

- [x] 9.1 正式实施开始时先运行现有 `.venv\Scripts\python.exe -m pytest -q`，记录当下基线；本轮文档没有复跑代码测试，不能把旧记录中的 47 项视为新修改已通过。
- [x] 9.2 用固定合成数据和模型桩运行六条谱系、两轮改写的闭环，逐一核对第 10 节验收矩阵；所有模拟数值必须注明来源。
- [x] 9.3 运行全量后端测试和前端构建；出现失败只针对原因修复，不通过删测试、降低隔离要求或放宽门槛来完成验收。
- [x] 9.4 在新的 `FACTOR_LAB_HOME` 中完成默认规则的浏览器流程，并在实证中途重启后台，核对恢复后的数量与费用。实际六谱系批次无人合格，按规格跳过环节 10；第 1→5→10→观察库正向分支另以明确数值桩集成测试验证，未放宽默认门槛。
- [x] 9.5 导出一份完整档案，对照反馈、模型实际输入、相似性警告、筛选清单及页面结果；确认相同冻结数据和口径重算一致。
- [x] 9.6 更新使用说明与真实验收结果，区分软件通过、模拟研究通过和仍待真实数据/云端模型验证的内容。

**预期结果：**交付可以运行、可以解释、可以恢复的新闭环；真实因子质量是否提高仍留给后续等预算真实研究验证。

## 10 验收矩阵与重点审查

| 编号 | 输入或场景 | 通过标准 | 负责步骤 |
| --- | --- | --- | --- |
| A01 | 一条价值起源谱系跑两轮 | 作者顺序为价值→成长→价值，父子 ID 正确，每代改写前有相应已归档结果 | 任务 5 |
| A02 | IC 较高但扣费超额为负 | 完整反馈同时出现两个事实；程序按预设资格规则拒绝，不仅按 IC 评价 | 任务 2、7 |
| A03 | 子版本 IC 降低、回撤和相似性改善 | 展示所有变化和原值；不只显示改善项，也不保证该版本一定被选中 | 任务 2、8 |
| A04 | 因子与历史参考高度相关 | 展示参考公式、机制、相关性和来源，下一次批判引用警告，改写说明处理方式 | 任务 3—5 |
| A05 | 同因子改名、改符号或倒序股票行 | 重复仍可识别；符号和行序不能绕过检查 | 任务 4 |
| A06 | 少量交集股票、恒定值、整月缺失 | 返回证据不足，覆盖分母不遗漏月份，不能显示相关性为 0 或满覆盖 | 任务 2、4 |
| A07 | 最终库里植入高收益与保留理由 | 全部研究上下文、参考成员及摘要均不受其影响 | 任务 3、5、7 |
| A08 | 不同模型输出顺序与相同公式多版本 | 父版本不串线，实验复用不丢假设记录 | 任务 2、5 |
| A09 | 崩溃、重复审批、暂停后恢复 | 已完成步骤不重复调用或计费，审批不会错误应用到下一环节 | 任务 6 |
| A10 | 20 个定义上限或预算不足 | 不新增超额定义或模型调用；原因和未执行项可见 | 任务 6 |
| A11 | 全部候选不合格 | 正常结束并解释，最终测试区保持未打开 | 任务 7 |
| A12 | 同数据、代码和策略重复筛选 | 数值、相似性和冻结定义集合一致；模型原始文本使用档案而非要求随机生成相同文本 | 任务 7、9 |
| A13 | 老任务和旧数据库 | 能读取导出，不被默认配置静默转为新流程，不擅自重新实验 | 任务 1、8 |
| A14 | 独立双 Agent 与单 Agent 对照 | 在研究期间不获得另一角色同批候选；使用同一外部参考与相同规则 | 任务 3、5 |

实施审查尤其关注五类容易遗漏的输入：引用缓存实验的多个版本、模型重排输出、低交集产生的虚高相关、最终选择造成的参考集污染、模型已计费但流程尚未落盘时的恢复。它们分别由任务 2、5、4、3、6 的明确测试覆盖。

## 11 升级兼容与失败处理

1. 新流程仅自动用于新建的版本 2 任务。旧版已完成任务保持原始报告与含义，不用新门槛补写其历史结论。
2. 已有未完成版本 1 任务不得直接从旧检查点跳入新循环。升级后将其标为 `needs_attention`，说明“旧流程检查点需使用对应旧版本继续，或从第 1 环节创建新版任务”；不删除检查点，不擅自重启实验或重开最终区间。
3. 实施部署前停止本项目的后台进程并备份完整运行目录；迁移先在副本验收。数据库事务失败应回滚并报告，不用清空数据库解决冲突。
4. 修正覆盖率或增加反馈字段后提升评估引擎版本；新研究逻辑另存签名。旧冻结档案仍保留原始源代码，新代码不能冒用旧签名。
5. 暂停在动作边界生效；一个正在进行的模型调用结束后先保存费用和响应，再暂停。原有不确定调用不自动重试。
6. 新增策略只允许在第 1 环节编辑；进入研究后的策略哈希变化会使继续运行被拒绝。新目标或新门槛通过新批次保留历史。

## 12 修改后的预期成果与边界

完成后应交付以下可见成果：

- 一条真正由回测证据驱动的双 Agent 实证辩论链，每次改写都能追溯到父版本、批判和具体指标。
- 一份完整、单位明确、区分开发与验证的固定反馈，包含失败、缺失和风险。
- 一个在最终测试前形成、可复核来源的研究参考集，以及可解释的相似因子警告。
- 一套预先固定的质量检查与去重规则，能够拒绝无效候选，并说明所有排除理由。
- 能在重启、重复点击和预算不足时正确停止或恢复的流程，以及用户可读的页面和导出档案。

这些成果验证的是研究过程的质量与可审计性。是否增加有效因子的发现率、是否提高扣费收益、辩论是否优于单 Agent，需要在真实数据与可比实际费用下另行验证；本次不承诺固定收益提升或必须产出有效因子。

方法来源为用户提供的《中金：Multi-Agent如何辩论出基本面Alpha？》PDF。本方案借鉴其回测反馈、交替改写和相似因子提醒；研究分区、人工节点、受限公式、参考来源隔离及上述具体统计口径以本项目约束为准。

原文位置：`D:/Desktop/中金：Multi-Agent如何辩论出基本面Alpha？.pdf`。当前代码依据：`research/workflow.py`、`research/evaluation.py`、`research/agents.py`、`research/literature.py`、`research/db.py`、`research/api.py` 与现有测试。后续执行如发现代码变化，应先记录与本计划的差异，再修改对应步骤，不能默默改变验收目标。

## 13 实施记录补充

2026-09-30 完成任务 1—9。最终 94 项测试及 TypeScript/Vite 构建通过；六谱系模拟流程得到 18 个候选定义、42 条实验、18 份反馈，零模型费用。复选框表示对应行为完成，部分测试使用等价分组或扩展断言，具体文件见验收记录。默认任务未通过候选门槛而跳过最终测试是预期结果；环节 10 正向分支使用数值桩验证，并未用伪造收益代替默认数值回测。

执行过程中修复了独立审查确认的五项问题，详见 [实施验收记录](../../优先项124实施验收.md)。原有前置辩论和月末判断保持原范围，真实数据、云端调用及研究增益仍待专门试评。
