# 第九轮：样本执行署名、行情容量等待与重试

本文件记录第九轮工程修复及隔离验收；不把受控模型/行情夹具当成真实投资研究。执行环境为本机 macOS 26.5.2 arm64、项目 `.venv` Python 3.14.4、SQLite 3.53.0。所有测试使用临时 SQLite，未读取、迁移或清空日常账户库，未调用收费模型或真实行情供应商。

## 未发布样本使用实际冻结尝试

第八轮查询用 `COALESCE(f.manager_model, batch.team_snapshot.manager_model)`，而批次已有任一发布时不再更新其团队快照。因此，同一批次第一只证券已由甲发布、第二只交给乙重试且在前置阶段失败时，第二只证券仍会被查询为甲。

第九轮用两个小表保存精确关系：`forecast_research_scope_samples` 记录任务分配的 sample_id（因此同时限定证券和期限），`forecast_sample_attempts` 记录这些样本在实际领取、配置冻结之后对应的 attempt_id。分配表可以在未领取前缩小范围；已经冻结的关联仅追加，不能 UPDATE。队列入队不改署名、不清掉旧失败。运行中新增目标实际分配/交付时，关联到该任务已冻结的配置。

- 已发布记录的 model、effort、team 始终来自不可变 `forecasts`；已弃权同样不改写。
- 未发布记录查询最近实际分配的冻结尝试；没有可证明历史时返回未知，不回填当前账户或整批团队。
- `execution_attempt_id`、`assigned_job_id`、`assigned_attempt`、`configuration_frozen_at` 可回查尝试历史；团队身份包含供应商与角色配置。
- 部分证券或期限任务失败只更新该次实际分配的样本。证券/期限筛选的尝试数也按关联范围计算。
- 批次列表的 `identity_count`、`attribution_status`、`identity_semantics` 说明混合配置；混合批次不再返回一个冒充全批次的 `manager_model`。详情的 `sample_attribution` 与列表使用同一SQL语义，原始批次快照保留为历史资料。
- 预期样本仍由固定批次定义。两只证券、三个期限始终是6个样本；尝试另计，失败不进入命中率分母。

历史迁移仅使用有单次冻结尝试、可证明范围的旧任务。此前已由另一任务发布的样本不归到后来的重试；多次尝试但范围无法重建的历史保持未知。已发布预测字段和发布时间没有迁移更新。

## 等待容量不计为网络尝试

第八轮在任务领取时增加 `attempts`，实际获取 `ClosingPriceSession` 信号量发生得更晚。短周期取消因此可能将从未进入供应商函数的目标也推到五次上限。

现在领取只持有有界 `lease_token`/`lease_until`。只有获得共享请求容量、即将真正调用供应商取数函数时，才在独立短事务中增加 `attempts`、写入 `request_started_at` 和下一次退避时间。SQLite事务不跨网络等待。

- 取消前尚未开始请求：`capacity_wait_cancelled`，不增加attempts、不耗尽网络失败上限，释放自身租约。
- 请求已经开始而周期取消：保留已计数尝试，原因 `request_started_result_unknown`，按原五次上限和有限退避处理；重启不能把第五次未知请求变成第六次。
- 完成写入和清理都核对执行token，迟到结果不能覆盖新租约。请求开始前及完成后再次核对截止和账户版本，ready_at使用真实完成时刻。
- 相同证券、日期、价格口径在单轮共享一个供应商取数任务。各消费样本记录各自使用的取数尝试，`price_requests` 单独记录实际共享请求开始/完成数；其中一方取消不会取消仍被其他消费者需要的请求。所有消费者取消后才取消无用请求。被取消的请求立即退出可复用缓存；同轮同键再次订阅创建独立请求，旧取消清理不能删除新请求或提前累计其attempts。
- 到期评分的请求开始也持久计数，结果和失败受评分租约token保护；等待评分/行情容量本身不消耗网络预算。
- 每轮基准目标上限40（仍可合法配置）、并发上限、请求超时、周期上限、固定截止和跨账户轮转均保留。

## 迁移和回退

schema升为9。升级前通过SQLite在线backup产生权限0600的 `.before-v9.bak`，包含WAL中的一致性状态。新关联表、索引、触发器、preparation新增列、迁移标记和schema号在同一迁移事务内；故障注入测试证明失败后这些内容一起回滚，旧数据及schema8仍可读取，备份仍在。第八轮代码会拒绝schema9数据库；代码回退必须配套恢复升级前一致性备份，不能让旧代码直接操作新库。日常停服、恢复及路径核对遵循 `mac-operations.md`，本轮测试没有执行日常停服或恢复。

## 生产路径回归证据

命令（项目根目录）：

```sh
.venv/bin/python -m pytest tests/test_ninth_forecast_identity.py tests/test_ninth_forecast_preparation.py tests/test_seventh_identity.py tests/test_eighth_forecast_preparation.py tests/test_seventh_windows_scheduler.py tests/test_sixth_forecasts.py tests/test_sixth_forecast_api.py -q
```

2026-10-30 最后执行结果：**113 passed，1条既有 Starlette/httpx 弃用警告，16.61秒**。这是一组相关生产路径回归，不冒充全项目测试或远端CI。

新增回归使用生产 Store、Service、forecast_tick、ClosingPriceSession 和实际SQLite事务。覆盖：

1. 部分发布后换模型、同模型、只改effort、同名跨供应商；前置失败；查询、统计与模型/团队筛选；已发布三条逐字段不变。
2. 只分配一只证券的一个期限；队列期间改配置但领取前不改署名；取消、重启；未分配样本不被标失败；历史未知、部分发布历史迁移及重复迁移。
3. 8目标、并发1、周期1秒、单请求超时2秒、每次行情0.65秒：五轮后5个ready，其余没有因为排队而terminal；再执行三轮全ready。每一轮持久attempts总和严格等于夹具实际进入取数函数次数。
4. 并发8正对照：8目标首轮全部ready，各一次；真实超时对照每目标恰好五次后终止。
5. 首次评分占用唯一容量时，新基准目标attempts为0；一个共享消费者取消后另一方仍成功；唯一消费者取消且旧请求仍在处理取消时重新订阅，要等待容量释放并真正开始新请求才计数；基准与评分迟到结果不能覆盖替换租约；第五次请求崩溃后重启不再发第六次。
6. 原回归继续覆盖41目标、跨tick/重启、多账户公平、局部基准准备、补充研究、截止、账户版本变化、价格复权和独立评分。

这些测试不证明所有真实生产负载，也不将受控时钟伪装成真实交易窗口。真实模型报告及正式窗口验收由本轮总验收另行记录。

## SQL性能

```sh
.venv/bin/python scripts/benchmark_ninth_queries.py --batches 5000 --output docs/iteration-9-forecast-query-benchmark.json
```

全新临时库包含5,000批次、30,000样本、10,000尝试、45,000样本尝试关联；每批甲已发布一只证券、乙失败另一只证券，具有真实新查询连接结构。三次本地微基准中位数：

| 操作 | 时间 | Python峰值分配 |
| --- | ---: | ---: |
| 全量物化后截取30条（控制组） | 325.976 ms | 77,286,297 B |
| SQL计数及分页30条 | 67.396 ms | 154,476 B |
| 按乙模型SQL筛选及分页30条 | 63.182 ms | 148,727 B |
| 按乙模型SQL聚合 | 145.334 ms | 35,306 B |

查询计划使用 `forecast_samples_batch_page`、`forecast_sample_attempt_latest` 及attempt主键，分页与筛选没有退回Python全量加载。详细环境、每次时间及EXPLAIN计划见 `iteration-9-forecast-query-benchmark.json`。这是本机合成微基准，不是生产SLA，Python内存统计不包含SQLite原生内存或OS缓存。
