# 第十三轮隔离真实验证器

入口 `scripts/verify_thirteenth_live.py`。实际执行必须显式提供 `--run-real`、已有授权参考账户 ID 和 `data/evaluations` 下的新目录。该命令可能产生供应商费用；本轮只使用任务明确允许的已有配置引用和单轮上限，不切换模型 / effort / 协议、不修改代理、DNS 或 TLS，不读取别的应用凭据。

验证器复用 `verify_twelfth_live.py` 的生产链：以 SQLite `mode=ro` 只读借用参考配置；准备服务 `worker=False`、随机回环端口；三笔明确模拟算术买入后核对现金 **86484.90**、数量、各证券总成本及合计 **13515.10**、累计费用 **15.10**、零已实现盈亏、三笔原始lot/日期/费用依据、4条流水/资金记录及逐项落盘回执，只有最新输入版本任务可运行才释放一次。定时研究、行情调度、预测评分和新闻扫描在隔离配置中暂停；建议观察及研究需求仍走正常 Service tick。准备产生的 superseded 任务仍保留，attempts 必须为零。

本轮额外使用权限 0600 的 `attempt-budget.sqlite3`。每个隔离服务进程在创建应用前给全部 ModelClient 的 `before_request` 加上统一原子预留；原运行时的账户 / 子任务 / 凭据 / 截止保护先执行，再用 `BEGIN IMMEDIATE` 预留本次 HTTP 尝试。正常请求、子任务、自动账户任务、全局任务及内部 transport retries 共用同一个计数，重启进程不会重置。上限为 `min(40, 已有max_model_calls)`；若配置本来更低，不提升。达到上限后没有新 HTTP 请求。

这项验证器保护只安装在它创建的隔离子进程，不改变日常生产预算或 ModelClient 文件。原来已经发送而结果未知的请求仍不自动重发；此脚本不为报告成功新增业务重试。预留后进程在 HTTP 前退出可能造成预留数高于已记录请求，保留差值，不把预留数自动当成可收费请求或成功数。

若报告完整发布，观察 10 个相隔 1.1 秒的正常 worker 周期，使用同代码和同隔离库重启后再观察 10 个周期。报告自然失效或额外自动任务都记录并使机械验收失败，不删除它们或为了绿色关闭复核逻辑。没有报告时不宣称这 20 个发布后周期已执行。

本地 `summary.json` 含完整任务 / 报告 / 工具业务信息，权限 0600，位于被 Git 忽略的私有评估目录。公开文档只能抽取白名单诊断，不直接复制该文件。代码摘要、请求与任务 ID、阶段、terminal、HTTP版本、cause 类型、代理环境是否存在、用量、报告 / 预测数量分别记录；不公开 Key、代理凭据、原始流、隐藏思维链。服务已接受但用量未回报的请求费用保持未知。

无费用验证：继承验证流程由 `tests/test_twelfth_live_verifier.py` 覆盖真实临时库 / HTTP / 服务生命周期及显式模型替身；新 `tests/test_thirteenth_live_verifier.py` 覆盖并发全局原子上限、跨实例持久状态、真实 ModelClient + HTTPX MockTransport 的连接失败重试和另一个自动客户端共用上限。它们不是供应商成功证据。

退出码 0 和原字段 `single_report_acceptance_passed` 仅涵盖报告存在、任务/版本/配置、20 个发布与重启观察周期等机械条件，不能自动证明账户研究内容合格。真实结果还必须独立读取生产页面实际展示的正文与结构化结论，逐项核对现金/成本/敞口、观察行动、依据、反向因素和失效条件。本轮机械检查通过，但内容审查失败；脱敏证据分别保存 `mechanical_single_report_acceptance_passed=true`、`account_report_content_accepted=false`、`overall_live_acceptance_passed=false`，不追加请求修饰结果。
