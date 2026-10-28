# 第五轮浏览器行为回归与第四轮 CI 定位

记录日期：2026-10-28（Asia/Shanghai）。本轮没有推送或重跑远端工作流；远端第四轮失败状态保持原样。

## 实际远端证据

- 审查基线：`026db28cbc867bb92dfad152028d2cf4c931791c`。
- [workflow run 40370429201](https://github.com/180cmchn/AI-Powered-Portfolio-Intelligence/actions/runs/40370429201)：`Local application tests`，push/main，attempt 1，结论 `failure`。
- [job 121739373982](https://github.com/180cmchn/AI-Powered-Portfolio-Intelligence/actions/runs/40370429201/job/121739373982)：Python 安装、pytest、Node/npm、Chromium 安装步骤成功；`npm run test:browser` 失败。
- [artifact 12672003693](https://github.com/180cmchn/AI-Powered-Portfolio-Intelligence/actions/runs/40370429201/artifacts/12672003693)：`browser-failure-evidence`，166,967 bytes，查询时未过期。已核对产物元数据；连接器不支持二进制下载，因此未声称检查过远端截图像素。

GitHub 连接器返回的实际日志包含：

```text
296 passed, 1 warning in 21.43s
PASS (deterministic HTTP fixtures + Chromium): polling, protected drafts/focus/version, ...
page.waitForResponse: Timeout 30000ms exceeded while waiting for event "response"
  at .../tests/frontend_production.mjs:103:22
Node.js v22.23.3
```

第一组 HTTP fixture 测试通过，第二组生产 FastAPI + 临时 SQLite 测试等待 checkpoint retry POST 响应超时。远端环境为 Ubuntu 24.04 x64、Python 3.12.15、Node 22.23.3、Playwright 1.62.1、Chromium 151.0.7922.34（revision 1234）。

## 缺陷、竞态与环境的区分

产品缺陷已证实：旧 `operation()` 在任意操作未结束时直接 `if (state.busy) return`。导航会先渲染可点击任务卡，再等待状态及账户读取；卡片存在不代表导航的 GET 已完成。在此区间点击重试，会没有 POST、没有提示。

新增浏览器回归受控暂停导航的 `/api/status`，在暂停期间点击实际重试按钮，并要求 POST 在解除导航暂停前发出。修复前该回归稳定出现 3 秒等待响应超时；修复后通过。3 秒仅作为受控局部负向判据，未提高旧测试 30 秒超时、增加 sleep、跳过断言或移除生产后端测试。

旧测试只等待卡片/检查点出现，没有等待导航完成，因此存在点击时点竞态。这个合法用户时点暴露了产品缺陷；只在测试中等待页面空闲会掩盖缺陷，故本轮修复应用并保留这个交错测试。

历史日志只有响应等待超时，没有当时请求轨迹或前端 busy 采样；它不足以排除旧 CI 上其他失败原因。可以确认本缺陷会产生相同症状，不能把它写成该次远端失败的唯一已证实根因。远端依赖安装及第一组浏览器成功，没有日志证据支持把该次失败归因于浏览器未安装或 Python 服务未启动。本机最初遇到的沙盒端口权限和缺少 Chromium 可执行文件属于本机环境问题，处理后才运行回归，不能用来解释远端失败。

## 最终交互行为

- GET 读取不再对写操作加全局锁；操作按目标键去重，进行中与重复提交都有可见提示。
- 在点击/提交时绑定账户、任务、表单值、`expected_version`，请求发出后切账户不会改投其他账户。
- 账户读取与导航使用序列标识，旧返回值不覆盖更新后的账户/页面；切账户期间明确显示加载状态。
- 保存期间的新输入继续保留，成功后更新草稿的基准版本；失败显示原因并保留草稿，可再次提交。
- 证券查询校验原 DOM、查询词及账户，旧查询不覆盖新输入；离开设置页后，本地工具探测结果通过独立对话框呈现。
- 设置提供严格五个 `reasoning_effort` 选项；五档分别验证 UI、设置 API、Store 重开持久化。当前已保存配置和在途任务启动时的旧配置分别展示，未记录的历史值保持未知。模型配置错误可由状态轮询直接显示。
- description-only 原生来源由生产 `ResearchTools.record_native_search()` 保存，经生产材料索引、范围读取及前端展示为“仅检索摘要 · 未读取全文”。

## 本地验证

macOS ARM64、Python 3.14.4、Node 25.9.0、锁定依赖、Playwright 1.62.1 + Chromium 151.0.7922.34：两组完整 `npm run test:browser` 已通过。另使用已安装 Chrome 的初始旧版测试通过，说明不加受控交错时失败具有时序敏感性；这不是旧 GitHub 状态已经恢复。

```sh
PYTHON=.venv/bin/python PLAYWRIGHT_BROWSERS_PATH=/tmp/guanlan-pw npm run test:browser
```

本轮新增生产后端行为断言涵盖：导航暂停期间 retry/save/analyze；重复点击请求数为 1；点击时账户/输入/版本；写响应暂停期间切账户；旧成功响应保留新草稿；受控 409 失败与再次保存；账户 B 输入隔离；五档保存/重开；在途 medium 与当前 max 区分；旧查询和离页探测。

Linux ARM64 补充验证也已通过：Debian 13.6、Python 3.12.13、官方 Node 22.23.3、锁定 Python/npm 依赖、Playwright 1.62.1 + Chromium 151.0.7922.34，完整两组 `npm run test:browser` 均通过。临时容器只挂载脱敏源码副本，未挂载 `.env` 或日常数据库；结束后已自动删除，未留下本轮镜像或命名容器。

同一 Linux 容器随后只把前端 `app.js` 替换为第四轮基线版本，保持当前生产后端和回归 fixture，受控暂停导航测试再次失败：`page.waitForResponse: Timeout 3000ms ... frontend_production.mjs:114:22`，进程退出 1，保留请求轨迹、trace 和截图。轨迹中没有 `/api/jobs/.../retry` POST。这隔离证实旧前端的丢操作机制；它仍不等同于完整重演历史 GitHub 运行。

Ubuntu/Node 基础镜像拉取长时间停留于 Docker Hub metadata，已停止该构建，转为上述已有 Debian 镜像。Linux 验证匹配 Node 与浏览器版本、Python 3.12 系列及锁定依赖；OS、CPU 架构、Python 补丁版本与 GitHub Ubuntu 24.04 x64 / Python 3.12.15 不同，不宣称完整复制远端机器。

Linux 使用执行时复制的脱敏后端快照；复制后其余并行修改涉及 `config.py`、`main.py`、`model.py`、`service.py`，因此该次结果用于前端及当时 API 行为验证，不能冒充后续全部后端补丁的验收。最终冻结工作区已另跑本机回归，见下节。Linux 测试副本与工作区最终前端 SHA-256 均为 `906c22af0dd28c0b16b28265912fa9ad730d5c9b9bcc56df3206541430a829de`。本地验证不改变远端 run 的 `failure` 结论。

## 最终冻结后的本机复核

2026-10-28 12:04:38（Asia/Shanghai）完成：在最终冻结后端（包括精确证券代码匹配的官方展示名称、保留 `source_name` 的行情修复）上，新建临时 SQLite 并重启 fixture FastAPI，完整两组浏览器测试均通过。运行前后重新计算 `app/` 源码清单，22 个源文件完全一致；其中 17 个 Python 后端文件。实际运行环境为 macOS ARM64 / Python 3.14.4 / Node 25.9.0 / Chromium 151.0.7922.34。

- 测试时 HEAD：`e0049cd2111ec20402d6752a7dc0a879202e3050`；最后的报价身份与验收脚本增量当时尚未提交。随后固定提交见第五轮交付记录；全程未推送。
- 应用源码清单 SHA-256：`e0bd212be7d76dbe9a311ecbf4c8ee7cf1837895c1faeb08a68c95d4c4fdc788`。
- Python 后端清单 SHA-256：`4cb7864c6dcd2c22eaa7e246286957eb4a128b9cd0e32c60300a0d26992fb39f`。
- [完整文件哈希清单](fifth-browser-final-manifest.json)。清单摘要计算方法：逐文件 SHA-256，按相对路径排序成 JSON 对象，使用 UTF-8、`sort_keys=True`、`separators=(",", ":")` 序列化后再做 SHA-256；排除 `__pycache__`。

| 后端文件 | SHA-256 |
| --- | --- |
| `app/config.py` | `d64f2f043984ca4b8a07c21fb8fae976a448be78865275e24c65d8dd4d5215ef` |
| `app/main.py` | `a1a1bb4725f1718132cbd9959a0a380c53f16955d81710fb5df8a51db9810055` |
| `app/model.py` | `e94b770c68761c46cf97ff95cf2fc0435bb0d03fa0b75faff0795f7c08d7a4fa` |
| `app/service.py` | `6415a9896f82ffdc5e9693d68bbb16bc9e210770ec914e596d8e8f03b34c4258` |
| `app/domain.py` | `db4f3e5dd86392174296f284801212f4a077996c1bf10fdfc6bffc7b0f255637` |
| `app/store.py` | `c00ff96dc57bc11583e7ff4b3668fd2d06341bbc88f492715780ccbdc8786c76` |

这些是受控浏览器验证，不能代替真实模型、真实报价或自然事件触发验收；远端历史失败仍未改变。

## 产物

- `test-results/frontend-production-settings.png`：本机受控设置页截图，含五档控件、当前 max 与在途 medium。属于测试 fixture，不是真实证券或真实模型验收截图。
- 失败时新增 `frontend-production-trace.zip` 与 `frontend-production-requests.json`，与既有截图一同由 CI 的 `test-results/` artifact 收集；请求记录只保存方法、API 路径和响应状态，不保存凭据或请求正文。
- 远端日志节选与独立环境日志保存在本机 `/tmp/guanlan-iteration5-ci/`：`remote-ci-excerpt.log`、`linux-browser.log`、`linux-old-ui.log`、`mac-old-ui.log`。
- Linux 设置页截图：`/tmp/guanlan-iteration5-ci/current/test-results/frontend-production-settings.png`；基线前端红回归截图/请求记录/trace：`/tmp/guanlan-iteration5-ci/old-ui/test-results/`。临时副本没有用户密钥或日常数据库。

## 转发响应兼容补丁后的复核（12:18）

2026-10-28 12:18:16 至 12:18:26（Asia/Shanghai），在 `app/model.py` 新增单层 `success/data` 转发响应兼容后的冻结工作区，再次执行 `PYTHON=.venv/bin/python PLAYWRIGHT_BROWSERS_PATH=/tmp/guanlan-pw npm run test:browser`：HTTP fixture 与生产 FastAPI + 新建临时 SQLite 两组均通过。此轮运行环境仍为 macOS ARM64 / Python 3.14.4 / Node 25.9.0 / Chromium 151.0.7922.34。

运行前后按前述清单方法核对全部 22 个应用源文件，完全一致。本次应用清单 SHA-256：`24911dae351bd15b0da38ac9ab5475c83d36f43362b07224b744f1d55d6b0a32`；本次 `app/model.py` SHA-256：`322936c59efb86bb07b92f890953c6671d738cfdba65808aad9e111b2ebe58a0`。原 12:04 哈希清单保留为当时证据，不作覆盖。

本次仅重跑本机受控浏览器验证，未重跑 Linux 安装或远端 CI，未发起收费模型请求；不能以此替代新补丁的真实模型验收。远端历史失败状态保持不变。
