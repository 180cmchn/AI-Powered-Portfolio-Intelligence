# 第十六轮：预测身份、期间与多事件回归

`app/report_metadata.py` 从已有批次、历史任务绑定和不可变预测记录生成系统标识。报告正文中的“正式”不会决定 kind；未知历史不据正文推断为正式。报告页、概览、预测行与预测详情显示实际类型，报告可展开查看基准日、原截止、期限、目标交易日、实际发布时间和 information_cutoff。该信息为只读投影，不迁移或重写旧正文、预测或 `content_assessment`。

`GET /api/analyses/{id}/export?format=original` 返回数据库 report 文本的严格 UTF-8 字节（包括已有前后空白和 CRLF）；`format=reading` 默认返回系统标识、结构化元数据及原文。文件分别命名 `-original.md`、`-reading.md`，`X-Content-SHA256` 对本文件字节计算，`X-Original-Report-SHA256` 单独给原文哈希。阅读版从未声称与原文文件字节相同。导出不会更改数据库。

主 Agent 和子 Agent 每次请求保留服务端 prediction_context。实际发布时间和信息截止在发布前明确为 null；不能预先宣称已发布。`data_as_of` 单独表示资料/估值时点。发布时间、information_cutoff 和行情时间不混为同一个事实；当前预测 information_cutoff 沿用生产发布时写入的时点，不额外声称每份来源均在该时点采集。

第十五轮的 return_period / moving_average_period 公式与投影保持。工具说明明确 20 个收盘点只有 19 个相邻收益间隔；固定 N 间隔需先选择实际 N+1 个可比收盘日期，再按 start/end 计算并核对实际覆盖。新回归在 Responses 与 Chat Completions 的实际最终请求中同时检查 20点/19间隔与21点/20间隔、精确日期、均线样本、收益值、复权及截断状态。已有计算摘要、子报告交付和压缩回归继续运行，不为命名问题增加 LLM 裁判或重跑整轮。

新增生产多事件夹具 `tests/test_sixteenth_events.py` 使用历史显式时钟、请求事件栅栏、真实 Service.tick/Agent/ModelClient/SQLite：

- A 的原请求暂停时改变全局候选范围，A/B 输入版本同步变化，A 真正因 input_version_updated 取消，A/B 新任务及公共候选研究均合法处理。
- 公共原始材料事件先持久化，再重建 Store/Service；公共研究读取原文，按相关性向 A/B 各产生一份需求。
- A/B 分别读取公共原文；B 尝试读取 A 私有材料被生产权限拒绝。各账户模型观点未串入其他账户或公共研究，公共模型观点未自动变成账户事实。
- 生成报告不产生无限反馈，重复 tick、再次重建 Store/Service 不重复报告、预测或账本变化。
- 全部 11 次受控 HTTP（包括取消的 1 次）与持久事件记录一致。5 个账户任务完成、1 个旧版本任务 superseded、2 个公共任务完成。没有只允许一个任务的 guard。

以上时钟、价格与模型响应均为工程夹具，不是自然前瞻样本、真实供应商表现或长期连续运行结果；重建 Store/Service 验证的是持久恢复，实际子进程启动/重启由本轮独立正常启动矩阵另行记录。

本分支执行证据：

- 初次新增语义、既有期间/时间/计算 HTTP 定向：30 passed。
- 全量首轮暴露新增 prediction_context 占用了 runtime_status 的子任务摘要配额；已改为独立 seed 顶层保留上下文，未扩大预算。随后 fourth_budget + semantics + ninth_calculation：24 passed。
- 最后 events + semantics + fourth_budget 联测：17 passed。这些定向执行相互重叠，不相加为独立用例数；最终全量源码身份和总数以本轮主记录为准。
- 增强 `tests/frontend_fourteenth.mjs` 通过真实受控服务浏览器链：系统标识、未发布目标、两种导出链接、严格原文及各自哈希。首次默认沙箱无法绑定回环随机端口，按既定权限重跑后通过；没有付费模型请求。

新事件测试编写期修正了夹具预期：取消需要接收 CancelledError；全局候选更新会改变账本投影的 input_version，但交易内容不变；模拟重启应复用声明的测试凭据存储（初次未复用触发钥匙串配置错误）；材料 access 存在 JSON body 中。未据这些夹具错误修改生产逻辑或删除业务断言。

真实验证器审阅已修：保留来源显式 max_output_tokens；waiting_config/waiting_scope 与其需求明确记为 blocked/deferred，不改成 failed；仍处理其他合法 pending；每阶段另记同版本唯一报告及契约状态，最终执行状态与内容复核分开；正式未验证原因不硬编码市场时间；退出失败仍保存证据。

后续独立审阅发现新身份再次复制整页 100 个预测目标，会绕过初始上下文的归档并挤掉计算。已改为固定大小的身份、目标页计数和路径指针，不复制目标列表。实际最终请求回归同时发现既有汇总转换重新载入完整 self.context 中的过大批次页；现在只在该页超过现有预算三分之一时归档完整原页，保留原批次身份、基准、截止、预期样本总数与分页位置，并明确 `items_omitted_for_context`。原预期范围和持久样本不变；最终仅允许 finish_analysis 时未读目标保持未覆盖。

小预算下，计算摘要与汇总检查点若包含与当前 seed 完全相同的 model_config，按需通过显式 `model_config_ref=seed.model_config` 复用同请求内的原值；不同配置绝不合并，原始完整记录保留。没有扩大预算或删除关键数值。新增两协议真实出站检查采用100目标页、32位批次ID、64位规则ID与10000字符预算，验证收益、实际收益间隔、观察点、均线期间及模型归属仍在。初始失败记录在 `test-results/sixteenth/prediction-context-initial.txt`，中间失败在 `prediction-context-regression.txt`，修复后的四模块定向为 **76 passed**（`prediction-context-fixed.txt`）。此前全量源码身份不作为这次修复后的完整重跑证据。
