# 第八轮：浏览器请求生命周期与 CI 取证

## 远端现状与历史失败

2026-10-29 23:47:49（Asia/Shanghai）最后一次通过 GitHub API 查询最新工作流，最新仍是提交 `85c530c8cb83727c87ddf6edc2df447fbbf980d9` 的 [40862236837](https://github.com/180cmchn/AI-Powered-Portfolio-Intelligence/actions/runs/40862236837)，attempt 1，状态 completed，结论 failure，远端更新时间为 2026-10-29 11:04:52 UTC。tests job 为 `123408081310`。第一组浏览器通过，第二组在原 `frontend_production.mjs:174` 等待加载条隐藏超时，第三组因 `&&` 未执行。Python 690 项通过是该历史运行的结果。本轮没有推送，最终本地通过不代表 GitHub 已运行本轮补丁。

实际下载并阅读 artifact `12891837688`（`browser-failure-evidence`，8,072,271 bytes，SHA256 `963612711a42b0286268dc25bd53dceab6e430c7968c98c174f4e0b05b990afe`）中的截图、请求记录、Playwright trace 和 network。原产物保存在本机 `/tmp/guanlan-iteration7-ci`，没有把其中的原始响应复制进交付文档。

trace 中可以直接确认：

- 研究页导航的 `.loading-line hidden` 检查在 5949.653 ms 就结束，实际状态请求在 5966.775 ms 才开始。测试把“上一刻没有加载条”误当成“这次导航已经完成”，随后安装的暂停器截获了上一导航的请求。
- 保存 PUT 已由 `route.fetch()` 在 6227.208 ms 收到 HTTP 200，6387.206 ms 调用 fulfill；截图显示账户 B 和“保存账户中”，没有后端保存报错或可见应用异常。
- 6387.887 ms 放行暂停导航。6389 ms 两个拦截调用结束，6392.770/6392.899 ms 两次 `unroute` 更新拦截模式，最后变为空列表。
- 保存成功引发的刷新恰在 6396.327 ms 发出新的 GET `/api/status`；network 记录该请求 status=-1、response timing=-1、无正文，其余 `/api/accounts`、`/api/universe`、`/api/settings` 均为 200。它不是一个已经返回失败的后端接口。原页面的 `Promise.allSettled` 一直等待这个没有结束的读取。

因此问题定位到测试导航同步和拦截器生命周期的竞态，并叠加产品读取无截止的弱点。trace 支持“最后一次拦截器撤销与新请求重叠”，尚不能仅凭这份产物判断 Chromium/CDP 内部是哪一步丢失推进事件。没有把第五轮已修好的 busy 丢操作当成本轮根因；原来的操作 Map 确实接受了保存。

随后在隔离 Ubuntu 容器中对未修改的 HEAD 固定运行三次，结果为通过 / **同一行超时失败** / 通过（15.844 / 34.712 / 14.159 秒）。失败 trace 再次显示 PUT 200，而 4054.857 ms 发出的保存后 `/api/status` status=-1、receive=-1，其余刷新响应为 200。再保持产品代码完全不变，只改测试的导航同步、永久透传路由和 gate 不即时卸载，固定三次均通过（14.545 / 14.600 / 14.082 秒）。这是本轮实际复现和测试侧对照，不是只根据远端截图猜测，也不是重跑到偶然成功；三次都保留。产品 GET 截止/取消则单独用明确注入的慢请求验证。

## 实现

`tests/frontend_production.mjs` 先等本次导航的 `aria-current` 出现，再判断空闲；为整个页面保留一个 API 透传路由。单次暂停器按方法消费一次，之后透传，直到浏览器销毁才卸载，避免在响应触发新请求时撤销最后一个拦截器。每个 gate 单独记录匹配、释放、结束，并断言全部结束。

`app/static/app.js` 给 GET 设置 15 秒总截止（包含正文解析）。切换路由或账户会取消旧读取，过期结果先检查代次再处理异常，`finally` 清理实际请求与操作项。写入不会因为页面切换被取消，仍绑定点击时账户、输入和 expected_version。保存成功立即更新原草稿版本，再刷新页面，因此刷新失败不会丢失已经确认的版本。

网络中断或不完整写入响应使用“服务端可能已提交”的明确状态，不自动重发。账户保存可通过一次 GET 核对原账户的版本恰好增加一次且完整输入一致，确认后继续保留新草稿；不能确认就保留未知。其他操作不凭客户端断开宣称服务端未执行。

诊断默认关闭。当前标签页执行 `sessionStorage.setItem('guanlan.diagnostics','1')` 后刷新，`window.__guanlanDiagnostics.snapshot()` 返回最多 600 条事件、累计计数与当前请求/操作集合；移除该项并刷新关闭。事件只含请求方法、去查询参数及 ID 的路径模板、匿名账户编号、代次、时间与状态，不含 Key、请求/响应正文、账户名称或持仓。诊断不改变调度和预算。

同一前端补充展示 `calendar.session`，旧 `phase=closed` 兼容显示为“非连续交易时段（旧状态）”，不再无依据称为已收盘。检查点显示具体缺口和完整子报告/摘要交付，明确非主 Agent 亲读原文；报告和任务显示保存的模型、供应商、角色及 effort，历史未知字段保持未知。

## 回归方式

生产 FastAPI + 临时 SQLite + 随机本地端口，模型能力探测仍是明确标记的协议替身；不启动日常服务或真实模型 worker。每次完整生产组固定重复三个响应释放顺序：同时、先保存、先导航；核对每轮仅一次 PUT、账户 A 版本、账户 B 不变、新草稿保留和空操作集合。另覆盖 409、旧读取取消、15 秒 GET 截止、服务端已提交后响应丢失的只读核对，以及原有 retry/analyze 和配置测试。用浏览器受控时钟推进 GET 截止，不倒调服务端业务时钟。

`npm run test:browser` 使用 `tests/run_browser.mjs` 顺序执行所有三组，并写每组退出码和总退出码；某组失败不会跳过后续组。CI 各组为独立步骤，最后明确要求三组全部成功，同时上传证据。没有删除原断言或放宽浏览器等待上限。

已执行命令与最终结果见 `iteration-8-browser-evidence.json`；最终集成运行以总交付清单为准。Mac Chrome 与本地 Linux/Chromium 的结果单独记录。没有推送本轮改动，因此本地通过不等于远端已修复。

以下应用摘要使用 `app.runtime_version.source_digest()`：按路径排序 app/ 内 .py/.js/.html/.css/.txt 文件（排除 __pycache__），逐文件 SHA256 后对 `repr([(相对路径, SHA256), ...]).encode()` 再作 SHA256。它不含 scripts/tests/docs，不与 `verify_sixth_live.source_digest` 的 app+scripts 原始字节算法直接比较；浏览器相关文件另列逐文件 SHA256。

最终源码摘要 `5dbd45dbf0f8a36386e2cbb6c86f311c8f89e179f486f755df4cef72b537b315` 上，Mac 26.5.2 arm64 / Python 3.14.4 / Node 25.9.0 / Chrome **156.0.8078.48** 的三组全部通过（20.956 / 21.945 / 15.923 秒）；Ubuntu 24.04.4 arm64 / Python 3.12.3 / Node **22.23.4** / Chromium **151.0.7922.34** 的三组全部通过（2.081 / 19.899 / 4.876 秒）。两个环境验证前后应用摘要一致。Linux 全量 Python **763 passed**（41.89 秒、1条原有 Starlette/httpx 弃用警告）；详见 `iteration-8-linux-validation.json`。

Linux 环境与远端同版 Node/Chromium、同 Python 3.12 和 Ubuntu 24.04 系列，但架构和 Python 补丁版不相同。固定为单 CPU、运行时无外网；每组的服务仅用容器回环端口。最终 Mac / Linux 的竞态段分别完成234 / 240个请求、43 / 43个操作，9个 gate 均匹配、释放并结束；最终未结束请求与操作均为0。两者均只有专门注入的1次 GET timeout，不能靠额外超时掩盖失去推进的请求。两个环境的轮询次数和机器负载不同，不解释为性能提升。

Linux 首次打包遗漏仓库跟踪的公开日历，第三组未启动；已明确记录并补入日历后完整执行三组，未复制日常数据库和 `.env`。中间候选 `125c982…` 的双环境通过结果保存在 JSON 的 `pre_final_cross_review_runs`，未改署为最终版本。最后交叉审查补充了后端材料读取、恢复版本范围、迁移事务和批次准备状态中文提示，随后才在上述最终 `5dbd45…` 上各完整执行一轮，全部通过后没有继续重跑。

## 最终真实页面只读抽查

最终独立真实服务的 [页面记录](../data/evaluations/eighth-final-v3-20261029/page-inspection.json) 包含账户总览、研究报告、Agent 工作台、预测评估、系统设置和账户 Agent 模型六页，`kind=real_independent_service_read_only_browser`，浏览器检查记录 `errors=[]`。同目录 `page-*.png` 为实际页面截图；本次检查只读取记录和截图，没有修改账户、应用或测试。抽查截图布局，并用同次保存的页面文本核对长页面细节：

- [总览](../data/evaluations/eighth-final-v3-20261029/page-overview.png) 可见 A 输入 v2、20,000 份、现金 10,000 元，成本未知；行情 0.916、10/29 15:35、`sina:public-website` 与已收盘状态分开显示。最近研究是当前版本，较早输入的报告在研究页保留。
- [研究报告](../data/evaluations/eighth-final-v3-20261029/page-research.png) 显示 `manager / grok-4.7 / legacy-4c93330dc068` 和请求 `reasoning_effort=medium`，并注明上游实际采用值以服务回报为准。来源区分“仅来源链接 · 未定位本地原文”与已保存的日线材料；材料保留 ID、版本、字符数及“读取完整性未知”。报告明确 PDF 正文未取得、地址校验失败，以及指标和比较范围尚有缺口，没有把保存全文等同于本轮完整阅读。
- [Agent 工作台](../data/evaluations/eighth-final-v3-20261029/page-agents.png) 可见 A 两轮任务及实际主角色配置。[账户模型页](../data/evaluations/eighth-final-v3-20261029/page-account-models.png) 展示同一套六个全局角色的继承绑定。A 两轮主 Agent 未派发子角色，因此这些 A 账户截图不能证明真实委派交付；B 则实际调用 ETF 和市场研究员，其交付依据见[真实交付复核](eighth-live-recovery-review.md)。本次没有真实汇总故障恢复，相关受控回归另列。
- [预测评估](../data/evaluations/eighth-final-v3-20261029/page-forecasts.png) 明确显示“探索记录”、不进入正式固定批次准确率；1/5/20 日均为“暂无已评估样本”，命中/已评估为 0/0。A 两轮各三项弃权，共六条；覆盖 100% 是弃权也已记录的覆盖口径，不是方向准确率。批次详情还显示研究 completed 与基准待采集状态，未把 completed 当作正式方向发布或基准就绪。
- [系统设置](../data/evaluations/eighth-final-v3-20261029/page-settings.png) 显示“收盘后 · 交易日”“已到当日收盘；精确收盘仍须数据源确认”，启动版本为 schema 8、基线提交含未提交修改、应用摘要 `5dbd45dbf0f8a36386e2cbb6c86f311c8f89e179f486f755df4cef72b537b315`。该摘要来自独立服务启动记录，不用仓库 HEAD 替日常旧进程署名。

截图只证明被捕获时的可见内容。Agent 页在展开事件详情后仍有加载提示；单张截图不能判断它是否悬挂，也不能证明其后已经空闲。加载生命周期是否结束由上述最终 Mac/Linux 受控浏览器运行的请求及操作集合归零断言验证。真实页面检查没有把正常报告完成、资料质量、方向预测和真实恢复合并为同一种成功。

执行命令：

```sh
PYTHON=.venv/bin/python BROWSER_EXECUTABLE='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' npm run test:browser
PYTHON=.venv/bin/python node tests/frontend_production.mjs
# 用源码临时副本作容器上下文：app、tests、scripts、docs、依赖清单和已跟踪公开日历；不含 .env / 日常数据库。
docker build -f docs/iteration-8-ci.Dockerfile -t guanlan-iteration8-ci /tmp/guanlan-iteration8-linux
docker run --rm --network none --ipc=host --cpus=1 -v /tmp/guanlan-iteration8-linux:/work -w /work guanlan-iteration8-ci npm run test:browser
# 最终 Linux 全量 Python 的实际子命令（驱动脚本另保存各组日志与前后源码摘要）：
docker run --rm --network none --ipc=host --cpus=1 -v /tmp/guanlan-iteration8-linux:/work -w /work guanlan-iteration8-ci /opt/guanlan-venv/bin/python -m pytest -q
```
