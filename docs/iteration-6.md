# 第六轮交付与验收记录

本轮从干净工作区 `925e9855ba28fda10e9fd446aa2d692a1d709cee` 开始；开发前通过 GitHub 连接重新核对 main，仍为该提交。没有回退、推送或修改日常 `.env`、已有凭据与日常数据库。测试、浏览器与真实外部验收分别使用独立数据库。

## 实现范围

- 供应商只接收 API Key、Base URL、协议，保存后由后端 HTTP 自动获取真实目录；模型与五档 effort 在共享角色或账户覆盖中选择。
- 六个统一角色拥有独立职责、提示词和工具权限。所有账户继承完整目录，主 Agent 自主派发具名角色；账户只能覆盖供应商模型和 effort。
- 任务开始冻结团队、角色、连接与凭据版本；每个调用实例独立持有会话和客户端。轮换不改写在途绑定，显式清除阻止后续请求。
- 跨账户共享仅限原始资料和可复算数据；生成结论、模型提取内容及其引用父链按账户隔离。
- 新增不可变主 Agent 预测、固定批次和覆盖分母、1/5/20 交易日确定性评估、复权口径与修订历史、共同样本对比。默认不启用每日新预测；探索和工程测试分开统计。
- 修复需求分页与显式完成、检查点实际阅读版本，以及模型研究、基础行情和到期评估的独立开关。

详细实现分别见 [供应商与角色](sixth-provider-roles.md)、[预测规则](forecast-evaluation.md)、[修复与隔离](iteration-6-repairs.md)。[README](../README.md) 提供正常网页流程、Mac 启停与备份恢复。

## 证据分类

确定性测试使用显式 HTTP、凭据、历史行情与时钟夹具；覆盖实际生产 Store、Service、Runtime、ModelClient 与 FastAPI。它们证明请求绑定、权限、隔离和计算规则，不证明真实供应商支持所有模型、effort 或原生联网。

浏览器测试包含已有 HTTP 夹具回归和实际 FastAPI/SQLite 页面集成。人工页面检查在 `tests/sixth_browser_server.py` 的独立受控数据库完成：三字段提交、规范化 API 根路径、自动目录、全局角色保存、账户 A 覆盖及 B 不变、正式空样本与测试样本分开显示。测试页面明确标注工程夹具；夹具中的命中不能作为真实准确率。

macOS 系统钥匙串用临时合成凭据完成写入、读取比对和删除，没有替换用户真实凭据。

真实验收脚本为 `scripts/verify_sixth_live.py`。脚本只读取项目现有环境与可选日常设置的只读副本，在新目录建立独立 SQLite，自动发现目录后只选择用户原先配置且实际出现在目录中的模型。通过 FastAPI 请求创建账户，并启动正式 lifespan/Service.loop/worker 执行研究；外部网络请求没有替身。记录脱敏 HTTP 请求摘要、角色配置、事件、报告与预测、源代码摘要、提交和依赖版本。此路径使用进程内 ASGI 请求进入正常 HTTP 路由，而非绕过 API 直接调用研究函数。

运行命令（输出目录必须不存在）：

```bash
.venv/bin/python scripts/verify_sixth_live.py \
  --output data/evaluations/sixth-final-YYYYMMDD-HHMMSS --timeout 420
```

真实验收保留当前 `grok-4.7 / responses / medium` 与原生联网设置。仅有这一组合是用户已有选择，因此没有擅自从目录挑选第二模型；多供应商、同名模型、不同协议与五档 effort 的工程绑定由协议夹具验证。新发预测的 5/20 日成熟表现尚未发生，不回填历史预测或报告真实命中率。

2026-10-29 00:49 上海时间结束的最终代码验收：目录真实返回 3 个模型，含用户已选 `grok-4.7`。两个账户均有六个角色，分别自主调用一个 `market_researcher` 并取得其回报。账户 B 经正常 worker 发布报告，账本保存 1/5/20 交易日三条明确弃权的探索记录；账户 A 在最终汇总遇到上游 `RemoteProtocolError`，三项预期样本保留失败，不冒充已预测。实际请求和服务回报的模型均为 `grok-4.7`，effort 均为 `medium`；观察到一次原生联网事件。修改 A 持仓后，B 输入与团队绑定保持不变。

行情实际确认名称为机器人ETF鹏华；取得东方财富日线至 2026-10-28，报价 0.949、行情时点 2026-10-28 15:34:18 +08:00。验收发生在休市阶段，只证明收盘资料路径，没有验证当日盘中新鲜行情。真实正式批次样本为 0，未宣称真实预测准确率或第二种模型组合实测成功。

最终源代码在真实重试期间没有改变，SHA-256 为 `175b3570bee9601f054ba6ca5438137c0f85e56c4fd6da395985c0a4bd7f99f9`（计算方法随 JSON 记录）。全量 Python **534 passed**，一条现有 Starlette 弃用提示；`npm run test:browser` 两套连续通过。早先集成尝试的两次汇总失败独立保留，不作为最终代码成功证据。

## 参考核对

没有复制第三方实现或引入其运行时依赖。Python 后端与现有轻量前端独立实现，参考仅用于交互和职责划分：

- [CC Switch](https://github.com/farion1231/cc-switch/tree/6d6b7162f122ac0ce1356a14688c8c4f712db3c8)，固定提交 `6d6b7162f122ac0ce1356a14688c8c4f712db3c8`：核对 `OmoFormFields.tsx`、`types/omo.ts`、`lib/api/model-fetch.ts`、`commands/model_fetch.rs`、`services/model_fetch.rs`。其 OMO 表单接收 modelOptions，代码同时有后端 HTTP 与 OpenCode CLI 模型路径；本项目只实现 HTTP 自动发现，不依赖 CLI。该提交为 MIT，署名 Jason Young。
- [oh-my-openagent](https://github.com/code-yeongyu/oh-my-openagent)：核对 `docs/reference/omo-json.md` 与 `packages/omo-config-core/src/schema/agent.ts` 的具名定义、提示词及权限分离；不照搬 fallback、per-account 角色集合或 effort 重映射。所查 `dev/LICENSE.md` 是 Sustainable Use License，不误标 MIT，也不纳入其源代码或衍生实现。
- [oh-my-pi](https://github.com/can1357/oh-my-pi)：核对 `docs/task-agent-discovery.md` 与 `packages/coding-agent/src/task/types.ts` 的定义/实例划分，不引入代码编辑、工作树、模型选择或审批机制。所查许可证 MIT，署名 Mario Zechner、Can Bölük、Stencil Labs。
- [OpenAI Models API](https://developers.openai.com/api/reference/resources/models/methods/list)：核对认证的 `GET /models` 和 `data[].id`。目录列出与请求接受、工具续接、原生联网、服务回报 effort 分别记录。

## CI 与最终版本

本轮核对远端 main 的最新 GitHub Actions 运行 [40670587410](https://github.com/180cmchn/AI-Powered-Portfolio-Intelligence/actions/runs/40670587410)，状态 `success`，对应原有 main 提交，2026-10-28 10:16:31 UTC 完成。未推送本轮变更，所以该远端结果不代表本轮补丁已通过远端 CI。

最终本地测试与重启验收结果见同目录的 `iteration-6-validation.json`。暂存检查仅清理了验收脚本行尾空白及测试文件末尾空行，未改变生产应用；JSON 同时记录真实重试与提交时的文件摘要。提交后的新进程校验记录为 `data/evaluations/sixth-release-restart.json`，包含最终提交、源摘要与正常 API 状态。真实网络的完整脱敏记录留在被 Git 忽略的 `data/evaluations/`；日常数据库不会因验收被升级或重置。
