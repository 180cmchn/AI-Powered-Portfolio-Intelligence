> 第四轮：原生搜索与本地工具分开解析。两者真实混合续接证据见 `fourth-native-mixed.json`；完整验收见 [第四轮记录](iteration-4.md)。

# 运行时 Agent 与模型接入

本项目的产品运行时使用本地 Python 编排器，不依赖开发这个项目的 Codex 会话、开发助手的内部工具或任何隐含模型权限。未设置模型名称或密钥时显示配置缺失，不生成假研究结论。演示模式仅执行固定样例数据的数值计算，不调用或模拟 Agent。

## 实际执行路径

`app/agents.py::run_research(context, config, research_tools, emit)` 为一个账户输入版本建立主 Agent 会话。主 Agent 可以自行研究，也可以调用 `spawn_research`。每个子任务具有独立 `ModelSession`、工具循环、收件箱、超时和阶段报告；模型调用由本机通过 HTTP 发往配置的服务商。

主 Agent 的工具包括 `spawn_research`、`send_message`、`follow_up`、`wait_agents`、`agent_status`、`cancel_agent`，以及所有研究工具。`send_message` 在正在运行的子任务的下一次协议安全边界送达；对子任务发出 `follow_up` 会沿用原会话，并保留此前子报告。子任务通过 `report_progress` 提交可见发现、通过 `submit_report` 交付报告。普通文本回复也可以结束该轮。主 Agent 自主决定是否派发、派发几个、何时追问和结束，不执行固定角色委员会。

等待采用 `asyncio.Condition` 的事件通知；材料保存、阶段发现、完成和失败会唤醒主 Agent。没有定时轮询子 Agent 的后台循环。主 Agent 在下一轮模型请求前收到未送达的子任务事件。所有显式保存的材料都能到达主 Agent，缺元数据或分类不确定不会被拒收。工具运行明细仍记录到界面，但工具开始/结束本身不强制额外模型调用。

主 Agent 和子 Agent 均可使用同模型服务端原生 `web_search`，以及本地 `read`、`save_material`、`list_materials`、`search_materials`、`get_material`、`read_material_range`、`recall_context`、`calculate`，以及 PDF 定位、历史行情、板块、基准和基本面工具。工具不提供账户修改、密钥读取、交易、文件系统或 shell 权限。网页和公告只作为数据进入工具结果。材料保留来源及访问范围；模型负责分类和采用，代码不做“可信度评分/来源白名单/双来源准入”。网络读取的私网地址阻止属于工具网络边界，与材料可信度无关。

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
| `model_request_timeout_seconds` | 90 | 资料研究单次请求总时限 |
| `synthesis_request_timeout_seconds` | 120 | 汇总单次请求总时限，与整轮600秒独立 |
| `synthesis_max_calls` | 4 | 准备或恢复汇总后的额外调用上限 |
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

**测试不会证明模型总能正确分类或给出高质量投资判断。** 第二轮因缺模型配置未执行真实模型研究；第三轮已执行独立真实四轮：公共研究与 B 账户完成，A 两轮均因模型超时失败，完整验收未通过。执行结果与研究质量审阅状态见 [验证记录](verification.md)。测试里分类与报告文本由 fixture 指定，验证的是材料不会丢失、混为同一事实或被工程准入规则屏蔽。不得把这个样例的通过称为真实模型质量验收。

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

历史记录（仅指第二轮，不代表当前配置）：当时模型名称、模型密钥和搜索配置均未设置；`evaluate_model.py --case mixed_fact_opinion` 实际退出 78（SKIPPED）。因此十类真实模型研究质量、真实搜索能力仍未验证。确定性上下文/协议/输入测试不代替上述验证。

## 第三轮建议、范围和引用协议

主 Agent 可以在 `finish_analysis` 中明确处理已有建议：

| 字段 | 语义 |
| --- | --- |
| `maintained_suggestion_ids` | 明确维持已有建议，保留 ID；服务端绑定本轮核实的账户版本。 |
| `suggestions[].id` | 原位更新当前账户已有建议，保留 ID 并增加修订号；不填 ID 时，新建议替代对应证券旧建议。 |
| `revoked_suggestion_ids` | 明确撤销，不再作为活跃建议。 |
| `review_suggestion_ids` | 尚待复核，保留条件和缺口。 |

省略旧建议不会撤销它；账户级报告会把未明确处理的建议标为 `review_status=needs_review`，价格观察继续。单证券任务只处理其目标证券。同一未解决事项自动补做一次后仍未完成明确处理，会保留为 `review_followup.state=waiting_review`，等待页面重试或新的外部事件，不因每次价格检查或重复文字报告无限调用模型。过期与已撤销建议不能通过重试恢复为有效建议。

`price_state` 表示 `met`、`not_met`、`stale`、`unknown` 等数值观察；`review_status` 表示判断是否有效。价格为 `met` 且判断为 `needs_review` 时，只能说价格条件满足，不能说投资判断已重新确认。模型不能指定服务端账户版本、价格观察或通知投递状态。

初始 `suggestions_to_review` 是当前页的建议列表；分页总数、页偏移、`next_offset` 和本轮事件直接关联的 `event_linked_items` 位于 `suggestions_coverage`，`suggestion_index` 提供相同的覆盖信息作为索引别名。`list_recommendations(view='active', limit=30, offset=...)` 可遍历全部活跃建议，`get_recommendation(recommendation_id)` 可按需取详情；两者只访问当前账户，公共任务不能借此获取账户建议。历史用 `view='history'` 单独查询。建议的 `revision`、`previous_job_id`、`replaces_ids`、`replaced_by_id` 保留延续和替代关系。

公共任务携带实际使用的 `universe_version` 与 `input_snapshot`。旧范围任务可完成并保留材料，但其综合结果标为 `stale`，不进入当前共享结论。账户结果保存本次 `universe_version` 和 `shared_research_used`，可追溯实际复用记录及范围；不能把旧公共结论无标记地当成当前结论。

`references` 是服务端生成的机械解析结果：`resolved` 表示可访问的已存材料，`source_link` 表示只有来源链接，`unresolved` 表示材料不存在或不可访问。解析包含实际保存页码、字符数和访问完整性；未知或跨账户 ID 不披露材料私有元信息。运行时对引用缺口提供有限修正机会，仍未解决时保留缺口与有效报告部分。它不核验内容真伪，不设置来源数量、可信度或事实准入门槛。

真实链路审阅还发现：已存 Agent 提取或生成报告本身可被定位，但这不等于已关联外部原文。此类引用保留 `representation_kind`，通过 `origin_references` 递归展示提交的父材料及各自解析状态；`origin_status=linked_materials` 只表示提交了父材料 ID，父项仍可能未解析或仍是生成内容。未关联时页面明确说明缺口，提取者自述来源页码放在 `claimed_source_range`，不作为已保存原文页码。抓取失败的材料只保留来源链接或未解析状态，不提供原文证据按钮。

## 小规模真实服务链路

`scripts/verify_live_chain.py` 使用生产 `Service`、工具与 Store，依次执行公共研究、两个不同虚拟账户研究、修改其中一个账户后再研究。所有新账户、材料、行情与任务写入独立 `data/evaluations/third-.../evaluation.sqlite3`，不把验收数据写入日常账户库。它只读取本项目配置：`.env` 提供环境变量，默认项目 `data/live.sqlite3` 中已保存的设置以 SQLite 只读方式加载并优先于环境默认值；可用 `--settings-db` 指定项目设置库。

先确认四项模型配置：`MODEL_NAME`、`MODEL_BASE_URL`、`MODEL_PROTOCOL`、`MODEL_API_KEY`（或 `MODEL_KEY_ENV` 指定的环境变量）。搜索和行情另行按实际服务配置，不因模型可用就视为已通过。

```sh
# 只检查项目配置，不发模型请求，也不初始化或迁移日常数据库
.venv/bin/python scripts/verify_live_chain.py --check-config

# 实际模型与公开数据；最多 4 轮研究，每轮默认最多 40 次模型调用、600 秒
.venv/bin/python scripts/verify_live_chain.py --max-calls 40 --timeout 600

# 可选：先单独做两次请求的协议检查，然后执行真实研究
.venv/bin/python scripts/verify_live_chain.py --probe --max-calls 40 --timeout 600
```

真实链路专用配置允许每个 Agent 最多 20 轮工具循环；这不改变上文日常运行的默认配置。研究题目收窄为核对实际原文中的营业收入、归母净利润和账户敞口，避免为了验收扩大研究范围。默认证券、公开 PDF 和物理页码见脚本参数；可用 `--source-url`、`--pages`、`--symbols` 与 `--query` 指定实际研究样例。材料与证券的对应关系仍须根据原文判断，不能因同时提供就假定相关。脚本记录实际来源、访问时间、材料 ID、工具事件、阶段输出、账户修改前后版本与可复算指标。`summary.json` 和各轮 `round-*.json` 为可追溯记录，不包含密钥；输出目录权限为 0700，JSON 为 0600。

退出 78 表示模型未配置，1 表示服务或执行失败，2 表示机械观察尚不满足，0 仅表示规定的机械观察通过。认证失败、联网/超时与研究执行失败分别记录；`research_quality=pending_human_review` 不随退出 0 自动变为通过。完整公告更正等受控回归另有确定性测试，不描述为真实发布者刚发生的更正。

## 第四轮汇总恢复

`prepare_synthesis` 保存可见子报告、材料索引、计算与未决问题，然后用精简上下文完成报告。子研究完成后主 Agent 下一次请求前也保存检查点。检查点没有隐藏思维链或不透明服务字段；重启和显式重试优先复用。恢复检查账户和公共范围版本、源报价、资料版本/访问权/时效、未覆盖事件。输入改变时重新研究，不发布旧判断。一个检查点最多恢复两次，每次最多4次模型调用；失败不发布半报告。

HTTP诊断包含阶段、Agent、输入字符和项目数、耗时、状态/异常、尝试数、可用请求ID与服务报告用量。不包含密钥、完整私有请求或隐藏推理。没有启用未经验证的流式协议。
