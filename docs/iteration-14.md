# 观澜第十四轮：明确提交账户判断

追加排查：用户随后提供供应商 `client_gone` / 流结束记录，已继续修补HTTP收尾与诊断分层，详见 [传输排查](fourteenth/transport-followup.md)。下列最终源码快照与测试数是该追加补丁前的交付记录；新增补丁的源码、测试和授权结果另记，不覆盖旧证据。

**最终结论：工程回归通过（1146项Python、十一组浏览器）；真实汇总4次未取得有效终止提交，实际账户判断交付未通过。完整研究按授权条件未启动，40次额度未用。** 详见 [真实内容与交付核对](fourteenth/live-content-review.md) 和 [验收清单](fourteenth/validation-manifest.json)。

本轮从干净的 `ee770061f40b092e4cb50a8b191530576d29ab36` 实施，开发与隔离验收期间没有回退基线、推送或重启日常服务；用户随后单独授权提交到main。原基线 [Actions 41323887407](https://github.com/180cmchn/AI-Powered-Portfolio-Intelligence/actions/runs/41323887407) 已只读核对：Python 1024 项、十组浏览器及汇总成功。新增代码的本地结果与真实端点结果另记，不能冒充该远端提交已包含本轮修复。

## 交付边界

旧 `ResearchRuntime._loop()` 把非空普通文本直接返回为报告，`Store.publish()` 又以正文非空允许发布，导致流程句也能令任务 completed、报告 current。当前普通文本只保存候选事件及可见草稿；有预算则要求原 Agent 基于现有事实补交，缺提交或契约无效则落库 failed，附 `final_submission_missing` / `delivery_contract_failed`。HTTP/SSE 的完整终止单独记录。

子 Agent 只用 `submit_report` 交专题；主 Agent 只用 `finish_analysis` 交账户/公共最终结果。提交轮必须唯一且不混本地研究工具。少量字段为 report、decision_summary、scope、completion(full/limited)、limitations；limited 必须说明具体缺口。没有最少字数、新闻数量、关键词黑名单、买卖方向、审核角色或信心门槛。合法短现金报告可提交；结构验证不等于实质内容验收，默认 `content_assessment=not_evaluated`。

服务端冻结范围并同时供 Agent 纠错及 Store 事务校验；账号、输入版本、候选版本、团队与提交身份由服务端绑定。建议只处理实际 scope，需求只完成显式覆盖项。报告、结构化预测、建议、需求、任务完成在原事务内发布，重复发布不重复产生记录。公共报告材料也进入同一事务。历史文字报告仍可读，不批量判坏、不改原文。

同版本正常刷新/重试失败保持有效旧报告；新版本、明确事实更新、配置/范围变化或时效失效仍使旧报告失效，不恢复已经 stale 的结果。报告发布与预测方向、弃权、未覆盖、待数据、超过截止分别显示。

## 协议、预算和提示词

`ModelClient.complete(tool_choice='finish_analysis')` 分别映射 Responses 的 `{type:function,name}` 和 Chat 的 `{type:function,function:{name}}`。研究阶段仍自主用工具；最终阶段明确指定函数并关闭原生搜索。保持实际协议、模型、五档 effort 和输出上限，不全局启用 strict。端点明确拒绝以 `tool_choice_unsupported` 失败，不删参重试。强制选择被忽略、无效参数、混批、完整响应中的未完成函数项、明确 length/incomplete 只有一次有界纠正；缺终止事件/未知结果不重放半截调用。

每次实际 HTTP 尝试（含运输重试）在同一并发锁内扣减；子任务不能占用主 Agent 默认最后 2 次容量，可配置 2–4，总上限不增。两次容量用于首次提交及一次恢复/纠正，不保证叠加多次运输重试后还有第三次语义修正。收尾等待仍按在途子任务和整轮最早期限计算，并在原整轮内预留最终时间；不退回生产路径固定30秒等待，不等待无关任务耗尽最终窗口。首次有效提交即停止。

正式预测批次的实际关联期限也进入执行限时，包括补充任务与样本范围关联。已过截止的执行不发HTTP；在途请求到期取消并记录 `forecast_deadline_exceeded`，未交付样本记为 missed，不能补填。普通账户及历史 exploratory 不误用该正式期限。

最终上下文保留成功计算摘要、收到的完整子报告、候选草稿和具体错误反馈。主提示词从旧多段重复流程收敛为围绕本轮判断的说明，强调事实支持、反向因素、条件与局部缺口；未知成本/可卖数量/未来收盘不再被解释为全面阻断。精确旧内置哈希才自动迁移；自定义提示词提供差异与显式载入草稿，保持模型、effort、凭据和冻结团队。

协议格式参照 [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling)；[xAI function calling](https://docs.x.ai/developers/tools/function-calling) 的文档不能证明兼容转发端点实际遵守。真实请求观察另列。

## 验证与真实运行

[固定证据与验证方法](fourteenth/fixed-evidence-and-validation.md) 区分真实历史证据、受控夹具与本轮真实请求。第十三轮隔离记录只读提取了两份完整子报告、21项成功计算；输入按原历史时点使用，不是原最终HTTP的逐字重放，不包装为当日实盘。

新增回归通过真实 ModelClient HTTP 夹具、Agent、Service、临时SQLite、API和浏览器证明拒绝、纠正、发布与旧报告保护。已有回归未删除；原成功fixture改为显式终止提交，旧纯文本兼容成功期望改为候选及补交；测试中的显式模拟作者不是生产后门。新增第十一组浏览器纳入CI矩阵。

四次真实请求使用源码 `e8023741df26543df93a4595cd678ea92b575f6c690655aa9244daaf7e3da3b9`，两组运行期间均未改变，原冻结快照保存在 [frozen-source.json](fourteenth/frozen-source.json)。真实请求完成后，复核又补上正式预测执行截止，以及首次有效提交即结束、迟到成果单独记录的边界。这些最后补丁只有受控回归，没有声称已通过真实端点验证；最终源码身份另存 [final-source.json](fourteenth/final-source.json)。[验收清单](fourteenth/validation-manifest.json)保留各次结果和中间失败。

完整正文未取得，内容状态为未评估。本轮用户已明确授权最多44次HTTP：固定证据阶段最多4次，只有指定终止提交和实际内容核对通过，才继续一次最多40次完整模拟账户研究；所有重试与子任务都计数，不用成功后重跑覆盖失败尝试。固定阶段已用完4次，完整阶段40次未解锁，不转作更多探测。

工程、协议遵守、事务发布、内容判断、预测覆盖及自然到期效果是不同结论。没有自然成熟样本不能宣称准确率或收益有效，单次研究也不能证明无人值守。本地Mac关机或睡眠时不运行；未添加登录项、云部署或外部通知。


## 八项交付问题的回答

1. 流程句曾经穿过Agent纯文本返回和Store非空校验；当前在Agent无终止工具路径保留为候选，在Store明确契约与身份校验再次阻止发布。
2. 进度/候选不终结；submit_report仅交专题；finish_analysis交主报告，full/limited配合实际scope和limitations；界面另显示历史版本/失效，不等同内容已核验。
3. 本轮第2/4次真实请求确实发送指定finish_analysis对象，但均流中断，遵守未验证。两协议本地映射与拒绝/忽略路径回归通过。
4. 主Agent的2次HTTP容量在共享锁内保护，子任务和重试均不能越总上限；首次成功即结束。时间保留也在原期限内，传输重试可消耗这段容量，不承诺无限纠错。
5. 固定证据两对真实对照共4次：3次流中断、1次明确incomplete，无有效提交。完整生产Agent流程的受控回归通过；付费完整研究因前置门槛未满足未启动。
6. 没有本轮真实模型完成的账户判断可以展示；只可查看历史证据事实与明确标为夹具的短现金报告。核心账户、新账务版本、候选/现金-only真实场景均未验证，不用开发者代写结论补齐。
7. 报告/预测/弃权/未覆盖事务及多目标边界受控验证通过；本轮真实预测与弃权0、自然到期评分0，准确率未知。
8. 请求始终max；第3次服务回报high及output_tokens9823>请求4000，其余回报未知。保留原值，不降档、不解释隐藏计算或换算ticks金额。
