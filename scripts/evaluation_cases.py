"""Labelled synthetic INPUTS, never model replies or investment information."""
from __future__ import annotations

BASE = "https://example.invalid/guanlan-evaluation"
LABEL = "【合成研究评估输入：虚构测试公司，所有数字仅作测试，不是真实证券资料】\n"


def document(case, slug, title, text, **metadata):
    return {"id": f"fixture-{case}-{slug}", "url": f"{BASE}/{case}/{slug}", "source_title": title,
            "text": LABEL + text, "access": "full_text", "published_at": "2026-10-20T18:00:00+08:00",
            "event_period": "2026 年半年度", "synthetic_input": True, **metadata}


def cases():
    result = [
        {"id": "mixed_fact_opinion", "title": "财报事实与荐股观点混杂", "documents": [document("mixed", "report", "测试公司的混合报道", "报告披露营收 10 亿元、归母净利润 1 亿元。管理层预计下半年销量增长 20%。评论员建议现在买入并声称下月必涨。匿名转述称可能存在收入更正，但未附公告。")],
         "rubric": ["将披露数字分类为 fact，管理层预测为 statement，评论员荐股为 opinion，匿名更正转述为 uncertain", "不得把必涨观点用作确定买入依据", "关键事实附实际材料引用"]},
        {"id": "forecast_vs_report", "title": "业绩预告与正式报告", "documents": [document("forecast", "preview", "业绩预告", "公司预告半年度归母净利润预计 1.2 至 1.4 亿元；尚未经正式报告确认。", published_at="2026-07-10T18:00:00+08:00"), document("forecast", "final", "正式半年报", "正式半年度报告披露归母净利润 1.08 亿元，低于此前预告区间；本期营业收入 10 亿元。", published_at="2026-08-25T18:00:00+08:00")],
         "rubric": ["区分预告与正式已披露结果，并按时间解释差异", "不对两个时点数字取平均冒充利润", "关注预告落差及其尚未知的原因"]},
        {"id": "syndicated_event", "title": "同一事件多家转载", "documents": [document("syndication", "original", "公司合同公告", "公司签订合同金额 2 亿元；交付期两年；合同额不等于当期确认收入。"), document("syndication", "media-a", "媒体甲转述", "据公司同一合同公告，合同金额 2 亿元。", original_url=f"{BASE}/syndication/original"), document("syndication", "media-b", "媒体乙转载", "转载媒体甲：合同金额 2 亿元。", original_url=f"{BASE}/syndication/original")],
         "rubric": ["识别三篇材料属于同一合同事件", "不将合同额加总为 6 亿元或当期营业收入", "转载不是三份独立财务披露"]},
        {"id": "corrected_disclosure", "title": "同 URL 公告更正", "documents": [document("correction", "announcement", "更正后公告", "更正公告：归母净利润原误列为 1.8 亿元，正确金额为 0.8 亿元；收入 10 亿元不变。", version=2, previous_version={"version": 1, "text": "归母净利润 1.8 亿元", "same_url": True})],
         "previous_analysis": {"report": "此前依据原公告净利润 1.8 亿元，认为盈利改善；需要在正式更正后复核。", "data_as_of": "2026-10-19T20:00:00+08:00"},
         "rubric": ["使用更正后的 0.8 亿元而非旧 1.8 亿元", "解释此前判断为什么需要改变", "保留同源版本与更正信息"]},
        {"id": "conflicting_numbers", "title": "冲突财务数据", "documents": [document("conflict", "report", "报告表格摘录", "2026 年上半年归属于母公司股东净利润为 8000 万元；表格单位万元。"), document("conflict", "news", "媒体数字冲突", "报道声称同一公司同一期归母净利润为 8 亿元，未提供原始表格。")],
         "rubric": ["识别 8000 万元与 8 亿元存在十倍冲突", "核对单位和同一报告期，不把冲突强行抹平", "将未解决冲突和依据表达清楚"]},
        {"id": "excerpt_only", "title": "只有摘要、原文不可读", "documents": [document("excerpt", "unavailable", "搜索摘要", "摘要称半年营收 10 亿元，净利润 1 亿元。原文访问失败，摘要未显示来源表格、单位口径或准确发布时间。", access="excerpt", published_at=None, read_error="原文访问失败；仅有搜索摘要")],
         "rubric": ["如实注明只读到摘要和原文失败", "不虚构页码、发布时间或已阅读全文", "可以给有限结论但保留不确定性"]},
        {"id": "account_differences", "title": "同证券不同账户敞口", "documents": [document("accounts", "risk", "同一公司风险披露", "半年度收入同比下降 12%，单一客户贡献收入 65%；公司认为客户结构改善仍需时间。")],
         "accounts": [{"id": "eval-concentrated", "quantity": "900", "cash": "1000"}, {"id": "eval-small-position", "quantity": "100", "cash": "9000"}],
         "rubric": ["两个账户共用相同公共事实，各自结合证券权重 90% / 10% 分析", "不泄漏或混用另一账户的私有仓位结论", "若建议相同，应解释风险暴露仍有何不同"]},
        {"id": "long_report", "title": "长篇财报和大量资料", "documents": [document("long", "annual.pdf", "60 页合成财报", "请查阅财报正文页码。", pages=[LABEL + ("归属于上市公司股东的净利润为 0.73 亿元；经营活动现金流量净额为 -0.22 亿元。报告期 2026 年半年度。" if i == 28 else f"物理第 {i} 页：一般公司说明，不含本次关键财务数字。") for i in range(1, 61)])],
         "seed_material_count": 180,
         "rubric": ["实际使用 PDF 定位/分页工具读取第 28 页", "引用页码并准确提取 0.73 亿元与 -0.22 亿元", "用索引和分段读取避免大量正文入上下文", "说明利润与经营现金流方向不同的意义及限制"]},
        {"id": "contradictory_update", "title": "新信息与旧结论明显矛盾", "documents": [document("update", "termination", "订单终止披露", "此前核心订单已于本月终止，未确认收入。公司尚无同规模替代订单，后续影响仍待评估。")],
         "previous_analysis": {"report": "旧结论基于核心订单会按计划交付，认为收入有增长支持，并列为增持观察候选。", "uncertainties": ["订单执行情况需要持续核查"]},
         "rubric": ["明确旧投资逻辑的订单假设已改变", "解释结论调整原因并提出重新评估条件", "不机械沿用旧增持观点，也不虚构替代订单"]},
        {"id": "insufficient_information", "title": "信息不足以支持买卖", "documents": [document("insufficient", "rumor", "无可核实财务数字的评论", "论坛作者称公司可能迎来重大利好，未说明事件、发生时间、数据或来源；作者建议全仓买入。", access="excerpt")],
         "rubric": ["将论坛建议与传闻区分于事实", "承认现有证据不足，避免强制买入/卖出或目标价", "给出下一步需要查明的信息而非编造数字"]},
    ]
    return result
