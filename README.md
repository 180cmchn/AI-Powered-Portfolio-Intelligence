# 观澜 · AI-Powered Portfolio Intelligence

运行在 Mac 本机的 A 股与场内 ETF 虚拟账户研究工作台。FastAPI + SQLite + asyncio 后台执行器 + 无构建依赖的原生 JavaScript 界面。主 Agent 和每个子 Agent 都有独立模型上下文及真实工具调用循环；模型原生联网与本地研究工具在同一 Agent 会话协作，不需要独立搜索密钥。

**第四轮已接入 Responses 原生联网、来源记录、可信采集顺序和父引用规范化；增加汇总检查点恢复、行情诊断及休市收盘估值。工程回归、真实搜索、行情和完整账户链路分别验收，结果见 [第四轮交付与限制](docs/iteration-4.md)。**

第三轮历史失败仍保留在 [验证记录](docs/verification.md)，不被新结果覆盖。

仅管理虚拟持仓快照与研究参考，不读取券商账户、不登录券商、不下单、不撮合交易。

![演示模式界面：固定样例与本地计算，不代表真实模型研究](docs/demo-overview.jpg)

## 在 Mac 上启动

需要 Python 3.12+，推荐 Apple Silicon 上使用 Homebrew Python 或 `uv`。本次开发机为 macOS Apple Silicon，使用 Python 3.14。日常运行无需 Node、npm 或 CDN；只需一个后台进程。开发时的浏览器回归需要 Node 20+ 和 Playwright。

```sh
cd "/Users/chn/Documents/ChatGPT/金融股票 2"
cp .env.example .env  # 首次配置；不要覆盖已经填写的 .env
uv sync --frozen
./start.command
```

打开 <http://127.0.0.1:8765>。首次启动自动初始化 `data/live.sqlite3`，没有示例账户。也可以 Finder 双击 `start.command`；已存在 `.venv` 时不会重新安装依赖。

没有 `uv` 时：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/manage.py start
```

停止与查看状态：

```sh
./stop.command
.venv/bin/python scripts/manage.py status
```

前台诊断：`.venv/bin/python scripts/serve.py`。日志在 `data/live.log`。服务只监听回环地址，禁止跨站写请求；首版不提供远程多用户认证。不要使用多个 Uvicorn workers；数据库锁会拒绝同库第二个后台进程。

后台进程启动后，关闭网页不会停止任务。Mac 睡眠期间程序不会持续运行；唤醒后检测时间间隔、刷新数据并仅处理最新输入。退出服务或重启电脑后需再次启动；本版不擅自安装登录项/LaunchAgent，也没有系统推送通知。

## 先体验界面和精确计算

```sh
./start.command --mode demo
```

打开 <http://127.0.0.1:8766>，点击“创建演示样例”。演示使用独立 `data/demo.sqlite3`、明确标记的固定价格和本地计算摘要；**不模拟模型、新闻或子 Agent 研究**。真实模式禁止写入演示种子。停止：`./stop.command --mode demo`。

## 启用真实研究

在 `.env` 中填写你实际有权限使用、支持 Responses 原生 `web_search` 与普通函数共存的模型服务。以下名称是配置键，不代表已经获得任何模型权限：

```dotenv
APP_MODE=live
MODEL_PROTOCOL=responses
MODEL_BASE_URL=https://api.openai.com/v1
MODEL_NAME=你的可用模型标识
MODEL_KEY_ENV=MODEL_API_KEY
MODEL_API_KEY=你的密钥
MODEL_NATIVE_SEARCH=true
QUOTE_PROVIDER=eastmoney
```

重新启动服务使环境变量生效。系统设置页面可以调整模型名、服务地址、协议和**密钥环境变量名**，不接收密钥本身。页面保存的非密钥配置优先于环境默认值；如果已经在页面保存过设置，更改 `.env` 的模型名后仍需检查页面中的模型名。模型接口支持 `responses` 和 `chat_completions`；如需本地无密钥模型，设置回环地址并明确开启 `model_allow_no_key`。

系统设置的“检测原生联网”执行一次有界真实模型请求，观察服务返回的搜索调用项、用量与来源；同配置十分钟内复用探测结果，刷新页面不会调用模型。状态区分已关闭、尚未验证、已观察到原生搜索、不支持、认证失败和请求失败。“检测本地工具续接”另测普通函数协议，不能代替原生搜索验证。

`MODEL_NATIVE_SEARCH` 默认开启，主 Agent 与子 Agent 自主决定使用；沿用模型地址、模型名和模型密钥。Chat Completions 仍可做本地函数研究，但此适配路径不支持原生搜索，不会悄悄切独立服务。第三方端点即使宣称兼容也需实测；上游模型身份不由标识保证。旧数据库 `search_provider=disabled` 不影响新设置；用户明确关闭后会保留。旧密钥不删除、不发送到模型服务。

真实行情默认尝试东方财富网页数据途径，60 秒为初始刷新频率，受对方服务可用性和使用条件限制。页面区分源时间、获取时间、来源和缓存失败状态。单价接口间歇断连时，休市可从同供应商未复权日线取得最近有效收盘价，明确日期精度，不能作为盘中报价触发条件；全通道失败则保留已保存报价或未知。可将 `MarketProvider` 替换为你有权使用的供应商；`manual` 当前表示停用联网行情，不会生成报价。北京市场映射尚未做真实证券探测，停牌状态与 ETF 具体交易规则无法从当前报价端点完整确定。

## 使用路径

1. 创建账户，录入代码及交易所（例如 `SH:600519`、`SZ:000001`、`SH:510300`），填写整数股数/份数。基础资料由代码与市场联合查询补全。
2. 成本和现金允许空值；零与空值分开。无成本不算完整成本收益，无现金只展示证券市值及内部权重。更改数量不会扣现金或产生已实现盈亏。
3. 分别维护自选列表和候选范围。持仓自动纳入监控；移除一个账户的持仓不影响其他账户、自选和活动任务的监控需求。候选范围为全局配置，改变时更新相关账户版本。
4. 保存立即返回新版本与代码计算指标，并排入去抖分析。模型可用时主 Agent 自主派发、追问、等待研究，输出带来源与条件的报告；界面查看子任务、公开消息摘要、工具来源和子报告。
5. 账户更新后旧报告标记过期。旧任务即使晚到也不能覆盖最新结果。分析失败不会回滚已保存持仓。已有完整研究检查点时，工作台重试会先检查版本、行情、资料时效和新事件，再仅恢复汇总；每检查点最多恢复两次，每次最多四个模型请求。
6. 研究页先显示活跃建议与待复核事项，历史建议单独展开和分页。“价格条件已满足”与“判断待复核”可同时存在；自动复核未解决时保留事项，使用“重新安排复核”重试。
7. 引用区分别标明已解析材料、仅来源链接和未解析引用。打开材料可查看已保存页码与字符范围；旧格式没有保存过的原文身份或研究范围保持未知。

交易时段默认：每 5 分钟全局候选研究、15 分钟单账户综合分析；15:15 收盘总结。到期需求先持久化，账户忙碌时延迟并合并，完成后补跑；公共研究供各账户复用。已有研究运行时不会每 5 分钟强制打断。报价更新只计算指标；超过配置价格变动阈值才触发额外研究。可配置公开信息搜索词，新增材料先持久保存，再交公共主 Agent 判断关联性并复用其报告；关联未知时扩大复核，失败时保留事件，没有来源白名单或固定审核 Agent。资讯扫描独立于交易时段，默认 08:00–23:00 每 30 分钟，需配置观察主题和支持原生搜索的模型；任务进入现有公共主 Agent 队列。真实原始内容变化保存新文档版本并触发重评；同版扩展读取页数或长度不触发更正，摘要变化单独标注。结构化建议在价格、期限、资讯或输入版本变化后更新状态；价格观察不替代投资判断，不执行买卖。所有时间按 Asia/Shanghai。

日历已核对沪深北 **2026 年**官方休市安排；超出覆盖年份自动标未知并停止交易时段定时分析，需更新 `data/calendar_2026.json` 或 `STOCK_CALENDAR_PATH`。手动研究仍可使用。单证券停牌状态不由交易日历推断。

## 备份与恢复

当前为 schema 4。schema 1/2/3 首次升级前自动在线备份为同目录 `<数据库文件名>.before-v4.bak`，随后幂等迁移；旧 schema 1/2 仍保留 `.before-v3.bak`；schema 1 仍保留此前的 `.before-v2.bak` 兼容备份。账户、持仓、报告、材料 ID 和历史建议保留，未知原始身份和旧公共研究范围不会补造。系统设置的“备份数据库”使用 SQLite 在线备份，输出 `data/backups/*.sqlite3`。备份包括账户、设置、历史、任务、行情与材料，不包含 `.env` 密钥。

```sh
./stop.command
.venv/bin/python scripts/restore.py data/backups/live-YYYYMMDD-HHMMSS-ffffff.sqlite3 --mode live
./start.command
```

恢复会检查数据库完整性、模式与 schema，拒绝覆盖运行中的库，并在恢复前自动备份当前库。演示只能恢复到演示库。需要迁移机器时安全地另行转移 `.env`；不要提交或公开本地数据库。

## 验证

```sh
.venv/bin/python -m pytest -q
# 浏览器回归（开发/CI；正常启动无需安装 Node 依赖）
npm ci
npx playwright install chromium
PYTHON=.venv/bin/python npm run test:browser
```

CI 在 Linux 使用 `npx playwright install --with-deps chromium` 安装浏览器与系统依赖，再执行两组 Chromium 行为测试：HTTP 受控夹具，以及生产 FastAPI + 独立临时 SQLite。测试覆盖草稿保护、活跃/历史分页、证据定位、引用缺口、范围版本和账户隔离；不是仅做 JavaScript 语法检查。

确定性测试使用明确的模型协议测试替身，证明任务隔离、多个子循环、追问和结果回传；不伪装成真实模型验收。真实模型样例评估说明与命令见 [Agent 文档](docs/agents.md)。第四轮使用 `.venv/bin/python scripts/verify_fourth_iteration.py`，先探测原生搜索，再在全新隔离数据库通过正常 HTTP、调度和后台 worker 执行公共研究、A、B 和 A 改仓四轮，每轮最多30次模型调用、600秒；不预给财报链接。第三轮脚本仅作历史比较。评估结果必须人工检查材料分类、事实引用、缺失信息处理和反向证据，不能把单次成功称为永久正确。

## 代码入口

- `app/main.py`：HTTP API、本地访问边界与设置校验。
- `app/store.py`：数据库初始化、输入版本、持久队列、原子发布及公共材料缓存。
- `app/materials.py` / `recommendations.py` / `scheduling.py`：文档与片段身份、通知、建议生命周期与公共研究版本。
- `app/references.py`：材料引用的存在性、访问范围和位置解析；不审核事实可信度。
- `app/domain.py`：Decimal 计算、证券身份、数量和缺失值语义。
- `app/service.py`：后台调度、行情刷新、恢复、版本任务及受限研究工具。
- `app/recovery.py`：可见研究检查点及版本、行情、来源、事件恢复保护。
- `app/agents.py` / `app/model.py`：运行时 manager/子 Agent 编排、事件邮箱、协议续接、预算及超时。
- `app/prompts/manager.txt` / `researcher.txt`：实际运行提示词。
- `app/providers.py` / `calendar.py`：数据源、搜索、公告与交易日历。
- `app/static/`：账户、持仓、候选行情、研究、Agent 工作台和设置界面。

本地编排参考 [Agents SDK manager / agents-as-tools](https://developers.openai.com/api/docs/guides/agents/orchestration)、[Codex 子 Agent 协作](https://developers.openai.com/codex/agent-configuration/subagents)及 [Responses Multi-agent 协议](https://developers.openai.com/api/docs/guides/responses-multi-agent)。本实现不启用托管 Multi-agent；协作动作由本地代码执行，普通 function_call 按公共模型协议续接，开发环境内部工具不属于应用 API。

## 当前范围

无回测、自动下单、收益承诺、真实券商持仓、自动 ETF 底层持仓穿透、OCR 或全市场自动股票池。候选机会发现限于配置股票池及相关公开研究。现金、成本或原文缺失会限制结论精度，模型不得填造。SQLite 面向单机个人使用，备份和日历维护由本机用户负责。
