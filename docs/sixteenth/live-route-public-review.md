# 第十六轮真实路线与公共任务独立复核

只读快照时间：2026-11-04T15:53:49.556335+00:00；持久事件截至 seq 1677。数据库为 `data/evaluations/sixteenth-normal-matrix-v1/evaluation.sqlite3`，矩阵冻结源码摘要为 `b7afd9860b14304d6c1f698e7fbaed5db80435d37c6f90db920000324c806470`。取数在一个 SQLite 只读事务内完成；后续合法任务仍可继续，本文不是整个矩阵的最终收口结果。结构化脱敏证据见 [JSON](live-route-public-review.json)。未改生产/测试代码、数据库或服务，也未触发模型调用。

## CONNECT 诊断结论

**不需要修复。** 首次观察的123条持久请求中，15条两处都记录 CONNECT，108条两处都没有新 CONNECT，字段冲突为0。最初怀疑来自较长输出中相邻请求混读，不能据此宣称同条记录冲突。

本次取数共 144 条模型 HTTP 尝试：嵌套 `model_network.proxy_connect_observed` 与 `proxy_tunnel.observed` 均有 18 条 true，均成功建立隧道；126 条没有新 CONNECT，冲突为 0。全部配置为 environment / revision 0，transport=httpx，证书校验开启。

[model_network.py](../../app/model_network.py) 为每次请求提供初始 false；[model.py](../../app/model.py) 的 CONNECT 请求头 trace 在同一分支同步把两处置 true，随后 Agent 深复制事件并持久化。因此存储的值是每次请求的最终观测，不是遗漏更新的客户端配置标签。复用已有连接池隧道时没有新 CONNECT 属于正常情况；false 不能解释为直连。配置 environment、实际观察到 CONNECT、没有新 CONNECT、操作系统 VPN/TUN 或物理中间层是不同事实。

## HTTP 与失败边界

本快照全矩阵共 141 条完整协议终止且 HTTP EOF；状态码分布为 `{'200': 144}`，错误码分布为 `{'None': 141, 'upstream_transport_error': 3}`。JSON 按任务分别保存这些分母，并列出所有已持久失败的任务、角色、事件序号、时间与错误码。未知结果不能因 HTTP 200 变为成功；持久请求记录也不包含尚在途且未发出 usage/error 事件的请求。费用未知为 null，不把无账单写成零。

## 公共报告与需求覆盖

公共 run `a3c848be78c94abfac8f940431128de5` 于 `2026-11-04T23:47:16.244058+08:00` 完成；11 次 HTTP 全部有 completed 与 EOF，未发现该 run 模型错误。交付 `limited`、scope `['SZ:159278']`，结构契约通过，内容仍为 `not_evaluated`。报告 2355 字符，SHA-256 为 `4afa76901ce4b90a00e7c63c95aee3f487cafd3445728566f9a2e7da01333556`；本文不复制大正文。

发布快照的 expected=55 / completed=0 / uncovered=55 不能当作当前状态。公共初始冻结需求只有4009（universe_changed），模型两次读取正确ID并提交 covered_demand_ids=[4009]，没有ID错用。它选择证券 scope，而 [complete_task_demands](../../app/scheduling.py) 的既有保守规则只让 account/public/cash_only清普通整体需求；证券 scope只接受有明确关联的 recommendation_review，因此4009被列为 unaccepted，仍为 uncovered。

这是**既有保守覆盖政策与模型有限范围选择**，现有证据不足以判为本轮必须修的确定生产缺陷。`test_fourteenth_publication.py::test_limited_security_scope_cannot_complete_account_wide_demand` 明确保守账户规则；`test_public_submission_requires_action_and_explicit_demand_coverage` 明确公共报告完成不代表需求完成。[Store.delivery_scope](../../app/store.py)允许 public 或具体证券范围，但未找到“公共候选恰好只有一个时证券 scope 应自动清 universe_changed”的专门契约或测试。可后续补明确说明与专门回归；自动清除需要显式政策，不能为了矩阵通过改写本次历史回执。

其他54项是任务执行中新获取的公开原始材料所绑定需求。发布后关联路由将它们逐项确认处理：当前需求状态 `{'completed': 54, 'uncovered': 1}`；路由状态 `{'awaiting_retry': 52, 'routed': 2}`。其中52项 awaiting_retry表示关联仍不确定、留待后续明确事件/研究判断，既不等于52次模型重试，也不代表52份原文均已研究。只有2项完成路由并向相关账户派发原始材料需求。报告中的发布瞬间覆盖快照与后续路由状态应同时呈现，不相互覆盖。

## 来源隔离与阅读证据

公共上下文使用 account=None、input_version=None、shared_research=[]；该 run 持久 Agent 事件的 account_id均为空。结果中的78个 material_delivery_ids实际是**交付回执ID**，不是78个材料ID：全部可在 research_material_deliveries找到，共57个不同材料，全部 public，account_id均为空；没有账户私有范围或 agent_extraction/generated_analysis材料。

其中69条只是原生搜索链接元数据，9条实际交付可见内容（5条结构化数据、3条正文片段、1条全文结果）。这些次数是交付次数，不能称78份原文已读。公共原始资料可由A先取得而共享，catalog的来源owner非空本身不意味着私有。引用和材料范围没有据此错误排除可共享原文。

本公共报告本身没有生成 material_notification；快照时global执行仅1次，未观察到报告自触发公共模型循环。合法账户后续原始材料任务仍由正常worker处理，未加单任务阻断。存储未保留每条原始出站prompt，故本结论是实际回执、持久上下文身份与代码路径的核对，不冒称逐包证明全量网络内容，也不代替未来持续运行验证。
