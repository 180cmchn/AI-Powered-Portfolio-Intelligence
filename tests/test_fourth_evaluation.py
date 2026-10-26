from app.store import Store
from scripts.verify_fourth_iteration import scheduled_news_demands


def test_scheduler_evidence_survives_more_than_one_page_of_discovered_sources(tmp_path):
    store = Store(tmp_path / 'evaluation.sqlite3')
    news = store.request_analysis(None, 'news_observation', 'news-observation:window', reason='Observed topic')
    run = store.claim_global()
    for index in range(130):
        store.request_analysis(None, 'material_changed', f'source:{index}', reason='New public source')
    assert news['id'] not in [row['id'] for row in store.demands()]
    assert [row['id'] for row in scheduled_news_demands(store, run['id'])] == [news['id']]
    assert scheduled_news_demands(store, 'different-run') == []
