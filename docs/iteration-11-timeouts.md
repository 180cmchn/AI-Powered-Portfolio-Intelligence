# 第十一轮：请求时间策略与恢复诊断

本页区分历史实测、生产路径受控测试和真实供应商验收。受控流没有调用真实模型。现场原始配置与脱敏请求证据见 `eleventh/daily-failure-evidence.json`；本轮真实端点结果以第十一轮总交付及 live evidence 为准。

## 已确认的现场取消层次

只读定位到任务 `085542c56c534e3b9e7851ff1d35616b`：北京时间 2026-10-30 22:12:56–22:21:43，约 527 秒。任务冻结值为普通请求 90 秒、汇总请求 120 秒、子研究 120 秒、账户 600 秒。主角色使用 grok-4.7，实际发送 `max`；部分成功响应回显 `high`，两者保留，不改写配置，不据此推定故障原因。

最终两次汇总分别在 120001.74 / 120002.01 毫秒被本地绝对请求时限终止。第一字节为 8963 / 7663 毫秒，首个 SSE 事件为 9777 / 7822 毫秒；已收到 112 / 87 个事件、146051 / 127427 字节，没有完整终止对象。原日志没有逐事件最后进展和最大空闲，因此不能声称一直持续输出到截止。三次子请求另被 120 秒子任务预算取消，没有完成子报告。还有一次约 62290 毫秒的 `RemoteProtocolError`，旧机制重试后成功；这也是供应商/网络中断证据，不能把所有失败一概归为应用计时。

原 `_post()` 同一个 timeout 同时交给 HTTPX 并包围整个流；SSE 数据持续到达也不会续期。旧子任务的 120 秒外层截止会独立取消更长的合法请求。现场精确到约 120 秒的两次汇总 `RequestDeadlineExceeded` 已证明它们由应用请求总限取消，不证明此前完全没有上游停滞。

## 新策略与优先级

| 设置 | 新任务默认 | 含义 |
|---|---:|---|
| `model_first_event_timeout_seconds` | 90 秒 | 等待有效正文/函数/推理事件类型或原生工具进展；HTTP 200、created/queued、注释心跳不算 |
| `model_stream_idle_timeout_seconds` | 120 秒 | 最近有效进展后的最大等待；每次有效进展重排此计时器 |
| `model_request_hard_timeout_seconds` | 300 秒 | 普通请求总成本/时长保护，永不续期 |
| `synthesis_request_hard_timeout_seconds` | 360 秒 | 最终和恢复汇总的独立总硬限 |
| `agent_timeout_seconds` | 600 秒 | 子任务排队、全部模型轮次/重试和工具合计预算 |
| `analysis_timeout_seconds` | 1200 秒 | 行情准备、主子研究、汇总、重试的整轮预算 |

HTTPX 的 connect / pool / write 分别为内部策略 15 / 15 / 30 秒；read 使用生产接收路径的首事件/有效空闲计时器。JSON 兼容响应按实际正文网络块记录接收进展，但只有解码和协议校验完成才能成为有效结果；未暴露中间正文的等待始终有界。

所有父预算用同一个进程内 monotonic deadline 传播，不写入模型上下文或可复用持久快照。每个请求取请求硬限、子任务剩余量、整轮剩余量中的最早截止。子任务记录排队耗时和请求执行耗时，排队不会悄悄赠送一轮新预算。整轮保留 `min(汇总硬限, 整轮预算×30%)`，默认 360 秒；子任务最迟在进入这一预留段时停止；主 Agent 在“剩余时间 ≤ 预留 + 普通请求硬限”时就进入收尾，默认剩余 660 秒时不再新发普通 300 秒请求，避免该新请求独自占掉汇总窗口。子角色此时也转为提交已见成果。代价是接近预算尾部时更早交付有限结论，而不是继续扩展研究；已在运行的工具和外部故障仍受整轮总限约束，不保证供应商一定能在窗口内交付。预留属于整轮预算，不在整轮到期后追加时间。设置和任务启动事件会提示父预算短于请求限等冲突。

默认值是有界运行策略，不是供应商 SLA：历史完成过约 94 秒的汇总，旧 90/120 秒缺少工具与终止余量；4000 token 在用户描述的 50 token/s 下仅正文约 80 秒，不能涵盖推理、工具与网关结束等待。普通 300 / 汇总 360 给这些阶段留出空间；首事件 90 / 空闲 120 保留停滞保护。历史缺少空闲数据，所以没有声称这些默认值来自精确的空闲分位数。

## 迁移和冻结

`migrate_timeout_policy(saved)` 仅派生当前设置和新任务快照；不修改已启动/历史任务的冻结配置。

- 旧普通 / 汇总 90 / 120 秒出厂值映射为新 hard 300 / 360 秒；旧子任务 120 / 整轮 600 映射为 600 / 1200 秒。
- 旧非出厂自定义总限原值映射到对应新 hard，自定义父预算原值保留。首事件与空闲是新独立字段，绝不把旧值同时当三种语义。
- 旧库无法分辨“用户显式设成出厂值”和“没有更改出厂值”；这里按值迁移，旧字段仍保留可审计，界面明示映射。用户可以在新策略中重新显式选择原上限。
- 已包含新字段的设置不再次扩大父预算。直接用无新字段的旧冻结配置创建 ModelClient，仍按旧字段作为总硬限，不回写或伪装历史。
- 设置读取、任务冻结、具名主子角色、follow-up、final、resume、后台公共研究均继承同一冻结策略。原生搜索能力探测另显式限制单次 hard 最多 60 秒、无自动重试；函数能力探测保持有限两轮。
- `GET /api/settings/timeout-policy` 返回普通/汇总有效策略和父预算警告；供应商连接仍只有 Key、Base URL、协议，模型/effort 绑定不变。

## 重试、完整结果与取消

未建立连接的 ConnectError / ConnectTimeout、连接池等待失败和明确的 HTTP 429/5xx 拒绝可有限退避重试；每次重试先经过共享调用预算，且父剩余时间必须容纳退避加一轮首事件等待。收到 200 后断流、本地首事件/空闲/硬限、发送结果未知、父任务取消均不自动叠加生成。没有构造供应商不存在的 resume/cancel API；HTTP 连接关闭不代表已证明远端停止计费。

SSE 仍必须得到完整、一致的 `response.completed` 对象，且所有函数项满足完成协议。delta 不拼装成报告，半截函数参数不执行。无 terminal 的 EOF、非法序号、身份变化、截断或 failed/incomplete terminal 均失败。原始 opaque continuation 仅保留既有请求内存用途，不写诊断。

稳定错误码包括 `first_event_timeout`、`stream_idle_timeout`、`request_hard_timeout`、`child_budget_exhausted`、`analysis_budget_exhausted`、`connect_timeout`、`pool_timeout`、`write_timeout`、`network_read_timeout`、`upstream_transport_error`、`cancelled`。用户取消、输入版本更新、服务停止通过白名单 Task.cancel 消息归因；发生实际父 deadline 时以 monotonic 归因为准。

底层 ModelClient 不再无条件承诺“可从汇总检查点恢复”。服务在失败后调用只读 `checkpoint_recovery_diagnostic`，不消费恢复次数；检查记录、成本预算、时效、账户/公共范围、材料访问与版本、事件及行情输入后，分别说明无检查点、需补研究、已失效、待核实、可有限汇总、可仅汇总。真正重试时仍由原完整恢复守卫重新验证。父预算耗尽或取消正在执行工具时，先把未续接调用标记为中断，再保存此前已经完成的可见计算/子报告；不会因 pending call 让早先成功计算失去恢复资格，也不会把未完成工具写成成功或执行半截函数。

## 安全可观察字段

每个 HTTP attempt 保存开始/结束 UTC、队列等待、请求 ID、连接 trace、响应头时间、第一字节、首 SSE、首有效事件、首正文 delta、首工具事件、首 reasoning 事件的时间、有效事件类型计数、最后有效进展时间/类型、最大有效空闲、总时长、terminal、每层剩余预算、实际取消者、重试序号/抑制理由及未知费用标记。没有认证头、Key、完整输入或 delta/隐藏推理内容。

`stream_first_event_ms` 包括响应创建事件，不能替代 `stream_first_effective_event_ms`；`stream_first_text_delta_ms` 才是本地观测到首正文 token 片段的时点。供应商“首字”定义未必一致，必须结合 request ID 对照。

## 验证

新增 `tests/test_eleventh_timeouts.py` 用实际 ModelClient、SSE 解析器、HTTPX 控制 transport、ResearchRuntime 和 semaphore，采用缩短计时覆盖：连续流超过旧等比例 90/120、首事件慢、created-only、纯心跳、流空闲、持续进展耗尽硬限、子/整轮提前耗尽、合法搜索/纯函数终止、网络分块、连接前有限 retry、剩余父预算不足、队列与执行分离、共享整轮 deadline、迁移幂等及旧快照不变，以及主 Agent 预留阈值前后只有可容纳的普通请求才会启动。

原 `test_seventh_streaming.py` 保留终止、半函数、损坏对象、EOF、JSON/SSE、取消、凭据反射等保护。唯一旧行为契约调整：已接收部分流后断线不再盲目重试（原测试期望自动重试），现断言一次请求、无 pending tool、无部分结果。另为数秒预算的旧受控 Runtime 夹具同步缩短请求硬限，避免用 5 秒整轮搭配隐式 90 秒请求；原共享调用、follow-up、独立角色与恢复断言均保留。0.2 秒父限测试改为已完成同一笔 8850 计算后工具等待耗尽整轮，明确验证父限归因与部分检查点恢复，不通过发起已不可能容纳的长模型请求来制造超时。

当前定向命令：

```sh
PYTHON_DOTENV_DISABLED=1 QUOTE_PROVIDER=manual .venv/bin/python -m pytest \
  tests/test_eleventh_timeouts.py tests/test_seventh_streaming.py \
  tests/test_fourth_budget.py tests/test_fourth_recovery.py tests/test_fifth_reasoning_effort.py \
  tests/test_eighth_delegation.py tests/test_agents.py tests/test_context_runtime.py -q
```

含共享预算测试的扩展定向集通过记录见 `eleventh/timeout-regression.txt`；最终集成重跑结果以第十一轮总验收记录为准。它不是实际供应商成功率或真实故障恢复证明。

协议参考：[HTTPX 分层 timeout](https://www.python-httpx.org/advanced/timeouts/)、[Python timeout / cancellation](https://docs.python.org/3/library/asyncio-task.html#asyncio.timeout)。
