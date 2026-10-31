# 第十二轮：账本时间边界与已有研究保护

本记录针对基线 `09557bd8a3cd404250fa9fa58093f6babb404ead` 上的本地工作区修改，尚未提交或推送。环境与依赖版本见 [time-ledger-manifest.json](time-ledger-manifest.json)。本分支没有读取模型 Key、调用付费模型、访问日常数据库或操作 8765 服务。

## 失败根因与修复前证据

原 `_replace_ledger_positions()` 把明确填入的 `available_quantity` 写成 `sellable_on=now()[:10]`。原两项测试没有控制这次校正的时钟，却固定在 `2026-10-30T10:00:00+08:00` 卖出。上海进入 10 月 31 日以后，校正只证明 31 日起可卖，30 日的可卖量仍为零。这是测试时间线未固定，不是正常卖出普遍失效。

修改前真实执行：

```sh
PYTHON_DOTENV_DISABLED=1 QUOTE_PROVIDER=manual .venv/bin/python -m pytest -q \
  tests/test_eleventh_ledger.py::test_unknown_cost_and_availability_require_explicit_correction \
  tests/test_eleventh_ledger.py::test_odd_lot_all_sale_and_tick_rounding_source
```

结果 **2 failed**，保留 [python-time-before.txt](python-time-before.txt)。这是本地原代码复现；远端 Actions 状态由本轮主验证记录另行获取，不能互相替代。

## 单一墙钟与明确时间线

新增 `app/clock.py::now_datetime()`，所有生产 `datetime.now()`、`date.today()` 和 `time.time()` 形式的墙钟读取统一经过 `_utc_now()`，再转换为调用方的上海或 UTC 时区。包括账本未来时间检查、快照/流水时间、队列 due_at、行情状态、调度、材料缓存、恢复窗口、模型调用的可见时间戳和服务启动身份。`time.monotonic()` / `perf_counter()` 仍用于实际经过时长与超时，不受测试墙钟影响。

`tests/conftest.py::frozen_clock` 是测试作用域 fixture，支持 `set(带时区时间)` 与 `advance()`。它只替换单一 UTC 时钟函数，适用于 FastAPI 工作线程和并发账本写入；结束后由 pytest 恢复。生产没有环境变量、配置或 HTTP 接口可以设置假时间。不改操作系统时间，也不依赖 CI 主机的本地时区。

两项原失败测试明确在上海 `2026-10-30 09:00` 建立账户并校正，推进到 `10:00` 卖出。原 Decimal、未知成本、零股、费用和剩余成本断言保留。新增边界使用临时文件中的明确交易日期列表，标记为受控日历，覆盖周末和假期，不将它作为真实行情证据。

## 生产校正语义

- **保持已有日粒度规则**：明确的可卖数量从本次校正的上海日期起生效，不承诺秒级历史重建，也不能证明更早日期的可卖数量。
- 新校正的流水和持仓 JSON 增加 `availability_effective_on`、`availability_timezone`；流水另保留 `recorded_at`。不覆盖旧流水、不补造历史成交、不回写旧记录。
- 一次校正只捕获一次生效日期并传入全部持仓，避免跨午夜循环把同一校正拆成不同生效日。
- 卖出不足的错误同时显示补录成交日期与当前校正生效日期，解释当前可卖量不能倒推。旧记录没有新增字段时继续按原 lots 的 `sellable_on` 校验，且显示通用未知/T+1 提示。
- API 的既有严格字段模型拒绝自填历史 `effective_on` 等额外字段；Store 边界也明确拒绝历史生效字段。未实现任意历史回放。
- 未放宽 T+1、交易日历覆盖、未来成交、未知可卖量、不可变流水、费用版本、账户 CAS 或操作幂等校验。保留既有未来时间一分钟容差，没有扩大容差。

这些新增审计字段位于现有 JSON 内容中，**schema 仍为 11，无迁移**。旧程序可读取这些额外字段；回退代码不删除已发生的流水。用户数据升级/回退仍应先用既有受控备份入口保留数据库，不用本次测试库替换日常库。

## 新增生产 HTTP 边界回归

`tests/test_twelfth_time_boundaries.py` 的 **8 项**使用生产 FastAPI、临时 SQLite 和实际 API，不是摘录函数：

| 场景 | 断言 |
| --- | --- |
| UTC 10 月 30 日 15:59:59 / 16:00:00、上海 10 月 31 日 00:00:01 | 同一个 10 月 30 日卖出在校正日为 30 日时可卖 1000；校正日为 31 日时预览与提交均 422，交易和账户不变。 |
| 自填历史生效日 | API 严格字段验证与 Store 均拒绝，账本无变化。 |
| 周五买入、周末卖出、周一卖出 | T+1 周末不解锁，下一个夹具交易日可卖；撤销末笔恢复现金和可卖量，费用回归，原买入流水不变。 |
| 假期前买入、假期内卖出、节后卖出 | 依据明确日历解锁，不按自然日猜测。 |
| 黄金 ETF T+0、未来输入、日历缺口 | T+0 当日卖出；未来成交拒绝；缺少下一交易日时股票买入拒绝且不写账。 |
| 统一时钟 | domain、日历、账户建立和队列计划时间一致；源码约束阻止新增绕过统一入口的墙钟读取。 |

## 第十一轮有效改动回归

`test_eleventh_recommendation_baseline.py` 继续使用生产 `Service.execute → Store.publish`，明确的无费用 runner 返回受控报告。固定上海周六，使用前一交易日旧收盘价；常规调度开启、行情网络刷新关闭、新闻查询为空。每次发布后执行 **10 次真正的 `Service.tick()`**，重新打开 SQLite 并创建新 Service 后再执行 **10 次**。发布两次仅调用受控 runner 两次；报告持续 `current`，没有因首次旧报价观察产生额外需求或任务。重启后手动复核仍可以创建任务。

同时运行既有 `test_third_recommendations.py`：已知 fresh→stale、真实价格跨越、资料及持仓变化仍生成应有且持久去重的需求。本轮未改 unknown→stale 的生产抑制逻辑。

分层超时生产逻辑未改。既有首有效事件、有效进展/心跳区分、流空闲、请求硬限、子任务/账户预算、完整 terminal、取消、重试和恢复测试一起运行。第十一轮真实请求 107.515 秒与汇总 210.695 秒完成，以及后续 120.002 秒无进展失败的历史记录保持原样；本轮受控通过不等于追加真实模型复验。

## 本分支验证结果

```sh
PYTHON_DOTENV_DISABLED=1 QUOTE_PROVIDER=manual .venv/bin/python -m pytest -q \
  tests/test_eleventh_ledger.py tests/test_eleventh_api.py \
  tests/test_twelfth_time_boundaries.py tests/test_eleventh_recommendation_baseline.py \
  tests/test_eleventh_timeouts.py tests/test_agents.py tests/test_third_recommendations.py \
  tests/test_sixth_service.py tests/test_fourth_budget.py tests/test_seventh_streaming.py \
  tests/test_tenth_failure_recovery.py
```

**190 passed, 1 warning，9.04 秒**。完整日志：[python-time-ledger.txt](python-time-ledger.txt)。警告为现有 Starlette TestClient / httpx 弃用提示。额外的报价协议与时钟兼容定向运行共 **84 passed, 1 warning**；这些计数有重叠，不能相加当作唯一用例数。完整 Python、浏览器和 Python 3.12 等价 CI 结果由主验证记录汇总。
