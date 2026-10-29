# 第七轮：预测统计、恢复与窗口运行记录

基线为 `537e390856c9b2d203b2df49d122f1eac1094416`。实施前本地工作区干净；GitHub `main` 也为此提交。已读取用户的第七轮要求与 `folio_review_round6_checks.zip`，后者仅作为局部复测证据。基线本地重新执行 534 项 Python 测试通过；远端工作流 `40731086202` 的 tests job 成功，属于历史基线。本轮未提交、未推送，也没有本轮远端 CI 成功声明。

## 修改与回归

| 问题 | 原因与实现 | 生产路径回归 |
| --- | --- | --- |
| 早期失败按模型消失 | 归属原先在上下文阶段补写；现 Store 领取事务即冻结尝试，Service 凭据预检前绑定探索范围 | `test_seventh_identity.py`：凭据、客户端、首次请求、发布失败；全量 3 / 按模型 3 |
| 重试归属与分母 | 独立尝试表保存原身份；入队不清空旧团队，领取新配置才换未发布样本归属；替代和重启中断同步尝试状态 | 同模型、换模型、延迟领取、同任务重领、superseded/interrupted；正式样本不重复 |
| 探索规则被随机数拆散 | 独立批次 ID、完整规则规范投影；不修改已发布账本 | 同日多次、跨账户/日期、参数/标签变化、历史随机后缀、迁移幂等和正式日唯一 |
| 探索比较覆盖或择优 | 累计保留全部记录；无预定时间窗口不配对，明确未配对数量 | 前后方向相反仍保留；共同样本不靠最后一条或赢家构造 |
| 新旧材料比较无法恢复 | 保存逐角色真实工具交付、访问片段与可见处理声明，按来源流覆盖判断 | `test_seventh_recovery.py`：旧+新已处理允许；索引/未读 ID/旧报告未更新拒绝；v3/回滚/父链/账户/年龄保护 |
| 整窗离线没有记录 | 设置、账户、范围修订进入轻量历史表；每日窗口独立统计 | `test_seventh_windows_scheduler.py`：10点观察、整窗离线、19点恢复；未知范围不捏造数量；暂停、日历缺口、未创建、重复恢复 |
| 手动评分误记暂停 | `create_batches=False` 仅控制本次生成，窗口历史使用真实研究设置 | 10点与16点生产 API 手动评分后，整窗离线仍记 missed |
| 旧评分挤占新任务 | 先保存批次，首次评分预留名额，历史复核限量与独立频率；共享行情证据、超时、取消清租约 | 大积压、重复失败公平性、慢复核不阻塞新批次、跨截止、行情更正、无 Key 独立评分 |
| 全量读取再分页 | SQLite COUNT/LIMIT/OFFSET 和聚合；当前评估修订唯一；批次列表用摘要 | 同完整基准结果比较，稳定排序、分页、修订与尝试不放大分母 |
| 正式基准不完整 | 每个正式方向样本校验证券、精确 D、来源、口径和正价格 | 部分基准、错误日期/证券/价格保持 waiting_data，不伪造弃权 |
| 浏览器操作与恢复状态 | 筛选/比较草稿保留；运行日期分页；明确需要继续研究的检查点 | `frontend_seventh.mjs` 加原两组浏览器回归；旧拦截器改为按目标 HTTP 方法消费并有界等待 |

主要文件为 `app/forecast_identity.py`、`app/forecast_windows.py`、`app/forecasts.py`、`app/forecast_prices.py`、`app/recovery.py`、`app/materials.py`、`app/agents.py`、`app/model.py`，以及 Store/Service/API/前端的接入。供应商三字段、统一角色、五档 effort、两种协议和账户隔离保持；没有新增搜索转接、准入评分或强制委员会。

## 可核对的统计语义

样本、执行尝试、交易日窗口三类分别计数。未发布按最近冻结尝试，已发布按发布团队，历史不明归未知。方向准确率只使用有效已评分方向样本；弃权、失败、漏预测、未成熟和缺数据均不进入分母。A 失败 B 成功并不产生第二套正式预期样本。完整定义及配置项见 [预测评估口径](forecast-evaluation.md)。

历史探索映射依据保存的完整规则，保留原版本与规范版本。旧报表曾被随机后缀拆为多组，现在相同规则可合并累计；这解释了组数减少，预测条数、方向、发布时间不变。没有可靠规则的记录保持未知。

恢复只复用实际交付、仍在版本范围且已处理更正的可见成果。`synthesis_ready` 与 `needs_research` 区分；新主模型汇总会记录当前配置与旧研究配置关系，不给旧子报告换署名。历史检查点缺少新增证明时保留产物并重新研究。详见 [恢复与网络诊断](seventh-transport-recovery.md)。

## 性能测量

环境：macOS 26.5.2 / arm64，Python 3.14.4，SQLite 3.53.0。测试不调用真实供应商，不包含建库/迁移时间；Python tracemalloc 不含 SQLite 原生内存。

- 查询：5,000 批次、15,000 样本，每页30条、offset60，3次中位耗时 **128.572 → 12.630 ms**；Python 分配峰值 **31,448,331 → 139,711 bytes**。这是在同一当前关联查询上比较全量物化算法与 SQL 分页。[原始结果](seventh-query-benchmark.json)
- 调度：5,000 条预测（4,500 历史复核、500 首次到期）、10账户、同证券日期的本地2ms行情替身。基线代码与新版单次比较：**61 → 3** 次行情请求；首次评分 **0 → 24** 条，历史复核 **30 → 6** 条；总耗时 **0.418039 → 0.210578 秒**，首个新批次基准请求 **0.413946 → 0.088781 秒**。[原始结果](iteration-7-scheduler-benchmark.json)

这是高复用临时夹具的受控测量，不代表长期生产吞吐或 SLA，亦未宣称所有指标都下降。

## 迁移、备份与运行

schema 升为7，首次升级前在线备份 `.before-v7.bak`，迁移幂等。只重建批次的过时唯一约束，预测账本、ID、报告和不可变触发器保留；旧凭据仅保留安全引用，不导出 Key。恢复脚本支持1–7。详见 [Mac 启停和备份恢复](mac-operations.md)。

本次真实验收使用独立 `data/evaluations/seventh-*` 数据库。日常服务在本次开发开始前已运行多日，其 schema3 数据库仍会自行更新行情和时间；所以文件字节哈希会变化，不能声称日常数据库文件完全不变。验收程序仅以 SQLite `mode=ro` 读取日常设置，全部评估写入独立路径，未停止、迁移或重置日常服务；`.env` 未修改。要使日常服务加载新版，需要按手册先备份、停服务，再启动迁移。

## 实际执行与证据

本轮实际执行：

```sh
.venv/bin/python -m pytest -q
PYTHON=.venv/bin/python BROWSER_EXECUTABLE='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' npm run test:browser
.venv/bin/python scripts/benchmark_seventh_queries.py
.venv/bin/python scripts/benchmark_seventh_scheduler.py
.venv/bin/python scripts/verify_seventh_live.py --output data/evaluations/seventh-live-release-20261029 --authorized-base-url https://api.xxyapi.com/v1 --model-a grok-4.7 --model-b grok-4.6 --timeout 420
git diff --check
```

最终测试数量、浏览器结果、源码摘要及真实阶段结果统一记录在 [验收清单](iteration-7-validation.json)。源码仍是本地未提交变更，commit 字段仅表示父提交。

最终本地回归为 **620 passed，0 failed**（22.38秒，1条已有 Starlette/httpx 弃用警告），三组浏览器测试全部通过；新增浏览器场景无 console error。测试在最后的代理诊断修补之后执行。没有本轮远端 CI 结果。

真实验收采用上海实际时钟与159278，已核对实际证券为机器人ETF鹏华及真实行情返回。执行时均在正式窗口之外，因此记录为探索。模型由实际目录确认，A使用grok-4.7、B使用grok-4.6，均为medium；B空仓但候选范围包含159278。使用用户明确授权的现有供应商，凭据仅发给 `https://api.xxyapi.com/v1`。

| 真实阶段 | 可确认结果 | 限制 |
| --- | --- | --- |
| 首轮，11:06–11:10 | A发布完整报告及3条弃权；随后持仓从10,000改至20,000份、现金10,000元不变，旧报告失效，新任务绑定v2；B输入与团队未变 | B及A改仓后的正文为占位内容，completed不代表有效研究 |
| 第二轮完整研究，11:17–11:23 | B发布报告并保存3条弃权；实际调用具名子角色，账户与团队归属可追溯 | A初始报告为占位；改仓任务在最终汇总两次约50秒断连，任务failed；没有可用检查点，未进行真实仅汇总恢复 |
| 最终诊断修补后重新启动，11:27完成 | 在独立评估库副本启动正常API/worker；状态、预测、窗口API返回200；生产协议探测两轮工具循环成功；未新建研究任务 | 仅证明最终代码启动及小请求协议链路，没有再次进行完整付费研究 |

首轮及第二轮原始摘要分别保留在 `data/evaluations/seventh-live-final-20261029/summary.json` 与 `data/evaluations/seventh-live-release-20261029/summary.json`。第二轮源码摘要为 `eadea9eeb2a2b907774429e2f8f249dba484f85b07c483b96b3f7f216aedcd72`；最终诊断修补后摘要为 `256c43c09b357fc43e455b988526fca0e3026d5b0982ff13f0ce48aa77a311e3`，重启产物保留在 `data/evaluations/seventh-postpatch-restart-20261029/summary.json`。两阶段运行期间各自源码均未变化，不能将前一阶段改署为后一阶段完整研究成功。可携带的脱敏结果、报告、网络阶段与原产物摘要见 [真实验收证据](seventh-live-evidence.json)。

第六轮RemoteProtocolError的历史诊断证据可确认汇总阶段约50秒断连，旧日志缺少响应阶段信息。本轮真实再次复现：A最终汇总两次耗时50,148.3和50,697.74毫秒，均停在接收源站响应头阶段；未取得源站状态、正文或请求ID，不能断言代理或供应商根因。第二次原始trace中的HTTP200来自代理CONNECT，最后补丁已将隧道与源站响应独立记录，原记录保留并加以解释。工程测试验证了合法检查点可只重试汇总；真实失败没有可用检查点，不能宣称真实恢复成功。

最终源码重启使用的命令为：

```sh
.venv/bin/python scripts/verify_seventh_restart.py --database data/evaluations/seventh-live-release-20261029/evaluation.sqlite3 --output data/evaluations/seventh-postpatch-restart-20261029 --probe
```

本轮**真实正式批次未验证、真实方向预测0、成熟前瞻样本0**；页面显示“暂无已评估样本”。第二轮保留9条预期样本：A初始3条未覆盖、B的3条弃权、A改仓3条失败。工程回归支持统计和恢复语义；外部服务出现占位与断连，研究只部分成功，尚无到期命中率或投资表现证据。未修改窗口、伪造时间、回填事后方向，也不安排后台等待交付。
