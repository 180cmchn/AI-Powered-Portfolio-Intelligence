# 第十六轮：正式批次等待与取消原因

## 默认策略

`forecast_batches` 及其全部预期 `forecast_samples` 先持久化，沿用批次身份、输入版本、基准日、规则、范围和原截止。首次正式批次发现当前账户有已经领取、运行中或等待范围的任务时，保存 `waiting_research`，不更改账户 `latest_job_id`，不取消旧请求，也不把正式目标事后标记成旧模型已经见到的输入。

尚未领取、`attempts=0`、没有配置快照、没有已分配预测范围、且原任务目标覆盖整个正式批次的 queued/waiting_config 任务允许合并。`forecast_job_bindings.reason=merged_before_execution` 保存这一决定；正式目标会在领取时进入真实输入和范围快照。现金专项或范围不兼容的任务仍等待。

正常 `Service.tick` 在派发普通积压需求之前接续等待正式批次；结束、失败或用户取消的旧普通任务都可以让后续独立正式需求获得执行机会。这不会复活原执行身份。已经绑定且被用户取消的正式执行，不会由后续普通定时事件自动重新绑定；明确用户重试仍按原政策处理。既有最多一次新增基准补充研究保持。

等待跨越截止后，未启动目标记录 missed，并明确“等待当前账户研究期间超过原正式截止”。同时暂停定时研究和预测评分时，worker 仍维护这项到期状态，但不启动新的正式研究。真正账户输入或候选范围更新仍使旧任务失效。批次原输入已变时停止该批次，不延长截止、不重写旧预测。

## 持久取消事实

账户执行替换时，在原任务 `task_metadata.cancellation` 保存 `code`、原/新输入版本、替换任务 ID 和时间；worker 与 ModelClient 使用同一事实：

- `input_version_updated`：替换任务确有不同账户输入版本。
- `same_version_replaced`：同版本显式替换。
- `superseded_reason_unknown`：旧历史 superseded 缺少持久原因，不能声称输入更新。

恢复时依据数据库账户版本和执行归属保存同样的结构化原因。复用既有 JSON 字段，不增加表或列，schema 仍为 11。没有对旧历史记录批量推断或改写。

## 确定性验证边界

`tests/test_sixteenth_scheduling.py` 使用真实 Store、Service.scheduler_tick/tick、Agent、ModelClient、SQLite 与 API。只有模型 HTTP 和市场价格被明确夹具替代；使用已核实的历史交易日期，绝不描述为真实前瞻预测或自然成熟效果。

主链事件栅栏在 HTTP handler 内暂停旧请求。15:30 正式基准准备后，验证旧任务仍 running、正式批次待执行可见、账户归属不变、重复 tick 不重复调用。释放后旧报告先合法发布；正常 worker 随后启动正式任务，其实际 HTTP 输入含该批次身份与正式目标，发布一项方向及两项有理由弃权。重启 Store/Service 后，暂停研究、启用评分，以次日精确日期价格得到一项评分；重复 tick 不重评分、不重发模型或改写方向。

其余新增回归覆盖 queued/waiting_config/已冻结队列/waiting_scope、原任务完成/失败/取消、等待时重启、输入更新、截止、已取消正式执行不被定时器复活，以及真实 HTTP 请求收到不同持久取消原因。已有第八、九轮准备测试继续验证双账户有界基准采集、单项缺基准、一次补充研究、容量等待不耗费请求预算和价格修订等。

工程记录不合并分母：首次六模块执行 `scheduler-targeted-first.txt` 为 97 passed、1 failed；原 atomic enqueue 测试把故障栅栏放在 `_enqueue`，新未领取合并正确绕过该分支，故没有触发原测试崩溃。测试现参数化未领取合并与新建两种情况，将栅栏移到共同绑定事务，保留并扩展回滚、状态和幂等断言。后续六模块结果在 `scheduler-targeted-final.txt`。暂停双开关的增量测试另行执行通过；最终全量以主交付冻结源码记录为准。

暂停维护增量加入后的六模块结果为 **100 passed / 18.67s**（`test-results/sixteenth/scheduler-targeted-final-with-pause.txt`）。随后同步合并返回对象的 reason 与数据库值，复测三模块 **57 passed / 2.29s**（`scheduler-merge-return-final.txt`），包括原暂停评分测试：新期望明确保留 `forecast_batch:…; close_summary` 两个触发原因，并新增断言证明仍是原 queued 任务、总任务数为一及收盘需求没有丢失。全量初轮发现这一旧标签断言差异，原失败仍由主交付全量日志保留。

独立代码复核另发现单证券任务的 `metadata.target.symbols` 含全账户证券，而其真正交付范围是 `target_security`。现合并条件同时限制该单证券范围；新增“同证券可合并/多证券必须等待”两项回归，防止正式目标和交付契约范围不一致。此最终补丁后三模块 **59 passed / 2.24s**，记录为 `scheduler-scope-review-final.txt`。
