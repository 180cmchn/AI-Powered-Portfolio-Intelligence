# Direct 矩阵 B 配置变更：内容未评估

**本任务失败且没有发布报告，内容为not_evaluated，accepted=null。** 不能使用前两份B报告或可恢复checkpoint替代本次成果。

- 任务 `a9caca3257d6490f9780d0488b179385`，B账户 `08b7c5c7f2524e67aa8232d3e0607581` 输入v2，原因为configuration_changed。
- 北京时间2026-11-05 01:03:25启动，01:05:31失败；任务配置实际为direct、`scheduled_research_enabled=false`。
- 固定源码 `b7afd9860b14304d6c1f698e7fbaed5db80435d37c6f90db920000324c806470`，仅只读隔离数据库，没有启动模型、请求恢复或修改DB。

本任务共7次模型HTTP：前6次completed+EOF，第7次在research阶段发生ReadError。失败请求UTC时间为2026-11-04 17:05:25.836444至17:05:31.868435（北京时间次日01:05），持续6032.4毫秒。HTTP状态200，约5932毫秒收到响应头；记录output_bytes=11983，但没有有效SSE事件、协议终止事件或完整EOF，接收响应体约6031毫秒时报错。错误码为`upstream_transport_error`，`cancelled_by=null`，并明确禁止自动重发已发出或已接收但结果未知的请求。

此次约6秒的失败早于配置的首事件90秒、流空闲300秒和硬期限900秒；现有日志不支持本地预算超时或主动取消解释。**HTTP ReadError本身不能确定谁先断开**，必须与另行抓包、连接时序证据结合。route=direct只是已验证的请求配置，不能据此推定完整物理路径，或者宣称绕过环境代理后所有问题都消失。

该job的analysis/report行数为0、新预测行数为0。应用结果为incomplete、contract_verified=false、content_assessment=not_evaluated。有1份可恢复checkpoint，只能证明保留了部分研究，并非已提交报告；复核没有触发恢复或增加模型调用。账户仍只有期初现金账，旧报告未改写。

已知成功请求的token和供应商ticks保存在机器记录；失败请求usage缺失，实际账单费用未知，不能把成功子集之和冒充全部费用。此次失败必须保留在direct总分母中，也不改变environment原有170次/165EOF的历史。

证据、请求边界与安全配置字段见[机器记录](direct-b-configuration-content-review.json)。另两次实际成果见[direct初始B](direct-b-cash-content-review.md)、[direct公共材料B](direct-b-public-materials-content-review.md)；公共材料的持久需求覆盖仍仅2/4。
