# 第十六轮认证拒绝与配置恢复

独立复核发现：原先 401/403 虽然不在 HTTP 客户端的重试状态列表中，但作为普通 `ModelError` 使任务进入 failed；之后的新定时需求可能对相同被拒配置再次发请求。这与参数配置错误已有的 waiting_config 门控不同。

最小修复仅修改 `app/model.py` 的 HTTP 错误分类：401/403 抛 `ModelConfigurationError`，仍使用 `upstream_http_401` / `upstream_http_403` 错误码，诊断记录 `retry_suppressed=authentication_configuration_rejected`。错误正文只按 HTTP 状态生成；供应商响应体、认证头和凭据不会进入错误信息。客户端不删除参数、不改模型、不切路线、不重新发送。

普通账户和公共研究因此沿用既有持久 waiting_config 机制。重复 tick、重启、无关设置保存以及后续同配置定时需求均不重发。供应商凭据保存产生新 credential_revision 后，通过配置指纹门控正常恢复。原任务被冻结的需求范围不会因此扩展；模型没有看到的新需求仍如实记 uncovered，不假装已覆盖，不自动复活旧范围。

## 受控验证

[test_sixteenth_authentication.py](../../tests/test_sixteenth_authentication.py) 使用真实 ModelClient → Agent → Service → Store / SQLite，外部模型 HTTP、目录与凭据服务使用明确夹具。它不是第三方供应商认证实测。

- Responses 和 Chat Completions 各验证 401/403：配置错误分类、原始状态码、已拒绝状态、一次 HTTP、敏感内容不持久化。
- 每种状态码都运行账户与公共任务；重复 tick、无关保存、重建 Store / Service 和三个新定时窗口后，仍只有最初两个 HTTP 尝试，所有新增需求保留在等待配置任务中。
- 通过生产供应商保存路径轮换隔离凭据，确认修订递增、账户原任务恢复、公共旧等待任务被新配置任务接替。随后合法新定时事件正常完成；先前冻结范围之外的需求保留真实未覆盖状态。

定向命令：

```text
.venv/bin/python -m pytest -q tests/test_sixteenth_authentication.py tests/test_fifth_reasoning_effort.py tests/test_fourteenth_model.py tests/test_agents.py
```

最后结果：**131 passed，1 warning，4.19s**，见 [authentication-final-v2.txt](../../test-results/sixteenth/authentication-final-v2.txt)。warning 是既有 Starlette TestClient 的 HTTPX 弃用提示。该定向结果与最终全量、浏览器及重复执行的数量分开报告。

## 保留的开发失败

- [authentication-first.txt](../../test-results/sixteenth/authentication-first.txt)：6 failed。四个独立客户端夹具遗漏测试 Key，构造阶段正确拒绝远程无凭据；两个 Service 测试错误要求恢复时覆盖冻结范围之外的新需求。生产认证分类本身已按预期进入 waiting_config。
- [authentication-second.txt](../../test-results/sixteenth/authentication-second.txt)：4 passed、2 failed。修复 Key 夹具后，仍把范围外需求预期为 pending；实际正确状态为 uncovered，等待明确后续操作。
- [authentication-final.txt](../../test-results/sixteenth/authentication-final.txt) 与 [authentication-final-pass.txt](../../test-results/sixteenth/authentication-final-pass.txt)：各 129 passed、2 failed。文件名中的 final / pass 是当时命名，不代表通过。最后新窗口只产生公共 HTTP，因为初始夹具只授权一次现金任务，后续普通证券任务没有持仓或候选而正确进入 waiting_scope；推进时钟不会改变其合法范围。
- 最后通过版本为夹具在开始前通过生产 universe 保存路径建立明确候选范围，再验证新定时需求。没有改生产范围校验、需求冻结、时间限制或执行归属以通过测试。

所有日志保留原身份，不覆盖失败。最终源码摘要、全量和真实业务结果由本轮总记录引用。
