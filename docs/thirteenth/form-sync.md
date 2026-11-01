# 第十三轮：表单实际值同步

`reconcile()` 原先先移动 incoming 的子节点，末尾才读取 incoming.value。移动 textarea 文本后，其 live value 已清空；移动 select 的 option 后，原选择也会改变。原来的“目标值”因此已不是渲染输入的目标状态。

新实现先捕获 textarea/input 的 live value、checked，以及 select 的 selectedIndex 和每个 option 的 selected 状态。随后沿用原稳定节点协调，最后仅在实际不同处同步 live 状态。默认值仍由 value/checked/selected 属性与 textarea 文本决定。多选按每一项的真实状态同步；重复 value 的单选按索引同步，不用 `.value` 把不同选项折叠。未变化 Text 不被替换，未改为全量重绘。

## 生产路径前后观测

[完整原始 JSON](form-evidence.json) 同时记录旧提交、修复实现、`value/defaultValue/textContent/checked/defaultChecked/selectedIndex/selectedOptions/FormData`。基线直接从 Git `e8cf4f6e937779b11ea4d7aad511fc070bb2d0ae` 加载静态模块；测试不复制协调函数。

| 场景 | 修复前实际值 | 修复后 |
| --- | --- | --- |
| 空 textarea → SH:518880 | value 空，defaultValue/textContent 为 SH:518880，FormData 提交空串 | 三者均为 SH:518880，FormData 一致 |
| select 新增并选择 b | value=a，selectedIndex=0 | value=b，selectedIndex=1 |
| 空 select 初始化 a/b 并选 b | value 空，selectedIndex=-1，FormData 无字段 | value=b，selectedIndex=1，提交 b |
| 完整 market 页空观察范围后台更新 | 轮询后 textContent 已更新、value 仍空 | 正常轮询后 value/FormData/后端范围一致；实际 PUT 保存也一致 |

18 项模块矩阵还覆盖非空→空、同值 Text 身份、程序 live 值与默认值不同、已有选择变化、选项删除/清空/重排、重复 value、无选项选中、disabled 开关、多选新增、multi→single 和 checkbox 默认/实际状态分离。普通 HTML 状态分别走 `reconcile()` 与 `patchHTML()`。

8 项完整生产页面场景覆盖后台范围变更与真实提交、焦点保持、未保存草稿与反向选区、compositionstart 后失焦仍保留输入、材料目录后台新增、所选类别退出当前页。材料筛选在 form 外：原生 select 和过滤谓词必须同时回到 all，避免菜单已显示“全部材料”却仍按已不存在的类别过滤。

## 编辑和服务端更新的契约

- 未编辑且未聚焦的表单接受新服务端状态；观察范围带版本，后台更新能提示草稿与新范围存在差异。
- 聚焦、dirty 或正在 composition 的表单保留连接中的 DOM、光标与内容。结束 composition 后仍作为未保存草稿；没有伪造 compositionend。
- 聚焦但未编辑的控件失焦后，可继续接受后续后台新版本。
- 原有角色表单只读滚动、普通草稿、任务详情/流水/报告阅读状态与买卖方向控件继续由原八组回归负责；本文件不将新表单矩阵冒称为那些测试的替代。

早期专组执行记录为 [form-browser.txt](form-browser.txt)，当时的 Python 3.14 环境和阶段源码摘要见 [form-manifest.json](form-manifest.json)。最终集成以 [冻结源码](frozen-source.json) 和 [验收清单](validation-manifest.json) 为准：Python 3.12 下 1024 项及十组浏览器全部通过，包含本组的最终重跑。本次新组在 macOS 的 Playwright Chromium 151.0.7922.34 通过，未运行 WebKit/Firefox；composition 使用浏览器 DOM 生命周期事件，不宣称完成真实操作系统中文输入法人工验收。最初一次启动碰到并行编辑中的 Pydantic validator 名称遮蔽，随后已由账本实现修正；另一次受沙箱 localhost bind 限制，改为已批准的隔离执行后得到本文件记录的完整通过。这些启动失败不冒充功能测试结果。

总览“未实现盈亏不含费用”已改成以剩余成本为准、已计入买入费用不再扣减、未估未来卖出费用；持仓卡说明普通交易会同步现金/持仓/费用，期初费用口径依依据，校正另存差异且不制造成交。研究恢复提示区分已保存部分成果继续研究与完整范围允许的仅汇总，并说明新增费用可能性；没有修改历史报告。
