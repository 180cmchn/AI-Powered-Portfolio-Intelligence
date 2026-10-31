# 第十二轮：实际 macOS 浏览器复核

日期：2026-10-31，Asia/Shanghai。macOS 27.2（26B123），Google Chrome 156.0.8078.74，Safari 27.2（22625.2.8.11.9）。这是通过本机应用的实际点击、键盘和浏览器缩放复核；另列的 Playwright/CDP 视口缩放不替代本记录。

启动 `tests/eleventh_browser_server.py`，生产 FastAPI 和静态模块、临时 SQLite、随机端口、worker=False。目录/长提示词/已保存报告为明确夹具，不调用外部网络或模型。修复前端口 61609，修复后新实例端口 63978。测试新开标签页；日常 8765 标签页、数据库及进程未操作。验证结束关闭本轮标签页、恢复测试页100%缩放。

| 检查 | Chrome | Safari |
|---|---|---|
| 实际点击展开 | 原生菜单灰蓝背景及偏移已复现；修复后浅色菜单与触发框左右边界一致 | 修复后浅色菜单与触发框左右边界一致 |
| 首项文字起点 | 与闭合值对齐，勾选固定在右侧不挤动文本 | 同左 |
| 键盘选择 | Down+Enter选择卖出，Space打开、Up临时高亮买入、Esc后仍为卖出 | 同左；另外点击买入成功 |
| Tab | 关闭菜单，焦点到证券代码 | 同左 |
| 真实125%页面缩放 | 浏览器AX明确显示“缩放比例125%”；展开/滚动后锚定保持 | 页面菜单明确显示125%；展开/滚动后锚定保持 |
| 真实200%页面缩放 | 浏览器AX明确显示200%；菜单宽度、首项起点仍对齐，不被card裁切 | 页面菜单明确显示200%；相同结果 |
| 长表单滚动 | 菜单与触发框一起移动，标签保持可见 | 同左 |

展开截图：

- [Chrome 修复前原生菜单](mac-chrome-before-open.png)
- [Chrome 修复后100%](mac-chrome-open-100.png)、[125%滚动后](mac-chrome-open-scrolled-125.png)、[200%](mac-chrome-open-200.png)
- [Safari 修复后100%](mac-safari-open-100.png)、[125%](mac-safari-open-125.png)、[125%滚动后](mac-safari-scrolled-125.png)、[200%](mac-safari-open-200.png)

字段唯一性、FormData、真实预览/确认请求、账户/费率版本冲突、异步保存期间的新草稿、菜单在轮询期间的原节点与候选状态、disabled/reset、弹窗 top layer、390/800px窄视口由 `frontend_twelfth_select.mjs` 的生产页面自动化补足，见 [选择控件契约](select-control.md)。可见原生应用复核没有提交交易；自动化交易仅写临时测试数据库。没有声称真实系统输入法或屏幕阅读器人工验收。

## 最终代码重新启动复验

2026-11-01 00:22–00:24（上海），最终实现 `478fefb22a123c71b6594e8666be5e9acbb2cf5f` 重新启动 `twelfth_browser_server.py`，端口51646，临时数据库 `/tmp/guanlan-twelfth-mac-478fefb/twelfth.sqlite3`。启动身份见 [mac-final-runtime.json](mac-final-runtime.json)，source `f9d22f8b...`、static `a081f49e...`，与最终冻结摘要相同。此前 e755 的中间截图单独保留，不借旧进程证明最终源码。

Chrome 和 Safari 再次实际展开菜单，Down+Enter 选择卖出，可见值变为“卖出”并显示“输入已改变，请重新预览”。截图：[最终 Chrome 展开](mac-chrome-final-478-open.png)、[最终 Safari 展开](mac-safari-final-478-open.png)。100/125/200%及长表单的全面检查在前述记录中；选择控件和CSS在其后没有再改动。最终提交另外修改阅读锚点和晚到结果，全部八组浏览器对最终文件重新执行。

Chrome 最终实例展开受控长事件 A，向隔离服务追加一条明确 `test_double` 事件，正常定时轮询从6条增至7条，原内层JSON仍展开：[最终事件轮询截图](mac-chrome-final-478-event-poll.png)。此项原生操作验证展开与持续更新；精确3418字符选区、纵横位置、十轮和弹窗分页由完整生产浏览器回归验证，没有混写为人工测量。

复核后关闭本轮新开的测试标签页，按已核实的临时数据库路径终止该隔离进程。没有重启或改写日常8765服务。
