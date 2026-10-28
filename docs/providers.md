> 第四轮行情实测和间歇断连限制见 [行情修复记录](fourth-quotes.md)。下方较早日期的网络记录保留为历史观察。

# 行情、搜索、原文与日历适配

适配代码位于 `app/providers.py`。证券身份使用交易所加六位代码，例如 `SH:600519`、`SZ:000001`、`SH:510300`。名称不作为身份键。数量由账户模块保存为股或份，行情价格为每股或每份人民币价格。价格与涨跌幅返回十进制字符串，避免二进制浮点误差。

## 第六轮当前入口

模型供应商和行情数据源是不同配置。模型连接在“模型供应商”页面只填写 Key、Base URL、协议，后端自动发现目录；在统一角色或账户模型矩阵选择模型及 effort。供应商安全存储、前缀规范化、分页、缓存、重定向和凭据边界见[供应商与统一角色](sixth-provider-roles.md)。正常使用不再要求手填模型名或密钥环境变量名。

基础行情自动刷新由 `market_refresh_enabled` 独立控制，已发布预测评分由 `forecast_evaluation_enabled` 独立控制；暂停 `scheduled_research_enabled` 不停掉它们。预测评分采用证券精确基准日/目标日的历史收盘和同源可比调整证据，不复用当前报价缓存冒充历史价格。规则及不可评估情况见[预测评估口径](forecast-evaluation.md)。

默认日历仍是已核实的 `calendar_2026.json`；`STOCK_CALENDAR_PATH` 支持一个文件或包含 `calendar_*.json` 的目录。每年文件必须有明确覆盖区间与来源，跨年缺口保持未知。Mac 启停、升级前备份和换机说明见[运行说明](mac-operations.md)。以下真实网络观察均保留其当时日期与结论，不表示第六轮供应商目录或模型调用已经通过。

## 第三轮实测（2026-10-25，Asia/Shanghai）

本轮重新执行 `PYTHONPATH=. .venv/bin/python tests/provider_smoke.py`。受限进程首次返回 ConnectError/gaierror；获准联网后于 **01:02:09–01:02:11 +08:00** 完成实际应用适配器访问。原始观察摘要见 [third-network-observations.json](third-network-observations.json)。

| 路径 | 本次观察 |
| --- | --- |
| 股票历史、板块历史、沪深300基准 | 各返回 11 行，覆盖到 2026-10-22；金额单位人民币元，成交量保留供应商“手”口径 |
| 行业 / 概念目录 | 各取 5 行，服务返回总量 496 / 516，明确截断 |
| 板块成分 | 取 5 行，服务返回总量 14，明确截断 |
| 财务数据 | 8 行，最近报告期 2026-06-30、披露字段 2026-08-15，保留原字段与单位 |
| 巨潮 PDF | [公开报告](https://static.cninfo.com.cn/finalpage/2026-08-15/1225475868.PDF)，832,321 字节、110 页；检索后实际读取物理第28页，1,136字符，`access=excerpt` |

PDF 原始字节 SHA-256 为 `0e10aa26be46b1cf3cd03f06e834c7fb98d5dd0d661b96f8fddd4af7e846a4f6`。搜索实际检查范围、命中页和截断标记均留存；并未声称完成全文阅读。不同片段共用文档版本，`requested_chars` 记录读取上限。搜索服务仍为 `disabled`，没有执行 Tavily/Brave 认证；模型验收可以直接读取公开 URL，不能把它写成搜索服务通过。

这些观察证明本次取得了结构化数据和原文片段，不保证接口长期稳定，也不独立认证所有财务口径。四轮真实模型链路、包括第一次预算耗尽的失败记录，另见 [验证记录](verification.md)。下文保留前两轮实测和接口说明。

## 东方财富公开行情

`quote_provider=eastmoney` 使用东方财富网页的 `https://push2.eastmoney.com/api/qt/stock/get`。上海 `secid=1.代码`，深圳、北京 `secid=0.代码`。这是实际验证过的网页数据途径，**不是已经取得授权的开放 API，也没有已核实的实时性、配额或可用性 SLA**。网页接口可能改变、限流或拒绝请求。适配器边界明确，可替换 `MarketProvider.quote/security` 接入用户拥有权限的数据供应商。

2026-10-22 查阅了[东方财富用户服务协议](https://about.eastmoney.com/home/protocol)，其知识产权条款包含行情复制、对外提供与衍生用途的限制，免责条款不保证数据及时、连续、准确。运行前应确认自己的使用范围与数据权限；本项目不授予任何行情许可、不绕过登录或访问控制。需要持续或正式服务时，建议把同一适配接口连接到拥有相应用途授权的行情服务。

读取字段：`f43` 最新价格、`f57` 代码、`f58` 名称、`f59` 价格小数位、`f86` 数据源时间戳、`f170` 涨跌幅（百分数的百分之一）、`f127` 数据源行业分类。ETF 不能固定除以 100，本实现使用 `f43 / 10**f59`。接口缺失精度时不猜测价格。`as_of` 仅来自 `f86`，`fetched_at` 是本地抓取时间；该网页时间字段不保证等同最后一笔成交时间。数据源不提供时间时显示 `unknown_time`；时间过旧显示 `stale`，不改写为当前行情。休市后旧行情通常是合理的最近快照，界面仍展示其实际时间与年龄。缺失有效价格不等于已经确认停牌，证券交易状态保留 `unknown`。

`security()` 同样从返回代码和名称补全基础资料。行业是供应商分类；ETF 的底层行业穿透需结合基金资料研究，不能把基金名称当作完整行业敞口。仅支持可识别的 A 股与 ETF；未知类型和无效代码保留明确错误。不接入券商账户、不读取实际可卖数量。

### 已执行的真实网络探测

在当前 Mac 上，2026-10-22 22:42:50（Asia/Shanghai）执行三次只读 HTTP 请求；三次均返回 HTTP 200、`rc=0`，没有密钥或账户登录。以下是当次响应摘要，不是实时报价，也不会导入账户历史：

| 证券 | f43 / f59 | 解析价格 | f86 解析时间 | 数据源名称 |
| --- | --- | --- | --- | --- |
| SH:600519 | 131849 / 2 | 1318.49 | 2026-10-22 16:12:00 +08:00 | 贵州茅台 |
| SH:510300 | 4107 / 3 | 4.107 | 2026-10-22 16:11:43 +08:00 | 沪深300ETF华泰柏瑞 |
| SZ:000001 | 1130 / 2 | 11.30 | 2026-10-22 15:34:00 +08:00 | 平安银行 |

初次受限工具环境的请求被 macOS 沙箱阻止（Operation not permitted）；获准直接联网后上述请求成功。这验证当前网络、路由与字段样例，不能证明未来可达性或全市场覆盖。北京行情代码映射尚未用真实证券实测。

随后对最终 `httpx` 适配器进行了真实调用：22:50:51 和 22:51:47 返回 `RemoteProtocolError`（服务端未给响应即断连），系统正确保留价格、行情时间为空并显示 `unavailable`。22:51:23 的两个直接 `httpx` 请求、22:52 左右的 `urllib` 和关闭环境代理的直接 `httpx` 请求又返回了相同的有效行情。后续对相同路由的直接请求再次断连。已将请求头调整为包含应用名称的浏览器兼容格式，但不能把一次成功解释为稳定修复；**当前结论是公开路由已实测可达、字段解析已验证，持续可靠性未证实**。没有静默生成替代价格。

公开原文适配器于 22:50:51 成功读取上述上交所年度休市公告，返回标题与 1,555 字符文字、`access=full_text`、`published_at=null`（页面没有本提取器识别的日期元数据，不能把页面正文日期自动冒充元数据）。这是最终应用读取代码的真实网络验证。

### 最终应用后端联网检查

2026-10-22 **22:59:09.750832–22:59:10.002087 +08:00**，使用 `tempfile.TemporaryDirectory` 内的全新 `mode=live` SQLite 数据库，未读写 `data/live.sqlite3`。调用最终 `MarketProvider.quote()` 一次，再创建两个都持有 `SH:510300` 的账户，数量分别为 **1,000 份**与 **3,000 份**、偏好分别为保守与进取，使用真实 `Store.claim_jobs()` → `Service.execute()`，没有注入模型或行情替身。

- 三次最终行情适配器调用均遇到 `RemoteProtocolError`，均返回 `status=unavailable`、`price=null`、`as_of=null`；因此这次检查**没有验证成功取得实时估值**。
- 两账户分别保留自己的数量、偏好与输入版本 `1`。行情缺失时持仓市值、内部权重、总资产及未实现收益保持 `null`；已知价格部分合计为 `0.00`，不能解释为完整账户市值为零。
- 因显式留空 `model_name`，两任务均进入 `waiting_config`，错误说明为“请先配置 model_name；应用不会默认假定任何模型权限”。未生成任何研究历史或投资建议。
- 进程正常退出，临时数据库已删除，无后台服务残留。该验证证明最终代码在真实网络失败和缺模型配置时保留用户数据、隔离账户并准确报告状态；不是成功完成真实多 Agent 研究的证明。

最终行情请求头为 `Mozilla/5.0 LocalStockResearch/1.0`，原文读取请求头为 `LocalStockResearch/1.0`。两者的功能与上述验证一致。

`quote_provider=manual` 明确返回“未启用”，不生成行情。测试中的 MockTransport 与独立演示库均不代表联网数据。

## 模型原生联网（第四轮）

不再配置独立搜索服务。主/子 Agent 的 Responses 请求同时传服务端 `{"type":"web_search"}` 与本地 function；网络由当前模型服务执行，沿用模型凭据。只解析可见调用、URL、标题、片段和引用；不透明续接仅在内存，既不写日志也不作事实证据。

2026-10-26 实测当前 `api.xxyapi.com/v1`、标识 `grok-4.7`：发现搜索返回两次完成的搜索调用、15个来源URL；混合本地函数两轮续接也通过。上游身份未独立验证。服务返回链接及零长度引用位置，不表示本地下载全文或定位页码。详细脱敏产物见 `fourth-native-probe.json` 与 `fourth-native-mixed.json`。

参考 [OpenAI Web search](https://developers.openai.com/api/docs/guides/tools-web-search)、[xAI Web Search](https://docs.x.ai/developers/tools/web-search)、[xAI Citations](https://docs.x.ai/developers/tools/citations)。官方能力不自动等于第三方端点能力。能力探测失败不会启用其他平台。

## 公开网页和公告

`read_url(url, config, *, start_page=None, end_page=None, page=None, offset=0, limit=None, refresh=False)` 支持公开 HTML、文本和可提取文字的 PDF。保留原 URL、重定向 URL、标题、内容类型、可见出版日期、原始抓取时间及正文。HTML 额外返回 `links`（最多 200 条）与 `attachments`（最多 100 条，各列表链接及标题合计最多 32,000 字符，并各自标明截断），根据最终页面 URL 和合法的 HTML `base` 解析相对链接；附件识别包括 PDF/Office 扩展名、下载属性与附件文字。发现链接不等于已读取附件，访问时仍单独执行 SSRF 检查。

PDF 工具共用下载缓存：

- `pdf_metadata(url, config, refresh=False)`：标题、元数据、总页数，`access=metadata`，没有读取正文。
- `search_pdf(url, query, config, start_page=1, end_page=None, max_results=20)`：按字面关键词（忽略大小写）定位，返回物理页码、字符位置、短上下文、`#page=N` 原文链接、实际 `searched_range` 和 `next_page`。默认每次最多检查 100 页，达到范围/命中上限或出现无文字页时，`search_complete=false`。达到命中上限后应读取最后命中页，再从下一页继续搜索；搜索结果本身始终是 `access=excerpt`。
- `read_url(..., page=29)` 或 `start_page=25,end_page=30`：读取第 20 页之后任意指定范围。默认每次最多 20 页，并非只能访问前 20 页。返回 `requested_range`、`extracted_range`、实际 `returned_range`、各页的正文与字符偏移、`next_page`。一页过长时用 `offset` / `limit` / `next_offset` 分段续读。
- HTML/文本也支持 `offset` / `limit`，字符偏移相对于所选正文；PDF 字符偏移相对于所选页范围拼接后的文字（包含页码标记）。继续字符读取时应保持同一页范围。

页码均从 1 开始，指 PDF 物理页，可能不同于报告印刷页码。返回 `full_text` 只表示本 HTTP 响应的全部可提取文字未被工具截断；选页、字符切片、页数限制或已检查页存在空文字时均为 `excerpt`。不能提取任何文字时返回 `failed`，保留原因。本版不执行网页脚本、不做 OCR；图片型表格或动态加载的内容可能仍未取得。

缓存采用进程内 LRU，默认 300 秒、32 个文档、32 MB 总容量；单文档默认 5 MB，上限 20 MB。相同 URL 的并行读取共用一次有总超时的下载。`document_id` 是**完整响应字节**的 SHA-256，能发现第 20 页之后的更正；`fetched_at` 始终是该版本首次下载时间，`retrieved_at` 是本次取用时间，`cache_hit` 区分缓存。`refresh=True` 强制重新下载，资讯更新扫描应使用它检测新版本。缓存不会在重新下载失败时静默返回旧内容，也不跨服务重启保存；持久材料正文与版本由 Store 管理。默认输出 40,000 字符，单次文档工具总超时 30 秒。

网页与公告是研究数据，不能写账户或更改工具权限。读取工具只支持公开 HTTP(S) 和标准端口；拒绝本机、私网、保留地址、URL 登录凭据；每次重定向重新解析检查，并将连接固定到检查过的公共 IP，通过原 Host 与 TLS SNI 访问。该边界防止研究材料触达本地服务，不判定材料可信度。适配层**不按平台、观点、评分或来源数量拒收研究内容**；混合材料原样送给 Agent 提取与分类。

## 2026 交易日历

`data/calendar_2026.json` 根据[上交所](https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml)、[深交所](https://investor.szse.cn/disclosure/notice/general/t20251222_618087.html)、[北交所](https://www.bse.cn/important_news/200027428.html)发布的 2026 年休市安排录入，已核对三所年度安排一致。调休周末仍休市；不采用国务院工作日替代交易日。

`market_state()` 统一转换为 Asia/Shanghai，调度时段为 09:30–11:30、13:00–15:00，不把集合竞价视为常规扫描窗口。日历覆盖范围外返回 `trading_day=null, phase=unknown`，而不是仅按周一至周五猜测。`STOCK_CALENDAR_PATH` 可指向更新后的 JSON 或包含 `calendar_*.json` 的目录，支持 `overrides: {"YYYY-MM-DD": false}` 处理临时休市；`coverage` 必须与已核实日期一致。当前并未自动下载后续年度日历，进入 2027 年前需要更新。日历不是单证券停牌信息。

## 配置键

| 配置 | 默认 | 作用 |
| --- | --- | --- |
| quote_provider | eastmoney | eastmoney 或 manual |
| network_timeout_seconds | 12 | 每次外部 HTTP 连接/读写超时 |
| quote_stale_seconds | 180 | 行情时间距当前超过该值标为 stale |
| model_native_search | true | 当前角色模型会话原生联网；能力需真实观察 |
| market_refresh_enabled | true | 基础行情自动刷新，独立于模型定时研究 |
| forecast_evaluation_enabled | true | 已发布预测的到期评估，独立于模型凭据与研究 |
| scheduled_research_enabled | true | 新定时研究；账户正式批次另需明确启用 |
| search_max_results（历史） | 不再进入当前运行设置 | 早期独立搜索适配参数；本轮不恢复 Tavily/Brave |
| read_timeout_seconds | 30 | 原文读取总时限 |
| read_max_bytes | 5000000 | 响应字节上限 |
| read_max_chars | 40000 | 可见正文字符上限 |
| read_max_pdf_pages | 20 | 每次 PDF 读取最多页数，指定起始页可访问后续内容 |
| read_search_max_pages | 100 | 每次 PDF 检索最多页数，1–300 |
| read_cache_seconds | 300 | 文档缓存 TTL，0 禁用，最大 3600 秒 |
| read_cache_max_bytes | 32000000 | 全部文档缓存总字节预算，1–100 MB |
| research_provider | eastmoney | 历史/板块/财务：eastmoney、tushare；manual/disabled 返回未启用 |
| tushare_token_env | TUSHARE_TOKEN | Tushare 密钥所在环境变量名 |
| research_cache_seconds | 300 | 结构化研究数据缓存 TTL，0–3600 秒 |

## 可重复验证

```sh
.venv/bin/python -m pytest tests/test_providers.py tests/test_provider_documents.py tests/test_provider_market.py -q
```

测试覆盖 ETF 精度、行情原始时间、身份不一致、未知精度、网络超时、HTTP 认证失败、密钥不回显、缺搜索密钥、混合材料保留、缺发布时间、读取失败、私网/重定向边界、截断标识及节假日/未知年份。确定性测试不代表 Agent 已正确理解材料；分类质量由运行时模型样例评估单独验证。


## 第二轮：历史、板块与基本面工具

`MarketProvider` 将研究行情与即时 `quote()` 分开；`research_provider` 可与 `quote_provider` 不同。默认公开源仍是东方财富，认证替代源为 Tushare。所有接口返回 `status`、`error`、实际 `source`、`source_url`、`fetched_at`、实际数据日期 `as_of`、`truncated` 与范围说明。`status=ok` 表示本次返回记录可解析，并不宣称数据完整、实时或业务上正确。没有记录为 `empty`；拒绝请求、缺权限和格式异常为 `unavailable`，不制造数据。

| 方法 | 数据及边界 |
| --- | --- |
| `history(symbol,start,end,interval="daily",adjust="none",limit=250)` | OHLC、成交量、成交额、涨跌幅；日期范围至多十年，每次输出至多 1000 根，长区间须分段。东方财富支持日/周/月和 `1m/5m/15m/30m/60m`，复权 `none/qfq/hfq`；1 分钟只允许不复权。分钟历史覆盖、复权样例尚未实测，不保证供应商返回完整窗口。Tushare 当前适配器只接 A 股未复权日线。 |
| `sectors(kind="industry",limit=100,offset=0)` | 东方财富行业或概念分类及返回的涨跌家数；Tushare 为申万 2021 一级行业。`offset` 是 `limit` 的非负整数倍，按返回 `next_offset` 续取。 |
| `sector_members(sector_id,limit=100,offset=0)` | 当前成分身份；公开源可返回行情及源时间。`breadth` 只计算当前返回页且有涨跌幅的成分，明确 `scope=returned_members_only`。不冒充历史成分或全市场广度。 |
| `sector_history(sector_id,start,end,limit=250)` | 板块日线及量额口径；公开源使用 `BK...`，Tushare 申万行业使用 `...SI`，身份不得混用。 |
| `benchmark_history(benchmark="CSI300",start,end,limit=250)` | 沪深 300、上证 50、中证 500 的指数日线；Agent 可按共同日期与板块数据比较。输出指数点位，与持仓证券行情分别查询。 |
| `fundamentals(symbol,periods=8)` | 股票利润表（最多 20 条）、累计收入、归母净利润、营业利润、利润总额、报告期、公告日期与原始供应商字段。Tushare 使用合并利润表。相同报告类型、相隔一年且同月日的记录可计算同比差额；基期大于零才输出同比百分比。没有把累计半年报减季度报后直接当成可比环比。 |

东方财富公开路由分别为 `push2his.eastmoney.com/api/qt/stock/kline/get`、`push2.eastmoney.com/api/qt/clist/get` 和 `datacenter-web.eastmoney.com/api/data/v1/get` 的 `RPT_DMSK_FN_INCOME`。这些是已探测的公开网页实现，**没有正式开放 API 合同、配额保证、全市场权限或实时推送承诺**，其服务协议限制与前文相同。原始 K 线量额使用“手 / 人民币元”；列表通过 `fltt=2` 请求小数格式，不能再次将涨跌幅除以 100。每条量额都明确单位，不混用 Tushare 的“手 / 人民币千元”与申万行业的“万股 / 人民币万元”。

Tushare 按官方 [HTTP JSON 协议](https://tushare.pro/document/1?doc_id=40)实现 `api_name/token/params/fields`，本项目只通过 HTTPS 发送 token，不降级到明文 HTTP。已核对 [daily](https://tushare.pro/document/2?doc_id=27)、[申万分类](https://tushare.pro/document/2?doc_id=181)、[分级成分](https://tushare.pro/document/2?doc_id=335)、[申万日线](https://tushare.pro/document/2?doc_id=327)、[利润表](https://tushare.pro/document/2?doc_id=33)及[权限说明](https://tushare.pro/document/2?doc_id=290)。行业分类/成分与利润表文档要求至少 2000 积分，申万日线要求 5000 积分；频率与权限以账号实际开通情况为准。分钟和公告是另行授权能力，本轮未把它们冒充日线权限附带服务。当前没有 Tushare token，**仅完成官方协议核对和 MockTransport 测试，尚未验证真实认证、账号配额及权限**。

结构化查询使用与文档分开的有界 LRU（128 项、16 MB），相同请求并发时只发起一次下载。缓存原始时间保留，返回缓存年龄、TTL 与 `cache_stale=false`（仅表示缓存未过期，**不表示数据源实时**）；超时、连接中断、非成功返回不会生成替代行情。过期后请求失败时直接报告失败。请求最大 5 MB、总超时默认 30 秒；不无限重试，不自动扩大到全市场下载。不同 token 的缓存键不同且仅保存哈希，错误不回显 token、供应商原始错误正文或请求头。

基础财务接口目前不等于完整三大报表库，也不替代 Agent 查阅正式财报；资产负债表、现金流量表、统一季度重述/追溯调整、历史板块成分、ETF 穿透及经认证的分钟数据仍是功能扩展范围。原报告与公告可由搜索、HTML 附件发现、PDF 定位工具读取。

## 第二轮真实联网验收（2026-10-23，Asia/Shanghai）

下列测试直接运行最终 Python 适配代码，无模型、搜索、行情或文档替身，没有读写账户数据库。

- **00:08:09**：行业、概念、`BK0420` 成分分别成功返回 5 条，供应商总数分别为 496、516、14；这些是该次供应商返回的分类数量，不等于全市场覆盖证明。利润表成功返回 `SH:600519` 最近 8 条，最新报告期 `2026-06-30`、披露日 `2026-08-15`。同轮股票 K 线、板块 K 线和基准 K 线遇到 `RemoteProtocolError`，均返回空记录及 `unavailable`。
- **00:11:08**：将三种历史查询缩小为 `2026-10-19`–`2026-10-22`，最终 `history()`、`sector_history()`、`benchmark_history()` 都成功返回 4 根日 K，实际最新日期为 `2026-10-22`。这证明最终解析可用，**也保留了前次真实断连证据，不能据此宣称公开接口稳定可靠**。
- **00:08:09 起**：实际读取[巨潮披露的贵州茅台 2026 半年度报告](https://static.cninfo.com.cn/finalpage/2026-08-15/1225475868.PDF)，832,321 字节、110 页，完整字节 SHA-256 为 `0e10aa26be46b1cf3cd03f06e834c7fb98d5dd0d661b96f8fddd4af7e846a4f6`。`pdf_metadata()` 返回 `access=metadata`；对第 21–80 页搜索“归属于母公司”命中物理页 28、31、32、37、38、73；`read_url(page=28)` 成功取得 1,136 字符且包含该关键词。检索和页读取均 `cache_hit=true`，文档 hash 相同，页读取 `access=excerpt`、`returned_range={start_page:28,end_page:28}`，没有把局部读取标成全文。
- 认证搜索与真实模型仍需各自凭据；这次联网材料/行情验证**不能替代真实模型研究评估**。本适配器未发送 Tushare 认证请求。

可重复联网探测（显式执行才联网，公共服务当时不可用会原样报告失败）：

```sh
PYTHONPATH=. .venv/bin/python tests/provider_smoke.py
```

第二轮 provider 确定性测试执行：

```sh
.venv/bin/python -m pytest tests/test_providers.py tests/test_provider_documents.py tests/test_provider_market.py -q
# 55 passed
```

其中长 PDF 回归由真实 pypdf 解析 35 页测试文档，HTTP 是可控替身；它验证附件 → 元数据 → 第 29 页检索/读取、单次字符和页数边界、空页/OCR 状态、缓存/刷新/并发下载、完整字节版本标识。市场回归验证真实请求协议、字段与单位、身份校验、财报累计期同比、分页广度、不支持口径、缺凭据、权限拒绝、总超时及密钥不回显。原有 24 条 provider 测试全部保留通过。

### 公开报价备用源

`QUOTE_FALLBACK_PROVIDER=sina` 在东方财富暂时性连接失败后读取新浪证券网页实际使用的报价资源；`none` 关闭。保留实际来源、证券身份、源时间与获取时间；HTTP 429 不转备用接口。整个报价请求最多45秒，新浪一次最多8秒。公开网页资源没有开发者API合同或SLA，仅用于本地小规模个人研究；真实测试和限制见 [第四轮行情记录](fourth-quotes.md)。
