# 第十二轮阅读状态契约与回归

本轮仍使用原有 FastAPI、SQLite 和原生 JavaScript。`view-state.js` 是小型节点协调器，不是新增前端框架。审查基线为 `09557bd8a3cd404250fa9fa58093f6babb404ead`。

## 根因与边界

旧 `render()` 仅在角色页面保留节点，其他页面用 `root.innerHTML = shell()` 销毁整棵树。`detailsOpen` 只保存少数带 key 的展开布尔值，不能恢复已销毁的 Text 节点、浏览器 Range、pre 横向/纵向位置和键盘焦点。无 key 的事件内层、流水详情、研究缺口等随每轮销毁；角色专用修补不会覆盖它们。弹窗原 `refreshDialogEvents()` 另有直接覆盖 `.job-events.innerHTML` 的入口；全站定时器在 dialog 打开时本来就暂停，不能将两条更新路径混说成同一个轮询。

新 `render()` 仅在明确切页面/切账户时更换页面，**同一页面同一账户**按实体身份协调现有节点。增加记录时插入新节点，未变化正文不写 `nodeValue`。既有 details 的 `open` 完全归用户所有；协调器不重新设置 open，因此不引发恢复 toggle 回写和反复取数。不同对象、不同面板与不同显示表面不会交换节点。

表单不是通用冻结整个页面：角色沿用专用 `syncRoleForm` 显示版本冲突，供应商沿用 `syncProviderForm` 且秘密只在当前表单，交易专用 `ledger.syncForm` 检测账户/费率更新并使旧预览失效。普通脏表单保留原 CAS 版本；账户 metadata/校正草稿显示后台新版本提示。干净表单按服务器数据协调。保存成功、明确重新载入或主动切段可以有意重建相应内容。

## 位置—刷新来源—身份—受保护状态—验证

表中“新增浏览器”指 `tests/frontend_twelfth.mjs`；“旧组”是既有生产浏览器回归，不把源码审计冒充单独交互验收。

| 生产位置 | 刷新来源 | 稳定身份/更新策略 | 受保护状态 | 验证 |
|---|---|---|---|---|
| 页面主内容与状态提示 | 四秒读取、主动刷新、版本更新 | route + account 页面边界；提示条与内容不同节点 | 视口文本锚点、页面焦点 | 新增浏览器十轮真实定时更新、27次切页 |
| 任务列表、任务阶段 | jobs API、后台新增任务 | account + task ID + main/dialog；新卡插入不移动旧卡 | 当前记录相对视口位置 | 新增浏览器上方插任务、上方任务说明增高 |
| 外层研究事件 | jobs/event API、用户展开 | account + job ID + surface | 用户主动展开/关闭 | 新增浏览器多个展开与主动关闭 |
| 每条事件来源/JSON | `/events?after=seq` | account + job ID + surface + 持久 seq；正文原节点协调 | open、pre纵横滚动、反向选区、焦点、原 Text 节点 | 新增浏览器十轮各有新事件；3418字符、top300/left240 |
| 任务详情弹窗、加载后续事件 | 用户“查看任务详情”“继续读取后续事件” | 独立 dialog surface；仅 patch 事件列表 | header/关闭、内层详情、选区、背景位置 | 新增浏览器延迟响应、连续双击、重叠数据；1请求且无重复行 |
| 检查点、未完成专题、复用缺口、原始动作/状态 | 任务或报告结果变化 | task/report ID + panel；需求行使用 demand ID | 嵌套 open、未变化文字 | 生产路径审计；tenth 检查点/缺口旧组 |
| 合并来源、研究对象 | task API | task identity；来源使用服务持久的 parent/at/trigger/action 组合（若无source ID） | 来源 open 与阅读位置 | 生产路径审计；tenth 合并任务旧组 |
| 最新/历史报告正文、发现与子成果 | analyses API、报告发布 | report ID，正文始终同一 details 结构；不会因从第一条变历史而替换正文 | 正文节点、引用展开 | 新增浏览器新报告插入、旧正文节点及引用保持，新引用默认关闭 |
| 引用定位、材料来源说明 | 报告/材料索引更新 | material ID + document_version + 片段范围；嵌套来源在所属记录内 | open、相同材料的文字节点 | 新增浏览器引用；tenth 来源定位旧组 |
| 按段查看材料 | 明确上一段/下一段 | 用户改变 material ID/片段；只换内容和页码按钮，不重建弹窗标题/关闭 | 对话框控件、其他页面状态 | 新增浏览器下一段；tenth 长材料旧组 |
| 交易流水、资金流水 | ledger API、后台账户版本 | account + entry ID；资金用 cash entry ID | details、横向表格scroll、原节点 | 新增浏览器流水/资金展开；eleventh 正常账务旧组 |
| 费用方案、费用更正、资金表单、交易表单 | 账本/方案版本、预览/保存 | 各自 draft-key；专用版本同步 | 草稿、菜单、预览失效与 CAS | twelfth_select + eleventh；本模块提供同步钩子 |
| 快照校正与元信息 | 账户 API | account draft-key；已存持仓用证券身份，新草稿分配UUID | 未提交值、CAS版本、版本变化提示 | iteration/production 旧草稿组；新增切账户 |
| 角色提示词、工具、账户模型覆盖 | 配置读取/保存 | role_id + global/account 作用域；保留原专用同步 | 只读/未聚焦scroll、方向选区、IME、草稿和冲突提示 | eleventh 原三类滚动+IME+冲突+跨账户旧组 |
| 供应商连接及模型目录 | 目录读取、连接修改、分页 | provider ID；目录项 model ID；秘密表单仅当前DOM | 目录展开、凭据输入、焦点 | production/ninth旧组；新增全页轮转 |
| 系统设置与运行版本 | settings/status读取 | settings draft-key；runtime-version 面板 ID | 未提交输入、展开、内部scroll | production旧组；新增全页轮转 |
| 预测明细、批次范围、运行证据 | forecast API、筛选/分页 | sample/forecast ID或batch+symbol+horizon；batch ID；window account+planned_date；规则版本+horizon | 各明细open、长表格scroll、筛选草稿 | seventh/ninth旧组；新增全页轮转；命名身份源码审计 |
| 证券查找与模型下拉内容 | 用户查找、用户改供应商/目录筛选 | 仅局部结果/选项明确更新；input+值+账户检查阻止迟到写回 | 上级表单和其他阅读区域 | production旧组 |

## 视口、异步与容量

- `readingTransaction()` 在更改前记录当前选中文本或可见文本/行的位置。更改后只对测出的相对位移补偿，未变的更新不调用滚动；不再每次 `window.scrollTo(savedY)`。所有实际有 scrollTop/scrollLeft 的节点都受保护，包含 pre、表格、材料区、输入与 textarea。
- Range 的两个原端点与方向保留；节点移动导致浏览器端点改变时，只在原节点仍连接且文字未变时恢复。正常追加不移动原节点，无需重设选区或焦点；IME 表单仍保持连接。
- 事件阅读段与 fetch 按 task + main/dialog 分开，分别合并在途请求；seq 去重、有序合并，cursor 单调递增。返回前同时检查账户和 navigation generation。迟到的旧页面读取不能写回新页面。
- 新事件继续显示并计数，用户“查看最新”才滚到末条。不会因打开 details 或滚轮暂停后台业务读取。
- 每个事件阅读段最多2000条（每批最多100），到达后显示“打开下一段事件”，只有用户明确切段才释放旧段DOM。后台任务阶段/计数仍更新；这不是停止全部刷新。最多缓存30个任务/显示表面段，计数作用域最多60个；任务展开选择最多100个，openedJobs清理非当前页且没有事件缓存的任务。离开Agent页/切账户释放事件数据，报告缓存最多100、未提交表单缓存最多40个非当前表单。没有把正文、完整事件JSON、密码或Key写入local/sessionStorage。
- 保留原行为：模态框打开时全局定时轮询暂停；弹窗读取通过其明确用户入口更新。每次更新不重复 showModal，不改背景scroll。材料页最后到达的旧请求受序号检查，所有异步开窗共用 generation + navigation + account 意图令牌，包括任务、单证券任务、报告、材料、公共结果、预测/批次和诊断结果；同步开窗、明确关闭、Esc与导航使旧意图失效。较慢的旧请求不能覆盖较新的弹窗，也不能在关闭后重新打开。

## 回归证据

`test-results/twelfth/reading-evidence.json` 与同目录截图记录生产源码修复前/后。基线通过静态资源路由加载 git 基线文件，后端仍是隔离生产 FastAPI；网络/模型以明确夹具代替，未调用外部模型。基线旧代码复现主页面关闭/选区清空、流水关闭、弹窗加载更多替换节点；这些失败断言表示已复现旧问题，不代表基线通过产品验收。

本分支针对性验证：`PYTHON=.venv/bin/python npm run test:browser:twelfth` 与 `npm run test:browser:eleventh` 均退出0；过程日志归档为 `browser-reading.txt`、`browser-eleventh.txt`，Mac上的Playwright Chromium 151.0.7922.34，生产四秒轮询未加速。修复前主/流水/弹窗失败均复现；修复后10轮与上方插入/增高、弹窗重叠分页通过。运行后仅另加只含缓存数量的诊断字段及相应容量断言；最终完整验收以总记录对最终代码的重跑为准。截图由运行系统的浏览器生成，不将 Chromium 截图等同于 Safari 或人工 IME 验证。没有修改日常数据库或8765进程。

## 整合回归补充

- 首次导航可能先渲染旧任务列表、后读取running任务。默认展开只在尚无用户选择时应用一次；主动作出的外层收起不会被后续状态刷新覆盖。
- 跨账户恢复未提交表单时，也恢复其所属稳定key祖先details状态，保留既有可直接继续编辑的体验；同页刷新仍不重建表单。
- 总览研究动态按account+job ID，持仓行按symbol，板块行按业务板块，报告摘要按report ID协调。新任务插前不会把旧记录文本节点重新当成新任务使用。
- `twelfth_dialog_cases.mjs` 已纳入twelfth完整组；独立快速复测命令为 `PYTHON=.venv/bin/python node tests/frontend_twelfth_dialog.mjs`。验证同页慢任务A/后开B、同步新账户覆盖读取意图、材料/研究范围跨类型竞态、材料分段关闭后不重开、sixth预测批次A/B和总览插入任务选区。
- `loadAccount()` 和 `refresh()` 在最后异步子读取返回后再校验请求序号、路由与账户，旧请求尾部错误也不能写回新页面。

## 文档缩短与晚到操作结果的最后整合修复

`e75542d` 完整浏览器串联发现总览边界：此前上方任务有很长的错误说明，新任务替换“最近任务”后文档总高2298→1591，原选中记录保持同一个Text节点，但期望scrollY732超过新的最大691，造成41px偏移（y439.89→480.89）。独立短组没有前序长说明，不能覆盖这个边界。原始精确复现见 `anchor-repro.txt`，严格2px断言没有放宽。

`view-state.js` 现在在读者锚点需要的滚动范围被内容缩短夹断时，仅附加缺少的临时尾部空间，不保留旧业务卡片/响应。下一次内容增长会重算并释放；用户滚离受约束底部后可安全释放；明确导航更换页面时释放。滚动容器原height/max-height不变，内部阅读容器使用其实际滚动父容器而不是错误调整页面背景。完整twelfth增加必要缺口高度、重复轮询不累积、内容增长、用户滚动和导航清理断言，并继续检查原节点、原选区和小于2px的位置误差。

已完成的备份与可能付费的能力检查具有结果交付语义：仍在原弹窗意图时照常展示；用户已导航或打开别的弹窗时只给“查看结果”通知，不抢焦点。实际已脱敏API返回保存在当前页面会话内最多10条，页头“操作结果”可再次查看；不写local/sessionStorage，不重发检查或再次产生费用。production回归通过明确HTTP替身验证跨页焦点不变、缓存结果原文可见、toast与页头两条查看入口均不增加POST计数。

最终实现 `478fefb` 的完整twelfth组重新运行通过，结构结果为本目录 [reading-evidence.json](reading-evidence.json)。十次正常轮询均保留3418字符反向选区、pre top300/left240、焦点及原节点；弹窗延迟重叠分页1个请求、439行唯一事件。27次导航后的事件段0、事件数0、草稿12、报告3。截图：[主页面修复前](baseline-main.png)、[主页面修复后](fixed-main-reading.png)、[弹窗修复前](baseline-dialog.png)、[弹窗修复后](fixed-dialog-events.png)。初期定向结构证据另存 `reading-evidence-initial.json`，没有覆盖失败日志。

独立生产页面生命周期最终复测见 `anchor-lifecycle-final.txt`：原选中文字 y439.890625 在更新后保持完全一致，所需补偿41px且仅有一个尾部节点，两轮真实轮询不增长；增长、用户滚离及导航均清除，页面容器原inline height仍为空。全部七项对话框/总览检查通过。早先新增生命周期测试曾在无滚动缺口的第二次收缩错误要求必须存在尾部节点，见 `anchor-lifecycle-first-failure.txt`；测量得到内容高度2298、scrollY732、视口900，确实无需尾部空间（`anchor-lifecycle-no-deficit.txt`）。该次是测试准备不足，已改为明确制造实际41px缺口并先断言缺口大于零，没有放松位置、原节点或选区断言。
