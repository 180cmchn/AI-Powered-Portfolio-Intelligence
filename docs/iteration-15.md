# 观澜第十五轮：提交前反馈与真实账户汇总

本页及机器证据在提交前冻结；其中“未commit/push”和远端旧CI失败均描述各记录所标时间的状态。后续提交与CI结果以对应Git提交和GitHub Actions为准，不回写历史验证快照。

**最终最小业务矩阵（11月3日）：初始A、实际卖出后的A、独立现金B三阶段均已取得真实有限报告，通过独立内容复核及发表后/真实重启检查。** 各阶段采用自身执行时的源码，不是统一在最终源码重跑；失败、纠正、旧内容拒绝均保留。当前`728f3fd75aa864696227b4b37b5a2ebc816c403859ccc578189780a309ea8dcf`仅改隔离helper/测试，生产app与7f8的Mac/Linux各1500项及十一组浏览器逐项相同，两端相关各204项通过。

| 已接受阶段 | 实际源码 | 模型HTTP终态 | 预测分母 |
| --- | --- | --- | --- |
| [初始A](fifteenth/business-long-task-a-content-review.md) | be9223ff… | 39：38 completed+EOF、1明确incomplete+EOF，纠正包含在38中 | 9探索：2 down、7 abstain；正式0 |
| [卖出A](fifteenth/business-output-omission-a-sell-direct-content-review.md) | 728f3fd7… | 29/29 completed+EOF | 6探索：全部abstain；正式0 |
| [现金B](fifteenth/business-output-omission-b-content-review.md) | 728f3fd7… | 11/11 completed+EOF | 3探索：全部abstain；正式0 |

这是三次各自有界的真实验收，不是连续无人值守稳定或方向效果证明。自然1/5/20日成熟未验证；不快进、不回填，也不要求为本次收尾空等20个交易日。 独立accepted来自开发期内容复核，应用数据库的content_assessment/not_evaluated保持原值，不伪造生产审核回执。

A 初始 v4 于 01:31:50 发表 2462 字符有限账户报告，提交契约、机械检查、发表后观察和真实重启检查通过。39 次模型 HTTP 中，38 次 completed+EOF，1 次在 379.777 秒明确返回 max_output_tokens incomplete+EOF；随后一次契约纠正耗时 258.840 秒，完整完成并发布，包含在上述 38 次中。本次无本地 timeout 或 503。3 项建议全部应用，9 项预测全为 exploratory（2 down、7 abstain），正式为 0、成熟准确率未验收。[独立内容复核](fifteenth/business-long-task-a-content-review.md)保留两处真实非阻塞错误：正文误称“正式预测”、一个财报页范围引用不精确；相应财务事实有主 Agent 实际已读材料支持，原文和应用内 not_evaluated 均不改写。

A 卖出后 v1 于 01:47:05 终止：28 次其他 HTTP 均完整完成，两个子任务分别在 144.099 秒、198.289 秒触发 stream_idle_timeout，均为连续 120 秒无有效进展。旧隔离验证器还把任一子任务未知结果扩大为整个任务的停止锁，误阻经理最后汇总；已将停止范围修正为经理自身未知结果停止当前任务，子任务半截响应不重放，经理可以使用已完成的生产成果独立汇总有限报告。A 卖出后 v2 随后实际执行，经理在约 414 秒传输中断，非本地取消；失败前保留两份完整子报告和 65 项完成计算。原失败和停止锁未改写。

随后在独立克隆库通过真实 POST retry，将失败父任务 `141ed002…` 续为新任务 `78f3d56e…`，账户保持 v5、未重复卖出，生产 Service 选择 continue_research。[该次终态](../data/evaluations/fifteenth-business-a-sell-continue-v1/execution/summary.json)共 12 次 HTTP：11 次 completed+EOF，1 次明确 max_output_tokens incomplete+EOF；一次预算内纠正后发表 2772 字符报告。提交、机械发布、两次发表后及两次真实进程重启后观察通过，旧 v4 正文与九条预测保持。不过[卖出内容复核](fifteenth/business-long-task-a-sell-content-review.md)为 accepted=false：三证券 2026-08-03 至 2026-11-02 的 60 个收盘样本、59 个收益间隔被多次称为“20 日收益”，错误还进入预测理由。`window=20` 原本只控制均线；原报告和预测不改写，需新的真实报告通过内容复核才能继续 B。

省略输出上限后的[首次执行](fifteenth/business-output-omission-a-sell-content-review.md)于 02:56 启动、03:10:36 停止，共 35 次 HTTP：34 次 completed+EOF，最终汇总在 357.740 秒出现 RemoteProtocolError，HTTP 200 后没有终止事件/EOF，cancelled_by=null。[随后正常续研](fifteenth/business-output-omission-a-sell-resume-content-review.md)由 Service 选择 continue_research，03:12:54 至 03:16:40 共 3 次 HTTP：2 次 completed+EOF，最后一次 156.950 秒断流，同样无本地取消或终止/EOF。两次都保持未知结果停止，均无新报告、建议或预测，内容 accepted=null/not_evaluated，不能写为内容失败；原账户账本、初始报告与九条预测保留。

[当前 direct 执行](../data/evaluations/fifteenth-business-output-omission-a-sell-direct-v1/execution/summary.json)只对隔离 ModelClient 指定直接传输，保留 TLS/环境证书、原供应商/模型/effort/schema、输出省略和所有期限；行情/PDF路线、系统代理和日常设置不变。它不是原请求体的原样重放：正常行情刷新令 checkpoint guard 不等，实际 fresh 研究。上述两次断流及[后台/本地观测差异](fifteenth/output-omission-transport-correlation.json)不证明某个代理或节点是唯一根因。[该次终态](../data/evaluations/fifteenth-business-output-omission-a-sell-direct-v1/execution/summary.json)已记录29/29 completed+EOF、契约与机械发表、四次发表后/真实重启观察通过，源码/环境前后保持；报告3924字符，SHA256 `be5881cc5c72e9934a44d7c2b7f95c71bcb1d2b9cc7f6f27402860d0cb9f33fa`。最后请求255.729秒，供应商回报output=21230、reasoning=14243，差值6987高于旧6000；这只是同供应商回报字段的算术比较，不保证通用token语义或第三方省略后的容量。[独立内容复核](fifteenth/business-output-omission-a-sell-direct-content-review.json)已接受这份有限账户判断：核心收益期间准确，六项均为exploratory弃权，正式为0。保留非阻塞问题：正文称“正式”不符合系统kind，一处“约两个月”应约三个月，独立Markdown的来源导航不如结构化结果精确。原文不修补；内容接受不代表方向效果或自然成熟评分。

[B现金候选阶段](../data/evaluations/fifteenth-business-output-omission-b-v1/execution/summary.json)于03:42:00.107至03:51:11.346完成，使用同728源码、输出省略、隔离direct及manager xhigh，独立100000元现金账户、候选含159278。11/11 HTTP均completed+EOF、cap省略和direct核验通过；契约/机械发布及四次发表后/真实进程重启观察通过，source/env保持。报告SHA256 `195ad2c191d4f9bf095befe584741ef25d05c7214f8d77186d7d9c9ecd8c7a8f`；末请求342856.76ms（约5分43秒），回报output31650/reasoning17576。[独立内容复核accepted=true](fifteenth/business-output-omission-b-content-review.md)：2606字符有限持币/观察判断，1项watch、3项探索性弃权、正式0。B保持现金100000、零持仓、零交易；159278原文实际只读第1–3页，未借用A结论，A的12份私有材料对B拒绝访问。

本阶段产生两项额外任务，不能报为零：实际PUT universe使A输入版本变为v6，universe_changed任务queued、attempts0；global candidate_scan因本隔离单任务guard被拒绝，failed、attempts1。终态中两项模型请求均为0，B本身11次之外没有额外model HTTP。[独立复核](fifteenth/business-output-omission-b-content-review.json)已确认这些触发：不是重复卖出或自报告循环。A账本全部行、15条预测原样，两个旧A报告仅因范围版本变化合法stale，正文/结果/身份不改；不能把单任务guard拒绝等同于验证了合法新事件的完整连续处理。

B正文“近20个交易日”对应2026-09-29至11-02的20个收盘观察、19个收益间隔，较长区间为08-03至11-02的60个收盘、59个间隔；明确日期及数值与保存输入相符。复核把期间命名精度列为非阻塞观察，接受限定于实际日期，不将其改写为严格20/60间隔，也不修改原报告。

[最终机器汇总](fifteenth/output-omission-closeout.json)独立连接三份报告哈希与review身份。全部省略验证四轮共78客户端HTTP＝76 completed+EOF＋2传输未知；最后direct卖出29＋B11为40/40完成。完整长任务十次尝试共222客户端HTTP、220供应商关联行；本日225行还包含此前5行，不能把这些不同分母互换。请求并非相同payload对照，成功不能唯一归因direct或省略cap。

direct A的29条供应商关联行$0.379542，B的11条$0.136138；最终长任务已知费用$2.633988，连此前历史已知合计$3.132086。本日后台225行显示$2.7184、逐行合计$2.718394；日范围与跨日历史累计不同，供应商行数不冒充客户端HTTP。全部尝试及未知边界见[长任务费用记录](fifteenth/long-task-validation.json)，关联金额不是发票级核销。

[最终保全](fifteenth/output-omission-final-preservation.json)于03:52:51核对日常DB/WAL及.env未变，8765/18765均无监听，217冻结源文件一致，git diff --check通过，未commit/push。03:53:06的[远端检查](fifteenth/long-task-remote-ci.json)仍是main654065e及Actions41413931460 failure；本地绿灯不重标远端。

本次修复在 `market_indicators` 及摘要中明确 `return_period` 与 `moving_average_period`，保持计算公式。初次期间回归的 [4 项失败](../test-results/fifteenth/indicator-periods-targeted.txt)与压缩后 [2 项失败](../test-results/fifteenth/indicator-periods-targeted-after-compact.txt)完整保留：额外重复元数据挤占了原可见内容预算。最终缩短重复 methods 描述，保留原全文，未扩大截断阈值；[47 项定向](../test-results/fifteenth/indicator-periods-targeted-final.txt)通过。这是工程修复，不替代新真实报告验收。

按用户最新要求，之后请求采用省略输出上限，取代未执行的 16000 提议。新默认 `max_output_tokens=None`；设置页留空并保存以显式 null 表示省略，出站请求不含 `max_output_tokens`、`max_completion_tokens`、`max_tokens`。旧显式数值和在途快照不自动迁移，日常配置未修改。官方协议允许省略，不保证第三方供应商默认容量。[输出设置相关 84 项及真实设置页回读](../test-results/fifteenth/optional-output-settings-validation.json)通过；各组定向数量不累加为唯一覆盖。详见[输出变更记录](fifteenth/output-limit-followup.json)和 [Mac 启用方式](mac-operations.md)。

默认首有效事件 90 秒、流空闲 300 秒、普通/汇总硬限各 900 秒、子任务 1800 秒、整轮 3600 秒，正式截止仍取更早边界。完整旧 factory v1/v2 迁移至 idle=300；已显式保存的 revision3 idle=120、自定义设置及在途快照保留，不回写旧评估库。[子任务收尾相关 82 项](../test-results/fifteenth/child-closeout-regression.txt)及 [超时相关 83 项](../test-results/fifteenth/long-idle-timeouts-targeted.json)已通过，后者包括真实 ModelClient 在受控时钟下经过 180 秒空闲仍完成、300 秒到期取消和旧 120 秒设置保持。此前 `7fd2cb7e…` 的 [Mac 全套](../test-results/fifteenth/long-idle-macos-full.json)与 [Linux 全套](../test-results/fifteenth/long-idle-linux-full.json)各 **1438 项通过**，[十一组浏览器](../test-results/fifteenth/long-idle-browser-full.json)全部通过；卖出续研源码 `7c859112…` 当时只改 business helper/测试，生产 app 与浏览器逐项相同，[Mac](../test-results/fifteenth/business-sale-retry-macos.txt)及 [Linux](../test-results/fifteenth/business-sale-retry-linux.json)各 **118 项定向通过**。这些保留原执行身份。当前 [7f8 冻结清单](../test-results/fifteenth/output-omission-v2-frozen-source.json)已包含生产 app 改动，另行执行的 [Mac 全套](../test-results/fifteenth/output-omission-v2-macos-full.json)为 **1500 项/197.85 秒**，[Linux 全套](../test-results/fifteenth/output-omission-v2-linux-full-with-space.json)为 **1500 项/146.75 秒**，[十一组浏览器](../test-results/fifteenth/output-omission-v2-browser-full.json)全过、耗时 287.03 秒；源文件与环境身份检查保持。Linux [首轮失败](../test-results/fifteenth/output-omission-v2-linux-full.json)为 /tmp 512 MB 耗尽（848 passed、262 failed、390 errors），记录未删除；仅将临时空间改为 4 GB，以同一源码重跑，没有跳过或放宽测试。

当前 helper 的 [冻结身份](../test-results/fifteenth/business-model-route-frozen-source.json)与 7f8 比较，仅 `scripts/verify_fifteenth_business.py` 和对应测试改变；[Mac 204 项/69.14 秒](../test-results/fifteenth/business-model-route-macos.txt)、[Linux 204 项/50.64 秒](../test-results/fifteenth/business-model-route-linux.json)通过。1500 项及十一组浏览器仍保留 7f8 的实际执行身份，通过相同生产文件映射到本次 helper，不声称在 728 重跑全套。

此前实际 A v4 源码 `be9223ff…` 的生产 app 与 `aff76f94…` 的 Mac/Linux 各 **1416 项**、**十一组浏览器**全套逐文件相同；对应 helper 的 Mac 57 项、Linux 151 项和中间 94 项结果保留原身份，不用它们替代新源码全套。主 Agent 支持独立专题同轮并行，汇总仅去掉完全重复的账户指标；隔离验证器保留配置中的 `model_retries`，缺省为 1，明确 503 有限重试，结果未知的中断响应不重放。

实际终态、源身份、失败分母和费用见 [长任务验证记录](fifteenth/long-task-validation.json)。此前 v2 的 25 次客户端 HTTP、v3 的 11 次及其 503 失败均保留；初始 A v2/v3/v4 后台关联小计 **$0.742288**，其中 v4 为 **$0.456382**。这是该三次初始 A 的范围，不包含后续卖出阶段，也不是发票级核销，不把供应商行数直接当客户端请求数。

[最新用户指示及验证边界](fifteenth/long-task-followup-user-instruction.json)：继续使用已配置供应商，不再逐次询问人工次数授权，每任务 40 次工程上限继续生效。以下失败、额度与旧审批记录保留为当时证据，不再作为本次验证授权门槛。

**历史恢复结果（00:28）：11 月 3 日 00:28 的单次恢复已获授权，但在发送前停止，实际模型 HTTP 为 0。** 正常行情刷新因东财连接错误转到新浪，三证券来源与数据时间改变，检查点的 research_inputs_changed 校验阻止继续汇总。无报告、无额外模型扣次；当时 A 为 **15/16**、余 **1 次保留**。此后检查点于 00:31:40 按原期限到期，授权仅限有效期内，未清除停止锁或再次执行。本次零请求失败不能解释前次 290 秒模型断流，也不宣称修复了该传输问题。

见 [本次零请求证据](fifteenth/business-a-single-recovery-live-evidence.json)、[明确授权](fifteenth/business-a-single-recovery-authorization.json)与 [检查点到期](fifteenth/business-a-single-recovery-expired.json)。

**此前付费执行：11 月 3 日全新隔离 A 用了保留 6 次中的 5 次，在最终汇总断流后停止，仍未发表。** 前四次均 completed+EOF；第五次在 290.123 秒出现 RemoteProtocolError/incomplete_response_body，无终止事件、EOF、usage 或最终提交，cancelled_by=null。该次汇总硬限为 360 秒，不能归因于本地 300/360 秒硬限。原 A 的 10 次加本次 5 次合计 **15/16**，当时余 **1 次保留且未授权自动继续**。供应商本次显示 **$0.084406**；最后一条后台在客户端断流约 23 秒后结束且没有可见红色流错误标记，这不证明客户端获得结果或确定断开节点。全部已关联费用合计 **$0.498098**，未作发票级核销。

见 [本次实际证据](fifteenth/business-a-fresh-v2-live-evidence.json)、[费用关联](fifteenth/business-a-fresh-v2-supplier-reconciliation.json)和 [内容状态](fifteenth/business-a-fresh-v2-content-review.json)。本次没有实际正文，内容保持 not_evaluated；未进入成功发表后的重启观察，A 卖出/B 真实阶段未启动。

本轮从干净的 `654065e8676e39fd6eba73f84d16155dcbe1ecab` 直接实施。没有回退、提交或推送，没有重启日常 8765，也没有修改日常 `.env`、模型矩阵、系统代理、证书或账户；明确获准的 effort、输出及直连变化仅作用于隔离验证。新增 JSON 回执不需要数据库迁移，schema 仍为 11，API contract 仍为 13。最终源码、工程与真实请求结果分别保存在本轮证据目录，不覆盖第十四轮记录。

**此前阶段记录（11 月 2 日首次 A）：正常 A 首次验证已执行但失败停止；历史 v13 有限报告仍为独立内容复核 pass、accepted=true。** 最后一次隔离直连请求于 21:58:53 完成，耗时 292862.27ms，收到有效 finish_analysis、response.completed 和 HTTP EOF，经预检与事务首次发布一份 5109 字符报告。正文 SHA-256 为 `76d4741121f24743648055860d8d7a63e4d502987ec86c36912d6b7a881d6c60`。见 [原文](../data/evaluations/fifteenth-historical-xhigh-v13-direct/execution/historical-account-report.md)、[发表审计](fifteenth/xhigh-v13-publication-check.json)、[独立内容复核](fifteenth/xhigh-v13-content-review.md)。两项非阻塞措辞观察保留，应用内 not_evaluated 不改写为开发期审阅结论。

用户随后独立授权正常 A 首次最多 16 次；22:13:53—22:23:43 实际通过记账 API 完成三笔模拟买入，经原 Service/Agent/ModelClient 发出 **10 次经理请求、0 次子 Agent 请求**。前九次均 completed + HTTP EOF；第十次仍在研究阶段，300002.89ms 触发本地 `request_hard_timeout`，末次文本进展为 299993.09ms，最大空闲只有 7038.44ms。无终态、EOF、usage 或有效 finish_analysis，未发表报告；成功发表后观察和服务重启验收均未执行。见 [实际执行证据](fifteenth/business-a-live-evidence.json)。

正常 A 已按“未知结果或失败即停”停止，独立额度 **10/16 已用、当时余 6 次保留且未授权继续**，未执行恢复。检查点随后按原期限自然过期，不能继续把先前 eligible 诊断当作当前恢复资格。历史汇总额度另计，**10/10 用尽**；旧 40 次仍为 0。A 卖出后版本变化与 B 现金候选阶段已完成离线实现、尚未实际执行，整个矩阵和目标未完成。本次有明确本地取消记录，证明 300 秒硬限切断活跃流；它不反向解释旧 v5 的 client_gone，也不证明代理是唯一根因。

本次历史 v13 及正常 A 首次执行源码 `851a647e…` 相对 `75b0a784…` 只改变隔离验证器及对应测试，生产 app 相同。Mac/Linux 全套各 **1311 项通过**；十一组浏览器的被测 app 文件与该实际执行阶段相同；最新 direct/context/business 两端定向各 **91 项通过**。身份和边界见 [工程收尾](fifteenth/engineering-closeout.md)。历史 v13 供应商显示 $0.029402，新十次历史汇总合计 **$0.213052**、xhigh 七次 $0.163564，旧六次加新十次 **$0.289246**。正常 A 十次另计 **$0.124446**，已关联全部记录合计 **$0.413692**；属于后台记录关联，未作发票级核销。第十次客户端 usage 仍未知，不能用后台数值补成 API 已回报。见 [A 费用关联](fifteenth/business-a-supplier-reconciliation.json)。完整沿革见 [最新调查与历史记录](fifteenth/xhigh-investigation.md)。

## 正常 A 首次失败与恢复边界

三笔模拟买入后的现金 86484.90、总成本 13515.10、费用 15.10 均与预期一致，账户版本为 v4。前九个完整响应共完成 51 个函数调用及 11 个原生 web_search 调用；合计 API output_tokens 为 16127，其中 reasoning_tokens 为 13410。第十次有 193 个 reasoning_summary_text.delta 和 464 个 output_text.delta，首个文本事件到 295739.64ms 才出现，没有函数参数事件；429834 是收到的 SSE 字节数，不能当作正文长度或 token 数。请求硬限从开始固定计算，活跃进展只刷新 idle；启动时父分析预算尚余约 910.665 秒。本次硬限按配置生效，未发现计时实现错误，不应以延长全局期限修复。

失败后仅保存已完成工具成果：22 项计算（19 项 ok、3 项 insufficient_data），51 个材料 ID，覆盖状态为 4 个 covered、47 个 index_only；没有子报告，也没有保存未终止流为完成成果。检查点记录 partial_research=true、synthesis_ready、resume_count=0，是可复用候选，不是已通过的完整研究。原拟最小恢复路径是在新明确授权后，经生产 POST retry → Service 重新核对账户版本、材料、事件、行情及时效，再以 synthesis_only 做有缺口的有限汇总；不重放原未知请求、不清除原阶段停止锁、不转用历史额度。检查点有 1800 秒时效及最多两次恢复限制，授权或旧诊断都不能跳过实时重检。最新恢复验证器已完成离线回归，但本检查点现在已过期，该路径没有实际执行。

前次恢复验证器工程冻结为 `803b3a64a2a65b3aa7f23020eac92b45e9ba849ccbaa94065e906ccf8ae64395`，相对实际执行 `851a647e…` 只新增 `scripts/verify_fifteenth_recovery.py` 和 `tests/test_fifteenth_recovery.py`，生产 app 与此前文件相同。它保留旧 unknown 停止锁，经稳定父任务身份的一次性原子认领限制新最多两次，禁止复制准备包刷新额度；真实生产重试、Service 选择 synthesis_only、完整最终 schema、发送前配置/父身份/时效复核、在途取消及最终落盘均由离线夹具覆盖。关键 16 项用例 9.85 秒通过；相关四模块 Mac **62 项/14.81 秒**、隔离 Linux **62 项/11.7 秒**通过，Linux 时间含容器运行开销。见 [恢复回归](fifteenth/business-a-recovery-regression.json)和 [Linux 身份与结果](../test-results/fifteenth/business-recovery-linux-targeted.json)。此前 1311 项全套、十一组浏览器及 91 项定向的执行身份保留，不改标为 803b 重跑。

真实检查点创建于 **22:23:43.746431**，原时效 1800 秒。离线 prepare 于 **22:53:03.409640** 完成，当时仍可用；随后在 **22:53:43.746431 自然到期**。22:58:01 只读检查为 eligible=false、reasons=[expired]，resume_count 仍为 0。创建时间及期限未修改，没有请求或获得恢复授权，没有新模型 HTTP、execution 目录或共享认领；暂不发起新的付费执行或审批。见 [到期证据](fifteenth/business-a-recovery-expired.json)。这只证明离线恢复工程和拒绝过期的边界，正常 A 报告、发表后重启及完整矩阵仍未通过。

后台第十行显示 22:23:43、29670/10612 token、$0.018452、首字 3.8 秒、耗时 4分58秒及红色标记；具体错误文字未打开，不能仅凭颜色称为 client_gone。前三笔账本和失败材料保留，日常 DB/WAL、`.env` 与服务状态的独立检查见 [A 保全](fifteenth/business-a-preservation-after.json)。

## CI 与期限边界

开始时重新读取 GitHub 最新提交与 Actions。远端仍为 [41413931460](https://github.com/180cmchn/AI-Powered-Portfolio-Intelligence/actions/runs/41413931460)：Python 1164 passed、1 failed、1 warning；十一组浏览器 success；强制汇总 failure。失败是 analysis 分支在 40 毫秒预算内尚未进入 HTTP，实际 `sent=0`。产品以 `analysis_budget_exhausted` 阻止发布正确；旧测试没有进入它声称验证的在途阶段。本地旧通过不能替代这份远端失败。

`tests/test_fourteenth_deadlines.py` 使用同一受控时间线驱动应用 wall clock、`time.monotonic` 和 asyncio loop clock。SQLite、客户端准备期间不消耗虚拟时间；事件栅栏确认所处阶段后，显式推进最早期限。formal/analysis/request 分别覆盖请求前零 HTTP、已进入 handler 后恰好一次请求被取消、正常完成恰好一次请求及发布。期限仍由实际 asyncio 定时器执行；没有只改 datetime、放宽生产限制或把请求数改为可选 0/1。材料保留、禁止晚填预测、正式目标 missed 及过期工作不重领继续断言。Mac 与 Linux/Python 3.12 的重复结果分开计数，初始打包/权限失败保留在验收记录。

## 一个校验规则源，两个执行时点

`ResearchTools.preflight_delivery()` 在 `finish_analysis` 返回 accepted 之前调用 `Store.preflight_delivery()`，后者向 `validate_final_result()` 提供冻结范围、当前账户有效建议索引及绑定预测目标。Store 发布在原事务中再次调用同一规则；公共研究也复用此入口。预检只读，不发布、写建议、预测或完成需求，不联网、不调用额外模型。

静态错误精确反馈字段路径：建议缺规范证券身份、缺非空 basis、越 scope、外账户或其他证券的建议 ID、重复/冲突动作、无批次预测、错误期限、重复预测等，由同一主 Agent 在既有一次纠正容量内补交。`account` 只表示冻结账户范围，不允许任意证券。hold、watch、有理由弃权和明确 limited 均保留；basis 不评分、不要求固定字数或来源数。

事务仍检查账户版本、最新任务及运行状态，并重新检查同一关联批次的正式截止。预检后建议失效属于状态冲突；预检后截止属于 `forecast_deadline_exceeded`。冲突候选与逐项未应用原因保留，过期结果不能变为当前建议。报告、建议、预测与需求仍在同一事务发布；重复终止、重复发布及重启不重复产生结果，已发布预测不改写。

`delivery_application` 对每一项提交记录状态及原因。合法方向缺基准属于 `waiting_data`，自然未研究目标仍 `uncovered`，不会强迫填方向或把它们判成协议错误。建议不再截取前 100 条或跳过无效对象后声称成功；过量或无效新提交须纠正。任务与报告页面显示提交数、已应用数及逐项原因。历史无效记录仍可读；新校验不批量改写历史数据。生产 ModelClient HTTP 夹具、Agent、Service、SQLite、API及浏览器均覆盖这些路径，夹具正文不算真实内容验收。

## 从短探测到三证券汇总

第十四轮原四次长证据请求为 3 次终止前断流、1 次 incomplete，零有效最终账户交付；第 3 次旧 incomplete 原因仍未知。传输补丁后两次相同短输入探测均收到有效工具、completed 和 HTTP EOF，仅针对无持仓的模拟现金 100 元。两类证据不能合并为三证券成功率。供应商截图六次显示合计 `$0.076194`，其中短探测 `$0.001274`；只是显示金额，未作独立账单核销或 ticks 换算。

本轮找到了私有历史原文包，不用摘要重造子报告。原 64,356 字符中计算记录占 36,329，含重复模型配置及口径说明。现将完全相同的元数据抽为请求内共享表，输入降至 42,584 字符（约减少 33.8%）。两份子报告逐字不变，21 项计算 summary 可无损还原、参数不变，40 项引用索引、账户/metrics、反向因素与缺口不变。单项仍有值、单位、输入、来源任务及明确上下文引用。裸金额算式缺证券/期间/来源时仍只是算术，不升级为财报事实。

资料时点为 `2026-10-30T15:34:06+08:00`，历史模拟账户 v4：现金 86484.90、证券数量 100/1000/1000、精确成本 13515.10、费用 15.10。不是今天价格或用户日常持仓。原访问流水和工具原输出留在私有包；最终请求不能按旧会话 ID 取回原文，输入明确说明该边界。实际发送钩子核对完整证据、账户、metrics 与生产完整工具 schema；截断或变更在运输前阻止，并保留已预留额度差异。

用户本轮明确回复“授权十次”。新持久账本最多 10 次，仅用于历史三证券汇总；每次执行最多首提 1 次加契约纠正 1 次，HTTP 重试为 0，成功即停，未知结果不自动重发。对照必须先固定一个变量；不自动耗尽额度。原 4 次汇总、2 次诊断均已用尽，条件性 40 次不转入本轮。费用金额无法在请求前可靠硬限，未知金额不写成零。正常后台业务矩阵需独立阶段授权。

### 本轮实际请求及阻塞

2026-11-02 17:30:15—17:35:15（Asia/Shanghai，收盘后），隔离历史 v4 汇总实际发送一次。证据主体 42,584 字符；加上生产角色、上下文和 schema 后，实际完整请求为 66,258 字符、80,511 字节，不能将证据主体长度当作总请求长度。请求仍为现有供应商/Responses/grok-4.7/`max`，输出上限 4000、请求硬限 360 秒、整轮 1200 秒，未改代理或 TLS。发送钩子确认完整证据及生产 schema 均未被投影截断。

首字节 10.308 秒、首有效事件 10.547 秒、末有效进展 212.560 秒、末字节 250.339 秒；300.054 秒断流，最大观测空闲 87.494 秒。请求没有触及本应用的 360 秒硬限或 120 秒流空闲限，也没有取消来源。错误链为 `RemoteProtocolError → RemoteProtocolError`，记录为 `incomplete_response_body` / `transport_error_before_terminal`。不能据此认定 300 秒边界属于代理、供应商或模型的哪一层；不是旧 90/120 秒总时限复发的证据。

安全关联 response ID 为 `2c622eb5-cd61-9336-b8fb-55f265370a66`，上游 HTTP request ID 未提供。该次没有回报 usage、effort 或 incomplete 原因，不能沿用先前短探测的 `high` 或估算零费用。收到了部分文本事件，但没有最终可验收对象；不拼接半截工具或将普通文本发布。失败任务、旧业务数据及材料仍在隔离库；重新打开 Store 后状态不变，没有新 HTTP、建议、预测或报告。账户仅任务指针与分析状态变化，冻结版本/持仓/账本和原报告未变。此检查不等于成功报告发布后的服务重启验收。

一次线下执行还在 HTTP 前被验证器的配置身份检查误拦：两次隔离迁移只改变六个角色的 `updated_at`。验证器已改用生产 `freeze_agent_team()['team_id']`，真实提示词/模型/effort 变化仍被拒绝；此预检失败消耗零额度。完整工程验收源码为 `5355f025…`；之后仅验证脚本及对应测试调整为 `ff8fffff…`，生产 `app/` 逐文件相同，相关 43 项在 Mac/Linux 分别通过。两份源码身份及测试归属分别保存，未把旧完整测试结果冒称为后续脚本的重跑结果。

当时 v5 停止在未知结果边界，剩余 9 次未使用；其失败仍独立保留，不能默认为未计费或自动重放。详细终止/EOF/契约/发布/正文分项结论见 [原真实请求证据](fifteenth/live-evidence.json) 与 [原内容核对](fifteenth/live-content-review.md)。后来明确恢复的 v6 见下文，不改写旧记录。

17:46 对现有登录后台进行只读核对时，旧页面只保留之前六条记录；独立刷新日志页显示 Cloudflare 525，未取得本次请求的可关联计费条目，费用继续记为未知。此后发生的前端 TLS 错误不证明先前模型流断开的根因，也不证明本次免费。没有修改证书、代理、凭据或钱包，临时标签页已关闭，见 [后台核对记录](fifteenth/supplier-dashboard-reconciliation.json)。

17:48 的第二次独立日志读取仍为 525。随后通过保存的 v5 任务配置、快照和原上下文表达式，在只读 SQLite、实际 Runtime/ModelClient 与 MockTransport 下离线重建请求：规范化 JSON 哈希、66,258 字符和 80,511 字节均匹配原发送记录，源码、原数据库、共享额度账本均未变。原记录未存 raw-byte 哈希，因此不宣称独立证明原始字节逐字相同。私有请求体已保存，见 [重建证据](fifteenth/payload-reconstruction.json)。

用户随后提供后台截图并说明红色感叹号为 `client_gone`。17:35:15 条目的模型为 grok-4.7，Tokens 栏为 `24,833 / 6,463`、首字 7.4 秒、耗时 4 分 57 秒、显示费用 `$0.013266`。时间、模型与本地耗时相符，但截图未显示 response/request ID，因此保留关联强度限制。客户端原始 `usage=null`、费用未知记录不改写；供应商显示计数/费用作为后续独立来源保存。这不证明本地程序主动取消，也不证明存在完整最终工具对象；应继续检查本地调用与连接路径。见 [截图证据](fifteenth/supplier-screenshot-evidence.json)。

曾准备 [一次直连恢复对照清单](fifteenth/recovery-proposal.json)，用户当时选择“暂不恢复，保留剩余 9 次”，因此没有执行。该份早期清单当时未执行。后续先授权下面的原网络路线修复验证；最终 v13 是另行冻结的最后一次隔离直连对照，结果见文首，两份清单及当时状态分别保留。

### 本地修复后的两次获准验证

用户明确回复“明确恢复，最多 2 次修复验证”。离线重新准备的 v6 绑定 `a2c74872448ff00cff0aa8de18151859e6e4a90d737d3403daf8601461e2a396`；与 v5 的准备清单仅 source hash 变化，完整证据逐字节一致，完整 schema、模型/effort、输出 4000、超时和角色身份均不变。执行仍经过原环境代理，TLS 未改。

18:34:45—18:41:35，首次请求及唯一一次契约补交分别耗时 202.331 秒、207.782 秒。两次 HTTP 200、明确 `response.incomplete` 原因 `max_output_tokens`、HTTP EOF；没有本地取消或终止前未知断流。首次已知 incomplete 后，仅在原预算中要求完整终止调用并适当精简，没有重新搜索或重放半截工具。第二次仍不合格，最终任务为 `delivery_contract_failed`，停止于本次两次上限。

两次 API 回报输出 13,905 / 13,380，其中 reasoning 为 9,905 / 9,380，相减均为 4000；发送 effort 为 max、回报 high。原值保留，不静默降档、不推断模型身份、不把 ticks 换算为实付金额。两次未收到函数参数事件，未发现本地提示与完整 schema 的明显互斥要求，但尚未验证提高输出上限能否完成工具提交。因此本次不能归类为网络未知断流，也不能归类为成功报告。

没有 `model-result.json` 或实际可展示正文，内容保持 not_evaluated。incomplete 的可见文本片段没有归档为候选，不能声称已保存；原历史证据与安全元数据保留。隔离 SQLite 两次只读重开确认新报告/建议/预测/批次均为 0，旧 current 报告、账本、账户版本和持仓完整；这不替代成功发布后的服务重启验收。

供应商后台新增条目显示 `$0.020168`、`$0.016054`，输入、输出和缓存计数与 API 一致，合计 **$0.036222**。新十次授权累计三次显示 **$0.049488**；未作请求 ID 或发票级核销。额度累计 **3/10**，剩余 **7 次**保留，本次恢复上限已耗尽。原 40 次完整研究未启用。详见 [修复验证机器记录](fifteenth/postfix-live-evidence.json)、[内容核对](fifteenth/postfix-content-review.md)、[费用核对](fifteenth/postfix-supplier-reconciliation.json)。

18:44 最终保全确认源码、日常 DB/WAL、`.env` 元数据与基线一致，8765 无监听。18:43 最新远端仍是原 HEAD 与失败的 Actions 41413931460，十一组浏览器 success；本地修复没有推送。见 [修复验证后的保全记录](fifteenth/postfix-preservation-after.json)。

### v7—v10：历史输出策略对照（当时状态）

后来用户明确要求“将思考强度改为xhigh，大胆重试，直到找出失败原因并修复”。在十次总额度剩余七次内逐次进行了 v7—v10 四次对照。实际发送与服务回报均为 xhigh。前三次明确 incomplete，私有可见终态证明必填正文已形成，随后反复扩展可选结构直到 4000/6000 输出额度耗尽。最终只改变历史汇总的输出格式要求：正文保留所有三证券判断、依据、反向因素、条件和必要来源/计算指向，省略本任务无需求的可选附录；完整生产 schema 仍为原哈希 `fe7fce61…`。

v10 于 20:15:33—20:17:30 完成，116.282 秒收到有效 finish_analysis、response.completed、HTTP EOF，经真实预检和事务发布恰好一份 current 报告。原模型五字段原样保留，没有程序修补 JSON 或裁掉字段。原文 SHA-256 为 `b3fecb85978f71631082244ff4f2ce7cc4e9bd194c98d9fdcba055edac28f2c1`，私有正文位于 `data/evaluations/fifteenth-historical-xhigh-v10/execution/historical-account-report.md`。它是历史模拟账户的有限判断，不能当作当前行情；没有结构化建议或预测提交，正常业务矩阵仍未验收。

20:21 最终保全确认日常数据、配置与 8765 未变；最新远端仍为原基线失败。完整对照、费用、实际采用与未验证边界见 [xhigh 调查](fifteenth/xhigh-investigation.md)、[成功请求证据](fifteenth/xhigh-v10-evidence.json)、[发布检查](fifteenth/xhigh-v10-publication-check.json) 和 [保全](fifteenth/xhigh-preservation-after.json)。旧 v5/v6 失败记录及当时的授权状态保留，不用成功覆盖。


### v11—v13：内容修复与最后一次路线对照

v11 完成了终态与发表，但独立内容复核发现黄金成本/收盘高低方向倒置，以及裸 URL 被中文闭括号后的正文污染，保持 accepted=false。随后修复裸 URL 提取边界，并在隔离汇总任务中要求核对比较主语、方向和分母。历史时间上下文明确区分判断时点、执行时点与估值来源，未挪动业务期限。旧报告原文及旧引用记录没有被批量重写。

v12 原路线在最后有效进展之后约 120 秒无新增 decoded chunk，触发本地 stream_idle_timeout，没有终态或发表。离线审计未发现逐 delta 写库或 Agent 消费等待；但该指标不是网络抓包，不能排除所有底层缓冲或事件循环停顿，也不能独自定位代理/供应商根因。见 [v12 证据](fifteenth/xhigh-v12-evidence.json)及 [本地审计](fifteenth/v12-local-idle-audit.json)。

v13 仅在隔离验证器切换为显式 direct transport，保留 xhigh、6000 输出、完整证据/schema、任务要求及 120/360/1200 秒期限；TLS 校验和环境 CA 保留。正文、任务结果、分析行和终态函数参数哈希一致，十一条引用逐字匹配输入，原材料、账本和旧报告受保护。独立内容复核接受本次有限历史判断，明确不是当前行情、投资效果、正式方向样本或正常业务矩阵的验收。不得把一次直连成功外推为旧 client_gone 已彻底解释。

## 复现及 Mac 升级

```sh
PYTHON_DOTENV_DISABLED=1 QUOTE_PROVIDER=manual .venv/bin/python -m pytest -q
PYTHON_DOTENV_DISABLED=1 QUOTE_PROVIDER=manual PYTHON=.venv/bin/python npm run test:browser
```

浏览器 runner 逐组保留结果，不因一组失败而跳过后续；CI 仍要求 Python 和全部十一组成功。Linux 使用隔离 Ubuntu 24.04/Python 3.12.3，测试容器无外网，源码快照不含 `.env` 或私有数据库，另含仓库公开交易日历；补装了 runner 所需 `lsof`。与 GitHub x64/Python 3.12.15 的补丁版本/架构差异明确保留，不宣称执行了未推送代码的远端 CI。

本轮没有替用户升级日常实例。用户准备采用时，先只读运行 `scripts/inspect_runtime.py` 核对实例/库路径和源代码摘要，再用 `scripts/backup.py --mode live` 做一致性备份。确认可中断日常研究后，按 [Mac 操作手册](mac-operations.md) 停止并重新启动该实例、刷新页面并检查 `/api/health` 的 `source_sha256_at_start`。不要手动复制 WAL、删除历史失败、改 `.env` 或重新保存模型矩阵。本轮无新迁移，历史记录和原 v11 备份继续保留。

最终数量、固定源码、失败记录、全部获准真实尝试与未覆盖场景见 [验收摘要](fifteenth/validation-manifest.json)；最新实际正文和来源/计算对应检查见 [v13 内容复核](fifteenth/xhigh-v13-content-review.md)，[v10 复核](fifteenth/xhigh-v10-content-review.md)保留为当时的有保留结论，[原失败复核](fifteenth/live-content-review.md) 保留。一份报告成功不代表连续稳定、方向效果或自然成熟准确率通过。


## 正常 A 后续本地修复

恢复检查点遗漏已交付财务材料、可选研究记录超过 100 条静默截断两处缺陷已修复，详见 [修复说明](fifteenth/post-a-fixes.md)、[原检查点只读审计](fifteenth/post-a-old-checkpoint-audit.json)和 [逐项要求审计](fifteenth/requirements-audit.json)。新代码不补写旧检查点，不放宽 TTL 或生产期限。上文旧源码的 1311 项和浏览器结果保持原身份，不用于替代新代码的整套验收。


最近一次全量回归冻结为 `247c5ba48205bd2bffa2dd3419585056cdd6ea808d6e753d827d09a9bd1e3f44`。Mac 与 Linux/Python 3.12 全套各 **1367 项通过**，各保留 1 条既有依赖警告；十一组浏览器全部通过，三者直接对应同一源码。见 [本次整套验证](fifteenth/post-a-regression.json)。本轮后续工程没有新增付费模型请求，这份工程记录完成时原 A 为 10/16 次；随后实际重试结果见最新状态。


最新隔离验证器源码为 `e850e6b3…`：新增单次额度与行情变化回归后，Mac/Linux 相关四模块各 **124 项通过**（恢复模块 23 项），详见 [单次恢复回归](fifteenth/business-a-single-recovery-regression.json)。相对全量回归源码 `247c5ba4…` 只改变恢复验证器及其测试，生产 app 逐文件哈希一致；1367 项全套和十一组浏览器保持原执行身份。
