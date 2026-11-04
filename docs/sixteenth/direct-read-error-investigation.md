# direct三次ReadError独立调查

只读复核于2026-11-04T17:17:50.004562+00:00完成，事件截至seq2021；冻结源码`b7afd9860b14304d6c1f698e7fbaed5db80435d37c6f90db920000324c806470`未改。结构化证据见[JSON](direct-read-error-investigation.json)，最终路线计数见[独立核对](direct-final-route-review.json)。本调查没有增加模型请求、外部或本机网络实验，也没有取得这三条失败的FIN/RST抓包。

## 三个独立样本

下表时间均为Asia/Shanghai，采用请求finished_at；job进入failed在其后，不能混用。

| 事件/任务 | 阶段 | 请求结束时间 | 总秒数 | 最后正文到异常 | 最长语义空闲 | 该任务HTTP/EOF |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| 1776 / A公共材料`0558397b…` | research | 01:03:17.293444 | 203.729 | 185.93毫秒 | 9.707秒 | 10/9 |
| 1976 / B配置`a9caca32…` | research | 01:05:31.868435 | 6.032 | 99.94毫秒 | 6.032秒 | 7/6 |
| 2019 / A配置`b13c7e3b…` | synthesis | 01:10:53.057792 | 249.806 | 142.84毫秒 | 62.129秒 | 11/10 |

三条均为manager、grok-4.7/xhigh、HTTP200已接受、direct/revision0、HTTPX、TLS验证开启、无CONNECT。它们没有terminal或完整HTTP EOF，异常类型链均为`[ReadError, ReadError]`、cancelled_by=null，保留`accepted_or_sent_request_outcome_unknown`且未重发。provider/credential revision均为1，role/prompt version均为3，同一team配置标识并不代表共享客户端。

A公共材料收到132,523字节和213个完整SSE事件；B配置收到11,983字节却尚无完整SSE事件或有效事件，6.032秒即失败；A配置收到1,592,317字节和6523个完整SSE事件。B未到90秒首事件限时，A两条首事件已及时到达；三条均未到300秒语义空闲、900秒请求硬限或3600秒整轮期限，也未达到8MiB/20000事件上限。无terminal，所以terminal后的tail drain均未进入。未保存的半截正文不能据字节数推断为有效SSE或供应商业务错误。

A配置的最后语义进展距异常约62.129秒，但最后正文块只距异常142.84毫秒，期间正文数据并未静默50秒。**三条均与environment约50秒正文静默后的RemoteProtocolError分开分类。** 它们表明direct绕过该代理路径不保证永远不发生其他读取错误。

## 任务独立性与关闭顺序

B公共材料`efe944ba…`在01:02:54.562658完成（11/11EOF）；A公共材料约01:03:17.108573仍记录正文块，晚约22.546秒，随后才ReadError。该正文时刻由UTC起点加单调时钟经过量推算，是近似解码时间，不是包到达时间。新配置任务直到01:03:24.757起才创建，晚于A原异常7.464秒。

B配置01:05:31.935429进入failed时，A配置仍运行；A最终失败的汇总请求直到01:06:43.253265才开始，晚于B终态71.317836秒，并又收到HTTP200、持续约250秒。两条配置任务与原A任务的所有模型请求均由manager发出，没有子角色请求。事件未记录取消、版本替换或停机中断；A/B各自的model_error先于本任务agent_failed和job failed。

冻结代码每次run_research创建新ResearchRuntime；manager创建新ModelClient，后者创建独立httpx.AsyncClient及传输池。Service按job_id管理active任务，完成B只移除B；Runtime.close只取消/await自己子任务并关闭自己持有的客户端。团队子角色使用独立client，遗留共享也仅在同一Runtime内；全局native capability缓存只存状态字典，没有客户端或连接池。配置探针构造/关闭的是另一个未发请求的客户端。

正常Runtime finally/close在本任务结果或异常处理之后。代码与上述时序未发现“另一账户完成就关闭当前账户共享client”的确定路径。但未持久运行时client对象标识、显式close意图或socket元组，不能把静态审阅提升为绝对排除所有本地关闭。此前受控client.aclose确实可产生ReadError/null；该实验只证明可能性，不证明这三次的实际原因。

## cause链丢失的具体位置

AnyIO/httpcore可将本地ClosedResourceError和传输BrokenResourceError映射为ReadError。已安装`httpcore/_async/connection_pool.py:401–407`的PoolByteStream在异常后关闭流并执行`raise exc from None`，清掉显式cause；HTTPX再包装成外层ReadError。应用目前只沿`__cause__`收集类型，因此两层ReadError不能证明底层没有ClosedResourceError、SSL错误或errno。

`receive_response_body.failed`在该清理之前产生，可承载原异常；b7仅保存阶段/时间，未保存此时`info['exception']`的细分类，也未记录response_closed或显式client.close意图。后续清理不能倒推成原始关闭原因。这三条均没有可关联的现场关闭方向抓包；此前environment代理先FIN证据不能移用于direct，未赶上的抓取也不能追认为有效样本。

## 下一次有针对性观察的最小方案

本轮保持b7。三条关闭发起方仍未知：本地资源/close、TLS/socket/OS、远端或中间层都未被唯一证实；不归责模型可用性，不盲增超时，不自动换路线重放结果未知请求。

下一独立冻结可加少量只读诊断：

- 在failed trace回调、httpcore清cause之前，保存首个失败的阶段和单调时间、有界类型/模块链、整数errno/winerror，以及经严格短标识校验的SSL reason/library和整数verify_code；不保存str(exception)、args、原始错误消息、头部、凭据或正文。
- 在ModelClient.aclose/Runtime.close真正执行前，记录随机客户端标识、现有任务/角色/attempt ID、首个close意图时间及allowlist原因；另记response_closed顺序。错误事件已持久时使用独立关闭事件，不回写历史记录。
- 仅用本地TLS夹具区分client.close、task.cancel、服务端close/reset，并核对敏感错误文本不会进入诊断、首故障不被清理覆盖。未来观察必须是新合法请求，不能为取证重放旧未知请求。

这些字段有诊断价值，无法回补本轮已丢失的信息，也不是已证实根因的修复；本轮未实施，更没有再跑矩阵凑成功。

## direct最终分母独立核对

已结束矩阵在seq2021共168次模型HTTP：165条完整协议终止+HTTP EOF，3条ReadError；状态码全部200。168条全部direct/revision0、HTTPX、TLS验证开启，两处CONNECT true均0、冲突0。manifest、首次启动、重启运行版本和当前磁盘源码摘要均为b7，manifest记录source_unchanged=true。非模型公共对照2个GET不在此分母。费用未知为null；165/168不能写成全部通过，也不能据此保证direct无未来断流。
