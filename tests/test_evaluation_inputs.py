"""Validate labelled evaluation inputs/tool access, not any model output."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_model.py"
SPEC = importlib.util.spec_from_file_location("evaluate_model", SCRIPT)
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)


def test_ten_distinct_cases_have_explicit_rubrics_and_synthetic_sources():
    cases = evaluation.cases()
    assert len(cases) == len({case["id"] for case in cases}) == 10
    for case in cases:
        assert len(case["rubric"]) >= 3
        assert all(doc["synthetic_input"] and "合成" in doc["text"] for doc in case["documents"])
    accounts = next(case for case in cases if case["id"] == "account_differences")["accounts"]
    assert len(accounts) == 2 and accounts[0]["quantity"] != accounts[1]["quantity"]


@pytest.mark.asyncio
async def test_long_input_supports_page_28_and_bounded_material_index():
    case = next(case for case in evaluation.cases() if case["id"] == "long_report")
    tools = evaluation.EvaluationTools({}, case)
    url = case["documents"][0]["url"]
    assert (await tools.pdf_metadata(url))["page_count"] == 60
    located = await tools.search_pdf(url, "归属于上市公司股东的净利润")
    assert located["matches"][0]["page"] == 28
    page = await tools.read(url, start_page=28, end_page=28)
    assert "0.73" in page["pages"][0]["text"] and "-0.22" in page["pages"][0]["text"]
    assert page["truncated"] and page["access"] == "partial"
    indexes = await tools.list_materials(limit=10, offset=160)
    assert len(indexes) == 10 and indexes[0]["id"] == "seed-160"
    assert all("text" not in item for item in indexes)
    material = await tools.read_material_range("seed-160", start=500, length=100)
    assert material["start"] == 500 and material["end"] == 600 and material["truncated"]


@pytest.mark.asyncio
async def test_unreadable_source_keeps_excerpt_and_read_failure_distinct():
    case = next(case for case in evaluation.cases() if case["id"] == "excerpt_only")
    tools = evaluation.EvaluationTools({}, case)
    search = await tools.search("半年报")
    assert search["results"][0]["access"] == "excerpt"
    read = await tools.read(search["results"][0]["url"])
    assert read["access"] == "failed" and read["text"] is None
    with pytest.raises(ValueError):
        await tools.read("https://example.org/not-a-fixture")
