# 第十二轮：买卖方向的控件与表单契约

## 实现与布局

`app/static/select-control.js` 提供一个固定选项、不可自由输入的选择控件；当前只应用于持仓管理的「买卖方向」。没有替换全站其他原生 select，也没有引入前端框架。

原入口由 `selectField()` 生成普通 select，弹出菜单由 macOS 浏览器/系统绘制，外层 padding 不能决定其系统菜单的定位和灰蓝背景。现在采用普通下拉布局：触发框保持原位，白底菜单距触发框下边缘 4 CSS px，左右边界与宽度相同。触发值与每项都使用 11 px 水平内边距，勾选标记在右侧固定宽度，不推动文字。选中项有文字与勾；鼠标悬停使用浅灰绿背景，键盘候选使用细描边及浅绿背景。

菜单以 trigger 的实时 `getBoundingClientRect()` 定位。使用 `popover="manual"` 进入 top layer，避免 `.card { overflow:hidden }` 裁切；不支持 Popover API 时使用 body/当前 dialog 内的 fixed portal。窗口、滚动、visual viewport 变化由单个 RAF 更新位置。空间不足时向上移到自身标签上方，或限制高度并在菜单内滚动；没有页面固定坐标或负 margin。触发框离开视口、被移除或禁用时关闭菜单。

一次模块加载安装一组委托事件与一个 MutationObserver，同一时刻最多一个临时 popup。关闭会删除 popup，断开触发节点也会清理；不缓存旧菜单节点，不将选择或正文写到浏览器存储。账本读取缓存另限制为最近 12 个账户快照/错误。

## 字段和事件

| 契约 | 行为 |
| --- | --- |
| 成功字段 | 仅隐藏的 `select#trade-side[name=side]`，值为 `buy` 或 `sell`；没有第二个同名字段。 |
| 可聚焦控件 | `button#trade-side-control`，`type=button`，带 combobox、haspopup、expanded、controls、labelledby。隐藏 select 为 `hidden tabindex=-1 aria-hidden=true`。 |
| 可访问列表 | listbox 关联原标签；option 有 selected；候选由 trigger 的 activedescendant 指向。当前选择与键盘候选分离。 |
| 选择提交 | 点击候选或 Enter/Space 确认时更新 select；值改变后触发冒泡的 `input` 和 `change`。这不会提交交易表单。 |
| 预览 | 原有监听立即删除该 form 的 WeakMap 预览、禁用确认按钮，并提示重新预览；最终请求继续由 FormData 生成 side。 |
| 新业务版本 | `ledger.syncForm(existing,incoming)` 暴露账户/费率版本变化并作废旧预览，保留草稿及原 CAS 版本；重新预览明确采用最新版本。服务端最终 CAS 未放宽。 |
| 保存中编辑 | 回执对应的买卖方向与在途 payload 固定。若用户继续改方向，保存成功不清除后来的草稿，也不将其冒充已成交。 |
| reset / disabled | reset 恢复初始 buy，关闭菜单并作废预览；native select 的 disabled 同步到 trigger，字段按原生规则从 FormData 排除。 |
| 账户切换 | 老 surface 的菜单关闭；新账户表单有独立草稿身份，初始 buy。 |

键盘：Enter/Space 打开并确认；方向键打开/移动候选；Home/End 定位首尾；Esc 丢弃临时候选并回到 trigger；Tab 正常移到下个输入，关闭菜单而不提交候选；点击外部关闭。弹窗中第一次 Esc 关闭菜单，第二次 Esc 才关闭 dialog。隐藏字段若出现原生 invalid，会把焦点转回可见 trigger。

全站协调器保留正在使用的表单节点，所以菜单、当前选择和候选无需在每轮刷新后重新创建。账本流水行采用 `ledger-entry:<account_id>:<entry_id>`，详情采用 `ledger-record:<account_id>:<entry_id>`；资金行同样依据其真实 ID。费用更正表单身份包含账户和 entry ID。校正详情在后端提供时单独显示「可卖校正生效日（上海）」与实际录入时间。

## 自动化证据

执行环境：macOS（darwin），实际安装的 Google Chrome `156.0.8078.74`，Playwright headless 驱动；Python 测试服务使用临时 SQLite、生产 FastAPI、生产静态模块、`worker=False`。显式目录/报告为受控夹具，服务器墙钟固定为上海 `2026-10-30T16:00:00+08:00`。未联网获取行情，模型请求为 0，不读写日常 8765。

```bash
PYTHON=.venv/bin/python \
BROWSER_EXECUTABLE='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
node tests/frontend_twelfth_select.mjs
```

最终专用回归退出码 **0**；日志 [browser-select-chrome.txt](browser-select-chrome.txt)，结构证据 [select-evidence.json](select-evidence.json)。覆盖：

- buy/sell 的唯一 FormData 值、可见文案、预览请求、确认请求及落盘方向完全一致；点击/键盘选择本身没有成交 POST。
- Enter/Space、方向键、Home/End、Esc 撤销、Tab、点击外部、reset、disabled。
- 菜单打开并高亮未提交 sell 时，通过生产 API 增加真实受控事件并等待正常轮询，trigger/popup 原节点、打开状态和候选保持，后台 event_count 增加。
- 方向、账户版本、费用方案版本分别使旧预览失效。一次 T+0 ETF 买入、卖出及 10 元受控资金转入后，精确现金为 `100200.00`。
- 空持仓卖出被生产 API 以 422「卖出数量超过持有量」拒绝，sell 草稿仍保留。延迟买入回执期间改选 sell，落盘仍是 buy，表单保留未提交 sell 草稿。总成交 POST 为 3，无重复。
- popup 位于 top layer；主表单和 modal 均实际点击打开。正常桌面、390/800 px 窄屏与 visual viewport 1.25/2 倍缩放有坐标/命中证据。宽度/左右起点/首项文字与 trigger 误差均小于 1 CSS px；正常桌面二者文字 x 均为 299。
- 账户切换关闭旧 popup，新账户默认 buy；页面 JS errors 为 0。

开发期间两次新增断言失败分别是把生产 422 写成 400、把「超过持有量」错误写成「可卖」；已按实际生产 API 契约修正，未修改后端校验。早期主要控件回归与最终补验均另有成功执行；以上最终日志来自包含补验的最后一次。

[自动化 macOS Chrome 展开截图](select-chrome-automated-open.png)；[390 px 窄屏展开截图](select-chrome-automated-narrow.png)。这里的 1.25/2 倍使用 CDP `Emulation.setPageScaleFactor`，**不是 Chrome 菜单里的真实浏览器缩放，也不是 Safari 验证**。实际可见 Mac 点击、系统浏览器缩放和 Safari 的结果以本轮主交付里的独立记录为准；不将本文件的 headless 或视觉视口证据混记为那些人工/原生入口已通过。

旧 `frontend_eleventh.mjs` 的 trade helper 已改为实际点击新的 trigger/option，不再用 Playwright 对隐藏 select 的 `selectOption()` 冒充用户操作。原表单账务、响应丢失、幂等和费用断言保留。该组完整结果由本轮总回归单列。

## Mac 复核与升级

隔离入口可运行 `mkdir -p /tmp/guanlan-choice-check` 后执行 `PYTHON_DOTENV_DISABLED=1 .venv/bin/python tests/eleventh_browser_server.py /tmp/guanlan-choice-check`，读取第一行中的随机端口，在 Chrome/Safari 打开 `http://127.0.0.1:<port>/#holdings`。该程序是明确的测试夹具，无模型 worker，不使用日常账户。夹具成交时间必须填写 `2026-10-30` 的有效交易时段；初始表单的浏览器本机当前日期不等于测试服务器墙钟。完成后只结束这个夹具进程。

真实页面使用方式：Tab 到「买卖方向」，Enter 打开；方向键只浏览，Enter 选择，Esc 取消；改方向后重新「预览现金与费用」，核对后再「确认记账」。后台出现新版本提示时也需重新预览。费用/资金表单保留原 CAS，冲突时核对当前记录后重新载入。

本控件与阅读状态修复无 schema 变更。部署时只重启相应隔离实例验证最终代码；未在本任务中重启日常 8765。正常升级/回退沿用项目备份和启停手册，保留账本数据库、ID、模型配置及凭据，不能用旧测试数据库覆盖日常数据。

参考：[MDN select](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/select)、[W3C select-only combobox](https://www.w3.org/WAI/ARIA/apg/patterns/combobox/examples/combobox-select-only/)。这些材料用于核对语义；实际组件行为以上述生产页面回归为依据。
