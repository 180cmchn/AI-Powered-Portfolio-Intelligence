# 第十二轮：单次真实研究与最终账务只读复核

**这次真实研究未通过最终报告验收。** 只释放一个最新账户任务，实际发生 39 次模型 HTTP 请求：37 次收到完整 `response.completed`，2 次在已接受请求并收到部分流数据后发生 `RemoteProtocolError`。主任务在汇总阶段失败，最终报告数为 0，自动额外任务数为 0；没有为了成功而再次研究、卖出或重试。

本记录来自 `data/evaluations/twelfth-live-20261101-final478/summary.json` 与该目录的 `evaluation.sqlite3`。本次审计用 SQLite URI `mode=ro` 加 `PRAGMA query_only=ON`，没有实例化 Store、启动服务或读取凭据。数据库文件审计前后 SHA-256 相同。完整去正文诊断在 [live-evidence.json](live-evidence.json)：保留 39 请求逐条计时/terminal/接收/用量/错误、141 工具调用的安全元数据及 33 次原生搜索完成事件；不含原始响应正文、工具正文、隐藏思维链或 Key。

## 运行身份与范围

- 实际 commit：`478fefb22a123c71b6594e8666be5e9acbb2cf5f`。
- 启动源码摘要：`f9d22f8bfc6e72aa96c8235924919e8159ecf31baeb93317ebbda0c902cb01fb`。
- 上海开始：2026-11-01 00:26:36；准备门释放：00:26:37；任务失败：00:33:03；退出审计完成：00:33:04。
- 自然日历是周日、非交易日、closed；没有更改系统时钟、交易窗口或预测期限。虚拟价格数量是算术输入，不是盘中报价或券商成交。
- 准备阶段 `worker=False`。三笔生产 API 预览/确认完成并对账后，只释放账户 v4 最新任务。隔离周期研究/定时市场/新闻/预测评分暂停，研究与建议检查仍使用生产执行路径；模型/协议/effort/分层时限未降低。
- 运行末尾 `final_source_unchanged=true`、`final_environment_unchanged=true`。没有操作日常 8765。

## 请求、角色与用量

主 Agent 自主派发三个子角色。没有运行的行业与组合角色保持 0 请求，不把它们强制补齐。39 条请求中的 model/provider/实际发送 effort 均与冻结绑定一致，`binding_mismatches=[]`。

| 角色 | 模型 | effort | 请求 | 完整 terminal | 失败 | 已回报原始 cost_in_usd_ticks |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| 主 Agent | grok-4.7 | max | 9 | 8 | 1 | 1977914000 |
| 公告披露 | grok-4.6 | high | 11 | 11 | 0 | 2115516800 |
| 市场价格 | grok-4.7 | medium | 7 | 7 | 0 | 1817898400 |
| 行业政策 | grok-4.7 | medium | 0 | 0 | 0 | 0 |
| ETF | grok-4.7 | medium | 12 | 11 | 1 | 3032813200 |
| 组合配置 | grok-4.7 | medium | 0 | 0 | 0 | 0 |

37 个完成请求回报 input_tokens **1,182,294**、output_tokens **36,802**、total_tokens **1,219,096**。这些是逐次请求的累计用量，包含多轮重复上下文，不是不同文字的去重数量。服务回报 `num_server_side_tools_used=33`；原始 `cost_in_usd_ticks` 累计 **8944142400**。这里不猜 ticks 的换算精度、不换算成美元或人民币、不宣称已对账供应商发票。两个被接受的失败请求没有 usage；它们可能有未回报费用，不能按 0 计费。

以下按请求开始时间排序。完整 JSON 还保留首字节、首有效事件、末字节、末有效进展、deadline、HTTP 状态、retry_suppressed、回报模型与用量。所有请求 HTTP 状态均为 200；200 不代表完整终止。

| # | 上海开始时间 | 角色 | 阶段 | 耗时秒 | 终止 |
| ---: | --- | --- | --- | ---: | --- |
| 1 | 00:26:38.412 | 主 Agent | research | 26.321 | response.completed |
| 2 | 00:27:04.762 | 主 Agent | research | 21.282 | response.completed |
| 3 | 00:27:26.199 | 公告披露 | research | 8.227 | response.completed |
| 4 | 00:27:26.201 | ETF | research | 21.641 | response.completed |
| 5 | 00:27:26.203 | 市场价格 | research | 6.201 | response.completed |
| 6 | 00:27:26.666 | 主 Agent | research | 7.942 | response.completed |
| 7 | 00:27:33.327 | 市场价格 | research | 8.277 | response.completed |
| 8 | 00:27:34.624 | 主 Agent | research | 22.955 | response.completed |
| 9 | 00:27:34.778 | 公告披露 | research | 10.715 | response.completed |
| 10 | 00:27:41.676 | 市场价格 | research | 35.770 | response.completed |
| 11 | 00:27:45.921 | 公告披露 | research | 5.066 | response.completed |
| 12 | 00:27:49.082 | ETF | research | 4.806 | response.completed |
| 13 | 00:27:51.013 | 公告披露 | research | 7.457 | response.completed |
| 14 | 00:27:53.904 | ETF | research | 3.206 | response.completed |
| 15 | 00:27:57.204 | ETF | research | 6.553 | response.completed |
| 16 | 00:27:57.632 | 主 Agent | research | 44.396 | response.completed |
| 17 | 00:27:58.597 | 公告披露 | research | 6.306 | response.completed |
| 18 | 00:28:05.835 | ETF | research | 7.840 | response.completed |
| 19 | 00:28:06.878 | 公告披露 | research | 12.566 | response.completed |
| 20 | 00:28:13.796 | ETF | research | 27.665 | response.completed |
| 21 | 00:28:17.525 | 市场价格 | research | 11.878 | response.completed |
| 22 | 00:28:20.990 | 公告披露 | research | 9.634 | response.completed |
| 23 | 00:28:29.485 | 市场价格 | research | 35.967 | response.completed |
| 24 | 00:28:30.647 | 公告披露 | research | 3.240 | response.completed |
| 25 | 00:28:33.898 | 公告披露 | research | 12.190 | response.completed |
| 26 | 00:28:41.537 | ETF | research | 8.314 | response.completed |
| 27 | 00:28:42.240 | 主 Agent | research | 26.860 | response.completed |
| 28 | 00:28:46.243 | 公告披露 | research | 34.542 | response.completed |
| 29 | 00:28:50.515 | ETF | research | 5.851 | response.completed |
| 30 | 00:28:56.553 | ETF | research | 9.978 | response.completed |
| 31 | 00:29:05.492 | 市场价格 | research | 23.238 | response.completed |
| 32 | 00:29:06.556 | ETF | research | 7.771 | response.completed |
| 33 | 00:29:09.197 | 主 Agent | research | 7.829 | response.completed |
| 34 | 00:29:14.905 | ETF | research | 9.702 | response.completed |
| 35 | 00:29:17.059 | 主 Agent | research | 19.230 | response.completed |
| 36 | 00:29:21.125 | 公告披露 | research | 59.434 | response.completed |
| 37 | 00:29:24.791 | ETF | research | 114.848 | RemoteProtocolError；无 terminal |
| 38 | 00:29:28.776 | 市场价格 | research | 29.839 | response.completed |
| 39 | 00:30:06.437 | 主 Agent | synthesis | 177.061 | RemoteProtocolError；无 terminal |

## 两次失败的实际时序

| 诊断 | ETF 子任务 | 主 Agent 汇总 |
| --- | --- | --- |
| 角色/模型/effort | etf_researcher / grok-4.7 / medium | manager / grok-4.7 / max |
| 请求开始（上海） | 2026-11-01T00:29:24.791158+08:00 | 2026-11-01T00:30:06.437398+08:00 |
| 请求结束（上海） | 2026-11-01T00:31:19.637782+08:00 | 2026-11-01T00:33:03.497278+08:00 |
| 请求耗时 | 114.84759 s | 177.06119 s |
| 首字节 / 首有效事件 | 8.38184 / 8.57217 s | 9.19928 / 9.55356 s |
| 末字节 / 末有效进展 | 64.84611 / 52.08837 s | 127.05656 / 108.61747 s |
| 末有效进展类别 | response.output_text.delta | response.output_text.delta |
| 失败距末字节 / 末有效进展 | 50.00148 / 62.75922 s | 50.00463 / 68.44372 s |
| 流最大有效进展空闲 | 62.75923 s | 68.44374 s |
| 已接收数据量 | 70924 bytes | 166909 bytes |
| 心跳数 | 1 | 2 |
| HTTP / request_accepted | 200 / True | 200 / True |
| headers received / body complete | True / False | True / False |
| 终止事件 / usage | None / None | None / None |
| 错误码 / transport kind | upstream_transport_error / incomplete_response_body | upstream_transport_error / incomplete_response_body |
| 阶段 | receiving_body | receiving_body |
| 配置：首事件 / 空闲 / 请求硬限 | 90.0 / 120.0 / 300.0 s | 90.0 / 120.0 / 360.0 s |
| retry_suppressed | accepted_or_sent_request_outcome_unknown | accepted_or_sent_request_outcome_unknown |

两次异常均是客户端观察到的**上游响应体不完整**，不是旧 90/120 秒绝对时限，也没有触发 120 秒有效进展空闲或 300/360 秒请求硬限。`effective_limit=request_hard_timeout` 记录的是当时计算出的最早 deadline 类别，不表示该 deadline 已触发。失败发生点距最后字节约 50 秒；仅凭客户端诊断不能进一步确定是供应商、转发器还是链路设备断开。

`request_accepted=true` 且结果未知，生产有限重试策略设置 `retry_suppressed=accepted_or_sent_request_outcome_unknown`，没有自动重发已被接受的失败请求。主任务最终错误保留“仅有部分成果，证据或报告交付范围不足；需补充研究，不能承诺仅汇总恢复”。没有把两个已完成子报告当成最终账户报告。

## 工具和可见网络事件

实际记录 141 个 `tool_started` 和 141 个对应 `tool_result`，不是 141 个成功外部 HTTP 请求。按生产实现分为：本地计算/已存结构化行 **65** 次，本地材料/上下文 **25** 次，Agent/预测协调 **11** 次，可能联网的公开数据/文档入口 **40** 次。

| 工具 | 次数 | 类别 | 事件可见边界 |
| --- | ---: | --- | --- |
| agent_status | 2 | 协调/目标读取 | 只保留可见安全元数据；result事件不等于全部外部请求成功 |
| benchmark_history | 2 | 可联网数据/文档 | 只保留可见安全元数据；result事件不等于全部外部请求成功 |
| calculate | 30 | 本地计算/已存行 | 只保留可见安全元数据；result事件不等于全部外部请求成功 |
| fundamentals | 3 | 可联网数据/文档 | cache_hit=true 1 |
| get_material | 2 | 本地材料/上下文 | 只保留可见安全元数据；result事件不等于全部外部请求成功 |
| history | 12 | 可联网数据/文档 | 只保留可见安全元数据；result事件不等于全部外部请求成功 |
| list_agent_roles | 1 | 协调/目标读取 | 只保留可见安全元数据；result事件不等于全部外部请求成功 |
| list_forecast_targets | 1 | 协调/目标读取 | 只保留可见安全元数据；result事件不等于全部外部请求成功 |
| list_materials | 5 | 本地材料/上下文 | 3 条事件结果截断，未展开正文 |
| market_indicators | 20 | 本地计算/已存行 | insufficient_data 7，未冒充有效指标；缓存标签来自存储材料，不是新HTTP证明 |
| pdf_metadata | 8 | 可联网数据/文档 | cache_hit=true 3 |
| query_market_rows | 15 | 本地计算/已存行 | 缓存标签来自存储材料，不是新HTTP证明 |
| read | 12 | 可联网数据/文档 | 3 条事件结果截断，未展开正文；cache_hit=true 7 |
| read_material_range | 1 | 本地材料/上下文 | 只保留可见安全元数据；result事件不等于全部外部请求成功 |
| recall_context | 16 | 本地材料/上下文 | 只保留可见安全元数据；result事件不等于全部外部请求成功 |
| search_materials | 1 | 本地材料/上下文 | 只保留可见安全元数据；result事件不等于全部外部请求成功 |
| search_pdf | 3 | 可联网数据/文档 | cache_hit=true 3 |
| spawn_research | 3 | 协调/目标读取 | 只保留可见安全元数据；result事件不等于全部外部请求成功 |
| submit_report | 2 | 协调/目标读取 | 只保留可见安全元数据；result事件不等于全部外部请求成功 |
| wait_agents | 2 | 协调/目标读取 | 只保留可见安全元数据；result事件不等于全部外部请求成功 |

12 个 `native_search` 事件合计含 **33 个不同 ID 的 `web_search_call` completed**，与回报的服务端工具计数 33 相符。它们发生于上述模型请求内部，不能再当成 33 个额外模型 HTTP 请求。公开数据实际记录包含东方财富来源、腾讯未复权日线备用来源、文档/PDF缓存命中；没有新增搜索平台或 Key。

可联网工具可能命中缓存，也可能在一次调用内发生备用源、连接重试或多个内部 HTTP 子请求。当前应用没有逐包覆盖这些内部子请求，因此本记录不把 40 次入口调用换算成“外网请求恰为 40”，也不把未返回完整结果或被截断的内容推断为成功。`market_indicators` 的 20 次里有 7 次 `insufficient_data`，这是保留的研究资料限制。

## 准备任务与最终账务

账户 v1 创建及两次中间买入产生的旧任务，均由正常输入版本变更转成 superseded；它们各自 attempts=0、模型请求=0。仅 v4 的 `937591e98d07415aa26950b6a413b70f` 被释放，attempts=1、39 请求，最终 failed。没有全局任务或额外自动任务。三项 ledger_update 需求均关联这一个任务，最后标记 failed；不把研究失败解释为交易失败。

| 证券 | 虚拟数量 × 单价 | 成交额 | 佣金/过户/印花 | 费用合计 | 记账后现金 | 输入版本 |
| --- | --- | ---: | --- | ---: | ---: | ---: |
| SH:518880 | 100 × 10.000 | 1000.00 | 5.00 / 0.00 / 0.00 | 5.00 | 98995.00 | 2 |
| SZ:002354 | 1000 × 10.00 | 10000.00 | 5.00 / 0.10 / 0.00 | 5.10 | 88989.90 | 3 |
| SZ:159599 | 1000 × 2.500 | 2500.00 | 5.00 / 0.00 / 0.00 | 5.00 | 86484.90 | 4 |

SQLite 只读复核全部通过：`100000.00 − 13500.00 − 15.10 = 86484.90`；持仓 518880=100、002354=1000、159599=1000；含买入费的剩余成本合计 `13515.10`。只有 1 条期初 + 3 条买入、4 条现金记录、3 个不同 operation ID 回执。三笔保存条目与原回执 entry 完全相同，预览/回执/最终库现金一致，账户版本仍为 4；没有新增卖出、费用调整、冲销或第二轮研究。使用的是未确认的模拟费用模板 v1，不能称为真实券商收费协议。

可卖批次也保留原生产规则：黄金 ETF 为 2026-11-01 起；普通股票和芯片 ETF 为下一交易日 2026-11-02 起。这里没有改写可卖日期，也没有补造历史成交。

最终库 `analyses=0`、`forecasts=0`；1 个 exploratory 批次、9 个样本的状态为 failed，批次记录保持 open。没有发布预测、成熟评分或准确率结论。库中 209 条材料与部分成果不等于最终报告。

失败后的只读展示复验结束后再次比对：4 个任务的 ID/state/attempts/input_version/error 与运行结束 summary 完全一致；406 个事件的分类计数也完全一致，模型请求仍是 39。查看历史记录没有新增模型尝试或账本流水。

## 真实交互与尚未完成的验收

研究仍在运行时，Mac Chrome 主页面事件从 **95 增至 331**，已展开的详情、JSON 滚动位置和文本选区保持。[刷新前选区](mac-live-selected-before.png)、[持续更新后选区](mac-live-selected-after-polls.png)。这是本轮真实进行中的阅读证据；不是停止业务刷新后静态截图。

进行中的任务弹窗首先读取 100 条并打开内层 JSON；当时点击加载更多尚未确认成功，研究随后结束。为核实分页，之后用同一隔离数据库启动 `worker=False` 的只读展示进程（历史随机端口 53537，现已停止），真实点击更多从 100→200，再按 Enter 从 200→300。最终可访问性状态显示“已读取300条，仍有后续”，原始第一条 JSON 仍展开：[失败后真实记录分页至300条](mac-real-records-dialog-page300.png)。这项是**失败后的真实历史记录只读复验**，不能称为仍在研究时的分页成功。

由于没有完整报告，发布后 10 轮与进程重启后 10 轮的真实观察均**未执行**，重启观察 worker 没有启动。本轮不能宣称真实模型已验证“发布且持续有效”，也不能用受控旧报价回归改写本次失败。第十一轮 unknown→stale 首次观察保护及 fresh→stale 等生产路径受控回归见主交付；此处保留真实运行的未达成边界。

证据文件：[完整安全诊断](live-evidence.json)、[实际运行终端日志](live-run.txt)。没有追加模型调用来改善这份记录。
