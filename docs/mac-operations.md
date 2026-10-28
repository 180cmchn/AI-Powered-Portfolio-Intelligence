# Mac 启停、升级与恢复

本说明对应第六轮代码。运行时仍是一个本地 Python 进程、SQLite 与浏览器，不需要 CC Switch、OpenCode CLI、Node 或另外的 Agent 服务。模型、行情和真实预测表现的验收结论另见各轮交付记录，本操作说明不代表供应商已经实测可用。

## 安装与日常启停

需要 Python 3.12+。在项目目录执行：

```sh
uv sync --frozen
./start.command --mode live
```

若没有 `uv`：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/manage.py start --mode live
```

打开 `http://127.0.0.1:8765`。Finder 双击 `start.command` 也可启动；没有虚拟环境时脚本先使用可用的 `uv` 或 Python 安装依赖。已有 `.venv` 不会在每次启动时重装。项目已有 `.env` 应保留，不要用示例覆盖。正常新增模型供应商直接在网页填写三项连接信息，不必手工维护模型名称或 Key 环境变量。

```sh
.venv/bin/python scripts/manage.py status --mode live
./stop.command --mode live
```

管理脚本只提供 `start`、`stop`、`status`。前台排查可在停止后台服务后执行 `.venv/bin/python scripts/serve.py`；后台日志为 `data/live.log`。只允许回环地址与单个 worker，同库文件锁阻止第二个服务实例。默认数据库是 `data/live.sqlite3`；显式设置 `APP_DB_PATH` 时使用该路径，备份/恢复前应核对实际目标。

关闭网页不会停止后台研究。Mac 睡眠时不执行任务；唤醒或重新启动后恢复持久任务并处理待数据/待评分记录。不会在第二天补造昨天未及时发布的正式预测。重启电脑后需要再次启动服务，本项目不自动安装登录项或 LaunchAgent。

演示模式使用独立库与默认端口：

```sh
./start.command --mode demo
.venv/bin/python scripts/manage.py status --mode demo
./stop.command --mode demo
```

打开 `http://127.0.0.1:8766`，演示固定样例不代表真实模型、行情或已到期预测表现。

## 第一次配置第六轮功能

1. **模型供应商**：填写 API Key、Base URL 和协议。Key 保存到 macOS 钥匙串；自动请求目录，查看成功、部分、认证失败或接口未支持等实际状态。刷新和继续加载只获取模型目录，不自动对全目录发付费推理请求。
2. **Agent 角色**：六个初始角色全局存在。逐行选择供应商、实际发现的模型和 `low / medium / high / xhigh / max`。职责、提示词、工具和全局启停在此管理；主 Agent 根据问题自主决定调用路径，不要求每轮全部角色运行。
3. **账户模型**：新账户自动继承完整目录。按需覆盖某行的模型对或 effort；清除覆盖回到默认，不删角色。配置错误仍显示该角色，不能使账户 A 的错误阻塞 B。
4. **预测评估**：选定账户并展开固定批次设置，明确开启新正式批次。默认每天 15:30 在收盘数据可用后发起、18:00 截止；期限为未来 1、5、20 个交易日，震荡阈值为 ±0.5%。页面显示启用状态与模型费用提示，旧账户升级不会自动补跑正式历史批次。

模型列表可见不代表账户有调用额度、服务接受全部 effort 或原生联网已验证。系统设置中的两个能力检测使用全局主 Agent 当前默认绑定，会产生真实请求费用，只证明实际观察到的该组合能力。Chat Completions 原生搜索不属于本轮适配范围。

## 暂停的范围

| 入口 | 设置 | 实际范围 |
| --- | --- | --- |
| 系统设置 → 定时研究 | `scheduled_research_enabled` | 控制新定时模型研究；暂停不取消手动或已持久化研究事项，不停止行情和已有预测评估。 |
| 系统设置 → 基础行情刷新 | `market_refresh_enabled` | 控制自动报价刷新；不删除已有报价和预测账本。 |
| 系统设置 → 已发布预测的评估 | `forecast_evaluation_enabled` | 控制到期历史数据评估；恢复后按原日期补算，评分不调用模型。 |
| 账户预测规则 → 正式预测批次 | `enabled` | 控制该账户以后新正式批次；不删除、改写或提前结算旧预测。 |
| `stop.command` | 整个本地服务 | 停止后台进程，持久数据与未完成事项仍保留。 |

基准日 D 不计入未来期限；新发的 5/20 日正式预测需等待真实到期，页面应显示待到期。价格缺失、停牌、日历缺覆盖或调整证据不足分别留下原因。准确率不是交易收益，也不把弃权或漏预测当作震荡。规则详情见[预测评估口径](forecast-evaluation.md)。

## 从旧版本升级

先在旧服务页面使用“备份数据库”，记下返回路径，然后：

```sh
./stop.command --mode live
uv sync --frozen
./start.command --mode live
```

没有 `uv` 时使用 `.venv/bin/python -m pip install -r requirements.txt` 更新依赖。停止再启动才能加载最终代码；仍在运行的旧进程不能证明新版本正常。

schema 5 或更早数据库首次进入第六轮前，程序先使用 SQLite 在线备份写入同目录 `<数据库文件名>.before-v6.bak`，然后幂等升级到 schema 6。日常账户、材料、报告与任务保留，重复启动不会重新造供应商或角色。历史其他 `.before-v*.bak` 保留，不会被第六轮覆盖。

旧 Base URL、协议及环境变量安全引用提取为供应商；旧 model name 和 effort 迁移到各统一角色默认。名称保留为历史绑定并标 `legacy_unverified`，不是新目录发现证据；在页面取回真实模型目录后确认。无效历史连接或 effort 显示待修复，不默认替换成另一模型或强度。新正式批次默认关闭，已有研究节奏保留。

## 数据库备份与恢复

页面“备份数据库”调用 SQLite 在线备份，输出 `data/backups/*.sqlite3`。不要把运行中的主库文件简单复制当作包含 WAL 的一致备份。恢复时使用页面返回的真实路径：

```sh
./stop.command --mode live
.venv/bin/python scripts/restore.py data/backups/live-YYYYMMDD-HHMMSS-ffffff.sqlite3 --mode live
./start.command --mode live
```

`restore.py` 接受 schema 1–6，核对完整性、模式和数据库锁；恢复前再保存 `data/backups/before-restore-*.sqlite3`。live 与 demo 不混用。恢复较旧库后，启动时按当前代码迁移；新于程序支持版本的库会被拒绝。

SQLite 备份包含业务数据、连接和安全凭据引用，**不包含钥匙串 Key，也不复制 `.env` 文件**。不要提交或公开数据库或本机 `.env`。恢复数据库也不能恢复已经明确删除的钥匙串秘密。

## 换机和凭据恢复

在新 Mac 安装项目、恢复目标模式数据库后，在“模型供应商”重新输入 Key，刷新目录并确认各角色绑定。不要把密文和解密密钥一起装进数据库或把 Key 写入浏览器 localStorage。旧环境变量兼容连接也可以通过页面输入新 Key，转换为新钥匙串修订。

Key 输入留空表示保持原值；掩码不是新 Key。轮换创建独立凭据修订，已启动任务保持旧引用；明确清除会撤销旧引用并阻止其后续 HTTP，历史预测仍可追溯。系统钥匙串不可用或未授权时，页面会显示错误，不退回明文存储；先在正常本地启动环境解决钥匙串授权再重试。详细实现与安全验证范围见[供应商与统一角色](sixth-provider-roles.md)。

## 交易日历维护

默认文件是 `data/calendar_2026.json`，当前默认覆盖到 2026 年。`STOCK_CALENDAR_PATH` 可指向一个已核实文件，或含 `calendar_*.json` 的目录；每份文件需有明确 `coverage` 和来源，采用交易所休市安排或完整交易日期，临时更正使用 `overrides`。将更新后的环境配置用于新进程后重新启动服务。

未覆盖的未来日期保持未知，不按普通工作日推算。补齐日历后只解析受影响样本的原期限目标日，保留解析证据和原预测。正常行情缺失与单证券停牌不是日历缺口，不能靠延长覆盖区间掩盖。
