# Direct 矩阵 B 复核索引

三项已绑定B任务合计 **29次HTTP、28次completed+EOF、1次结果未知的ReadError**。两份报告达到有限账户内容最低要求；配置任务没有报告，内容未评估。此索引只覆盖这三项B任务，不代表整轮或后续任务全部通过。

| 阶段 | 结果 | 模型HTTP | 保留边界 |
| --- | --- | --- | --- |
| [初始现金B](direct-b-cash-content-review.md) | 报告`e9cecf33d4b9446f8dc3f37d874e1443`，有限内容接受 | 11/11 EOF | 流动性只获全天容量支持；0.786是收盘低点。 |
| [公共材料B](direct-b-public-materials-content-review.md) | 报告`ca5bb5d28aa64a42800b3f016157e668`，有限内容接受 | 11/11 EOF | 需求仅2/4完成；单证券scope的保守规则保留两项public_information未覆盖。 |
| [配置变更B](direct-b-configuration-content-review.md) | 任务`a9caca3257d6490f9780d0488b179385`失败，无报告、not_evaluated | 6/7 EOF | 第7次HTTP200后约6秒ReadError，无有效SSE终止或EOF；无本地超时/取消证据，不据此单独判定谁先断开。 |

账户均为输入v2的现金100000、无持仓，没有交易。两份报告合计6条探索弃权；正式预测及方向样本为0。已知成功用量不能覆盖失败请求缺失的usage，实际费用仍未知。

源码仍为`b7afd9860b14304d6c1f698e7fbaed5db80435d37c6f90db920000324c806470`，数据位于独立`data/evaluations/sixteenth-normal-direct-v1`。原文/结果哈希均只读复核，应用not_evaluated未改；完整最终模型材料输入未持久化，阅读范围限制仍保留。

**direct没有消除本轮所有传输失败。** 这是一轮独立、有界解决方法验证，不是同payload严格对照或供应商可用性保证。断流发起方以单独网络证据为准；[environment原索引](environment-b-content-review-index.md)及其失败分母不受此次结果替代。

各任务、哈希、用量及分项状态见[机器索引](direct-b-content-review-index.json)。
