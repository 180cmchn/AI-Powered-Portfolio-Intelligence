# direct公共任务与路线独立复核

只读事务快照：2026-11-04T16:57:44.672000+00:00，持久事件截至seq 1688。数据库为`data/evaluations/sixteenth-normal-direct-v1/evaluation.sqlite3`，冻结源码`b7afd9860b14304d6c1f698e7fbaed5db80435d37c6f90db920000324c806470`。公共任务已终态，其他正常任务仍可继续；本页不是direct整个矩阵的最终计数。结构化脱敏证据见[JSON](direct-public-review.json)，字段与[environment公共复核](live-route-public-review.json)对应。

## 路线与终止

本快照全矩阵已持久143条模型HTTP，全部143条具有完整协议终止和HTTP EOF；全部direct/revision0、HTTPX、TLS证书校验开启。两处CONNECT标签均为false，冲突0；环境代理仍存在的诊断不改变direct客户端的冻结路线。当前设置及每个已保存任务配置另列于JSON，逐请求证据只保留身份、时刻、终止及网络诊断，不含认证或正文。尚在途且未持久usage/error的请求不计入此分母。

公共run `1f1602bc10684da5995b8dfc414a4637` 于`2026-11-05T00:52:58.207748+08:00`完成，模型HTTP为39/39完整，报告model_calls=39，没有该任务模型网络失败。最终交付是**limited**，scope=['SZ:159278']；contract_verified=true，content_assessment=not_evaluated，不能据此宣称内容已全面验证。报告3470字符，SHA-256 `b64ecb6bd02229ffd5cfdfc9d3e3f66d58b336761c9478ea1bc26b81e0cc3ced`，不复制正文。

预算收尾在seq1428保留2次经理HTTP额度，并等待当时两个活动子任务；seq1441记录3份子报告均已完成，unfinished_child_ids为空。最后交付仍列出7项限制，包括尚未吸收的行业结论、数据或原文缺口。完整网络成功与有限研究交付是不同结果。

## 发布快照与当前需求

发布瞬间expected=305、completed=0、uncovered=305，unaccepted_claims=[2058]。初始冻结scope、read记录与模型claimed ID均为2058，类型universe_changed，没有需求ID错用。报告选择具体证券范围，而`complete_task_demands`仅允许account/public/cash_only范围清整体需求；证券范围只承认可明确关联的recommendation_review。因此2058当前仍uncovered。

其余304项是研究中新公开资料关联的public_information需求。后续路由已将304项作处理确认，所以当前需求状态为`{'completed': 304, 'uncovered': 1}`；这不表示304份资料均已研究或全部相关性已闭环。材料路由为`{'awaiting_retry': 302, 'routed': 2}`，302项awaiting_retry代表关联仍未确定、留待后续，不等于302次模型重试；2项routed向相关账户派发公开原始材料需求。发布时覆盖快照和当前路由状态都应保留，不能彼此覆盖。

这一边界与environment公共任务一致：是既有保守政策和模型有限范围选择的结合，现有证据不足以判为必须修的生产缺陷。不得为了把completed变成“全需求覆盖”而事后改写回执。未来若要改变单证券公共报告对universe_changed的覆盖语义，应单独明确契约并补专门回归。

## 来源与交付隔离

公共上下文account=None、input_version=None、shared_research=[]，持久事件account_id均为空。最终经理报告声明46条材料**回执ID**，全部找到，涉及40个不同材料；其中28条仅元数据，18条内容交付。这个口径与environment复核中的“最终报告声明回执”一致。

另扩展检查全公共任务及其子角色的474条材料回执，涉及329个不同材料：全部public，私有范围0，generated_analysis/agent_extraction输入0；416条仅元数据，58条实际内容交付。类型分布`{'native_search_source': 416, 'original_document': 28, 'text_representation': 30}`，账户字段均为空。交付次数不是独立文档数，更不是同等数量全文已读。

还有6条报告交付：本公共任务内部3个子报告的完整文本/摘要交付，source_job_id均为本run，跨任务或跨账户报告输入0。这些是经理汇总自己子任务的报告，不是把私有账户分析混入公共原始材料；也不能把这些子报告当作原始证据。

公共报告自身material_notification为0，快照global执行数为1，未见报告自触发公共模型循环。公共原文可能由某账户先取得，catalog来源owner不等于private；必须按scope和实际交付回执判断。数据库未留存每个原始出站prompt，此复核是身份、回执和冻结代码路径证据，不冒称逐包排除所有潜在泄漏。

未改数据库、源码、测试、服务或日常配置；未请求模型、取消任务或加单任务拦截。费用未知为null，HTTP次数不代表账单金额。
