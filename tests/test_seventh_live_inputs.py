"""No network: exact credential destinations and real-directory selection boundary."""
import httpx
import pytest

from scripts.verify_seventh_live import validate_provider_destination, validate_request_destination, select_directory_models


@pytest.mark.parametrize('url', ['http://api.xxyapi.com/v1', 'https://api.xxyapi.com.evil.example/v1',
    'https://api.xxyapi.com/v1?redirect=elsewhere', 'https://user:pass@api.xxyapi.com/v1', 'https://api.xxyapi.com:8443/v1'])
def test_evaluator_rejects_nonexact_authorized_config(url):
    with pytest.raises(ValueError):
        validate_provider_destination(url, 'https://api.xxyapi.com/v1')


@pytest.mark.parametrize('url', ['https://elsewhere.example/v1/responses', 'http://api.xxyapi.com/v1/responses',
    'https://api.xxyapi.com/private/responses', 'https://api.xxyapi.com:8443/v1/responses'])
def test_evaluator_fences_actual_key_bearing_request(url):
    request = httpx.Request('POST', url, headers={'authorization': 'Bearer controlled-not-real'})
    with pytest.raises(ValueError):
        validate_request_destination(request, 'api.xxyapi.com')


def test_evaluator_can_only_select_actual_directory_ids():
    directory = {'data': [{'id': 'actual-a'}, {'id': 'actual-b'}, {'id': None}, 'bad']}
    assert select_directory_models(directory, 'actual-a', 'actual-b') == (['actual-a', 'actual-b'], ['actual-a', 'actual-b'])
    assert select_directory_models(directory, 'actual-a')[1] == ['actual-a', 'actual-a']
    with pytest.raises(ValueError, match='absent'):
        select_directory_models(directory, 'invented')
    validate_request_destination(httpx.Request('GET', 'https://issuer.example/report'), 'api.xxyapi.com')


@pytest.mark.parametrize('report', ['占位报告', '测试占位', '测试占位报告：尚未完成', '测试报告占位', '  测试占位'])
def test_evaluator_records_placeholder_variants(report):
    from scripts.verify_seventh_live import placeholder_report
    assert placeholder_report(report)


@pytest.mark.parametrize('report', ['', None, '机器人 ETF 研究报告', '研究说明：存在占位数据，应保留缺口'])
def test_evaluator_placeholder_annotation_does_not_invent_content_policy(report):
    from scripts.verify_seventh_live import placeholder_report
    assert not placeholder_report(report)
