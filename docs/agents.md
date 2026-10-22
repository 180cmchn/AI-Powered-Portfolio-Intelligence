# 运行时 Agent 与模型接入

本项目的产品运行时使用本地 Python 编排器，不依赖开发这个项目的 Codex 会话、开发助手的内部工具或任何隐含模型权限。未设置模型名称或密钥时显示配置缺失，不生成假研究结论。演示模式仅执行固定样例数据的数值计算，不调用或模拟 Agent。

## 实际执行路径

`app/agents.py::run_research(context, config, research_tools, emit)` 为一个账户输入版本建立主 Agent 会话。主 Agent 可以自行研究，也可以调用 `spawn_research`。每个子任务具有独立 `ModelSession`、工具循环、收件箱、超时和阶段报告；模型调用由本机通过 HTTP 发往配置的服务商。

主 Agent 的工具包括 `spawn_research`、`send_message`、`follow_up`、`wait_agents`、`agent_status`、`cancel_agent`，以及所有研究工具。`send_message` 在正在运行的子任务的下一次协议安全边界送达；对子任务发出 `follow_up` 会沿用原会话，并保留此前子报告。子任务通过 `report_progress` 提交可见发现、通过 `submit_report` 交付报告。普通文本回复也可以结束该轮。主 Agent 自主决定是否派发、派发几个、何时追问和结束，不执行固定角色委员会。

等待采用 `asyncio.Condition` 的事件通知；材料保存、阶段发现、完成和失败会唤醒主 Agent。没有定时轮询子 Agent 的后台循环。主 Agent 在下一轮模型请求前收到未送达的子任务事件。所有显式保存的材料都能到达主 Agent，缺元数据或分类不确定不会被拒收。工具运行明细仍记录到界面，但工具开始/结束本身不强制额外模型调用。

主 Agent 和子 Agent 均可使用 `search`、`read`、`save_material`、`list_materials`、`search_materials`、`get_material`、`read_material_range`、`recall_context`、`calculate`，以及 PDF 定位、历史行情、板块、基准和基本面工具。工具不提供账户修改、密钥读取、交易、文件系统或 shell 权限。网页和公告只作为数据进入工具结果。材料保留来源及访问范围；模型负责分类和采用，代码不做“可信度评分/来源白名单/双来源准入”。网络读取的私网地址阻止属于工具网络边界，与材料可信度无关。

提示词源文件是 `app/prompts/manager.txt` 和 `app/prompts/researcher.txt`。提示词要求区分事实、主体声明或预测、外部观点、传闻/冲突和自身推论；允许未知类别和空元数据。外部荐股观点可以用于研究线索，不能改写成已证实的客观依据。代码计算数值，模型解释及综合。公开材料不要包含账户私有信息；账户上下文与结论不放入公共材料缓存。

## 模型协议与能力检查

在后端环境配置 `MODEL_NAME`、`MODEL_BASE_URL`、`MODEL_PROTOCOL` 以及 `MODEL_API_KEY`。界面只保存密钥所在的环境变量名，例如 `MODEL_API_KEY`；密钥不会进入前端配置或任务日志。名称由用户选择，不预设任何当前开发模型的 API 可用性。

支持两种明确协议：

| 配置值 | 本机实际行为 |
| --- | --- |
| `responses` | 调用 `{base_url}/responses`，保存并重放完整 `output` 项，包括函数调用和不透明 reasoning 项；追加 `function_call_output`。使用 `store: false` 和 `include: ["reasoning.encrypted_content"]`。 |
| `chat_completions` | 调用 `{base_url}/chat/completions`，保留独立消息历史，追加带 `tool_call_id` 的 `tool` 消息。 |

`model_base_url` 指向 API 前缀，例如 `https://api.openai.com/v1`，不要填写 `/responses` 完整方法地址。远程服务要求 HTTPS，本机 localhost / 127.0.0.1 / ::1 可用 HTTP。仅当本机服务无需认证时设置 `model_allow_no_key=true`。某些 Chat Completions 模型要求 `max_completion_tokens`，此时设置 `chat_use_max_completion_tokens=true`；不自动猜测或静默改换协议。

界面的模型能力检查调用 `app/model.py::probe_model`：第一次让模型真实调用函数，本机返回一个此前未提供给模型的随机 token，第二次检查模型是否能正确续接返回值。检查会产生两轮模型请求和可能的服务费用；正常研究不会反复执行此检查。它只验证当前端点、模型的函数调用与状态续接，不代表研究质量或全部可选功能都已获验证。如果服务不支持上述字段或工具协议，错误直接展示，不降级为伪造的多 Agent。

Responses 的不透明 reasoning 状态仅在进程内用于协议续接，不作为可见思维链、日志或界面内容展示。Chat Completions 服务商若返回其特有续接字段，同样只保存在该会话的内存历史中。生产日志不保存模型 HTTP 请求体、响应体或认证头；HTTP 错误仅报告状态码。

本实现选择独立的本地编排层，而没有调用 Responses 的托管多 Agent 执行功能。派发、消息、等待、计算、抓取和取消在本机执行；模型提供工具调用意图与文本。托管 API 有不同权限和生命周期，不能仅依据“兼容 OpenAI”就当作已接入。

## 生命周期与成本边界

每个事件携带账户 ID、输入版本、任务 ID、Agent ID、上海时间戳和可见 payload。服务层在保存结果时再次核对最新账户版本与任务状态；旧任务结果无法覆盖新版本。取消 `run_research` 会传播到所有子任务。已保存公开材料可以复用，账户报告和输入依然隔离。失败不修改或回滚用户持仓。

子任务超时、失败或取消后会补齐尚未完成的工具调用结果，因而可以在合法的工具协议边界继续该子会话。主任务失败时保留已写入的阶段事件和材料；可以从界面重试。服务重启后由持久任务层恢复为新一次执行，复用已保存材料，而非声称恢复未落盘的模型隐藏状态。Mac 睡眠期间不执行请求。

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `max_concurrent_agents` | 3 | 同时运行的子任务工具循环；主 Agent 独立运行，可接收进展 |
| `max_total_agents` | 16 | 单轮可创建的子任务总数；可继续已有子任务 |
| `max_model_calls` | 40 | 单轮主/子 Agent 共享实际 HTTP 请求预算，包含重试 |
| `max_agent_turns` | 12 | 每次 Agent 执行的模型/工具循环上限 |
| `agent_timeout_seconds` | 120 | 子任务取得并发槽位后的本轮时限 |
| `analysis_timeout_seconds` | 600 | 整个账户分析时限，包含排队和所有子任务 |
| `model_request_timeout_seconds` | 90 | 单个模型 HTTP 请求超时 |
| `model_retries` | 1 | 网络错误、429 或暂时服务错误的有限重试，最多支持 3 次 |
| `max_output_tokens` | 4000 | 每轮模型输出上限 |
| `max_context_chars` | 240000 | 每个会话序列化历史预算；约 78% 时在完整工具轮次后归档可见历史并生成引用检查点 |
| `max_tool_result_chars` | 16000 | 单次结果进入模型的字符预算；超长内容可按归档记录分段取回 |

报告、材料与来源索引保留在持久层。材料索引默认每页 20 条，不携带长正文；`get_material` 和 `read_material_range` 返回可继续读取的范围。超长工具结果存入当前 Agent 的可见归档，模型收到片段、材料引用和 `record_id`；`recall_context` 可搜索摘要与来源，再按字符范围取回。会话接近预算时整组归档已完成的调用与结果，避免孤立的函数输出破坏协议；历史事实、计算、待查问题、材料 ID、URL、页码保留在可检索归档，检查点只陈述可见摘要，不生成替代事实。原始 opaque reasoning 不进入可见归档或事件。该归档仅存内存，重启后的任务依赖持久材料重新研究。仍过大的账户初始输入会明确报错，不能把未读片段假装读过。模型/工具错误返回给 Agent，局部来源打不开不自动让整个账户研究失败。全局预算或主任务时限到达则明确失败，不用固定文本冒充模型完成的投资分析。

## 验证与限制

执行 `./.venv/bin/pytest -q tests/test_agents.py tests/test_context_runtime.py tests/test_evaluation_inputs.py` 可重复验证协议和编排。测试使用明确命名的 scripted HTTP fixtures，仅存在于测试目录，不是生产模型实现。覆盖：

- Responses 不透明项重放及两轮真实协议形状、Chat Completions 工具消息续接。
- 两个独立子任务、不同任务上下文、阶段发现、混合事实/观点材料回传，以及对原子任务的一轮追问。
- 两个账户上下文隔离；模型请求和事件均绑定对应任务与版本。
- 预算耗尽、网络权限错误、子任务超时、父任务取消；不会凭空生成完成结果。
- 缺成本/现金、未知发布时间和原文失败能在管道中完整表达。

**测试不会证明模型总能正确分类或给出高质量投资判断。** 当前开发环境没有配置真实模型密钥，尚未执行真实模型多 Agent 研究。测试里分类与报告文本由 fixture 指定，验证的是材料不会丢失、混为同一事实或被工程准入规则屏蔽。不得把这个样例的通过称为真实模型质量验收。

配置真实服务后，先执行能力检查，再创建账户并执行“立即分析”。在任务详情确认多个独立子任务与工具结果，使用包含财报事实和荐股评论的公开材料作代表样例，检查主 Agent 是否引用事实并把评论标为观点；再检查缺失字段、冲突材料和条件性建议。子任务数量由主 Agent 自主决定，单次未派发子任务不表示编排不可用，可在账户研究目标中指定需要分别调查的两个实际问题。对模型输出质量进行多样例人工评估，并记录模型名称、服务商、日期、提示词版本和实际结果，不能将单次通过解释为永久保证。

另附可运行的真实模型评估脚本，读取 `.env` 的模型配置，执行实际 HTTP 请求；不会写入应用的研究历史：

```bash
# 仅测试当前配置的模型工具协议（会产生模型调用费用）
./.venv/bin/python scripts/evaluate_model.py --probe-only

# 只检查配置是否存在或列出输入，不输出密钥、不调用模型
./.venv/bin/python scripts/evaluate_model.py --check-config
./.venv/bin/python scripts/evaluate_model.py --list-cases

# 真实模型 + 十类明确合成输入；最多 40 次请求/账户案例，会产生 API 费用
./.venv/bin/python scripts/evaluate_model.py

# 单独运行有代表性的案例（可重复 --case）
./.venv/bin/python scripts/evaluate_model.py --case mixed_fact_opinion --case corrected_disclosure

# 真实模型 + 指定公开来源 + 真实行情适配器；不会包含合成材料
./.venv/bin/python scripts/evaluate_model.py --live-url 'https://公开来源的实际网址' --symbol SH:600000 --query '研究指定披露及当前组合风险'
```

缺少真实模型配置时退出码为 `78`，明确打印 `SKIPPED`，不生成“验收通过”报告。运行失败退出 `1`；模型未完成要求的两个独立子任务、追问或材料保存时退出 `2` 并保留实际记录供检查。结构性检查通过退出 `0`，**仍需人工审阅材料分类和结论质量**。结果保存在 `data/evaluations/eval-.../manifest.json` 及各案例/账户目录的 `evaluation.json` 和 `report.md`，包括模型、协议、服务商主机、输入与提示词 SHA-256、实际工具事件、请求用量、失败和输出；每完成一个案例写入 manifest，便于中断后保留结果。`human_review=pending`、`quality_passed=null` 不会因结构检查通过而改成研究质量通过。默认执行十类合成输入（同证券两账户场景运行两个独立账户，因此共 11 次研究）：混合事实与观点、业绩预告与正式报告、转载、公告更正、冲突数字、摘要与访问失败、不同账户敞口、60 页财报与 180 条历史资料、新信息推翻旧结论、信息不足。每类都有独立人工审阅 rubric。长财报案例必须实际请求第 28 页才通过对应结构检查；它使用合成文档工具，不替代真实 PDF 网络读取测试。`--live-url` 模式实际读原文，可以在搜索服务未配置时工作。没有任何模式用预设文本伪造模型响应。

## 实施时核对的官方资料

核对日期：2026-10-22。以下是协作与接口参考，不代表已获对应托管服务权限。

- [Function calling](https://developers.openai.com/api/docs/guides/function-calling)：函数调用与返回值续接。
- [Agents SDK: Orchestration and handoffs](https://developers.openai.com/api/docs/guides/agents/orchestration)：manager / agents-as-tools 协作思想。
- [Responses API: Multi-agent](https://developers.openai.com/api/docs/guides/responses-multi-agent)：托管多 Agent 与本地工具执行边界的参考。
- [Reasoning models](https://developers.openai.com/api/docs/guides/reasoning)：手动管理会话时保留 reasoning / function call / function output 项。

## 第二轮运行时补充

`finish_analysis.suggestions` 将建议和自然语言报告分开。建议包含证券、动作（watch / buy_candidate / add_candidate / hold / reduce_candidate / exit_candidate）、依据、材料 ID、可选有效期、明确价格条件和需要 Agent 判断的条件；服务端绑定账户及持仓版本并维护状态。没有可靠定量依据时保留自然语言条件，不强造目标价。`suggestions` 可省略；纯文字报告不会隐含撤销全部旧建议，新建议仅替换其对应证券。需要明确撤销时提交当前账户已有的 `revoked_suggestion_ids`，并说明依据；单证券研究仅复核目标证券，全局任务不能撤销账户建议。

`finish_analysis.research_records` 分别表示 market / sector / candidate 研究，保留入围原因、事实、关键变化、引用与账户适配。`scope=global` 且 `account=null` 的任务可输出公共研究；运行时拒绝它提交账户建议。账户研究仍保有独立上下文，持久层决定哪些结果可作为公共内容。

`model_usage` 事件及最终结果记录实际 HTTP 请求次数（含重试）、每次耗时、状态与安全错误类型。token 只记录服务商实际返回的数值计数，未提供时为 null；嵌套 cached/reasoning token 细节保留在每次请求记录，汇总只相加顶层计数。错误消息不含密钥、认证头或原始服务响应。`context_compacted` 记录整理次数和可检索记录数。

第二轮实测：模型名称、模型密钥和搜索配置均未设置；`evaluate_model.py --case mixed_fact_opinion` 实际退出 78（SKIPPED）。因此十类真实模型研究质量、真实搜索能力仍未验证。确定性上下文/协议/输入测试不代替上述验证。
