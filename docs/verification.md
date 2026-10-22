# 交付验证记录

## 第二轮最终验证（2026-10-23，Asia/Shanghai）

基线：`e62ee5e`、原有 **57 passed**。最终执行：

```sh
.venv/bin/python -m pytest -q
# 131 passed, 1 warning in 3.69s
node --check app/static/app.js
.venv/bin/python -m compileall -q app scripts
git diff --check
```

**0 项失败，原有测试全部保留通过。** 唯一 warning 是现有 Starlette 测试客户端关于 httpx 的弃用提示。没有将协议替身结果视为模型质量验证。

| 验证类别 | 实际执行与结果 | 能证明的范围 |
| --- | --- | --- |
| 确定性单元 / 持久化 / 本地集成 | pytest 合计 131 项通过；其中原 provider 24 项及新增 31 项共 55 项通过 | 值计算、版本保护、状态迁移、数据解析与网络失败边界 |
| 调度集成 | 模拟完整上午/下午交易时段并重复 tick | 48 次全局扫描，4 个账户各 16 个综合研究需求均完成；公共研究只执行一份 |
| 生命周期 / 更新集成 | `test_iteration.py`、`test_iteration_edge_review.py` | 忙碌需求补跑、重启幂等、waiting_config 恢复、资讯更正与通知恢复、建议监控/触发/过期/持仓变更、旧结果迟到、语义相同价格条件与失效日期防重跑 |
| 材料 / 文档集成 | 大量长正文索引、字符分页；pypdf 实际解析 35 页 fixture | 列表有界、跨账户访问隔离、同源多条提取不会误判更正、HTML 附件、PDF 第 29 页、错误/部分读取状态、范围/来源字段保留 |
| 模型协议替身 | `test_agents.py`、`test_context_runtime.py` | 独立主/子会话、工具调用/续接/追问、可取回上下文、两种协议、请求用量和撤销元数据传递；不证明真实模型推理正确 |
| API 与浏览器 | 新增 API 8 项；真实 Chrome 执行 `frontend_iteration.mjs` | 市场报价自动更新且保持草稿/焦点/选区；持仓草稿保留原 expected_version；材料 ID/PDF 范围；过期展示；报告缓存；事件游标；公共报告详情 |
| 真实本机进程 | `.venv/bin/python scripts/check_local_lifecycle.py` 输出 PASS | 随机回环端口、临时 demo 数据库；HTTP 创建账户、后台摘要、优雅停止、重启输入不变、v2 在线备份与实际 restore 脚本恢复。无外部模型或数据调用 |
| 真实联网数据 | `PYTHONPATH=. .venv/bin/python tests/provider_smoke.py`，另执行缩小区间的最终适配器探测 | 公共股票/板块/基准日 K、行业/概念目录、成分、利润表有成功结果；也出现真实 RemoteProtocolError。详情见 providers.md |
| 真实长 PDF | 最终适配器直接读取巨潮 110 页财报，检索后读取物理页 28 | 1,136 字符、关键词存在、同 document_id、缓存复用、access=excerpt；不是 OCR 或全文财务理解验收 |
| 真实模型评估 | 下列命令实际执行，退出 78 / SKIPPED | 缺模型名/密钥；没有模型输出可评估，十类输入的真实研究质量均待验证 |

```sh
.venv/bin/python scripts/evaluate_model.py --check-config
# model_name_present=false, model_key_present=false,
# search_provider_configured=false, search_key_present=false
.venv/bin/python scripts/evaluate_model.py --case mixed_fact_opinion
# SKIPPED (缺少真实模型配置；所有未运行案例仍未验证), exit 78
```

浏览器回归需要开发时的 Playwright 与 Chrome；运行正式应用不需要它们。本次使用：

```sh
NODE_PATH=/Users/chn/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules \
BROWSER_EXECUTABLE='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
node tests/frontend_iteration.mjs
```

浏览器使用明确的 HTTP fixture，真实联网测试使用真实外部端点；两者没有混为一类。本机生命周期脚本首次因 Uvicorn 在优雅关闭后转发 SIGTERM（退出 -15）的检查假设失败；修正测试为校验退出码及“Finished server process”后完整通过，没有隐去该过程。

尚未覆盖：真实模型与搜索认证、Tushare 认证/权限、真实分钟/复权样例、完整财务口径、实际系统睡眠、长期连续运行压力和投资收益。自动化验证不代替投资研究效果。实现、迁移和运行说明见 [第二轮交付](iteration-2.md)。下面保留第一轮历史记录，数量与状态不代表本轮最终结果。

## 第一轮历史验证记录

验证日期：2026-10-22，Asia/Shanghai。环境：macOS / arm64、Python 3.14.4、项目 `.venv`。依赖版本已锁定于 `uv.lock` 和 `requirements.txt`。

## 已通过的确定性验证

最终执行 `.venv/bin/python -m pytest -q`：**57 passed**。另有 FastAPI/Starlette 对测试客户端 httpx 的弃用提示 1 条，不影响当前测试通过。`node --check app/static/app.js` 通过。正式界面运行无需 Node。

最终版本的后台 start/stop 脚本已在本机实际启动、停止和重启验证；live 与 demo 分别监听 8765 / 8766。`scripts/restore.py` 已在隔离临时数据库上实际执行并验证账户恢复成功。

| 需求行为 | 证据与结果 |
| --- | --- |
| 两账户相同证券、不同数量与偏好隔离 | 集成测试创建两账户，市值分别 1,012.50 / 3,543.75；修改甲不改变乙的输入、现金或任务 |
| 保存即时指标与新分析 | 账户与输入快照、版本、任务同事务写入；API 返回新版本与 Decimal 指标；浏览器修改 ETF 份数后即时刷新 |
| 新结果先到、旧结果后到 | 测试先发布新结果，再发布旧结果；current 始终只有最新版本。相同版本不同任务也检查最新任务 ID；重复发布幂等 |
| 独立主/子 Agent 与后续追问 | HTTP 协议测试替身驱动真实本地运行器创建两个独立子会话，完成阶段发现与子报告，并继续原子会话补充研究 |
| 混合事实与荐股观点 | 管道测试保留两种分类、不因同 URL 丢失材料，交给主会话；该测试的分类由 fixture 给出，不能作为真实模型分类质量证明 |
| 缺失成本/现金/发布时间和原文失败 | null 与 0 分离，未知收益与总资产保持 null，保留来源失败与访问范围，局部材料仍可到达主会话 |
| 超时、网络错误、恢复和重复事件 | 模型网络有限重试、全局预算、子任务时限、父任务取消、SQLite 恢复、只恢复最新队列、事件去重均有测试 |
| 公共资料复用与私有上下文隔离 | 原始公共材料可跨账户取回；模型提取内容默认按账户关联；旧来源按 ID 检索不受最近 300 条列表限制 |
| 休市与年份边界 | 核对沪深北 2026 年官方休市安排，节假日不定时扫描；2027 未覆盖返回未知 |
| 安全边界与备份 | 跨站写请求拒绝；原文拒绝私网/本机与不安全重定向；SQLite 备份完整、真实/演示模式不能混用 |
| 指定未持有证券 | 手动任务目标纳入行情刷新和 target_security 上下文，不仅出现在文字 reason 中 |

测试文件：`tests/test_core.py`、`test_agents.py`、`test_providers.py`。测试替身只存在测试代码，不被生产任务调用。

## 真实浏览器操作

本机启动 `http://127.0.0.1:8766` 独立演示服务，通过浏览器操作：

- 显式创建两个演示账户。
- 将“稳健观察”的沪深 300 ETF 从 10,000 份改成 12,000 份；版本 v1→v2，证券市值 170,000→178,000，新增摘要，旧摘要过期。
- 切换“长期组合”，仍为 3,000 份、市值 12,000；现金与成本收益均显示未知。
- 保存候选范围触发相关版本 v3；设置保存、能力探测、任务事件展开及历史过期状态正常。
- 浏览器 error/warn 日志为空。页面没有模拟 Agent 研究；演示摘要明确是本地数值计算。

截图 `docs/demo-overview.jpg` 是 **演示模式**，不代表真实研究结果。

## 真实外部调用

东方财富公开网页端点曾对 SH:600519、SH:510300、SZ:000001 返回 HTTP 200，已核对 ETF 三位小数与源时间戳。后续最终 httpx 适配器多次遇到服务端断连，准确返回 unavailable；不能把最初成功称为持续可靠接入。完整时间、字段和失败记录见 [providers.md](providers.md)。

最终原文读取器成功读取上交所 2026 年休市公告，提取 1,555 字符正文；无法识别的日期元数据保持 null。PDF、截断与失败边界使用确定性测试覆盖，未声称已实测所有公告格式。

最终真实后端烟雾测试使用临时 live 数据库：两个账户分别持有 SH:510300 的 1,000 / 3,000 份，配置与版本隔离。报价失败保持缺失；两任务进入 waiting_config，真实研究历史为 0。临时库已清理，不污染用户 live 或 demo 历史。

## 待配置后验证

环境没有 `MODEL_API_KEY`、可用模型名、Tavily 或 Brave 密钥。执行：

```sh
.venv/bin/python scripts/evaluate_model.py --probe-only
```

实际结果为 `SKIPPED (缺少真实模型配置)`，退出码 **78**。没有进行真实模型研究、真实搜索认证或真实模型混合材料分类验收。开发助手的模型权限和网页检索工具不等于应用权限。

配置后使用系统设置中的能力检测、`scripts/evaluate_model.py` 以及真实账户“立即分析”继续验收。评估脚本生成真实模型输出；默认输入是明确标记的合成案例，也可用 `--live-url` 指定真实公开资料。即使结构检查通过，仍需人工检查事实引用、预测/观点区分、不确定性与反向证据。一个样例不能保证未来模型输出质量。

服务睡眠恢复通过时钟和持久状态测试覆盖，**未人为让这台 Mac 进入系统睡眠**。没有系统通知或补发推送；进程重启会重新执行最新任务，不恢复未落盘的模型内部会话。当前年度日历、公开行情稳定性、联网权限与真实模型质量是交付时的明确限制。
