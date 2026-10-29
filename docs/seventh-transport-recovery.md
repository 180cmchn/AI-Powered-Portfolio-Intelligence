# 第七轮：材料更正恢复与汇总传输诊断

## 已核对的第六轮证据

读取了本机已有的脱敏产物 `data/evaluations/sixth-live-final-20261029/summary.json`。A 的两次最终汇总请求均为 Responses 协议、manager 角色、`grok-4.7`、medium：

| 字段 | 首次请求 | 有限重试 |
| --- | ---: | ---: |
| 输入字符数 | 69,024 | 69,024 |
| 输入项目数 | 33 | 33 |
| 请求输出上限 | 3,000 | 3,000 |
| 实际耗时（毫秒） | 50,738.16 | 50,453.55 |
| HTTP 状态 / 请求 ID | 未取得 | 未取得 |
| 已记录响应字节 | 0 | 0 |
| 错误 | RemoteProtocolError | RemoteProtocolError |

旧实现是在 `httpx.post()` 完成、响应正文全部读完后才记录状态和请求 ID。因而上述缺失字段**不能证明没有收到响应头**，也不能确定是建连失败、等待响应头时断开还是正文中途断开。约 50 秒的相近耗时只构成进一步查看代理／服务商超时设置的线索，不足以定位根因。该产物没有可关联的请求 ID，也未取得服务商内部日志；没有声称看过原始私有请求。

本轮未依据此现象增加超时、模型调用预算、Agent 数量，未切换供应商或 effort。新增 httpcore 边界观测只记录阶段名、经过时间、状态与经过脱敏的请求 ID。可区分 TCP、TLS、发送请求、接收响应头、接收正文和本地 JSON 解析；异常记录至多四级异常类型。原始 trace 对象、响应头、私有正文、Key 与隐藏推理均不写入观测。是否存在环境代理只记录布尔值。协议替身已覆盖响应头前断开、正文断开和本地解析失败；这些是工程测试，不是新的真实上游恢复证明。

## 恢复语义

检查点格式升级为 `material_guard_version=3`，保留原文版本、表示哈希、来源流、读取范围、父材料关系、实际任务和角色实例。`research_material_deliveries` 保存运行时实际放入会话的有限工具结果证明，正文不复制进该表；索引、模型自行提交的 ID，以及只留下上下文归档指针的超大结果均不能成为读取证明。模型自己写出的提取材料记录为 `authored_content`，不伪装成原始资料读取；其父原文仍必须有实际交付证明。

每份子报告保存提交前已有的 delivery ID 快照，避免报告提交后才读到的更正被追认为已吸收。相同角色的旧研究轮次继续留在检查点，最新可见报告作为当前研究结论。`prepare_synthesis` 和 `submit_report` 可提交 `material_updates`，包含新版材料 ID、历史材料 ID、可见处理说明及 `handled / needs_review` 状态。这个声明表达研究是否处理更正，不是新增事实审核或可信度打分。

同时引用 v1、v2 时，若 v2 已实际交付，并有可见比较／更正处理说明，可进入 `synthesis_ready`。若旧子报告尚未更新，主 Agent 可以在亲自取得新版后，用可见说明明确列出被其更新的子角色实例；原子报告及模型来源仍保留。只出现新版索引、另一角色读过但本报告未收到、更正仍待处理或某个其他来源仍落后时，状态为 `needs_research`，不会称为仅汇总恢复成功。

保存后流头变化（包括真实回滚）、原材料／父关系变化、账户输入或公共范围变化、行情／事件保护不符、跨账户、检查点过期与两次恢复上限仍阻止复用。使用新主模型汇总时，旧子报告继续保存原 `model_config`，检查点 `configuration_relation` 单列本次汇总配置。旧 v2 检查点原内容保留，但缺少逐角色的实际交付与处理证明，不从当前状态倒造证明。

## 回归证据

`tests/test_seventh_recovery.py` 的 24 项工程测试使用生产 Store/RecoveryStore、临时 SQLite，包含 Service＋ResearchRuntime＋受控 HTTP 协议替身。完整路径为：实际工具读取 v1 和 v2 → 显式可见比较 → 汇总传输失败 → 新任务只恢复汇总并发布报告。没有真实模型或真实远端错误被伪装为该测试。

另有 `tests/test_seventh_live_inputs.py` 的 19 项无网络边界测试，验证真实验收脚本只向指定 HTTPS 主机和 API 路径发送凭据、只能选择真实目录返回的模型 ID。脚本使用独立数据库，在真实正式窗口内先等待生产 scheduler 完成精确基准准备，再启动 worker，防止账户保存任务先跑成探索研究；基准不可用则记录正式链路未验证。最终成功状态与保留的早先失败证据分开记录。

命令：

```sh
.venv/bin/python -m pytest -q tests/test_seventh_recovery.py tests/test_seventh_live_inputs.py
```

本次结果：43 passed。全仓、浏览器及真实网络验收以本轮总交付记录为准。

## 第七轮真实运行后的诊断修正

`data/evaluations/seventh-live-release-20261029/summary.json` 对应上海时间 2026-10-29 11:17:30 至 11:23:29 的完整真实研究运行，其应用／脚本摘要为 `eadea9eeb2a2b907774429e2f8f249dba484f85b07c483b96b3f7f216aedcd72`，记录确认运行期间源码未变。此运行在最后一项诊断修补之前，不能直接当作修补后完整模型研究链路的验收。

其网络 trace 暴露了一个诊断归类错误：HTTPS 代理 CONNECT 成功的 HTTP 200 被暂时填入了原站响应状态。随后 trace 明确发生 `proxy.start_tls`，再发送实际请求，最终在 `receive_response_headers` 阶段失败。原产物中的 `status_code=200`／`response_headers_received=true` 因此不能解释为模型原站返回了 HTTP 200，也不是正文阶段失败的证据。原始产物保留，不覆写或美化。

最终修补通过 `send_request_headers.started` 中的运行时请求方法区分 CONNECT 与实际请求。隧道状态单列在 `proxy_tunnel`；原站开始请求时清空原站响应证据，只有原站响应头完成才填写原站状态及请求 ID。trace 项只增加来源范围标签，不保存请求／响应头或正文。已用协议替身分别验证“CONNECT 200 → TLS → 原站响应头失败”和“CONNECT 200 → 原站成功响应”；前者原站状态、请求 ID 与 accepted 均保持未知，headers/body_received 均为 false。修补后 `app/model.py` SHA-256 为 `0c6df652c24de1d48df28bd599c79f4fc24ed6b78c5f03c45082285369851bcf`。

同时扩大了**验收脚本自身的结果注释**，识别以“占位”“测试占位”“测试报告占位”起始的正文。生产报告保存、发布和研究流程没有因此新增内容门槛。能保存报告与交付了实质研究必须分别解释。此项不用于替模型判断投资观点。

最终修补后已在独立评估库副本重新启动正常 API/worker，上海时间 11:27 完成；生产协议探测两轮工具循环均收到源站 HTTP 200，`result.ok=true`。首请求的代理 CONNECT 与源站状态分开记录，第二请求复用连接，未观察到新的 CONNECT。源码摘要为 `256c43c09b357fc43e455b988526fca0e3026d5b0982ff13f0ce48aa77a311e3`，运行期间不变，未新建研究任务。证据为 `data/evaluations/seventh-postpatch-restart-20261029/summary.json` 与 [可携带验收证据](seventh-live-evidence.json)。这是最终代码启动与小请求协议验证，此前完整研究运行没有改署为最终源码的完整研究成功。
