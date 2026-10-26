# 第四轮模型原生联网：协议与实际端点证据

检查时间：2026-10-26 至 2026-10-27（Asia/Shanghai）。读取项目 `.env` 与现有数据库设置时仅输出脱敏字段；未修改密钥或使用其他应用凭据。

实际配置为 `responses`、`https://api.xxyapi.com/v1`、模型标签 `grok-4.7`。live 数据库没有模型覆盖设置。这里的模型名称只是转发服务配置标签，未独立验证其上游模型身份。

## 公开协议核对

- [xAI Web Search](https://docs.x.ai/developers/tools/web-search)：Responses 使用服务端内置 `web_search`。
- [xAI Function Calling](https://docs.x.ai/developers/tools/function-calling)：内置工具与本地 function 可共存；本地调用返回应用处理，服务端工具由服务商执行。
- [xAI Advanced Usage](https://docs.x.ai/developers/tools/advanced-usage)：无服务端存储时可保留输出项与 `reasoning.encrypted_content` 继续本地工具会话。
- [xAI Streaming & Synchronous Requests](https://docs.x.ai/developers/tools/streaming-and-sync)：Responses 可以 `include=["web_search_call.action.sources"]` 请求可见来源。
- [xAI Citations](https://docs.x.ai/developers/tools/citations)：URL 注释带来源及可选位置；应用不将注释当成本地下载证明。
- [OpenAI Web Search](https://developers.openai.com/api/docs/guides/tools-web-search)：同时核对共同的 Responses 原生工具边界。未将官方能力直接等同于任意转发服务能力。

针对当前转发服务，公开搜索 `site:xxyapi.com Responses web_search 文档` 与 `"xxyapi" "文档" API` 没有找到可核对的协议文档。联网阅读工具不能打开其主页；随后对 [服务主页](https://www.xxyapi.com)、[实际端点主页](https://api.xxyapi.com) 和 [docs 路径](https://api.xxyapi.com/docs) 进行三个无认证、每个最多 10 秒的 GET：均 HTTP 200，但仅返回标题为 `New API` 的 JavaScript 页面外壳，没有可读的协议说明或文档链接。因此未验证其公开承诺、参数覆盖范围或上游身份；以下结论来自实际接口响应，不能扩展到未测试模型、路径或账号权限。

## 实际网络验证

`fourth-native-probe.json`：只提供“贵州茅台及官方投资者财务报告”问题，没有预先提供报告 URL。一个模型请求耗时 11.71 秒，服务返回两个 `status=completed` 的 `web_search_call`、15 个来源 URL，以及 `usage.server_side_tool_usage_details.web_search_calls=2`；发现了上交所披露页。请求用了 `tool_choice=required`，但服务端实际执行两次搜索，未把提示中的“一次”误写成实际工具调用预算。

`fourth-native-mixed.json`：实际通过应用 `ModelClient` 发送原生搜索与本地 `capability_echo`。第一次返回两个完成的搜索调用和一个本地函数调用；第二次带原样输出项及本地函数结果续接，准确返回本地生成的随机令牌和发现的平安银行官方报告链接。共两次 HTTP 请求、9.70 秒。第二次未请求新的搜索。令牌已在证据中替换为占位符；不保存隐藏推理或不透明续接内容。

实际来源只返回 URL，没有原网页全文或原始 PDF 字节。首次探测的注释位置均为 `0 → 0`，不能定位正文；混合探测没有返回注释。上述缺口原样保留。来源 URL 可保存并供 Agent 自主补读，不强制下载全部结果。HTTP 200、返回模型标签或附带普通链接均不单独构成联网执行证明。

模型请求日志记录 Agent、阶段、请求序号、输入字符数、输出字节数、耗时、状态、服务请求 ID 及实际工具用量。混合探测输入从 824 字符增长到约 30 KB，反映必须续接的不透明协议状态也会增大请求。运行时在完成资料研究后提供可见汇总检查点并建立精简汇总上下文；这不是对历史超时根因的单一归因。

### 明确的中间消息不能结束研究

[OpenAI 官方 reasoning 指南的 phase 说明](https://developers.openai.com/api/docs/guides/reasoning#phase-parameter) 明确区分 `commentary`（中间更新）与 `final_answer`（完成回答），并要求手动续接时保留原始 `phase`。适配器因此只把明确最终或未带阶段字段的普通文本作为最终候选；`phase=commentary` 或消息自身 `status=in_progress/incomplete` 的文字不会触发 Agent 完成。仅中间消息会继续现有有界循环，同一响应同时含中间说明与最终回答时只提取后者。原始输出项原样续接，整理后的可见归档也保留 `phase/status`，不保存隐藏推理。

本轮转发端点的两份脱敏探测记录没有采集消息 `phase`，不能据此声称实际端点曾返回该字段，亦不能断言其从不返回。此次修复依据明确的官方协议与受控协议测试，没有按文字含义猜测“我将继续”等句子。省略 `phase` 或返回 `phase=null` 的普通最终文本保持兼容。

## 验证范围

协议替身与生产组件验证位于 `tests/test_fourth_native.py`、`tests/test_fourth_recovery.py`、`tests/test_agents.py`、`tests/test_context_runtime.py`。48 项通过，包括主/子 Agent 原生来源、混合续接、未完成响应不发布半份报告、失败分类、来源入库、真实 Service/Runtime/SQLite 汇总恢复、账户/范围/事件/行情/时效/源材料守卫及两次恢复上限。补查失败后，检查点保留旧报告对应轮次、未完成补查的状态/错误和已投递但尚未完成的消息，不能把旧报告误当成补查完成。阶段字段测试覆盖中间态续接、原样保留元数据、最终文本筛选、没有最终回答时的预算停止、无阶段字段兼容和上下文整理后的标签保留。这些替身测试不是网络实测；上面两个 JSON 才是模型网络证据。完整多账户后台验收另行记录。
