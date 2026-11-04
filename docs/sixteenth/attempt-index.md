# 第十六轮验证与失败索引

唯一完整最终 Python 分母是 `python-final.txt` 的 **1567 passed、1 warning**。以下定向、开发中和历史冻结结果相互重叠，不能相加。日志保留在本地 `test-results/sixteenth/`，通常不提交；本目录的 JSON/说明记录可审查结论。文件名含 `final` 也不保证通过，以下按实际内容列示。

| 阶段 | 日志/证据 | 实际结果与处理 |
|---|---|---|
| 首轮全套 | `python-initial.txt` | 1539 passed、10 failed；设置修订兼容、上下文配额、调度合并断言；未删除旧保护 |
| 初次冻结全套 | `python-frozen-v1.txt`、`frozen-source-v1.json` | 1559 passed；后续另补上下文/认证边界，不能重署为最终源码 |
| 最终冻结全套 | `python-final.txt`、`engineering-final.json` | 1567 passed、1 warning；最终源码 `b7afd986…` |
| 浏览器首轮 | `browser-frozen-v1-first.txt` | 未提供项目 Python，多服务启动超时；中断，原日志保留 |
| 浏览器解释器核对 | `browser-interpreter-check.txt` | 显式项目 `.venv/bin/python`，production组通过；未扩大保护时间 |
| 最终浏览器 | `browser-final.txt`、`browser-groups-final.json` | 11组全部退出0 |
| 模型路线初轮 | `model-route-first.txt`、`model-route-second.txt` | 7 failed/2 passed；修正夹具/行为后9 passed，详见模型路线说明 |
| 模型路线扩展 | `model-route-related.txt`、`model-route-related-final.txt` | 1 failed/168 passed → 169 passed；未放宽原限时 |
| 路线与设置 | `model-route-final.txt`、`model-route-final-settings-compatible.txt`、`model-route-settings-compatibility.txt` | 两次10 passed、32 passed；GET→PUT保存修订兼容回归 |
| 路线浏览器 | `model-route-browser.txt` | 路线设置/原文与阅读导出相关浏览器组通过，计入后续11组重跑 |
| 调度首轮 | `scheduler-targeted-first.txt` | 97 passed/1 failed；新合并事务绕过原故障注入位置，扩展为两条事务路径回滚断言 |
| 调度补测 | `scheduler-targeted-final.txt`、`scheduler-targeted-final-with-pause.txt` | 99 passed、100 passed；暂停时仍维护截止 |
| 调度范围审阅 | `scheduler-merge-return-final.txt`、`scheduler-scope-review-final.txt` | 57 passed、59 passed；同步合并原因返回值及单证券真实覆盖范围 |
| 上下文初次/旧回归 | `prediction-context-initial.txt`、`prediction-context-regression.txt` | 2 failed/6 passed、2 failed/74 passed；大批次目标归档以及原数值/期间仍可见 |
| 上下文修复 | `prediction-context-fixed.txt` | 76 passed，预算未增大 |
| 认证夹具初轮 | `authentication-first.txt`、`authentication-second.txt` | 6 failed；2 failed/4 passed；逐次区分夹具设置与生产认证分类 |
| 认证扩展中间结果 | `authentication-final.txt`、`authentication-final-pass.txt` | 均2 failed/129 passed；尽管文件名带pass，实际失败记录保持 |
| 认证最终定向 | `authentication-final-v2.txt` | 131 passed；401/403跨tick、重启和真实配置修订恢复 |
| 自动审批 | `real-validation-authorization-scope.json` | 初次拒绝未执行，模型HTTP为0；补充既有目的地/模拟数据/授权证据后同一命令获准，用户再次明确确认 |
| 固定源码environment真实矩阵 | `live-matrix.json`、`live-request-index.json`、本地`live-matrix-v1.txt` | 170 HTTP / 165 completed+EOF，5次断流；9项已启动任务，重启保留报告/预测；矩阵未全通过，不能仅展示成功报告 |
| 本地关闭调查 | `stream-disconnect-investigation.md`、`local-stream-lifecycle-review.json`、`tcp-close-observation.json` | 10个本机受控case、既有Mihomo本地DIRECT的55秒控制、第5次真实失败代理先FIN；不把旧4次追认为已抓包 |
| 实际代理链空闲对照 | `public-idle-route-control.json/.md` | 仅2个无认证非模型GET；environment正文空闲50.040秒断，direct跨两段55秒后完整EOF；与模型请求分母分开 |
| 固定源码direct方案验证 | `direct-matrix.json`、`direct-request-index.json`、本地`live-direct-v1.txt` | 同一b7源码、新独立库、正常入口显式direct；以finished_at及逐阶段内容复核判断最终结果，不覆盖environment失败 |

实现与失败原因分别见 [调度](scheduling.md)、[模型路线](model-route.md)、[上下文与多事件](semantics-and-events.md)、[认证](authentication.md)。真实原文与逐事件数据库位于私有 `data/evaluations/sixteenth-normal-matrix-v1/`，开发期内容复核使用报告/任务身份及原文SHA-256对应；不改应用的 `content_assessment=not_evaluated`，不引入生产LLM裁判。

第十五轮原有断流、内容拒绝、不同运行源码及最终成功仍在 `docs/fifteenth/` 与 `docs/iteration-15.md`，本轮未覆盖或清理这些记录。验收封存时尚未提交或推送，因此该时点没有本轮新提交的远端CI；后续推送不回写本地验收事实。
