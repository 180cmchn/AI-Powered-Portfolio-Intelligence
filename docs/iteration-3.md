# 第三轮修复与验收记录

审查基线：`7c9358367d98890dedd732b449224028cdd23485`（第二轮提交）。本轮保留主/子 Agent 独立会话、真实工具循环、持久研究需求、公共研究、材料分段/PDF、行情及板块工具，不新增事实审核或准入模块。以下六项均在当前业务代码中确认并修复；旧数据与第二轮说明继续保留。

## 修复与回归对应

| 问题 | 根因与本轮行为 | 主要代码 / 回归依据 |
| --- | --- | --- |
| P1-1 同文档不同读取长度误报更正 | 原先用读取正文及参数参与版本判断。现在原始版本与读取表示分离，同版不同页码、偏移、长度共存；字节实际变化仍建立版本。摘要、原文、Agent 提取和生成报告保留独立语义。 | `app/materials.py`、`ResearchTools.read`；`test_third_materials.py` 的 lengths、pages_offsets、original_change、independent_identity、fetch_metadata 场景。 |
| P1-2 Agent 先保存导致漏通知 | 原先只在创建材料时登记通知。现在 `notify=True` 按文档版本锚点幂等补建通知，可靠登记下游需求后才确认送达；中断重放使用稳定事件键。 | `save_material`、`notify_material_changes`；existing_material、agent_then_scan、partial_delivery、transaction_abort 场景。 |
| P1-3 `needs_review` 后停止价格跟踪 | 原先建议状态混合判断有效性与价格条件，普通报告又可能漏建复核需求。现在独立保存 `review_status`、`price_state`，待复核继续观察价格，并有去重需求、有限自动复核及明确重试。 | `app/recommendations.py`、`agents.py`、manager 提示词；`test_third_recommendations.py` 的 plain_report、maintain_and_update、restart、stale_quote、local_scope 场景。 |
| P1-4 历史记录挤掉活跃建议 | 原先混合排序再截取最近 N 条。现在先筛选业务状态再分页；Agent 获得覆盖信息及事件直接关联项，页面活跃和历史分开。 | 建议查询 API、`recommendation_context`、`list_recommendations`、前端；active_pagination、active_pages、production_research_tools 及两组 Chromium 测试。 |
| P2-1 旧公共任务晚归覆盖当前范围 | 原先任务和研究记录未绑定候选范围。现在保存 `universe_version` 和输入快照，旧范围完成后标记历史适用性；记录按版本保留，当前共享查询不被旧任务覆盖。 | `app/scheduling.py`、`save_research_records`、`Service.execute_global`；`test_third_consistency.py` 的 old_global_completion、scope_change、current_research 场景。 |
| P2-2 材料 ID 被直接显示为有效证据 | 原先缺少统一引用存在性、范围和位置解析。现在报告、建议、研究记录统一生成解析状态；未知/其他账户 ID 不披露内容，来源链接不冒充已存原文，有限修正后仍可带缺口发布。 | `app/references.py`、发布链路、前端；citations_resolve、publication_preserves、reference_feedback 及浏览器跨账户/选页定位断言。 |

补充的 `test_third_review.py` 覆盖新范围不继承旧范围重试耗尽、单证券任务不错误完成其他证券复核需求。已有取消、持仓更新、恢复和多轮调度回归继续执行。测试名称完整列表以仓库测试文件为准。

最终并发审查还复现了“模型运行期间出现新的价格事件，旧任务发布时误将新事件完成”。现已限制任务只能完成自身覆盖的复核需求；维持或原位更新保留后到事件，替代建议继承未覆盖事件并进入后续上下文。三个参数化生产回归先失败后通过，确认后续任务处理完毕后不会无限派发。

真实产物页面审阅另发现导航刷新期间的账户选择可能被忙碌保护丢弃。前端现保留并应用最后一次选择，浏览器回归覆盖请求暂挂时连续切换与各账户草稿隔离；检索材料按钮也明确标为检索结果。

## 数据与升级

当前 schema 为 3。启动 schema 1/2 数据库前，Store 用 SQLite 在线备份保留 `<数据库文件名>.before-v3.bak`（包含 WAL 已提交内容，权限 0600），已存在备份不会覆盖。schema 1 仍会生成第二轮 `.before-v2.bak`。迁移可重复执行，保留账户、持仓、报告、旧材料 ID、引用和历史建议。

材料的 `document_version_id` 表示同一文档版本，`material_id` 表示可读取的已存表示；同版多个片段可以通过文档版本 ID 检索。`document_identity_kind=raw_bytes_sha256` 才表示已保存原始字节身份；只有摘要/文本时为 `representation_sha256`。旧记录缺失这些证据时保留 `legacy_unknown` 或相应未知状态，不根据 URL 或摘要补造文件哈希、完整读取状态。历史公共研究没有候选快照时范围版本为未知，不自动成为当前公共结论。

旧建议保留 ID，补充的历史价格观察为未知；待复核记录获得可追踪需求。新建议替代关系、原位更新修订和对应任务均可追溯。旧引用仍保留，前端旧格式未解析项不显示成已建立证据链。

升级前也可在设置页生成独立手动备份。恢复时先停止服务：

```sh
./stop.command
.venv/bin/python scripts/restore.py data/live.sqlite3.before-v3.bak --mode live
./start.command
```

恢复脚本检查 SQLite 完整性、模式和支持的 schema，拒绝运行中覆盖，并先备份被替换库。使用当前代码重启后会再次执行迁移；如需回退旧程序，应配合升级前备份，不能让旧代码直接读取 schema 3。备份不包含 `.env`，密钥需按原部署方法单独保管。

## API 与调用方兼容

| 请求 | 返回与用途 |
| --- | --- |
| `GET /api/accounts/{id}/recommendations` | 兼容列表；默认 `view=all`，活跃建议优先，保留历史可见性。默认最多 100 条。 |
| `GET .../recommendations?view=active&envelope=true&limit=10&offset=0` | 页面/新调用方分页：`items,total,offset,limit,next_offset,has_more,view,counts`。`page=true` 也是 envelope 别名。 |
| `view=history / triggered / needs_review` | 分别查询已撤销/过期、价格满足的活跃建议、待复核建议；先筛选再分页。 |
| `GET .../recommendations/{recommendation_id}` | 当前账户单条详情，含独立价格、判断状态及 `review_followup`。 |
| `POST .../recommendations/{recommendation_id}/retry` | 对仍活跃的待处理事项显式重新安排复核；不恢复已撤销或已过期建议。 |
| `GET /api/research?current_only=false` | 当前公共结论之外的历史范围记录也可查询；默认 `current_only=true`。 |
| `GET /api/materials/{id}?...` 与 `/range?...` | `account_id` 指定可访问账户范围；省略仅允许公共材料。分段参数仍为 `start`、`length`。列表接口采用相同范围规则。 |

`counts.active`、`counts.history`、`counts.triggered`、`counts.needs_review` 是各范围总数，后两个可能重叠。`triggered` 表示价格满足，不能据此认定判断有效。账户工具提供 `list_recommendations` / `get_recommendation`；初始上下文额外携带 `event_linked_items`，直接触发本轮研究的建议不会被第一页上限挡住。

普通文字报告省略旧建议时，价格观察继续，判断进入待复核。对于同一未解决事项，自动补做一次仍无明确维持、更新或撤销，就保留 `waiting_review` 及原因，等待显式重试或新的事件。这样既不静默停止，也不由重复报告触发无限模型调用。单证券任务、过期和持仓版本变化各自保持原有边界。

报告、建议、公共研究的 `references` 分为 `resolved`、`source_link`、`unresolved`，附可用来源、页码/字符范围和访问完整性。它只说明引用指向和权限，不评判真假、利好利空或来源可信度。模型可继续研究并表达不确定性。

真实链路审阅还发现：已存 Agent 提取或生成报告本身可被定位，但这不等于已关联外部原文。此类引用保留 `representation_kind`，通过 `origin_references` 递归展示提交的父材料及各自解析状态；`origin_status=linked_materials` 只表示提交了父材料 ID，父项仍可能未解析或仍是生成内容。未关联时页面明确说明缺口，提取者自述来源页码放在 `claimed_source_range`，不作为已保存原文页码。抓取失败的材料只保留来源链接或未解析状态，不提供原文证据按钮。

公共运行记录附 `universe_version`、`input_snapshot`、`applicability`；`current/stale/legacy_unknown` 分别表示当前、历史、未知适用范围。账户结果另记本次范围和 `shared_research_used`。页面展示实际使用的公共研究记录与使用时范围；旧范围原始事实仍可读取。

## 可复现验证

```sh
# 生产业务代码、临时 SQLite、受控时钟与协议替身
.venv/bin/python -m pytest -q

# 两组实际 Chromium 行为回归
npm ci
npx playwright install chromium
PYTHON=.venv/bin/python npm run test:browser
```

本轮已实际执行两组浏览器脚本并通过，不能以 `node --check` 替代：

- `tests/frontend_iteration.mjs`：明确 HTTP 夹具与可控浏览器时钟，验证市场自动刷新、未保存草稿/焦点、106 条混合建议、超过一页的活跃建议、引用解析、选页 PDF、公共范围与报告复用范围、增量事件。
- `tests/frontend_production.mjs` + `browser_app_server.py`：启动生产 FastAPI，创建并销毁独立临时 SQLite，验证活跃/历史分页、价格满足但判断待复核、持久重试、证据范围隔离、公共任务晚归和账户切换。关闭后台 worker，不调用模型或行情服务。

GitHub Actions 安装 Python 依赖、固定 npm 锁文件和 Chromium（Linux 使用 `npx playwright install --with-deps chromium`），随后运行 Python 与两组浏览器测试。失败时上传 `test-results/` 中截图/页面记录。前端运行依旧无需 npm 构建。Python 测试收集与浏览器服务器都在导入应用前指定临时库，避免默认初始化日常库。

Python 最终命令、实际测试数及新增回归结果统一记录在 [验证记录](verification.md)，不沿用第二轮的 131 项作为本轮结果。上述确定性/浏览器测试不等于真实联网或真实模型研究质量验证。

## 真实链路与当前限制

本轮已使用用户配置和授权的模型执行真实小规模链路：公共研究与 B 账户完成，A 初始及改仓后研究均因主 Agent 请求 ReadTimeout 失败，完整端到端验收未通过。两只证券没有取得可用当前报价；A 改仓后的输入仍保留，B 账户、报告与任务不受影响。实际服务、时间和输出路径见 [验证记录](verification.md)、[数据源实测](providers.md)，已取得报告的来源和计算核对见 [真实产物逐项核对](third-model-review.md)。后者是开发助手审阅，未代替用户人工签字。

`scripts/verify_live_chain.py --check-config` 只读取项目 `.env` 与只读项目设置库，后者优先，不扫描无关凭据。真实运行在独立评估库完成公共研究、两个不同账户、单账户持仓修改后重算，并保存实际来源、引用、事件、版本、指标和失败原因。脚本默认每轮最多 40 次模型调用、600 秒、每个 Agent 20 轮工具循环；本次研究范围收窄为营业收入、归母净利润与账户敞口。四项模型配置、执行命令、费用预算和退出码见 [Agent 文档](agents.md#小规模真实服务链路)。可选协议探测单列，HTTP 200 不等于数据口径或研究成功。

外部服务稳定性、材料完整性和模型分析质量仍需实际证据。部分 PDF、摘要或网页不可访问时保留限制；不新增 OCR、完整财务库、真实交易、自动下单、完整回测或全市场股票池，也不引入独立审核 Agent。主 Agent 继续负责资料相关性、账户影响和投资判断。
