# 第十四轮固定证据与分层验证

本轮找到第十三轮停止后的隔离评估 SQLite，按任务 `ab76e4941a7044119ecce3ee31ba468d` 只读导出已保存可见材料，没有根据公开摘要重造子报告。来源为 `data/evaluations/thirteenth-live-20261101-final8d2767d/evaluation.sqlite3`，不访问日常账户材料。前后数据库文件 SHA-256 相同，见 [来源清单](evidence-package-manifest.json)。

完整证据包存放在 Git 忽略的私有路径 `data/evaluations/fourteenth-fixed-evidence/historical-visible-v1.json`，目录权限 0700、文件权限 0600。包内有原历史账户快照、两份完整可见子报告、21 项成功计算、295 项材料来源索引和 340 条历史材料访问回执。导出使用字段白名单，没有凭据、隐藏推理或供应商内部响应。现金 86484.90 元及三证券数量属于历史模拟账务，不是日常账户和当前市场行情。

最终汇总对照输入是一个明确投影：保留两份报告全文、21 项计算摘要及其计算参数、账户与指标、材料来源索引和未完成市场专题；完整原始计算输出和逐次访问回执留在包内。输入哈希与大小见 [实际输入投影清单](comparison-input-manifest.json)。它不是原第十三轮最终 HTTP 请求的逐字副本。主 Agent 可使用收到的子报告及来源链，不能据此声称本次亲读了全部原文。

`verify_fourteenth_live.py` 的两分支共用同一事实输入、同一提示词和当前账户实际模型/effort/协议；唯一变量是未指定工具选择与明确指定 `finish_analysis`。指定来源账户的角色覆盖由既有 `configure()` 展开到隔离角色默认绑定，再冻结团队；撤销凭据标志也只读复制。请求钩子记录真正发送的请求体哈希、输入哈希以及完整报告/计算摘要可见性；如果初始上下文变成前缀摘要，发送前即停止。总 HTTP 尝试闸包括运输重试，不提高来源配置上限。指定提交缺失、无效、未运行，或源码/环境发生变化，退出码为 2，不允许作为扩大真实研究的通过信号。

用户随后明确授权本轮总计最多 **44 次 HTTP 尝试**：先最多 4 次固定证据最终汇总，只有明确交付契约与实际内容均通过，再最多 40 次执行一次完整隔离账户研究。本文预检写入时真实调用数为 **0**，等待主执行者统一释放；最终真实结果以运行清单为准。第十三轮“一次、最多40次”的授权已经耗尽，没有挪用于本轮。尚未验证当前真实转发端点是否接受/遵守指定函数，也没有新增真实账户结论、更新版本报告、现金场景报告、正式方向预测或自然成熟评分。这些均保持未验证。

已完成的受控检查使用真实 ModelClient 的 httpx 响应夹具，经 Agent → Service → 临时 SQLite → HTTP API。覆盖流程句不发布、完整草稿需要补交、合法短现金报告、有限交付、唯一终止工具、截断参数、缺字段、混批、重复终止、上游忽略/拒绝、期限耗尽、单份子报告、同版本旧报告保护、重复发布与重启，以及大于初始上下文三分之一的固定证据仍完整发送。它们不代表真实模型的内容能力。

浏览器回归通过生产链先发布有限报告，再让同版本新任务只返回流程句。页面和 API 均显示交付未完成、稳定错误码和重试入口；历史有效有限报告继续 current，正文和具体缺口可读。该浏览器组为第十一组，不替代已有十组。

第十二/十三轮真实验证器原 `single_report_acceptance_passed` 的机械部分移至 `mechanical_single_report_acceptance_passed`。新分层结果分别报告传输、明确交付契约、落库发布、内容核对、预测覆盖和自然到期效果。没有真实内容核对时 `accepted=null / not_evaluated`，总体验收为 false。人工核对文件必须绑定实际正文 SHA-256、评审人、时点及账户事实/回答范围/依据与反向因素/条件/缺口五项结论；不是让结构校验自动冒充人工通过。

复核旧记录不花费模型额度：

```bash
.venv/bin/python scripts/verify_fourteenth_live.py \
  --summary data/evaluations/thirteenth-live-20261101-final8d2767d/summary.json
```

本轮已取得上述44次授权，主执行者使用新私有输出目录运行最终汇总对照；`--authorization-note` 只登记已有授权，不自行产生授权。先审阅两份真实可见结果，满足用户设定条件后才能释放完整研究。默认额度为 0；不带正额度、授权说明、来源账户、证据包和新目录时，CLI 在任何连接前拒绝执行。

Mac 操作边界保持不变：不重启日常服务，不写日常 SQLite，不改变 .env、模型绑定或系统睡眠设置；隔离浏览器只监听 127.0.0.1 随机端口。最终源码的全量测试结果由本轮总验收清单记录。

统一闸和具体命令（只由主执行者执行，以下不是已经发生的调用）：

```bash
.venv/bin/python -c "from scripts.verify_fourteenth_budget import SharedAttemptBudget; SharedAttemptBudget.initialize('data/evaluations/fourteenth-20261102/authorized-attempts.sqlite3', maximum=44, final_maximum=4, full_maximum=40)"

.venv/bin/python scripts/verify_fourteenth_live.py \
  --run-real --authorized-http-attempts 4 \
  --authorization-note '用户授权本轮4次最终对照；内容与契约通过后才允许40次完整研究，总计44次' \
  --global-budget data/evaluations/fourteenth-20261102/authorized-attempts.sqlite3 \
  --source data/live.sqlite3 --reference-account 96b91df38e854008b989206c63808c3a \
  --evidence data/evaluations/fourteenth-fixed-evidence/historical-visible-v1.json \
  --output data/evaluations/fourteenth-20261102/final
```

对照结果的 `named_finish_analysis.tool_calls[0].arguments.report` 是待人工核对的真实可见正文；不是用结构字段自动评判。主执行者阅读正文后，把 SHA-256、reviewer、reviewed_at 和五项逐一核对写入 `manual-content-review.json`。任一失败或未知不得运行下步：

```bash
.venv/bin/python scripts/verify_fourteenth_research.py \
  --run-real --authorization-note '用户授权条件通过后一次完整模拟账户研究，最多40次，总计44次' \
  --global-budget data/evaluations/fourteenth-20261102/authorized-attempts.sqlite3 \
  --source data/live.sqlite3 --reference-account 96b91df38e854008b989206c63808c3a \
  --approved-final-summary data/evaluations/fourteenth-20261102/final/summary.json \
  --content-review data/evaluations/fourteenth-20261102/manual-content-review.json \
  --output data/evaluations/fourteenth-20261102/full
```

同一个 SQLite 事务同时检查44总额和4/40分阶段上限并插入尝试，所有进程共用一个文件；重启不能重建或清空闸。授权额度预留发生在 HTTP 前，若预留后异常而未留下可见请求，差异保留，不能认作零费用再花一次。完整阶段只允许一次业务执行，已有尝试会拒绝新完整运行。准备时复制的角色仅在目标库按生产精确旧内置提示词哈希迁移，自定义提示词保持；不会把来源日常库迁移或覆盖。

最终执行结果已更新：4次汇总额度全部使用，0有效终止提交；完整研究条件未满足，40次未用。详见 [真实结果](live-content-review.md)。正文核对支持明确标注评审者的人工检查或基于实际内容的开发证据检查（evidence_content_review），二者均不可由结构校验代替，也不能冒称人类签字。本次未产生任何通过的内容核对。

受控完整页面截图：[有限现金报告与具体缺口](delivery-controlled.png)。截图中的账户、现金及报告均为测试夹具，不代表真实模型已交付账户判断。
