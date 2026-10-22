# 行情、搜索、原文与日历适配

适配代码位于 `app/providers.py`。证券身份使用交易所加六位代码，例如 `SH:600519`、`SZ:000001`、`SH:510300`。名称不作为身份键。数量由账户模块保存为股或份，行情价格为每股或每份人民币价格。价格与涨跌幅返回十进制字符串，避免二进制浮点误差。

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

## 搜索服务

配置 `search_provider=tavily` 或 `brave`，把密钥置于环境变量，设置 `search_key_env` 为变量名；默认分别读取 `TAVILY_API_KEY`、`BRAVE_API_KEY`。前端和数据库只需保存变量名，不保存密钥。缺少服务或密钥时返回 `status=unavailable` 与原因，不返回模拟资讯。

接口依据 2026-10-22 核对的供应商文档实现：[Tavily Search](https://docs.tavily.com/documentation/api-reference/endpoint/search) 使用 POST `/search` 与 Bearer 认证；[Brave Web Search](https://api-dashboard.search.brave.com/app/documentation/web-search/get-started) 使用 GET `/res/v1/web/search` 与 `X-Subscription-Token`。结果保留标题、URL、摘录、供应商返回的发布时间与抓取时间。日期没有返回时为 `null`，不从抓取时间推测。搜索摘录统一标记 `access=excerpt`，不声称读过原文。供应商可能计费，需拥有对应账号与套餐权限。

本次没有应用搜索密钥，因此**未完成 Tavily/Brave 真实认证请求**；测试仅验证请求格式、缺配置、错误处理和字段保留。开发时浏览器/检索工具访问文档不等于应用已拥有该工具或其 API 权限。

## 公开网页和公告

`read_url(url, config)` 支持公开 HTML、文本和可提取文字的 PDF。保留原 URL、重定向后的 URL、标题、抓取时间、内容类型、可见出版日期及正文。PDF 返回页号；无法提取扫描件文字时明确失败，本版本没有 OCR。动态网页、登录页、反爬、网络失败可能导致无法取得正文；调用方可保留已获得的搜索摘录并注明访问失败。

返回 `full_text` 表示在本次 HTTP 响应内提取到的文字没有被本工具截断，不代表付费/动态页面之外的所有内容均已获取。`excerpt` 表示受字符或页数限制截断，`failed` 表示未读到原文。默认 5 MB、40,000 字符、20 页、总读取 30 秒。网页脚本不执行。

网页与公告是研究数据，不能写账户或更改工具权限。读取工具只支持公开 HTTP(S) 和标准端口；拒绝本机、私网、保留地址、URL 登录凭据；每次重定向重新解析检查，并将连接固定到检查过的公共 IP，通过原 Host 与 TLS SNI 访问。该边界防止研究材料触达本地服务，不判定材料可信度。适配层**不按平台、观点、评分或来源数量拒收研究内容**；混合材料原样送给 Agent 提取与分类。

## 2026 交易日历

`data/calendar_2026.json` 根据[上交所](https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml)、[深交所](https://investor.szse.cn/disclosure/notice/general/t20251222_618087.html)、[北交所](https://www.bse.cn/important_news/200027428.html)发布的 2026 年休市安排录入，已核对三所年度安排一致。调休周末仍休市；不采用国务院工作日替代交易日。

`market_state()` 统一转换为 Asia/Shanghai，调度时段为 09:30–11:30、13:00–15:00，不把集合竞价视为常规扫描窗口。日历覆盖范围外返回 `trading_day=null, phase=unknown`，而不是仅按周一至周五猜测。`STOCK_CALENDAR_PATH` 可指向更新后的 JSON，支持 `overrides: {"YYYY-MM-DD": false}` 处理临时休市；`coverage` 必须与已核实日期一致。当前并未自动下载后续年度日历，进入 2027 年前需要更新。日历不是单证券停牌信息。

## 配置键

| 配置 | 默认 | 作用 |
| --- | --- | --- |
| quote_provider | eastmoney | eastmoney 或 manual |
| network_timeout_seconds | 12 | 每次外部 HTTP 连接/读写超时 |
| quote_stale_seconds | 180 | 行情时间距当前超过该值标为 stale |
| search_provider | disabled | disabled、tavily、brave |
| search_key_env | 按供应商默认 | 环境变量名称 |
| search_max_results | 6 | 搜索每次返回上限，1–20 |
| read_timeout_seconds | 30 | 原文读取总时限 |
| read_max_bytes | 5000000 | 响应字节上限 |
| read_max_chars | 40000 | 可见正文字符上限 |
| read_max_pdf_pages | 20 | PDF 页数上限 |

## 可重复验证

```sh
.venv/bin/python -m pytest tests/test_providers.py -q
```

测试覆盖 ETF 精度、行情原始时间、身份不一致、未知精度、网络超时、HTTP 认证失败、密钥不回显、缺搜索密钥、混合材料保留、缺发布时间、读取失败、私网/重定向边界、截断标识及节假日/未知年份。确定性测试不代表 Agent 已正确理解材料；分类质量由运行时模型样例评估单独验证。
