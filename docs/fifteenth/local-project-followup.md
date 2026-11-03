最新进展：用户后来授权改为 xhigh 并继续调查，四次续跑后已取得有效历史有限报告；定位与边界见 [xhigh 调查](xhigh-investigation.md)。以下保留此前本地接收器排查与 v6 两次修复验证的独立记录，文中当时的额度和未交付结论不代表最新累计状态。

当时按用户要求检查本地项目，不将其他用户能正常调用模型或供应商 `client_gone` 标签当作本地正确性的证明。离线排查后另获明确授权，执行了两次修复验证；该阶段没有更改模型、effort、输出上限、代理或 TLS。此前供应商截图显示金额 `$0.013266`、Tokens `24,833 / 6,463`，按时间/模型/耗时与 v5 记录对应；原客户端 `usage=null` 保留，见 [截图证据](supplier-screenshot-evidence.json)。

发现并在真实 localhost HTTP socket 上复现两处接收端缺陷：

1. `app/model.py` 将网络块末尾的 `CR` 暂存，等待下一字节确定是否为 `CRLF`。但单独 `CR` 就是合法 SSE 行尾；完整 `response.completed` 事件的最后空行若以 `CR` 结束，且服务器不立即发送 EOF，客户端会延迟识别已完整到达的终止对象。服务器随后发生 HTTP 尾部异常时，原代码甚至将其记录为 `RemoteProtocolError / transport_error_before_terminal` 并丢失完整结果。修复为立即处理 `CR`，仅吞掉紧随其后的一个 `LF`，包括跨网络块的 `CRLF`。[SSE 规范](https://html.spec.whatwg.org/multipage/server-sent-events.html#parsing-an-event-stream)明确允许 CR、LF 和 CRLF；未由空行终结的半个事件仍不能采用。
2. `response.function_call_arguments.done` 中非空参数表示生成有进展，但原进展计时没有计入。复现中 delta → done → completed 的两段间隔各小于空闲上限，原代码仍从 delta 起计时并提前取消；把中间事件替换为 delta 则正常。修复只将非空字符串参数的 done 计入进展；空 done、心跳不续期，硬限/父期限仍不延长，done 本身也不能替代最终终止对象。

原红灯复现保存在 `test-results/fifteenth/transport-local-audit.json` 和对应离线脚本。受控毫秒预算用于快速制造边界，不是生产超时；生产限制未调大。这些本地复现不是付费模型报告。

其他核对结果：

- [本地超时审计](local-timeout-audit.json)：实际冻结配置的普通研究硬限为 300 秒，但 v5 汇总调用采用 360 秒，分析总限为 1200 秒，HTTPX read timeout 为 null。受控 Runtime → ModelClient 在 300.054 秒未取消，并可于 310 秒完成；实际 360 秒截止、外层 300 秒取消和显式取消均被区分记录。未发现 v5 实际启用的本地 300 秒计时器。
- [请求结构审计](local-request-structure-audit.json)：旧短探测与 v5 顶层仅 instructions/input/tools 不同；model、effort、stream、include、store、max_output_tokens 和 flat Responses tool_choice 相同。完整 schema 的五项必填定义与短探测一致，没有发现静态不可满足的契约。此结论不等于供应商对任何完整请求均兼容。
- 已只读核对本地代理日志的相关时间段：能够找到 17:30:16 的目标连接记录，未找到与本请求直接关联的 17:35 关闭/超时原因；另一条 17:31:55 的错误不指向该目标，未据此归因。没有更改代理或读取/导出凭据。

历史 v5 的原始 SSE 字节未保存，因此不能证明当次响应使用了 CR 行尾。第二处 done 缺陷的事件类型也未出现在 v5 安全计数中。这两项是已证实的本地缺陷，不是已证实的历史断流唯一根因；真实报告验收仍未通过。

最终固定源码为 `a2c74872448ff00cff0aa8de18151859e6e4a90d737d3403daf8601461e2a396`，旧 5355/ff8 证据不覆盖、不重标。新增 16 项 localhost socket 回归，相关五组 154 项通过；其中两个期限用例随后改为事件栅栏和统一受控时钟，32 项联合回归通过。改造时出现的测试服务关闭等待已保留日志并修正 fixture 的连接结束方式，没有改动生产限制或通过重跑掩盖等待。

最终 Mac 全套 **1227 passed、1 warning，118.88 秒**；隔离 Linux/Python 3.12 全套 **1227 passed、1 warning，132.99 秒**；十一组浏览器全部通过。单条警告为既有 Starlette/httpx 弃用提示。源码与测试哈希验证前后相同，见 [回归摘要](local-stream-fix-validation.json) 和 [固定文件清单](local-stream-fix-source.json)。

此次追加的生产改动仅在流接收器，没有修改请求生成、默认期限、账户配置或供应商配置。旧准备包绑定 ff8，真实验证前已重新准备绑定当前源码的 v6，原包和失败记录保留。

两份准备清单仅 source hash 不同，输入逐字节相同，完整 schema、模型/effort、输出上限、超时及角色身份均相同，见 [准备比较](postfix-preparation-comparison.json)。用户随后明确回复“明确恢复，最多 2 次修复验证”，授权原环境路线的首次提交及唯一一次契约纠正；先前直连提案未执行。

v6 于 18:34:45—18:41:35 完成两次调用：均为 HTTP 200、`response.incomplete`，明确原因 `max_output_tokens`，且 HTTP EOF 完整。两次没有有效 `finish_analysis`，未发布报告；没有未知结果断流或第三次请求。请求仍为输出上限 4000，回报输出分别 13,905/13,380，其中 reasoning 分别 9,905/9,380；相减均为 4000。当前不能证明提高输出上限就能成功，也不能把明确 incomplete 称为传输已完整交付报告。

累计额度 **3/10**、剩余 **7 次**，本次两次恢复上限已耗尽。供应商后台显示本次费用合计 **$0.036222**，计数与两次 API 回报一致，未作发票级核销。完整分项结果及正文未验收的原因见 [修复后内容核对](postfix-content-review.md) 和 [机器记录](postfix-live-evidence.json)。
