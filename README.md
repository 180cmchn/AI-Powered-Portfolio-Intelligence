# 观澜 · AI-Powered Portfolio Intelligence

运行在 Mac 本机的 A 股与场内 ETF 虚拟账户研究工作台。FastAPI + SQLite + asyncio 后台执行器 + 无构建依赖的原生 JavaScript 界面。主 Agent 和每个子 Agent 都有独立模型上下文及真实工具调用循环；模型服务与搜索服务通过配置接入。

**第二轮交付：持久需求调度、公共研究复用、材料版本与分页、长 PDF 定位、建议条件跟踪、历史/板块/利润表工具和增量前端同步已实现。当前开发环境没有模型/搜索 API 密钥，真实模型研究质量尚未验收。公开数据适配器有真实成功与断连记录，不能承诺稳定实时行情。**

完整修改、数据库升级及剩余范围见 [第二轮交付说明](docs/iteration-2.md)。 详见 [验证记录](docs/verification.md) 和 [数据源实测](docs/providers.md)。

仅管理虚拟持仓快照与研究参考，不读取券商账户、不登录券商、不下单、不撮合交易。

![演示模式界面：固定样例与本地计算，不代表真实模型研究](docs/demo-overview.jpg)

## 在 Mac 上启动

需要 Python 3.12+，推荐 Apple Silicon 上使用 Homebrew Python 或 `uv`。本次开发机为 macOS Apple Silicon，使用 Python 3.14。前端无需 Node、npm 或 CDN；只需一个后台进程。

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

在 `.env` 中填写你实际有权限使用的模型和搜索服务。以下名称是配置键，不代表已经获得任何模型权限：

```dotenv
APP_MODE=live
MODEL_PROTOCOL=responses
MODEL_BASE_URL=https://api.openai.com/v1
MODEL_NAME=你的可用模型标识
MODEL_KEY_ENV=MODEL_API_KEY
MODEL_API_KEY=你的密钥
SEARCH_PROVIDER=tavily
SEARCH_KEY_ENV=TAVILY_API_KEY
TAVILY_API_KEY=你的密钥
QUOTE_PROVIDER=eastmoney
```

重新启动服务使环境变量生效。系统设置页面可以调整模型名、服务地址、协议和**密钥环境变量名**，不接收密钥本身。页面保存的非密钥配置优先于环境默认值；如果已经在页面保存过设置，更改 `.env` 的模型名后仍需检查页面中的模型名。模型接口支持 `responses` 和 `chat_completions`；如需本地无密钥模型，设置回环地址并明确开启 `model_allow_no_key`。

点击“检测工具调用能力”，会真实执行两轮模型请求，验证函数调用和工具结果续接；可能消耗服务额度。通过该探测不代表研究质量合格。供应商仅宣称兼容某协议不足以证明工具可用；检查返回原因，再根据该服务的实际能力配置 `chat_use_max_completion_tokens` 等参数。没有配置时，任务显示 `waiting_config`，已保存持仓和基础指标仍可用。

搜索支持 Tavily 和 Brave；缺少搜索配置时工具明确返回不可用。Agent 仍可以读取已知公开 URL 或使用已有材料；不会把搜索功能假装已经接通。模型与搜索认证本次未实测。详见 [Agent 运行与模型接入](docs/agents.md)、[数据适配与权限限制](docs/providers.md)。

真实行情默认尝试东方财富网页数据途径，60 秒为初始刷新频率，受对方服务可用性和使用条件限制。页面区分报价时间、抓取时间、来源、延迟及缓存失败状态。可将 `MarketProvider` 替换为你有权使用的供应商；`manual` 当前表示停用联网行情，不会生成报价。北京市场映射尚未做真实证券探测，停牌状态与 ETF 具体交易规则无法从当前报价端点完整确定。

## 使用路径

1. 创建账户，录入代码及交易所（例如 `SH:600519`、`SZ:000001`、`SH:510300`），填写整数股数/份数。基础资料由代码与市场联合查询补全。
2. 成本和现金允许空值；零与空值分开。无成本不算完整成本收益，无现金只展示证券市值及内部权重。更改数量不会扣现金或产生已实现盈亏。
3. 分别维护自选列表和候选范围。持仓自动纳入监控；移除一个账户的持仓不影响其他账户、自选和活动任务的监控需求。候选范围为全局配置，改变时更新相关账户版本。
4. 保存立即返回新版本与代码计算指标，并排入去抖分析。模型可用时主 Agent 自主派发、追问、等待研究，输出带来源与条件的报告；界面查看子任务、公开消息摘要、工具来源和子报告。
5. 账户更新后旧报告标记过期。旧任务即使晚到也不能覆盖最新结果。分析失败不会回滚已保存持仓，可在工作台重试。

交易时段默认：每 5 分钟全局候选研究、15 分钟单账户综合分析；15:15 收盘总结。到期需求先持久化，账户忙碌时延迟并合并，完成后补跑；公共研究供各账户复用。已有研究运行时不会每 5 分钟强制打断。报价更新只计算指标；超过配置价格变动阈值才触发额外研究。可配置公开信息搜索词，新增材料交给主 Agent 判断关联性，无来源白名单、可信度分数或固定审核 Agent。资讯扫描独立于交易时段，默认 08:00–23:00 每 30 分钟，需配置搜索词和搜索服务。内容更正保存新版本并触发重评。结构化建议在价格、期限、资讯或输入版本变化后更新状态；不执行买卖。所有时间按 Asia/Shanghai。

日历已核对沪深北 **2026 年**官方休市安排；超出覆盖年份自动标未知并停止交易时段定时分析，需更新 `data/calendar_2026.json` 或 `STOCK_CALENDAR_PATH`。手动研究仍可使用。单证券停牌状态不由交易日历推断。

## 备份与恢复

schema 1 首次启动自动在线备份为同目录 `.before-v2.bak`，幂等升级到 schema 2 并保留旧数据与材料 ID。系统设置的“备份数据库”使用 SQLite 在线备份，输出 `data/backups/*.sqlite3`。备份包括账户、设置、历史、任务、行情与材料，不包含 `.env` 密钥。

```sh
./stop.command
.venv/bin/python scripts/restore.py data/backups/live-YYYYMMDD-HHMMSS-ffffff.sqlite3 --mode live
./start.command
```

恢复会检查数据库完整性、模式与 schema，拒绝覆盖运行中的库，并在恢复前自动备份当前库。演示只能恢复到演示库。需要迁移机器时安全地另行转移 `.env`；不要提交或公开本地数据库。

## 验证

```sh
.venv/bin/python -m pytest -q
node --check app/static/app.js   # 可选，Node 只用于语法检查
```

确定性测试使用明确的模型协议测试替身，证明任务隔离、多个子循环、追问和结果回传；不伪装成真实模型验收。真实模型样例评估说明与命令见 [Agent 文档](docs/agents.md)。评估结果必须人工检查材料分类、事实引用、缺失信息处理和反向证据，不能把单次成功称为永久正确。

## 代码入口

- `app/main.py`：HTTP API、本地访问边界与设置校验。
- `app/store.py`：数据库初始化、输入版本、持久队列、原子发布及公共材料缓存。
- `app/domain.py`：Decimal 计算、证券身份、数量和缺失值语义。
- `app/service.py`：后台调度、行情刷新、恢复、版本任务及受限研究工具。
- `app/agents.py` / `app/model.py`：运行时 manager/子 Agent 编排、事件邮箱、协议续接、预算及超时。
- `app/prompts/manager.txt` / `researcher.txt`：实际运行提示词。
- `app/providers.py` / `calendar.py`：数据源、搜索、公告与交易日历。
- `app/static/`：账户、持仓、候选行情、研究、Agent 工作台和设置界面。

本地编排参考 [Agents SDK manager / agents-as-tools](https://developers.openai.com/api/docs/guides/agents/orchestration)、[Codex 子 Agent 协作](https://developers.openai.com/codex/agent-configuration/subagents)及 [Responses Multi-agent 协议](https://developers.openai.com/api/docs/guides/responses-multi-agent)。本实现不启用托管 Multi-agent；协作动作由本地代码执行，普通 function_call 按公共模型协议续接，开发环境内部工具不属于应用 API。

## 当前范围

无回测、自动下单、收益承诺、真实券商持仓、自动 ETF 底层持仓穿透、OCR 或全市场自动股票池。候选机会发现限于配置股票池及相关公开研究。现金、成本或原文缺失会限制结论精度，模型不得填造。SQLite 面向单机个人使用，备份和日历维护由本机用户负责。
