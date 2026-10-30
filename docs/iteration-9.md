# 第九轮交付与验收

本轮基于 `046fdfc9351b498653c47b0fa697a3baefdc6e40` 直接继续开发，保留已有 `deliverables/`、日常数据库、`.env`、账户及凭据，没有回退后续提交。本文件记录推送前的本地验收；之后的提交与推送状态以 Git 历史为准。详细工程证据分为[启动手册](mac-operations.md)、[配置与供应商交互](iteration-9-config-ui.md)、[计算成果保留](iteration-9-synthesis.md)、[样本归属与重试](iteration-9-forecast.md)。本文件的最终测试与真实链路结果以本次冻结验收记录为准。

## 用户现场与六项症状

检查环境是用户 Mac 上的实际项目目录 `/Users/chn/Documents/ChatGPT/金融股票 2`，不是 Linux 替代环境。项目 `.venv` 为 Python 3.14.4，锁定 python-dotenv 1.2.4；其 `load_dotenv` 的 `override` 默认值实测为 false。远端 main 和本地 HEAD 在开始时均为第八轮基线；冻结验收时本轮代码尚未提交，以补丁及源码摘要区分。

只读检查时 **8765 没有监听**，GET `/api/status` 连接失败；日常 `data/live.sqlite3` 为 schema 3。未观察到与历史 8765 故障对应的 live 服务 PID、启动时间或当时的浏览器响应，无法据此复原原始故障。另有旧 demo pidfile 指向存活进程，未停止或操作该实例。没有取得用户当时两条命令的真实终端记录，不能认定反斜杠被作为 shell 行续接执行。新手册明确分行执行、不加连接反斜杠。

[现场诊断记录](ninth-site-diagnostic.json)不包含 Key，未导入 Store、未迁移源库；下表区分用户历史现场与可重复的隔离复现。

| 用户症状 | 本轮证据与处理 | 现场根因边界 |
| --- | --- | --- |
| 部分研究记录 `Not Found` | 新/旧API兼容缺口在隔离页面中复现；错误记录 method/path/status，路由缺失与无数据区分。 | 未取得当时失败路径，不能证明具体命中了哪个旧进程。 |
| 基础功能就绪但模型未配置 | 将连接、Key、目录、主角色绑定、函数、原生联网状态分开；无模型不影响基础API。 | 不能把原始提示归因于错误Key或模型联网能力。 |
| `.env`已有URL/Key仍无模型 | 当前环境可读取，真实目录确认当前配置模型存在；空绑定与一次性迁移死角已有显式检测/导入恢复。 | URL/Key本身不构成已选择模型；原旧进程实际环境无法追溯。 |
| 供应商输入不正常 | 已在真实FastAPI+临时SQLite+Chromium复现局部错误引起重绘、丢输入/焦点；现保留当前DOM，并修编辑弹窗label重名。 | 确认代码链路存在，不假称观察过原浏览器的按键。 |
| 顶部连接中断随后404 | 原 `sixth.load` 抛错进入全局poll错误分支；现局部接口错误与网络不可达分开。 | 404本身不证明服务器断网。 |
| 重新连接仍404 | 对确定性缺路由暂停自动轮询；显式重试重新检查状态/兼容性，显示安全升级操作。 | 原始404缺失接口还是对象ID仍无法现场确定。 |

## 启动、版本与迁移

`scripts/manage.py` 保留幂等start，核对真实进程启动身份、实例ID、模式、端口、项目、目标库、源码、非秘密环境设置及`.env`文件修订；GET200和相同mode不足以通过。新进程提前退出或未完成API就绪会报失败。未验证pidfile、PID复用、其他项目和无关端口不会被误杀或冒充就绪；停止超时保留pidfile。旧整数pidfile停止前还需本机端口PID、实际mode和目标数据库锁证据。

`serve.py` 先绑定监听socket；`create_app(worker=True)` 在Store构造前取得同库锁。占用端口/旧worker时，不会先升级目标库再发现冲突。生产无热重载；静态HTML/JS固定为进程创建时读取的字节，状态保存当时源码/静态摘要，不用后来磁盘HEAD冒充加载版本。`/api/health`、OpenAPI以及必需GET接口共同构成就绪条件，不调用收费模型。

schema9新增样本与尝试关系、行情租约/开始时间。升级先在线backup为`.before-v9.bak`，新迁移事务失败可回滚；历史不可还原的身份保持未知，已发布预测不改写。恢复脚本接受schema1–9，新库拒绝旧代码。`scripts/backup.py`使网页不可用时仍可做SQLite一致性备份；不复制活动WAL主文件、不删锁。

[全新目录frozen安装证据](ninth-frozen-entry.json)：实际`uv sync --frozen`、zsh `start.command`、重复start、stop均退出0；仅`.env`三项连接被识别，主Agent待选模型，未随机绑定。[实际schema3日常库副本升级证据](ninth-old-database-upgrade.json)：只读backup到私有临时目录，副本禁用推理/行情/调度后经管理入口升级至9，基础接口全200、完整性ok，原账户与快照数量保留；源库仍为3。测试后临时进程和副本清理。

## 配置、表单与错误恢复

网页连接与显式角色覆盖优先保留。环境来源保持shell优先，不全局override；修改环境需重启后检测，显示预览目标并显式导入。已撤销环境连接不能自动复活，换端点/Key/协议生成新修订并隔离目录/能力缓存。历史MODEL_NAME只有实际目录确认后才可用。供应商始终只有Key、URL、协议三个连接字段；模型和五档effort在角色里选择。

选中账户的状态使用其实际覆盖后配置；设置页的明确能力检测仍针对全局主Agent，并单独显示global状态，避免将全局探测结果误记为某个账户。未配置子角色不阻止基础操作，不批量付费检测角色。Chat原生网页搜索未扩展。

表单Key仅在当前DOM保留，轮询/局部错误不清空；成功或取消清除。保存请求带操作ID，连接提交与操作登记原子完成；响应丢失和写5xx都先读核对，不自动重发。目录401/404/429或意外失败不迫使用户重复创建，目录错误按目标与修订隔离，成功恢复时清除对应错误。新浏览器测试不录制秘密输入trace；原生产浏览器组也移除可能捕获请求正文的trace。

## 三项审查问题

| 问题 | 生产修复与证据 | 保留限制 |
| --- | --- | --- |
| 强制汇总裁掉成功计算 | `calculation_context.py`对真实输出做确定性摘要，主/子会话、检查点与恢复一致保存；40条拥挤结果在两协议实际最后HTTP请求中逐项验证。 | 上下文仍有预算，无法纳入时明确索引/遗漏；进入请求不等于模型采用。旧真实报告不改写。 |
| 部分发布后失败归旧模型 | sample_id→冻结attempt关系，已发布使用不可变发布署名，未发布用实际领取的最新范围配置；SQL分页/筛选/聚合统一。 | 无法证明的历史身份未知；入队未领取不会抹去旧失败，预期数不随尝试增加。 |
| 等待容量也耗重试 | 真正开始供应商取数才累计；容量等待/执行中取消区分，租约token防迟到写入，共享取消按消费者及请求代次处理。 | 已发起但结果未知仍耗一次；截止、上限、退避与整体预算保留。 |

八目标低并发复现场景五轮后5 ready，其余没有因未执行而terminal，继续三轮全部ready；并发8正对照首轮成功。实际超时仍五次终止；独占消费者取消后同轮再订阅也只在真正新取数开始时计数。SQL微基准见[30,000样本记录](iteration-9-forecast-query-benchmark.json)，保持数据库分页而非Python全量筛选。

## Mac网络与钥匙串

[实际Mac DNS诊断](ninth-mac-network.json)观察到巨潮、东方财富PDF和鹏华官网的解析结果均在`198.18.0.0/15`基准测试地址段；生产`_resolve_public_url`因此在address_validation阶段返回non_public_dns，未请求PDF正文。同时观察到代理启用和utun接口，证据支持本地DNS/代理环境影响，不能指定某个软件为唯一原因。未关闭SSRF/TLS、未放行保留地址、未改网络设置。应由用户按网络工具配置检查公开文档域名的真实DNS/直连方式，再重新读取；仅搜索链接仍不算本地已读全文。

[钥匙串真实检查](ninth-keychain-check.json)在独立测试service中写入随机值、读回核对、删除成功；没有读取或修改用户日常条目。浏览器中的写入回归使用显式凭据替身，与这次Mac安全存储实测分别报告。

## 最终验收与真实研究

真实验收时间为 **2026-10-30 01:37:07—01:44:42（Asia/Shanghai）**，使用独立端口 53951、独立虚拟账户和 `data/evaluations/ninth-final-v3-20261030/` 数据库，经实际 API 与 worker 执行。验收进程已退出。冻结源码摘要为 `c055b65c0eaa54f5c105f6b8074834295ecdd00b9c6b603e08ace246e0834546`；源码、验收文件和 `.env` 元数据前后不变。此前两次预检因验收脚本过严拒绝收盘状态及新浪实际简称而退出，均为 0 次模型请求；仅修正验收脚本的身份判断，应用代码没有在完整回归后变化。

基础必需 API 均返回 200，实际浏览器检查供应商、角色、总览、研究、Agent、预测、设置及账户模型共 8 页，无 JavaScript 错误及 Not Found/连接中断/版本不兼容提示。实际目录返回两个模型；本轮只使用已配置且目录存在的 `grok-4.7 / Responses / medium`，不代表第二模型已测试。原生搜索另做一次明确预检并完成。受控浏览器写入回归、独立真实钥匙串验证和此次真实供应商目录验证分别留证，不混称为日常库已经接入成功。

| 实际业务结果 | 数量与边界 |
| --- | --- |
| A 初始、A 改仓、B 候选报告 | 3 份完成；A 数量 10,000→20,000、现金 10,000 不变；旧报告保留并 stale，B 输入及配置不变。 |
| 业务请求 | 40 次，均收到 HTTP 200；38 次完整 `response.completed`，2 次接收阶段失败，无终止事件。 |
| 子角色 | B 的 ETF 子报告完成并由一次有界迟到更新采用；价格子角色超时无报告。ETF 首次传输失败后第二次成功。 |
| 成功计算最后可见性 | 11 条 calculate 全部以确定性摘要进入最后请求；集中度 47.81%→64.69% 被采用，B 份额除法明确为全额投入假设情景。 |
| 预测 | 3 个探索批次、9 条弃权；方向预测 0、未提交 0、失败样本 0。正式批次 0，自然成熟评分 0，accuracy 为 null。 |

请求数按 `model_usage.payload.requests` 与 `model_error.payload.requests` 合并核对，避免漏掉价格子角色的超时。业务 40 次加原生预检 1 次中，39 次有供应商 usage：输入 915,661、输出 42,918、合计 958,579 tokens；原始费用字段合计 `cost_in_usd_ticks=6,621,670,000`。缺失的两次费用未知，未按零计算；没有账单或确认的字段换算契约，不换算为美元实付金额。业务中 5 次供应商实报输出超过请求的 3,000 token 上限，故不能把请求参数当作供应商严格执行的费用封顶；没有为通过验收上调全部超时或调用预算。

本次日线和同期基准持续 `RemoteProtocolError`，没有真实执行 `market_indicators`；因此真实长指标成果的最终采用、多窗口正反证据质量仍未验收。B 的 7 条算术结果虽可见却未采用，其原始参数缺证券、区间、单位和来源，不能认证为合格的金融比较。ETF 子报告声称读取披露，但本地 PDF 因 `non_public_dns` 未取正文，原生来源只保存链接，不能独立认证其全文事实。B 没有把任何买入自动等同满仓；全额现金只是明确假设，不是已完成的仓位方案。详见[报告原文](ninth-live-reports.md)、[逐项真实复核](ninth-live-research-review.md)及[脱敏验收 JSON](ninth-live-evidence.json)。

验收处于正式窗口 15:30—18:00 之外，正式设置 disabled，未改时钟或延长截止，也没有等待到期后再伪称完成评分。真实跨模型重试归属仍只有工程回归覆盖。第八轮最终真实成功及其报告质量问题继续保留在原文档，没有改写为失败。

## 用户日常实例的安全升级

本轮没有替用户运行日常停服或迁移。[收尾只读检查](ninth-site-final.json)仍未观察到 8765 监听，日常库仍为 schema 3。历史故障根因无法追溯，不把隔离验收说成日常实例已升级。以下是交付给用户的可执行步骤，不表示已执行：

```sh
cd "/Users/chn/Documents/ChatGPT/金融股票 2"
.venv/bin/python scripts/inspect_runtime.py --url http://127.0.0.1:8765
.venv/bin/python scripts/backup.py --mode live
```

核对输出的实际PID/模式/库与备份路径，确认允许中断当前研究后执行：

```sh
./stop.command --mode live
uv sync --frozen
./start.command --mode live
```

看到“基本API与实例身份已核对”后打开页面，在“模型供应商”检测并确认环境连接，从真实目录配置全局/账户Agent模型。若检测显示另一个端口、库或不明进程，先按[只读排错](mac-operations.md)核对，不对端口所有者批量kill。回退需同时恢复`.before-v9.bak`，不能让第八轮代码直接读schema9。

## 冻结工程回归结果

[完整环境与命令记录](iteration-9-validation.json)：macOS、本地Python3.14.4、Node25.9.0、Playwright1.62.1；所有受控测试使用临时数据库/端口。应用文件摘要为 `9fc25dec73d16436af2e56f29a64683110184ce2482caf1e0706f13132f9240f`。

| 实际命令 | 最终结果 |
| --- | --- |
| `.venv/bin/pytest -q` | 812 passed，1条既有Starlette/httpx弃用警告，62.42秒。 |
| `PYTHON=.venv/bin/python npm run test:browser` | 原iteration、production、seventh以及新增ninth状态/浏览器组全通过；逐组退出码均0。 |
| `.venv/bin/python scripts/check_handbook_frozen.py --output docs/ninth-frozen-entry.json` | 临时新目录frozen安装、真实zsh手册入口、无MODEL_NAME环境识别、重复启动和停止通过。 |
| `.venv/bin/python scripts/check_upgrade_copy.py --source data/live.sqlite3 --output docs/ninth-old-database-upgrade.json` | 实际旧库的只读一致副本升级通过；原库schema和业务计数不变。 |
| `.venv/bin/python scripts/benchmark_ninth_queries.py --batches 5000 --output docs/iteration-9-forecast-query-benchmark.json` | 30,000样本SQL分页/聚合及索引路径通过；数值见专门记录。 |
| `git diff --check` | 通过。 |

原生产浏览器回归保留所有账户隔离、草稿、迟到写响应、事件与能力断言。状态请求新增account_id后，其拦截模式和诊断文本断言同步调整；受控时钟只在无关读取完成后前进，避免人为把另一个正常GET也推进超时。测试过程中还实际发现并修复“账户状态冒充全局能力状态”，并新增独立API回归，未删去失败测试以换取通过。新trace策略避免把输入的测试Key存入网络快照。

截至本次本地验收记录时间，远端 main 为第八轮 `046fdfc`；重新查询的 [Actions 40909112902](https://github.com/180cmchn/AI-Powered-Portfolio-Intelligence/actions/runs/40909112902) 为 completed/success。这是旧基线的成功，当时本轮尚未推送，**该记录不证明本补丁的远端 CI 通过**。CI 配置已保留原三组并加入第九轮组；本补丁推送后的结果须按实际提交 SHA 对应的 Actions 另行确认。
