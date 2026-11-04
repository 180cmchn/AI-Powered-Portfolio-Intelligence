# 本地流读取与主动关闭独立调查

复核时间：2026-11-04T16:16:25.671257+00:00。冻结源码 `b7afd9860b14304d6c1f698e7fbaed5db80435d37c6f90db920000324c806470`；HTTPX 0.28.1、httpcore 1.0.9、AnyIO 4.15.1、h11 0.16.0。生产代码、测试文件及日常环境均未修改；未重发真实模型请求。本机实验代码/日志位于 `/private/tmp/sixteenth-stream-lifecycle/`，完整脱敏时序与脚本哈希见 [JSON](local-stream-lifecycle-review.json)。

## 当前结论

供应商反馈是请求方断开，这与本次调查并不冲突。**RemoteProtocolError 和 cancelled_by=null 不能证明模型上游不可用，也不能排除本机主动关闭。** 受控本机代理主动关闭、直接关闭本地 socket 都重现了同样的异常组合。原四次及后续第五次真实失败均呈现约50秒的正文无新字节边界；尚未确定该边界由本机代理、其传输链路的哪个具体部件执行。

已检查的 Python 请求链未发现隐藏50秒读取限制。HTTP、开启证书校验的直连 TLS，以及现有 Mihomo 入口经 DIRECT 到本机 TLS 的控制请求均静默55秒后完整成功。正常应用取消与应用idle超时在受控实验中保留明确归属；没有证据把这四次请求改署为这些已知取消类型。

## 四条持久证据

| 事件 seq / 阶段 | 请求总秒数 | 最后正文块后秒数 | 最后语义进展后秒数 | 硬限时剩余秒数 |
| --- | ---: | ---: | ---: | ---: |
| 408 / research | 112.764 | 49.709 | 74.270 | 787.236 |
| 590 / research | 234.771 | 50.015 | 51.487 | 665.229 |
| 1423 / synthesis | 235.748 | 49.921 | 66.990 | 664.252 |
| 1722 / synthesis | 414.654 | 49.920 | 88.490 | 485.346 |

四条均为HTTP200已接受、RemoteProtocolError、未收到terminal、HTTP正文不完整；语义idle上限300秒、硬截止900秒，首个有效事件均已及时到达，子任务/整轮预算也尚未到界。累计输出最大约1.94MB，小于8MiB；最大7636个事件，小于20000。终止后的1秒tail drain从未进入，不能解释terminal之前这四次中断。最后字节字段来自 `response.aiter_bytes()` 解码后的正文块，不等同每个TCP包；它提示50秒边界，不能独自定位执行者。

## 生命周期与依赖路径

1. [ModelClient._post](../../app/model.py) 对HTTPX明确设read=None，应用另用90秒首事件、300秒语义idle与900秒硬期限。HTTPX的read超时是等待下一块数据的独立限时；本项目没有使用其默认值。[HTTPX官方超时说明](https://www.python-httpx.org/advanced/timeouts/)。
2. `_receive_stream` 仅在收到完整terminal后进入短暂tail drain；未知半截不会触发主动终止返回。异常退出 `httpx.AsyncClient.stream` 才在finally调用Response.aclose。
3. 已安装httpcore的 `HTTP11ConnectionByteStream.__aiter__` 在Trace作用域内读取；读取异常先触发receive_response_body.failed，随后在取消屏蔽区调用response_closed清理，再抛原异常。由清理发出的关闭不能倒推为原始原因。
4. 应用trace目前只保留connect/tls/headers/body阶段，未保留response_closed或ModelClient.aclose调用点。因此旧4条证据缺少完整的主动close归属记录，不能仅凭cancelled_by=null作绝对排除。官方trace接口提供这些事件；精确事件集合须按安装版本核对。[HTTPCore trace文档](https://www.encode.io/httpcore/extensions/#trace)。
5. `Runtime.run` 的finally在主循环结束后关闭客户端；`Runtime.close` 先取消并await子任务，再关闭各自客户端。已完成子任务本身没有提前关闭父客户端。Service的配置可用性检查构建并立即关闭的是另一个未发请求的客户端。生产团队角色构造独立ModelClient，不共享一个可被其他账户关闭的实例。
6. httpcore开始活动HTTP/1.1请求时设置ACTIVE并清空keepalive expire_at；连接池清理不会因静默正文把活动连接视为普通空闲连接。四次失败之一有本次新CONNECT，其余没有新CONNECT，纯“复用旧连接”解释不足。
7. AnyIO的显式stream.aclose设置closed状态，受控HTTP/TLS中读端得到ReadError；底层socket接收方向shutdown可呈EOF，h11对缺失chunked结束符生成RemoteProtocolError。TLS适配使用standard_compatible=False，异常TLS EOF也可变为EndOfStream；协议异常名不标识真正关闭方。

## 临时受控实验

均使用生产ModelClient，无真实供应商、无MockTransport。仅临时子类包装trace与关闭方法记录类型和时间；没有改变正常读流逻辑。TLS使用临时CA，未关闭证书或主机名验证，未写系统信任。短idle例仅为受控故障触发，55秒控制保持生产300/900秒预算。

| 场景 | 请求结果 | cancelled_by | 时序要点 |
| --- | --- | --- | --- |
| peer_fin | RemoteProtocolError | null | 夹具源端先close，随后failed→response_closed |
| client_aclose | ReadError | null | 明确client.aclose先于ReadError |
| task_cancel | cancelled | user_cancelled | cancel先于CancelledError→响应清理 |
| application_idle_timeout | stream_idle_timeout | stream_idle_timeout | 应用timer取消→失败→清理→ModelTimeoutError |
| raw_socket_shutdown | RemoteProtocolError | null | 本地socket接收方向shutdown→协议EOF错误 |
| local_proxy_idle | RemoteProtocolError | null | 本机代理idle主动关闭双腿；源端看到请求方EOF |
| stable_idle_55_seconds | 成功 | null | 55秒无字节后terminal+HTTP EOF成功 |
| tls_client_aclose | ReadError | null | 证书验证TLS下主动client.close仍为ReadError |
| tls_raw_socket_shutdown | RemoteProtocolError | null | 证书验证TLS下socket shutdown仍可协议EOF |
| tls_stable_idle_55_seconds | 成功 | null | 证书验证TLS下55秒静默后完整成功 |

本机代理例中的源端观察到请求方EOF，应用同时得到RemoteProtocolError/cancelled_by=null，足以说明供应商反馈与现有异常可以同时为真。此例采用明文HTTP转发代理和0.2秒受控idle，只证明机制；它不是当前Mihomo或真实TLS链路配置的实测结论。直接TLS关闭/55秒控制另行完成。

本机代理例关键顺序（秒）：

- 0.215290：local_proxy_idle_close
- 0.215447：origin_observed_client_eof
- 0.215561：http11.receive_response_body.failed
- 0.215723：http11.response_closed.started
- 0.215819：http11.response_closed.complete
- 0.216006：response_aclose_called
- 0.216104：model_client_aclose_called

## 修复范围与最小后续验证

当前不应盲改超时、自动换路线或重发四个结果未知的请求。需要先把50秒边界定位到实际本地关闭部件；若证实代理/所选链路的idle政策，修复该具体配置/路径，并独立验证，不能把未核实的网络假设写成模型供应商故障。

生产诊断可最小增加：明确关闭发起点/原因/时间、response_closed与receive_response_body.failed的有序记录、已知本地主动close的归属。保留错误主因，清理异常另记；旧upstream_transport_error可兼容保留，但应增加initiator=undetermined，不能把字段名当归责。可为Runtime的cancel→await→close顺序补并发事件栅栏回归，保证其他角色或账户不能误关在途连接。

最小回归：HTTP与TLS各自验证本地主动close/任务取消/应用timeout/中途EOF；受控CONNECT代理idle关闭；direct与不主动关闭的CONNECT静默超过50秒并覆盖连接池复用。上述先在本机夹具完成；定位实际部件后，只用明确身份的新真实请求校验关联ID与边界trace，不暗中重放旧未知请求。

单看TCP首个FIN方向仍有局限：TLS close_notify或接收EOF可能先发生，Python随后正常清理发送FIN。应合并读端EOF/异常、应用close意图、代理两腿与FIN时间；不要只凭FIN发送方就宣称应用超时或取消。

## 第五次失败的抓包补证

主代理已完成仅保留包头/方向/长度的lo0抓取（未保存payload），独立SQLite复核对应事件seq2043、任务`bb3bbd42e2954c56aba6ebc34307aed5`，于2026-11-05 00:15:35.123992+08:00记录RemoteProtocolError，cancelled_by=null；正文块到错误49.921秒。抓包文件身份及哈希保存在JSON。

本机7890代理先于00:15:35.120369发送FIN，应用端口49550在2.575毫秒后发送FIN；上一次带payload包为00:14:45.202233（35字节），至首个FIN为49.918136秒，期间TCP keepalive得到ACK。**这一新样本可排除应用首先主动关TCP**，确定本机代理腿先关闭；尚不能仅据此区分代理本身空闲政策与其外部节点/传输腿的起因。不能将第五次证据倒签成前四次已抓包，也不能归责模型可用性。

当前应保持b7冻结实证。现有字段即可离线派生最后正文块空闲时长，无须修改源码。response_closed及显式client.close意图/时间字段值得下一次独立冻结增加，但属于提高可诊断性，本身不会修复已经观察到的代理先FIN。继续验证具体网络路径时使用独立正常入口、新任务身份和明示路线，不重放旧未知请求、不改日常设置。

## 现有 Mihomo 入口的 55 秒本机 TLS 控制

执行前通过现行 Unix controller 只读查询 `/configs` 与 `/rules`，确认 rule 模式、mixed-port 7890、TUN 关闭。按顺序检查字面目标 `127.0.0.1`：此前511条规则均不匹配，且没有无法判断的规则类型；首次匹配为第511索引的 `IPCIDR 127.0.0.0/8 → DIRECT`。确认后才向7890发起一次 CONNECT，避免把本机目标发至外部节点。请求期间再只读 `/connections`，精确匹配临时目标端口51326，得到唯一连接 `chains=[DIRECT]`，匹配规则仍为127/8。

生产ModelClient使用`environment`路线和现有本机代理，代理相关变量及临时CA只在该实验子进程设置；TLS证书与主机名验证保持开启。临时本机TLS服务发出created/delta后，正文静默55秒，再发送完整`response.completed`及HTTP chunk结束符。仅发送固定的测试文本，无真实模型请求、业务数据或供应商凭据。

结果为成功，模型请求耗时55,009.62毫秒，CONNECT返回200且established=true，HTTP正文完整、close_reason=http_eof、cancelled_by=null。源端在55.006276秒发送terminal，HTTP读取在55.008470秒完成；之后才进入response.aclose及最终ModelClient.aclose清理。原始脱敏时序、执行前预检及脚本哈希已并入JSON。

这一结果证明**现有Mihomo本机入口加本地DIRECT路径不会必然在50秒正文静默时断流**，并不能证明真实请求所选外部代理节点链健康。真实第五样本仍是本机7890先FIN；剩余边界包括外部节点/传输腿的关闭及其与代理链的交互，不能仅据控制通过断言具体外部部件已确定。未修改代理规则、系统网络、信任库、日常配置或冻结源码。

## 用户提供的供应商侧时间相关性

用户补充的供应商侧断流时间，与本地失败记录在同一秒吻合（Asia/Shanghai，+08:00）：

| 用户提供的供应商侧时间 | 本地finished_at | 事件seq / 任务 |
| --- | --- | --- |
| 2026-11-04 23:48:05 | 2026-11-04 23:48:05.436916 | 1423 / `15d837b14dda4883b71648ef6a93a0c5` |
| 2026-11-05 00:02:00 | 2026-11-05 00:02:00.229066 | 1722 / `cd93c5fd7a3541bd95175f1df6225b1e` |
| 2026-11-05 00:15:35 | 2026-11-05 00:15:35.123992 | 2043 / `bb3bbd42e2954c56aba6ebc34307aed5` |

最后一条同时具有本机代理先FIN的包头证据。以上是**用户提供的供应商侧时间相关性**；未取得供应商request ID、原始日志、时钟偏差证明或完整生命周期trace，不能称为完整provider log审计，也不能仅由时间吻合确定链路中的具体关闭部件。

## 公共端点双路补充对照

随后严格只执行environment/direct各一次无认证Httpbun请求。经旧模型请求同一选中代理链，及时首字节之后50.039565秒断流；direct在两字节间54.999367秒及最后字节后55.015729秒静默后完整结束。两者HTTP200、chunked、TLS验证开启。它把怀疑范围收窄到选中外部代理路径或远端交互，仍不唯一定位组件。参数、离线路径规范化修正及完整边界见[公共端点对照](public-idle-route-control.md)。
