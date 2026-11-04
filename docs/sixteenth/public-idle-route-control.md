# 公共端点静默对照：选中代理链与 direct

本次只执行两条请求，environment 与 direct 各一次，未重试，未请求真实模型。两者使用生产 `app.model_network.model_http_options` 构造独立 HTTPX 客户端，无供应商配置、Authorization、Cookie、业务数据；HTTP/1.1、证书及主机名验证开启，read=None。源码保持 `b7afd9860b14304d6c1f698e7fbaed5db80435d37c6f90db920000324c806470`，未改变网络或日常配置。完整脱敏时序、路径哈希与脚本哈希见 [JSON](public-idle-route-control.json)。

## 结果

| 路线 | 首字节 | 字节间静默 | 结果 | HTTP正文 |
| --- | ---: | ---: | --- | --- |
| environment，经旧模型请求的同一选中代理链 | 0.631988秒 | 首字节后50.039565秒断流 | RemoteProtocolError，总50.671553秒 | 只收到1/2字节，无完整EOF |
| direct | 0.631862秒 | 两字节间54.999367秒 | 完整成功，总110.646958秒 | 收到2/2字节；第二字节后再55.015729秒收到EOF |

两条均为HTTP200、chunked、无Content-Length，并且证书及主机名验证均实际开启。没有首字节延迟、限流状态或两字节被一次性缓冲的现象。environment在途连接有CONNECT200，匹配Match规则、两跳链；与历史模型连接49550/49551日志中的 `using group[node]` 一致，仅保存规范化哈希 `2b7eb76697fa05be69808ed5b57c634c7d80b60c445d5e0c99a993c54b620501`。direct没有CONNECT。

environment顺序为：50.669566秒receive_response_body.failed → 50.670390秒response_closed.started → 50.671555秒显式response清理。此请求在应用显式关闭前已读取失败；130秒总观察上限和15秒首字节观察上限均未触发。正文只核验固定测试字节的数量与类型，没有保存正文。

## 设计与边界

[Httpbun官方说明](https://httpbun.com/)允许drip duration最长300秒。[当前公开实现](https://raw.githubusercontent.com/sharat87/httpbun/main/routes/routes.go)按 `duration/numbytes` 间隔等待，每次先写星号；[BodyWriter](https://raw.githubusercontent.com/sharat87/httpbun/main/response/response.go)写后立即flush。正式发送前，主代理把原拟55秒/1字节改为110秒/2字节、总观察上限130秒，以避免Content-Length=1让客户端首字节后立即判定完整。这一变更发生在任何drip请求发出之前；最终两条请求参数均为duration=110、numbytes=2、delay=0，各附无敏感nonce。

执行前只读检查当前规则、确认目标公网DNS和既有127.0.0.1:7890代理；实际路径再由请求在途 `/connections` 核对。历史日志使用logfmt引号，临时脚本的初次哈希把末尾引号计入，导致原始comparable标记为false；随后仅离线用shlex解码日志，规范化历史链与已捕获实际链完全匹配，未重发请求。原始日志保留，JSON的offline_route_correlation记录修正依据。

临时脚本的原始eligible_control还要求完整收到两块数据，实际是完整成功条件；它不应被用于否定收到及时首字节后断流这一有效失败观察。JSON的assessment分别记录有效失败与有效成功，明确覆盖原始字段的命名不足，没有隐藏原始记录。

**同一选中代理链在非模型公共端点也重现了约50秒正文静默断流，direct则跨过55秒静默完成。** 结合先前“既有Mihomo入口加本地DIRECT”55秒成功，怀疑范围进一步收窄到选中外部代理路径或其与远端传输的交互。仍不能据此确定Mihomo本体、外部节点或链中某个部件为唯一原因，也不能宣称所有请求都有固定50秒政策，或归因于模型可用性。

此处仅每路一次。公共托管端点不保证可靠性；不同路径可能涉及不同远端边缘或处理，因此不能排除路径与远端交互。本次未抓包；第五次真实模型请求的代理先FIN证据属于另一独立样本。运输对照也不能替代正常入口真实业务矩阵。
