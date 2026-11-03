# 本地请求结构复核

此次复核没有找到已证实的请求构造缺陷，可以解释 v5 为什么只出现部分文本事件而没有提交工具。审计没有发送网络请求、改变模型配置或写入生产源码；具体机器可读结果见 `local-request-structure-audit.json`。

离线重建 v5 与两个旧短探测，canonical payload hash 均精确匹配原始记录。三个请求仅在 `instructions`、`input`、`tools` 不同；`model`、`stream:true`、`store:false`、`reasoning:{effort:max}`、`include:[reasoning.encrypted_content]`、`max_output_tokens:4000` 和 Responses 的 flat `tool_choice` 完全相同。旧短探测使用五字段 schema，不能据此证明服务端支持完整 schema。

v5 是完整生产 `finish_analysis`：17 个顶层字段、5 个必填；这 5 个字段定义与短探测逐字相同。完整工具本身为 3725 字符，工具数组为 3727 字符（不含 wire 的 UTF-8/整体 JSON 转义开销）。递归检查 74 个 schema 节点没有不存在的 required 字段、重复枚举、非法类型或自相矛盾的长度约束；没有 minLength/minItems。可选 suggestions/forecasts 等可省略。没有安装独立 JSON Schema metaschema 库，此结论是静态一致性检查与可满足的本地契约样例，不是服务端兼容性认证。

真实输入只有一条 user JSON，没有旧 assistant、函数调用或 reasoning 对象回放；没有悬空 tool output。两份报告、21 条计算、共同计算说明和 40 条来源索引与准备输入完全一致；metrics 只保留一份。最终轮明确 `closing:true`，剩余调用/轮次均为 2，且只提供并强制 `finish_analysis`。公开经理提示也明确收尾不再研究。

有可优化的上下文负担：最终仅一项工具，却仍携带 5571 字符的六角色目录及 141 项工具名称，以及普通研究阶段说明。这是非证据元数据，没有证据证明它导致本次输出方式或断流；不应在缺乏单变量证据时把它称为故障根因。未发现把账户/metrics 或整份报告重复装入历史的情况。长度至少 120 的完全相同字符串仅四组，重复额外字符共 1022；该数字不是全部语义重复估算。

对同 v5 canonical hash 的完整请求，用 MockTransport 分别返回完整函数和已完成普通文本，生产 ModelClient 分别识别为 `observed/1 call` 与 `missing/0 call`。另四项定向控制回归全部通过，覆盖完整 schema 接受、一次契约纠正、未知传输结果立即停止，以及子调用不能挤占经理的提交/纠正预算。这些都是离线控制数据，不是实际投资报告或模型服务成功证据。

实际 v5 未取得终态或 usage，不能从部分文本事件认定模型已完成普通文本回答，也不能判断 4000 输出额度在服务内部如何消耗。上游对完整 schema 的执行、研究提示的行为影响以及具体断流位置仍未排除。SSE 与 timeout 的独立复核由其他审查者负责。
