"""Compare the requested output limit with numeric provider usage, without guessing."""
import httpx
import pytest

from app.model import ModelClient


@pytest.mark.parametrize("usage,expected", [
    ({"output_tokens": 13303}, True),
    ({"output_tokens": 3000}, False),
    ({"output_tokens": 2500}, False),
    ({"output_tokens": "13303"}, None),
    ({"output_tokens": True}, None),
    ({"input_tokens": 1000}, None),
    (None, None),
])
@pytest.mark.asyncio
async def test_output_limit_diagnostic_is_only_based_on_numeric_reported_usage(monkeypatch, usage, expected):
    monkeypatch.setenv("OUTPUT_DIAGNOSTIC_KEY", "controlled-test-key")
    config = {"model_name": "diagnostic-fixture", "model_base_url": "https://model.example/v1",
              "model_key_env": "OUTPUT_DIAGNOSTIC_KEY", "model_protocol": "responses",
              "model_native_search": False, "model_retries": 0, "max_output_tokens": 3000}
    body = {"status": "completed", "output": [{"type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": "Visible final report"}]}]}
    if usage is not None:
        body["usage"] = usage
    client = ModelClient(config, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
    try:
        result = await client.complete(client.session("system", {"task": "diagnostic"}), [])
        observation = client.requests[0]
        assert result.text == "Visible final report"
        assert observation["requested_max_output_tokens"] == 3000
        if expected is None:
            assert "reported_output_exceeds_requested" not in observation
        else:
            assert observation["reported_output_exceeds_requested"] is expected
        assert observation["status_code"] == 200 and observation["error"] is None
    finally:
        await client.aclose()
