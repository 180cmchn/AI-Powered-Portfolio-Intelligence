# 第十四轮生产回归对应关系

这些测试是受控生产链验证，不等于真实供应商/语义质量通过。最终运行结果以 validation-manifest.json 与日志为准。

| 场景 | 对应生产回归与断言 |
|---|---|
| completed仅流程句、无预算 | test_fourteenth_chain::test_plain_completed_process_message_fails_through_api_without_budget；failed/error_code落库、API无报告、候选事件保留 |
| 正文普通消息后补交、短现金报告 | test_complete_plain_draft_requires_explicit_correction_then_short_cash_report_publishes；实际次请求保留草稿、指定finish、短正文发布 |
| 缺字段/截断JSON/混批/重复终止 | test_invalid_terminal_is_corrected_without_partial_publication；不执行混批研究、不产生半份、多份报告 |
| 双协议函数选择/拒绝/忽略 | test_fourteenth_model；实际payload分别映射，模型effort保持，拒绝不删参重试，忽略落提交失败 |
| 明确incomplete/length、空响应、重复call ID | test_known_incomplete_final_is_corrected_through_production_publication及test_empty_or_duplicate_final_corrects_once_through_service_and_store；半截历史不重放、两次内纠正 |
| 缺SSE终止、请求/整轮期限 | test_missing_final_stream_terminal_does_not_spend_a_correction_request、test_final_endpoint_failure_remains_visible_and_bounded及原eleventh_timeouts；保留真实失败原因、无伪报告 |
| 正式批次执行截止 | test_fourteenth_deadlines覆盖主任务、补充任务、assigned sample与多批次：已截止零HTTP，飞行中取消，正式/整轮/请求最早期限生效；材料与旧报告保留，missed不能补填；普通账户和旧exploratory不误拦 |
| 有限报告/有理由弃权 | chain与publication及原tenth_tasks/sixth_forecasts；合法scope发布、未覆盖与弃权区分 |
| 只有一份子报告 | test_single_child_is_material_for_main_not_automatic_account_report；主报告仍须独立finish |
| 首次有效提交与迟到子成果 | test_fourth_late_child与test_ninth_calculation_context；剩余调用不强制使用，首次接受即停止，迟到报告/计算保留且明确未吸收，在途子任务取消并保存实际状态；普通草稿及引用反馈仍可必要补交 |
| 三子任务争最后调用 | test_fourteenth_runtime::test_concurrent_children_cannot_take_two_final_http_attempts；真实HTTP夹具并发，4研究+2主提交/纠正≤6 |
| 成功计算仍进入实际最终请求 | test_successful_calculation_remains_visible_in_actual_final_request及原ninth_calculation_context/thirteenth_recovery；可见摘要与来源身份保留 |
| 同版本刷新失败与旧报告 | test_failed_refresh_keeps_current_and_saves_visible_draft覆盖manual/scheduled/forecast；旧有效current不撤销 |
| 已失效结果不复活 | test_failed_delivery_never_revives_invalidated_report及test_fact_change_invalidation_is_not_undone_by_failed_refresh；版本/事实更新仍stale |
| scope、账户与材料访问边界 | test_full_scope_cannot_omit_requested_security、test_reference_resolution_discards_forged_private_access；身份服务端绑定 |
| 有限scope不更新别的证券/账户需求 | test_limited_scope_does_not_revise_other_security_suggestions、test_limited_security_scope_cannot_complete_account_wide_demand |
| 原子性与幂等/重启 | test_report_forecasts_and_suggestions_share_rollback、test_public_report_material_rolls_back_with_publication、test_duplicate_after_restart_uses_receipt_and_never_republishes |
| 多目标方向/弃权/未覆盖/截止 | 原sixth_forecasts/eighth_forecast_preparation/seventh_window_api/tenth_tasks保持；forecast_coverage单列 |
| 研究期间账务/配置版本变化 | 原third_consistency/fourth_recovery/sixth_runtime及thirteenth_ledger保持；新旧发布隔离，不重记交易 |
| 历史纯文本/演示模式 | test_legacy_plain_text_remains_readable、test_demo_uses_explicit_local_delivery_without_fabricated_model_submission；demo不假扮finish |
| 提示词迁移 | test_prompt_migration_only_exact_prior_builtins_and_custom_diff；旧内置精确哈希、用户编辑保留、重复启动幂等 |
| 浏览器与旧账本/阅读状态 | 原十组保留，第十一组frontend_fourteenth验证API错误、有限正文/缺口、旧current和重试入口；thirteenth精确成本/费用/可卖校正用例全部保留 |
| 真实验证闸与内容分层 | 固定证据CLI输入同一性、默认0授权、总44/阶段4+40共享原子闸、正文hash内容核对与源码一致门槛；未评估内容不得总体验收通过 |

旧用例的变化是把原合法终止fixture显式补齐契约，及把原“普通文字结束成功”改为候选后显式提交；没有全局把任意文本转换为报告的测试替身。预算用例因最终保留2次、一次纠正上限调整期望，仍检查实际HTTP分母与不发布行为。目录API测试的缓存目录为明确受控fixture，不把网络失败当业务失败。

旧“迟到成果再请求一次最终更新”预期改为首次有效提交即结束，以落实本轮明确要求；迟到成果、计算、并发第二子任务与错误场景均保留覆盖。检查点用例显式给 ResearchTools 传入真实 context，沿生产持久化路径验证，没有放宽存储准入。
