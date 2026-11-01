# 观澜第十二轮交付

> 本文主体保留首次归档时的本地验收与远端状态。随后已获授权推送 main；首次推送 `56272f9` 的 Python 和六组浏览器通过，另外两组因浅检出缺少历史基线而失败。后续定位、修复及独立检出回归见 [CI 跟进记录](twelfth/ci-followup.md)；远端状态应按对应提交的 Actions 运行判断。

**工程验收已通过；单次真实研究因上游传输失败，未发布报告。** 最终实现 `478fefb22a123c71b6594e8666be5e9acbb2cf5f` 完整 Python 为 **976 passed、0 failed、0 skipped、1 warning**，八组浏览器全部退出 **0**；最终源码已重新启动隔离进程，并完成 Mac Chrome / Safari 复验。阅读状态、买卖方向控件、统一时钟及校正日期语义的工程回归完成。真实研究记录39次请求：37次完整终止、2次RemoteProtocolError，无额外自动任务，不为成功再次运行。真实发布后有效性仍未通过；远端仍是未推送前基线的失败。精确身份、环境、命令与数量见 [validation-manifest.json](twelfth/validation-manifest.json)。

本轮固定审查基线为 `09557bd8a3cd404250fa9fa58093f6babb404ead`，第十一轮最终实现为 `1540733f258cf07fc66701062d59803ca97e3c54`。开始时工作区干净、实际 HEAD 与审查基线一致，没有回退。开发中的检查点 `e75542d609b9ad086cf282af0cc7b6417713b11c` 及其中间运行保留为历史证据，见 [candidate-source-e755.json](twelfth/candidate-source-e755.json)。最终冻结于上海时间 **2026-11-01 00:22:12**，身份见 [frozen-source.json](twelfth/frozen-source.json)：source SHA-256 `f9d22f8bfc6e72aa96c8235924919e8159ecf31baeb93317ebbda0c902cb01fb`，static SHA-256 `a081f49e3cd9a42af07f6343395f6dd2f1992c7344ac7ac08c108346c0d1895c`。归档工程结果时没有最终提交之外的生产代码或测试修改；后续文档更新不冒充运行源码。

实现仍为 FastAPI、SQLite、原生 JavaScript，不引入完整前端框架、新审核 Agent 或业务工作流系统。schema / API contract 保持 **11 / 11**。工程验证使用临时数据库与隔离端口；没有为本轮修复停止、重启或改动日常 8765 服务，没有清空用户交易、模型配置、`.env` 或安全凭据，也没有推送本轮代码。可选真实验证如执行，仅按项目已有引用只读借用授权配置，具体尝试单列。

## 交付证据索引

| 内容 | 记录 |
| --- | --- |
| 最终工程验收身份、精确环境、命令、数量和中间失败 | [validation-manifest.json](twelfth/validation-manifest.json)、[frozen-source.json](twelfth/frozen-source.json)、[environment.json](twelfth/environment.json) |
| 全站“位置—刷新来源—稳定身份—受保护状态—测试”矩阵 | [reading-state.md](twelfth/reading-state.md) |
| 主页面、弹窗、流水修复前后与连续十轮更新 | [reading-evidence.json](twelfth/reading-evidence.json)、[browser-reading.txt](twelfth/browser-reading.txt) |
| 同页异步弹窗乱序、总览动态记录及集成审查 | [integration-review.md](twelfth/integration-review.md)、[dialog-evidence.json](twelfth/dialog-evidence.json)、[browser-dialog.txt](twelfth/browser-dialog.txt) |
| 选择控件、FormData、预览、键盘、版本与保存契约 | [select-control.md](twelfth/select-control.md)、[select-evidence.json](twelfth/select-evidence.json) |
| 实际 macOS Chrome / Safari 的展开、键盘、缩放和滚动 | [mac-validation.md](twelfth/mac-validation.md)、[mac-final-runtime.json](twelfth/mac-final-runtime.json) |
| 跨日失败复现、统一时钟、校正语义及旧保护回归 | [time-ledger.md](twelfth/time-ledger.md)、[time-ledger-manifest.json](twelfth/time-ledger-manifest.json) |
| 远端最新核对、实际失败与 skipped 步骤 | [remote-latest.json](twelfth/remote-latest.json)、[remote-baseline.json](twelfth/remote-baseline.json)、[remote-failure-excerpt.txt](twelfth/remote-failure-excerpt.txt) |
| 本地首轮与后续完整浏览器运行 | [browser-all-first.txt](twelfth/browser-all-first.txt)、[browser-groups-first.json](twelfth/browser-groups-first.json)、[browser-integrated-second.txt](twelfth/browser-integrated-second.txt)、[browser-groups-second.json](twelfth/browser-groups-second.json) |
| 最终八组浏览器完整运行 | [browser-final.txt](twelfth/browser-final.txt)、[browser-groups-final.json](twelfth/browser-groups-final.json) |
| Python 3.12 完整运行日志 | 首轮 [python-full-py312.txt](twelfth/python-full-py312.txt)、最终 976 通过 [python-final.txt](twelfth/python-final.txt) |
| 可选单次真实研究的隔离与费用边界 | [live-verifier.md](twelfth/live-verifier.md) |
| 本轮真实失败、全部请求/工具诊断、费用与账务 | [live-research.md](twelfth/live-research.md)、[live-evidence.json](twelfth/live-evidence.json)、[live-run.txt](twelfth/live-run.txt) |
| 真实研究中的Mac选区/滚动与失败后真实事件分页 | [live-ui.md](twelfth/live-ui.md) |
| 日常操作、实例身份、升级与回退 | [Mac 操作手册](mac-operations.md) |

文件名中的 `final` 或 `frozen` 是该次运行的名称，不代替对其退出码、源码摘要和后续修改的核对。失败记录保留；后续成功不能把先前失败覆盖成从未发生。

## 全站阅读状态的根因与工程契约

原 `render()` 只在角色相关页面使用专用节点修补，其他页面仍通过 `root.innerHTML = shell()` 销毁整棵树。少数 `details[data-key]` 的 open 布尔值即使恢复，也无法恢复已经销毁的 Text 节点、Range、焦点和内部滚动。任务外层“研究事件”有任务状态，内层“查看事件与来源详情”没有稳定身份，因而出现外层还展开、内层自动关闭。交易流水、嵌套来源与其他阅读区域也受相同机制影响。

任务弹窗另有 `refreshDialogEvents()` 直接替换 `.job-events.innerHTML` 的入口。原全局定时轮询在 dialog 打开时本来就暂停，因此弹窗复现采用其真实“继续读取后续事件”入口，未把它误写成同一个定时器必然重绘所有弹窗。

新 `view-state.js` 负责同页节点协调和阅读事务：服务器拥有任务、版本、报价、报告和账务；用户拥有展开、滚动、选区、焦点、未提交草稿及菜单候选。**同页、同账户、同实体**优先保留连接中的节点，只更新变化的业务内容；明确导航、切账户、切段或保存后重新载入才有意替换相应区域。没有以暂停全部轮询、长时间滚轮防抖或停止事件更新掩盖问题。

| 区域 | 稳定身份与更新策略 | 阅读状态与业务正确性 |
| --- | --- | --- |
| Agent 任务、事件与来源 JSON | account + job + surface；事件使用服务端持久 seq / ID；追加和重叠分页按身份合并 | 内外层 open、pre 纵横位置、原 Text、选区方向和键盘焦点保留；阶段与事件计数继续更新。 |
| 任务弹窗与材料分段 | 主页面和 dialog 各自作用域；加载更多仅协调事件区；跨类型异步弹窗共享最后一次用户意图代次 | 已有标题/关闭按钮保留；较慢旧请求不能覆盖后开的弹窗或关闭后重新打开；用户明确翻材料段只改变该内容。 |
| 检查点、未完成专题、来源、原始状态与动作 | 所属 task / report + 命名面板；需求使用 demand ID | 嵌套展开不由程序恢复 toggle 反写；未变化正文不无故替换。 |
| 最新与历史报告、引用和材料 | report ID；material ID + 文档版本 / 片段范围 | 新报告加入时旧报告正文和引用节点保留，新引用默认关闭；“最近一次”总览摘要有独立 report ID。 |
| 交易、资金、费用、更正 | account + entry ID；表单 draft-key 包含账户 / 对象身份 | 流水详情与长表格位置稳定；草稿保留，后台版本变化仍使旧交易预览失效，最终 CAS 不放宽。 |
| 角色提示词、工具、账户模型 | role_id + global / account 作用域；沿用专用角色同步 | 延续只读、未聚焦滚动、IME 生命周期、选区方向和跨页草稿保护；新版本提示不把草稿冒充已生效。 |
| 供应商、系统设置、证券查找 | provider ID、模型 ID、命名设置表单；局部查找验证输入与账户仍相同 | 秘密只在当前输入 DOM；结果迟到不写回已离开的表单。 |
| 预测、批次、长表格与筛选 | forecast / sample / batch ID，窗口账户与计划日期，规则版本与期限 | 实体明细保持；明确筛选 / 分页改变范围，不让旧查询覆盖新范围。 |
| 总览研究动态 | account + job ID | 新任务插入时不再把旧任务行当作新任务改写；原记录仍可见时其文字节点和选区保留。 |

完整位置及对应生产测试见 [阅读矩阵](twelfth/reading-state.md)，该表区分实测和源码审计，没有把每个页面“走过一次”称为所有细节均已独立验证。

阅读事务记录可见文字或记录锚点及相对偏移，只补偿实际布局位移；不是每轮强制恢复绝对 `window.scrollY`。实际已有纵横滚动的 pre、表格、材料区及表单节点一起保护。未变化的 Text 不写 `nodeValue`；节点移动后仅在原端点仍连接且文字未变时恢复 Range。用户主动关闭的 details 维持关闭。页面缩短导致浏览器触底钳位时，仅保留锚点所需的有限底部空间；不设置固定容器高度，内容增长、用户主动滚动或导航即清理，连续轮询不会累加。最终 [锚点与清理证据](twelfth/anchor-lifecycle-final.txt) 实测只补足 41px，原可见文字位置和选区保持。

事件请求按 task + surface 合并在途读取，seq 去重、排序，游标单调前进；返回时核对账户和导航代次。新事件显示新增数量与“查看最新”，用户主动定位后才滚到末条。每段最多 2000 条，每批最多 100 条，用户主动切下一段才释放旧段；后台阶段和总数继续变化。事件缓存最多 30 段、已读计数 60 个、报告缓存 100 份，非当前表单草稿有 40 份上限；离开 Agent 页或切账户释放相关事件数据，旧展开任务 ID 随可见范围与缓存清理。不把正文、完整事件 JSON 或 Key 复制进 localStorage/sessionStorage。

## 用户反馈的修复前后依据

主页面生产回归以基线静态资源和隔离生产 FastAPI 复现：展开事件内层、JSON 滚到 300px、横向滚动并反向选择 3418 字符后更新，旧代码内层关闭、节点断开、选区清空；流水详情也关闭。任务弹窗通过真实加载更多入口复现已有详情节点被替换。这些“旧行为断言成立”只说明故障已复现。

修复后专用回归使用正常四秒轮询，**连续至少 10 轮，每轮实际写入新受控事件并改变任务阶段**。原事件节点、JSON 纵横位置、3418 字符反向选区、焦点与视口锚点保持；还覆盖上方新增任务、上方说明增高、主动关闭一个详情、相邻详情保持展开、主页面与 dialog 状态隔离、重复点击与延迟重叠分页。流水和资金展开、旧报告引用与新报告默认引用、角色旧组也分别有回归。

独立审查另外实际复现同页 A 详情慢请求覆盖后开的 B 弹窗，随后增加统一弹窗意图保护；专用结果见 [dialog-evidence.json](twelfth/dialog-evidence.json)。总览动态记录的修复后真实插入验证也保留了原行与选区。完整整合回归中发现的默认展开、跨账户和异步顺序问题另留失败日志；修复后最终八组完整运行全部通过，锚点触底空间的有界性与清理也单独验证。

## 买卖方向控件与表单行为

原入口是普通 select，macOS 的原生菜单背景、蓝色选中行和定位不能由外层 padding 统一。新增 `select-control.js` 的固定选项控件，**仅用于买卖方向**，仍只有 buy / sell，不重写全部 select。

采用常规下拉布局：触发框保持原位，菜单紧邻下方，左右边界与宽度一致；触发值和选项文字采用相同水平起点，右侧勾选位固定，不随选中移动文字。选中、鼠标悬停和键盘候选分别表达。支持 Popover top layer，兼容路径使用 body / 当前 dialog 内 portal；实时读取触发框几何并处理滚动、窗口、缩放和空间不足，不使用页面固定坐标或负 margin，也不被 card 裁切。

只有一个成功字段 `select[name="side"]`，值为 buy / sell；它不重复暴露可聚焦控件。可见 button 采用 select-only combobox / listbox 语义，并关联标签、expanded、controls、option、selected 和 activedescendant。Enter / Space 打开及确认，方向键只浏览，Home / End 定位，Esc 撤销临时候选，Tab 正常离开；选择方向不会提交交易。

确认选择后更新真实字段并触发 input / change，使原预览立即失效；FormData、可见标签、预览请求、最终请求和落盘方向一致。后台账户或费率版本变化也必须重新预览，草稿及菜单不会随机恢复 buy。reset、disabled、校验失败、切账户、异步保存期间继续编辑均有专用生产页面断言。隐藏兼容字段不会造成重复表单值，校验焦点回到可见控件。

实际 macOS Chrome **156.0.8078.74** 与 Safari **27.2** 的点击、键盘、真实 100 / 125 / 200% 浏览器缩放及长表单滚动已有独立记录。原生灰蓝菜单的修复前截图与修复后展开截图均保留：[Chrome 修复前](twelfth/mac-chrome-before-open.png)、[Chrome 修复后](twelfth/mac-chrome-open-100.png)、[Safari 修复后](twelfth/mac-safari-open-100.png)。专用自动化另覆盖 390 / 800px 窄屏、字段、请求、disabled/reset 与弹窗层级；CDP visual viewport 缩放不混写成 Safari 或实际浏览器菜单缩放。没有声称操作系统 IME 或读屏器已人工验收。上海时间 2026-11-01 00:22–00:24，最终 `478fefb` 的新隔离进程再次接受 Chrome / Safari 实际展开及 Down+Enter 卖出操作，并显示重新预览提示；Chrome 另观察受控事件由 6 条增至 7 条，原内层详情保持展开。全面缩放和长表单记录对应的控件/CSS 此后未变；最终八组对冻结文件重新执行。最终夹具实际监听 **51646**，`mac-final-runtime.json` 的 `port=8765` 是其未设 `APP_PORT` 的默认诊断字段，不能据此认定操作过日常 8765。

语义参考：[MDN select](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/select)、[W3C select-only combobox](https://www.w3.org/WAI/ARIA/apg/patterns/combobox/examples/combobox-select-only/)。参考示例不替代本项目的实际表单与浏览器回归。

## 两项跨日失败与生产校正日期

本轮于 **2026-10-31 16:11:18 UTC / 2026-11-01 00:11:18 上海** 核对，最新远端仍是 [Actions 41089590512](https://github.com/180cmchn/AI-Powered-Portfolio-Intelligence/actions/runs/41089590512)，基线 `09557bd...`，结论 **failure**。job `124179677368` 的 Python 为 **964 passed、2 failed、1 warning**，两项均在当前校正后提交固定 10 月 30 日卖出，报当前可卖 0。其 Node、浏览器安装及六组浏览器步骤 **skipped**，不能写成通过或六组产品失败。

日志时间 `2026-10-30T16:27Z` 已是上海 10 月 31 日。原代码把明确可卖量写成 `sellable_on=now()[:10]`，而两项测试固定在上海 10 月 30 日 10:00 卖出且未冻结校正时钟。校正因此只证明 31 日起可卖；先前本地同日通过和跨日 CI 失败并不矛盾。修改前本地也实际得到 **2 failed**，见 [python-time-before.txt](twelfth/python-time-before.txt)。

新增 `app.clock.now_datetime()`，全部生产墙钟入口经单一 UTC 时钟转为所需时区，避免只 patch `now()` 却遗漏 `datetime.now(TZ)`。fixture 按测试作用域冻结并显式推进时间，覆盖 FastAPI 工作线程及并发账本路径；无生产改时钟设置，不改操作系统时钟。超时仍使用真实 monotonic / perf_counter，不因测试墙钟冻结而失效。

两项原测试明确在上海 10 月 30 日 09:00 建账户并校正，推进到 10:00 卖出；保留原成本、费用和零股断言。新增 8 项生产 HTTP / 临时 SQLite 回归覆盖上海午夜、UTC 与上海不同日、周末/假期、T+0/T+1、日历缺口、未来成交、撤销、幂等和失败无写入。交易日历用明确夹具，不随机选择“今天能通过”的日期。

生产规则维持**上海日期粒度**：明确的期初可卖数量从本次校正日期起生效，不能证明此前日期可卖；实际录入时刻另存，不宣称秒级历史重建。新校正和持仓 JSON 保存 `availability_effective_on` / `availability_timezone`，错误同时提示成交日期与校正生效日期。一次校正捕获一次日期，避免跨午夜把不同持仓写成不同日期。API 与 Store 不接受自填历史生效日，没有新增历史重放引擎。

未放宽未知可卖量、T+1、未来成交、日历覆盖、不可变流水与费用、账户 CAS、Decimal 或幂等规则。新增审计字段使用现有 JSON，不迁移 schema、不改原始账本与 ID；旧记录继续依其已有 lots 校验。

## 保留第十一轮账本、超时与发布保护

账本仍由服务端原子写入成交、资金、成本、费用、账户版本、快照、研究需求和幂等回执。角色和模型没有记账工具；刷新 UI 不产生交易或研究需求。费用方案历史不可变，正向/撤销费用不重复扣收，未知成本不当作零，未知可卖日期不猜测。

分层超时没有重做或取消：首个有效事件、有效进展空闲、请求硬限、子任务和账户预算、完整 terminal、取消原因及有限重试保留。心跳不等于有效进展；不降低用户 effort，不设置无限超时，不记录隐藏思维链。第十一轮真实普通请求 **107.515 秒**、汇总 **210.695 秒**完整结束，以及另一次最后有效进展后 **120.002 秒**无进展失败均保留，不能继续把旧绝对 90 / 120 秒上限当成当前未修复问题。

第十一轮最终代码已有 unknown→stale 首次旧报价观察保护，本轮只保留并增强回归。受控生产 `Service.execute → Store.publish → Service.tick` 在发布后连续 10 轮观察，重开同一 SQLite / 新 Service 后再观察 10 轮，报告保持 current，没有首次旧价引起的无意义自动任务。fresh→stale、真实价格条件变化、材料和持仓变化仍产生应有、持久去重的需求；手动复核仍可用。

这些受控结果不把第十一轮发布后又 stale 的真实报告历史改成成功。第十一轮最终保护发生在当时真实运行之后，其真实复验不足的边界见 [第十一轮交付](iteration-11.md) 和 [原真实记录](eleventh/validation-manifest.json)。本轮如有真实研究，须按最终源码重新启动隔离进程，检查最终有效状态，而不只检查历史表曾插入报告。

## 验证命令、CI 与中间结果

CI 改为独立 Python 作业和八组浏览器矩阵，含既有六组与 twelfth / twelfth-select；每个作业有限时，矩阵 `fail-fast: false` 保留各组证据。总 `tests` 作业要求 Python 与所有浏览器成功，失败、取消或必需步骤 skipped 均不通过，未使用 continue-on-error 制造总状态假绿。未授权推送时，这只能称为本地等价验证和工作流修改，不能声称 GitHub 已绿。

最终完整工程验收实际命令（2026-11-01，Asia/Shanghai）：

```sh
PYTHON_DOTENV_DISABLED=1 QUOTE_PROVIDER=manual /tmp/guanlan-twelfth-py312/bin/python -m pytest -q
PYTHON_DOTENV_DISABLED=1 PYTHON=/tmp/guanlan-twelfth-py312/bin/python npm run test:browser
```

第一条退出 0，**976 passed、0 failed、0 skipped、1 warning，108.42 秒**；警告为现有 Starlette TestClient/httpx 弃用提示。第二条依次执行既有六组与 twelfth / twelfth_select，**8 组 passed、0 failed、0 skipped**，总退出 0。浏览器夹具自行设置 manual 报价和受控模型/网络替身，未把真实调用算入工程结果。

安装项目依赖后的常规复核入口：

```sh
PYTHON_DOTENV_DISABLED=1 QUOTE_PROVIDER=manual .venv/bin/python -m pytest -q
PYTHON=.venv/bin/python npm run test:browser
PYTHON=.venv/bin/python npm run test:browser:twelfth
PYTHON=.venv/bin/python npm run test:browser:twelfth-select
PYTHON=.venv/bin/python node tests/frontend_twelfth_dialog.mjs
PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest -q tests/test_ninth_lifecycle.py
```

最终本地环境为 **macOS 27.2 arm64、Python 3.12.13、Node v25.9.0、Playwright 1.62.1、Chromium 151.0.7922.34**。固定 requirements 的主要依赖为 pytest 9.1.1、FastAPI 0.159.1、httpx 0.28.1、Pydantic 2.14.1、uvicorn 0.54.0，完整已安装分发版本及依赖文件 SHA-256 见最终 manifest。原开发 `.venv` 为 3.14.4，仅定向早期记录使用该环境。远端目标 Ubuntu / Python 3.12 / Node 22 未在本轮推送执行，不把本机 macOS / Node 25 结果冒充相同远端环境。

| 已有中间运行 | 实际结果 | 不能替代的验收 |
| --- | --- | --- |
| 两项原 Python 失败修复前 | 2 failed | 不是修复后结果。 |
| 时间、账本、超时、发布保护定向 | 190 passed、1 warning | 与完整 Python 有重叠，不相加计数。 |
| 首轮完整 Python 3.12 | 974 passed、1 warning | 是新增两项无费用真实验证脚本预检前的运行，不替代最终 976。 |
| 首轮八组浏览器 | 6 组通过；iteration / production 失败 | 本轮新协调器的默认展开 / 跨账户回归，不是旧远端 skipped。 |
| 第二轮八组浏览器归档 | iteration 通过；production / twelfth 当时失败，其余通过 | 中间失败保留；已在最终八组全部成功前修复。 |
| 专用主页面 / 弹窗 / 控件与 Mac 检查 | 见各自通过记录和限制 | 不替代最终八组总运行，也不证明真实模型质量。 |

手册启动回归使用临时复制仓库、隔离运行目录与随机端口，检查实际 start.command / 管理入口、健康和接口身份，不通过重启日常 8765 验收。`tests/test_ninth_lifecycle.py` 的 **11 项**包含在最终 976 项通过中，没有另外虚构一次独立最终运行。六位代码、交易预览/确认、任务与事件、报告入口由最终浏览器组覆盖。新增验证脚本的 **2 项**无费用预检也包含在 976 项中：一项使用明确 ASGI/模型替身走真实生产 API 并核对三笔交易、现金 86484.90 及发布后/重启后各十轮观察；另一项启动真实隔离子进程核对随机 socket、worker=False 及零研究尝试。它们不算真实供应商调用。

## 受控端到端与可选真实研究

工程回归中的证券、事件、材料、报告和模型协议替身都明确标记为夹具。它们证明交易预览/确认、账务对账、任务追踪、事件阅读、报告入口及发布保护的生产代码路径；不证明投资研究真实成功、实时行情或预测准确率。新增前端验证不会为了展开/选区问题触发付费模型。

可选真实入口 `scripts/verify_twelfth_live.py` 的使用和限制见 [live-verifier.md](twelfth/live-verifier.md)。准备期 `worker=False`，独立账户使用 100000 元测试现金与 518880、002354、159599 三只真实证券代码，但数量和价格是明确虚拟输入。三笔预览/确认及现金 `86484.90` 核对后，才释放唯一最新研究任务，避免每笔准备交易都启动研究。模型、供应商、协议和 effort 沿用已授权配置，不读其他应用凭据，不为成功循环重试。

真实成功后的观察须包括正常发布、后续轮询及隔离进程重启后的有效状态；有真实新消息引起的合理 stale / 复核应如实记录。没有自然触发的超时、故障或旧报价条件时，应写“未覆盖”，不能拿受控失败注入替代自然实测。服务商发票费用无法观察时保留未知，不用 token 数推算已收费用。

| 本轮真实供应商项目 | 最终状态 |
| --- | --- |
| 是否实际启动及授权配置身份 | 2026-11-01 00:26:36–00:33:04上海；最终478fefb，隔离账户、一个业务任务。manager沿用grok-4.7/max；各子角色沿用各自已授权绑定，绑定差异0。 |
| 全部实际 HTTP 请求、重试、完整 terminal、失败与取消 | 39次模型请求，37次response.completed，2次RemoteProtocolError；没有业务重跑。两次失败均已接收请求并返回200，缺少完整终止，按结果未知策略禁止自动重发。 |
| 真实网络尝试及可见用量 / 费用 | 37次有usage，原始cost_in_usd_ticks合计8,944,142,400；未自行换算，发票费用未知，两次失败费用未知。全部请求诊断与可观察工具/原生搜索记录见独立审计，不承诺未插桩HTTP子请求的逐包计数。 |
| 发布报告、后续 current / stale / 无最终报告 | 汇总阶段失败，report_id为空、not_published。没有最终报告，真实发布后10轮与重启后10轮未执行；受控发布保护通过单列，不能替代。 |
| 额外自动任务与来源 | 0；三笔准备操作产生的旧版本任务保留为superseded且未尝试，只有最新v4任务实际调用。现金仍86,484.90，未改日常test账户。 |
| 最终代码重启后复验 | 全程source/.env摘要不变。真实worker已停止；另用同一最终源码、同一隔离记录、worker=False展示进程核对分页100→300且原JSON仍展开，已停止。这不是发布成功后的重启观察。 |

两次失败持续约114.848秒和177.061秒，最大有效进展空闲分别约62.759秒、68.444秒，均低于本轮120秒空闲限制，硬限分别300/360秒。可观察分类是接收响应体期间的 `incomplete_response_body`，不是此前90/120秒绝对超时；记录了代理隧道，但无法只凭该异常断定上游服务或代理链路的更细根因。未延长超时、降低effort或反复重试来制造成功记录。

真实进行中的主页面从95条增长至331条，拖选JSON和内部滚动仍保留；完成后的只读分页观察与精确受控十轮断言分别记录。详见 [真实UI复核](twelfth/live-ui.md) 与 [完整真实审计](twelfth/live-research.md)。

## Mac 操作、升级与回退

日常使用中，展开研究事件后可持续阅读原事件；新增事件数量提示不会强制跳底，点击“查看最新”才定位。每段到 2000 条后可主动打开下一段。主页面与任务弹窗分别保持各自展开状态；明确切页、切账户、分页或材料分段允许改变对应阅读范围，未提交草稿只在本次浏览器会话和容量范围内保留。

买卖方向用 Enter / Space 打开、上下键浏览、Enter / Space 确认，Esc 放弃高亮候选，Tab 正常离开。改方向或收到新账户/费率版本后重新预览，再核对并确认记账。选择方向本身不成交。校正可卖量仅证明当前上海日期，补录更早日期失败时核对依据和时间，不通过反复改量绕过校验。

升级前按 [Mac 手册](mac-operations.md) 检查目标端口、数据库、PID 和实际启动身份并备份。已有运行实例不会因为磁盘文件改动而自动加载新代码；最终代码应重启**相应隔离进程**复验。日常 8765 的安全升级属于用户管理的日常操作，本轮未擅自执行。

本轮无 schema 变化，无需为了轮次重建数据库。代码回退到第十一轮可继续读取新增 JSON 审计字段；不要删除新账本记录、重置 ID、覆盖正在运行的 SQLite 或清理 WAL。恢复旧备份会丢失备份之后的业务，不能当作普通代码回退。数据库备份不包含钥匙串 Key 或 `.env`；保留现有安全凭据与模型配置，换机按已有安全存储流程处理。

共享角色目录、预设模型与五档 effort、供应商三字段和自动发现目录保持；主 Agent 按需派发，不强制全角色或固定审核员。原生联网路线、账户模型结论隔离、固定预测期限/阈值/不可变评分账本不变；不新增搜索 Key、券商连接、自动下单或投资评分门槛。

## 最终冻结工程验收

下表工程部分均对应最终 `478fefb`。真实供应商部分如实记录失败，未由工程通过填充成成功。

| 必填项 | 最终值 |
| --- | --- |
| 最终实现 commit / 工作区是否仍有生产修改 | `478fefb22a123c71b6594e8666be5e9acbb2cf5f`；归档时无其后的生产代码或测试修改，仅文档收束。 |
| 最终 source SHA-256 / static SHA-256 / schema / API contract | `f9d22f8b…` / `a081f49e…`，完整值见 [冻结身份](twelfth/frozen-source.json)；11 / 11。 |
| 日期、Asia/Shanghai 时区、Python / Node / Playwright / 依赖版本 | 2026-11-01，Asia/Shanghai；3.12.13 / v25.9.0 / 1.62.1；详见 [最终 manifest](twelfth/validation-manifest.json)。 |
| 完整 Python 命令、通过 / 失败 / 未执行数量、退出码 | 上述完整命令：976 / 0 / 0，1 warning，108.42 秒，exit 0。 |
| 既有六组 + 两组新增浏览器的逐组退出码与产物 | iteration、production、seventh、ninth、tenth、eleventh、twelfth、twelfth_select 全部 exit 0；[逐组状态](twelfth/browser-groups-final.json)。 |
| 主页面与弹窗十轮更新、迟到/重叠分页、跨账户最终回归 | 通过；主页面连续十轮正常轮询和真实新事件，弹窗按真实加载入口验证；选区/纵横位置/节点/关闭状态、重叠分页及最后用户意图保持。 |
| 最终源码对应的 Mac Chrome / Safari 复验范围 | 新进程 51646 与冻结摘要一致；双浏览器点击展开、键盘卖出与预览失效通过，Chrome 受控事件轮询通过；缩放与长表单证据及未覆盖边界见 [Mac 记录](twelfth/mac-validation.md)。 |
| 手册启动 → 六位代码 → 预览确认 → 任务/事件 → 报告入口 | 11 项真实隔离生命周期检查包含在完整 Python；实际生产页面链路由八组浏览器通过。模型/网络为明确替身。 |
| 账本、分层超时、发布后旧报价保护最终结果 | 完整 Python 通过；未放松可卖/费用/CAS/幂等，分层超时保留；生产发布后与重启后各十轮旧报价观察无无意义自动任务，真实变更仍可触发。 |
| 是否真实调用、全部请求/失败/终止/费用/报告/额外任务 | 单次真实研究失败；39请求、37完整、2传输失败、0最终报告、0额外任务，费用边界见上表及独立审计。 |
| 最近一次 GitHub 状态及核对时间；是否推送 | 2026-11-01 00:39:22 上海核对：基线09557bd的run41089590512仍failure，Python964 passed / 2 failed、浏览器skipped；本轮未推送，不声称远端已绿。 |
