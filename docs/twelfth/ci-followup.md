# 第十二轮首次推送后的 CI 修复

2026-11-01（Asia/Shanghai），按用户授权检查并修复 main 的首次推送。生产源码、数据库 schema、账本校验和模型配置均未改动；没有操作日常 8765，也没有模型调用。

## 失败证据与根因

提交 `56272f938208b654bd09982835abc504fdd39827` 的 [Actions 41278962791](https://github.com/180cmchn/AI-Powered-Portfolio-Intelligence/actions/runs/41278962791) 为 failure。Python 实际 **976 passed、1 warning**；iteration、production、seventh、ninth、tenth、twelfth-select 六组浏览器成功。eleventh、twelfth 两组在约 2 秒后退出，必需汇总 `tests` 正确返回失败。逐组身份及原始错误摘录见 [ci-first-push.json](ci-first-push.json)。

两个失败组均用真实历史静态源码复现修复前的问题，然后验证当前页面。GitHub `actions/checkout@v4` 默认只取一个提交，缺少 `874b9875a9649480cde78bca804af2693ba5d1d8` 和 `09557bd8a3cd404250fa9fa58093f6babb404ead`。路由回调中的 `git show` 因此报 `fatal: invalid object name`，浏览器行为断言尚未执行。这不是已复现的两个产品交互回归，也不是此前上海跨日导致的两项 Python 失败；后者在本次远端已通过。

本地完整历史仓库可以读取这两个提交，因此此前本地八组成功不能证明浅检出的 CI 成功。[checkout 官方说明](https://github.com/actions/checkout#usage) 定义 `fetch-depth: 0` 为获取完整历史。

## 修复

- 仅浏览器 job 的 checkout 设置 `fetch-depth: 0`，同时适用于 push 和 pull_request；保留默认待测 ref。
- 两组测试使用完整、固定 SHA，通过 `tests/browser-baseline.mjs` 在创建临时目录、服务和浏览器之前同步预读历史资源。缺历史时明确报错并提示补齐检出；不回退到当前源码，不跳过基线。
- 路由回调只发送已经读取的资源，消除其中未捕获的同步 Git 异常及遗留服务风险。
- 原行为断言全部保留。矩阵仍为八组、`fail-fast: false`，汇总仍要求 Python 和全部浏览器成功；没有 `continue-on-error`。

## 本地复验

在 `/tmp` 创建与首次 CI 相同 HEAD 的全新 `--no-local --depth 1` 克隆，两份基线均读取失败；`git fetch --unshallow origin` 后均成功。另一个全新浅克隆运行修改后的两组入口，均在启动服务前退出 1，出现明确基线提示；替代 Python 启动探针确认没有调用服务。

补齐历史的隔离克隆使用当前修复后的测试文件、生产 FastAPI、临时 SQLite、随机端口与明确受控事件；环境为 macOS、Python 3.12.13、Node v25.9.0、Playwright 1.62.1。命令如下，日志和机器可读结果随本记录归档：

```sh
PYTHON_DOTENV_DISABLED=1 QUOTE_PROVIDER=manual PYTHON=/tmp/guanlan-twelfth-py312/bin/python npm run test:browser:eleventh
PYTHON_DOTENV_DISABLED=1 QUOTE_PROVIDER=manual PYTHON=/tmp/guanlan-twelfth-py312/bin/python npm run test:browser:twelfth
```

两组完整入口均 **exit 0**：eleventh 62.18 秒、twelfth 103.31 秒。见 [逐组结果与测试文件摘要](ci-focused-local.json)、[浅检出及提前失败证据](ci-checkout-reproduction.json)、[eleventh 日志](ci-eleventh-local.txt)、[twelfth 日志](ci-twelfth-local.txt)。三份 JavaScript 语法检查及 `git diff --check` 通过。

远端 Ubuntu / Python 3.12 / Node 22 的完整 Python 和八组浏览器由修复提交触发重新验证；是否通过必须读取该提交对应的 [main Actions](https://github.com/180cmchn/AI-Powered-Portfolio-Intelligence/actions?query=branch%3Amain)，不能用本地结果替代。此记录保留首次失败事实，不覆盖此前本地或真实供应商记录。
