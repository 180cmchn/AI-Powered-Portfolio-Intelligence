"""Deterministic real PDF parsing + fake HTTP; these are not live model tests."""
import asyncio
import io

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app import providers


def pdf_bytes(page_count=35, long_page=None, blank_page=None):
    writer = PdfWriter()
    writer.add_metadata({"/Title": "Annual Financial Report", "/Author": "Test issuer"})
    for number in range(1, page_count + 1):
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        value = f"Page {number} revenue 1234.56 report-period 2026-H1"
        if number == 29:
            value += " parent-net-profit 44516880421.86"
        if number == long_page:
            value += " abcdef" * 600
        if number == blank_page:
            value = ""
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 50 700 Td ({value}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def clean_document_cache():
    providers._DOCUMENT_CACHE.clear()
    yield
    providers._DOCUMENT_CACHE.clear()


@pytest.mark.asyncio
async def test_attachment_to_long_pdf_with_cache_page_29_and_hash(monkeypatch):
    document = pdf_bytes()
    calls = []
    async def fetch(url, config):
        calls.append(url)
        if url.endswith("announcement.html"):
            return b'<title>Announcement</title><article>Annual results <a href="../files/report.pdf">Full report</a></article>', "text/html", "https://issuer.example/notices/announcement.html"
        return document, "application/pdf", url
    monkeypatch.setattr(providers, "_fetch_public", fetch)
    listing = await providers.read_url("https://issuer.example/notices/announcement.html", {})
    attachment = listing["attachments"][0]["url"]
    assert attachment == "https://issuer.example/files/report.pdf"
    info = await providers.pdf_metadata(attachment, {})
    assert info["access"] == "metadata"
    assert info["page_count"] == 35
    assert info["title"] == "Annual Financial Report"
    found = await providers.search_pdf(attachment, "parent-net-profit", {})
    assert found["matches"][0]["page"] == 29
    assert found["matches"][0]["source_url"].endswith("#page=29")
    assert found["search_complete"] is True
    read = await providers.read_url(attachment, {}, start_page=29, end_page=29)
    assert "44516880421.86" in read["text"]
    assert read["pages"][0]["page"] == 29
    assert read["returned_range"] == {"start_page": 29, "end_page": 29}
    assert read["access"] == "excerpt"
    assert read["truncated"] is True
    assert read["cache_hit"] is True
    assert read["document_id"] == info["document_id"] == found["document_id"]
    assert read["fetched_at"] == info["fetched_at"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_default_first_twenty_not_full_text_and_range_bound(monkeypatch):
    async def fetch(url, config):
        return pdf_bytes(), "application/pdf", url
    monkeypatch.setattr(providers, "_fetch_public", fetch)
    result = await providers.read_url("https://issuer.example/report.pdf", {})
    assert result["returned_range"]["end_page"] == 20
    assert result["next_page"] == 21
    assert result["access"] == "excerpt"
    result = await providers.read_url("https://issuer.example/report.pdf", {"read_max_pdf_pages": 3}, start_page=22, end_page=30)
    assert [page["page"] for page in result["pages"]] == [22, 23, 24]
    assert result["next_page"] == 25


@pytest.mark.asyncio
async def test_page_character_chunks_do_not_mislabel_full_text(monkeypatch):
    async def fetch(url, config):
        return pdf_bytes(page_count=1, long_page=1), "application/pdf", url
    monkeypatch.setattr(providers, "_fetch_public", fetch)
    first = await providers.read_url("https://issuer.example/long-page.pdf", {}, page=1, limit=1000)
    second = await providers.read_url("https://issuer.example/long-page.pdf", {}, page=1, offset=first["next_offset"], limit=1000)
    assert len(first["text"]) == len(second["text"]) == 1000
    assert first["pages"][0]["truncated"]
    assert second["pages"][0]["text_start"] > 0
    assert second["access"] == "excerpt"
    assert sum(len(page["text"]) for page in first["pages"]) <= 1000


@pytest.mark.asyncio
async def test_search_scan_bound_paginates_past_twenty(monkeypatch):
    async def fetch(url, config):
        return pdf_bytes(), "application/pdf", url
    monkeypatch.setattr(providers, "_fetch_public", fetch)
    first = await providers.search_pdf("https://issuer.example/report.pdf", "parent-net-profit", {"read_search_max_pages": 20})
    assert first["matches"] == []
    assert not first["search_complete"]
    assert first["next_page"] == 21
    second = await providers.search_pdf("https://issuer.example/report.pdf", "parent-net-profit", {}, start_page=first["next_page"])
    assert second["matches"][0]["page"] == 29
    assert second["searched_range"] == {"start_page": 21, "end_page": 35}
    assert not second["search_complete"]  # earlier pages weren't part of this read


@pytest.mark.asyncio
async def test_blank_page_does_not_claim_full_text(monkeypatch):
    async def fetch(url, config):
        return pdf_bytes(2, blank_page=2), "application/pdf", url
    monkeypatch.setattr(providers, "_fetch_public", fetch)
    read = await providers.read_url("https://issuer.example/scan.pdf", {})
    assert read["access"] == "excerpt"
    assert read["incomplete_text"] is True
    search = await providers.search_pdf("https://issuer.example/scan.pdf", "absent", {})
    assert search["pages_without_text"] == [2]
    assert not search["search_complete"]


@pytest.mark.asyncio
async def test_refresh_changes_version_and_cache_keeps_original_fetch(monkeypatch):
    calls = 0
    async def fetch(url, config):
        nonlocal calls
        calls += 1
        return f"<article>version {calls}</article>".encode(), "text/html", url
    monkeypatch.setattr(providers, "_fetch_public", fetch)
    first = await providers.read_url("https://issuer.example/update", {})
    cached = await providers.read_url("https://issuer.example/update", {})
    fresh = await providers.read_url("https://issuer.example/update", {}, refresh=True)
    assert cached["text"] == first["text"]
    assert first["document_id"] != fresh["document_id"]
    assert calls == 2
    assert not fresh["cache_hit"]


@pytest.mark.asyncio
async def test_concurrent_agents_share_one_download(monkeypatch):
    calls = 0
    async def fetch(url, config):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return pdf_bytes(), "application/pdf", url
    monkeypatch.setattr(providers, "_fetch_public", fetch)
    results = await asyncio.gather(providers.pdf_metadata("https://issuer.example/report.pdf", {}),
                                   providers.read_url("https://issuer.example/report.pdf", {}, page=29))
    assert calls == 1
    assert results[0]["document_id"] == results[1]["document_id"]


def test_base_relative_attachment_and_unsafe_schemes():
    result = providers._extract_document(b'<base href="/filings/2026/"><article>Report <a href="../annual.pdf">download</a><a href="javascript:alert(1)">bad</a><a href="https://name:pass@example.com/private">bad</a></article>', "text/html", {}, url="https://issuer.example/index")
    assert result["attachments"][0]["url"] == "https://issuer.example/filings/annual.pdf"
    assert len(result["links"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{"page": 0}, {"start_page": 0}, {"page": 200}, {"start_page": 12, "end_page": 11}, {"offset": -1}])
async def test_invalid_ranges_fail_explicitly(monkeypatch, options):
    async def fetch(url, config):
        return pdf_bytes(), "application/pdf", url
    monkeypatch.setattr(providers, "_fetch_public", fetch)
    result = await providers.read_url("https://issuer.example/report.pdf", {}, **options)
    assert result["access"] == "failed"
    assert result["error"]


def test_html_character_pagination():
    body = ("<article>" + "x" * 5000 + "</article>").encode()
    result = providers._extract_document(body, "text/html", {}, offset=4000, limit=1000)
    assert result["access"] == "excerpt"
    assert result["next_offset"] is None
    assert result["text_length"] == 5000


@pytest.mark.asyncio
async def test_tighter_byte_limit_cannot_reuse_oversized_cache(monkeypatch):
    async def fetch(url, config):
        return b"<article>" + b"x" * 11000 + b"</article>", "text/html", url
    monkeypatch.setattr(providers, "_fetch_public", fetch)
    first = await providers.read_url("https://issuer.example/big.html", {"read_max_bytes": 20000})
    second = await providers.read_url("https://issuer.example/big.html", {"read_max_bytes": 10000})
    assert first["access"] == "full_text"
    assert second["access"] == "failed"
    assert second["text"] is None


@pytest.mark.asyncio
async def test_refresh_failure_never_returns_prior_text_as_fresh(monkeypatch):
    calls = 0
    async def fetch(url, config):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("unreachable")
        return b"<article>old filing</article>", "text/html", url
    monkeypatch.setattr(providers, "_fetch_public", fetch)
    first = await providers.read_url("https://issuer.example/filing.html", {})
    second = await providers.read_url("https://issuer.example/filing.html", {}, refresh=True)
    assert first["access"] == "full_text"
    assert second["access"] == "failed"
    assert second["text"] is None
    assert second["error"]
