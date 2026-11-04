# 第十六轮：正常运行的模型连接路线

运行设置增加 `model_network_route`，默认 `environment`，可显式选择 `direct`。修订由服务器维护，完整 GET→PUT 可以回传当前修订进行冲突检查，不能指定未来或历史修订。新任务领取时冻结路线及修订，各角色、公共研究、追问、恢复与汇总继承同一配置；目录发现及显式能力探测读取各自开始时的设置。目录缓存按路线/修订失效，旧请求迟到不能覆盖新路线目录。此变更不增加数据库列，schema 仍为 11。

`direct` 将模型客户端 `trust_env=False`，同时显式构造启用证书/主机名校验的 SSLContext，按 SSL_CERT_FILE → SSL_CERT_DIR → certifi 选择 CA；没有将行情、材料/PDF 客户端接入该策略。供应商仍仅有 API Key / Base URL / 协议，模型与 effort 仍从角色绑定读取。无自动切线或未知结果自动重放。

实际 HTTPX 版本 0.28.1；已对照所安装的 `create_ssl_context` 行为与官方 [环境变量](https://www.python-httpx.org/environment_variables/) / [Transports](https://www.python-httpx.org/advanced/transports/) 文档。没有新依赖；certifi 已是锁定的 HTTPX 依赖。

`model_network` 请求诊断分开记录选择、修订、环境代理/NO_PROXY 是否存在、CA 来源标签、是否在此请求观察到 CONNECT。代理凭据、代理 URL、认证头和 CA 路径/内容不进入该记录。连接池复用时后续请求可能没有第二个 CONNECT；无 CONNECT 不证明物理链路直连。系统 VPN、TUN 与外部网络设备不可由应用判定。注入的自定义 transport 明确标记 injected，证书状态为未知而非假称已验证。

验证仅采用随机回环端口、临时 SQLite、受控模型 HTTP 响应、自建短期 CA 与本机 CONNECT 代理，不代表第三方模型内容验证：

- [最终路线回归](../../test-results/sixteenth/model-route-final-settings-compatible.txt)：10 passed / 9.65s。包含 environment、direct、NO_PROXY、错误主机名与不受信 CA 拒绝、环境 CA 目录、多个供应商、任务角色快照、缓存/迟到响应、正常 start.command→manage.py 子进程保存/重启，以及正常 worker 通过 HTTP 生成并持久发布受控报告。
- [设置兼容回归](../../test-results/sixteenth/model-route-settings-compatibility.txt)：32 passed / 1 warning。覆盖既有完整设置回写与评估器配置。
- [相关历史回归](../../test-results/sixteenth/model-route-related-final.txt)：169 passed / 48.50s。是定向组，不与全套用例数机械相加。
- [实际浏览器](../../test-results/sixteenth/model-route-browser.txt)：frontend_eleventh 全组通过，新增路线选择→保存→API 回读→刷新显示与修订断言，保留原滚动/选区/输入、账户隔离和交易断言。此组运行在 revision 回写兼容修之前，UI 提交不含只读 revision；最终浏览器全套由本轮总记录关联源码。

失败记录保持：最初默认沙箱禁止绑定本机端口，7 项失败、1 项错误、1 项通过（工具输出记录）；解除沙箱后 [首次路线夹具](../../test-results/sixteenth/model-route-first.txt) 有 7 项失败、2 项通过，原因是测试使用了不存在的 close 而非 aclose，以及错误要求连接池第二请求再次 CONNECT。修正测试，未更改生产时间保护，[第二次 9 项](../../test-results/sixteenth/model-route-second.txt)通过；加入迟到目录测试后[10 项](../../test-results/sixteenth/model-route-final.txt)通过。历史相关组[首次](../../test-results/sixteenth/model-route-related.txt)168 通过、1 项因沙箱 loopback 拒绝失败，解除限制重跑 169 通过。全设置 revision 回写兼容问题在全量准备阶段发现并修复，未修改断言回避旧 API 合约。

未修改日常数据库、凭据、角色绑定、系统代理、DNS、证书或日常服务。正常用户操作步骤见 [Mac 手册](../mac-operations.md#模型连接路线第十六轮)。
