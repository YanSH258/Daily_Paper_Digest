import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from core.fetcher import JournalFetcher
from core.tracking import collect_tracking_articles
from integrations import openalex


class SourceSyncP0Tests(TestCase):
    def test_rss_empty_feed_success_but_network_failure_fails(self):
        f = JournalFetcher({'journals': [{'id': 1, 'name': 'X', 'rss': 'http://x'}]})
        empty = Mock(entries=[])
        with patch.object(f, '_fetch_rss', return_value=empty):
            self.assertEqual(f.fetch_all(), [])
        self.assertTrue(f.source_results[0]['success'])
        with patch.object(f, '_fetch_rss', side_effect=RuntimeError('offline')):
            self.assertEqual(f.fetch_all(), [])
        self.assertFalse(f.source_results[0]['success'])

    @patch('integrations.openalex._get')
    def test_openalex_first_window_to_date_and_missing_date(self, get):
        get.return_value = {'results': [{'title': 'No date', 'publication_date': '', 'publication_year': 2026}], 'meta': {}}
        f = JournalFetcher({'_run_date': '2026-09-13', 'journals': [{'source_type': 'openalex', 'query': 'chem'}], 'fetcher': {'date_filter_days': 2}})
        rows = f.fetch_all()
        params = get.call_args.args[1]
        self.assertIn('from_publication_date:2026-09-12', params['filter'])
        self.assertIn('to_publication_date:2026-09-13', params['filter'])
        self.assertEqual(rows[0]['pub_date'], '')
        self.assertTrue(rows[0].get('quarantine'))

    @patch('integrations.openalex._get')
    def test_openalex_journal_name_prefers_work_metadata(self, get):
        get.return_value = {'results': [{'title': 'Paper', 'doi': 'https://doi.org/10/x',
                                        'publication_date': '2026-09-13',
                                        'primary_location': {'source': {'display_name': 'Real Journal'},
                                                            'landing_page_url': 'https://example.test'},
                                        'authorships': []}], 'meta': {}}
        f = JournalFetcher({'_run_date': '2026-09-13', 'journals': [{'id': 7, 'name': 'OpenAlex 检索: chem',
                         'source_type': 'openalex', 'query': 'chem'}], 'fetcher': {'date_filter_days': 3}})
        rows = f.fetch_all()
        self.assertEqual(rows[0]['journal'], 'Real Journal')
        get.return_value['results'][0]['primary_location']['source'] = None
        rows = f.fetch_all()
        self.assertEqual(rows[0]['journal'], 'OpenAlex 检索: chem')

    @patch('integrations.openalex._get')
    def test_openalex_page_at_limit_is_truncated(self, get):
        get.return_value = {'results': [{'title': 'x'}] * 100, 'meta': {'next_cursor': 'abc'}}
        rows, meta = openalex.search_works_page('chem', limit=100)
        self.assertEqual(len(rows), 100)
        self.assertTrue(meta['truncated'])
        self.assertFalse(meta['complete'])
        self.assertEqual(meta['next_cursor'], 'abc')

    @patch('integrations.openalex.time.sleep')
    @patch('integrations.openalex._session.get')
    def test_retry_after_sleep_is_capped(self, get, sleep):
        from integrations import openalex as oa
        old = oa._cooldown_until
        oa._cooldown_until = 0.0
        try:
            get.side_effect = [Mock(status_code=429, headers={'Retry-After': '7200'}),
                               Mock(status_code=200, json=lambda: {})]
            oa._get('/works', {})
            waited = [c.args[0] for c in sleep.call_args_list]
            self.assertTrue(waited, 'expected capped backoff sleeps')
            self.assertLessEqual(max(waited), oa._MAX_COOLDOWN_SECONDS + 1)
        finally:
            oa._cooldown_until = old

    def test_batch_fast_fail_skips_remaining_openalex_during_cooldown(self):
        from integrations import openalex as oa
        old = oa._cooldown_until
        oa._cooldown_until = __import__('time').monotonic() + 120
        try:
            f = JournalFetcher({'journals': [
                {'id': 1, 'name': 'OA1', 'source_type': 'openalex', 'query': 'x'},
                {'id': 2, 'name': 'OA2', 'source_type': 'openalex', 'query': 'y'}]})
            with patch.object(JournalFetcher, '_fetch_openalex_source', side_effect=AssertionError('must not fetch')):
                f.fetch_all()
            self.assertEqual(len(f.source_results), 2)
            self.assertFalse(any(r['success'] for r in f.source_results))
            self.assertIn('cooldown', f.source_results[1]['error'])
        finally:
            oa._cooldown_until = old

    def test_tracking_failure_and_truncation_do_not_advance(self):
        db = Mock()
        db.get_watched_seeds.return_value = [{'id': 1, 'doi': '10/x', 'title': 'seed'}]
        db.list_watch_authors.return_value = []
        with patch('integrations.openalex.get_citing_works', side_effect=RuntimeError('fail')):
            _, meta = collect_tracking_articles({'tracking': {'enabled': True}}, db)
        self.assertNotIn(1, meta.get('seed_windows', {}))
        with patch('integrations.openalex.get_citing_works', return_value=([], False)):
            _, meta = collect_tracking_articles({'tracking': {'enabled': True}}, db)
        self.assertNotIn(1, meta.get('seed_windows', {}))

    def test_author_uses_last_run_window(self):
        db = Mock()
        db.get_watched_seeds.return_value = []
        db.list_watch_authors.return_value = [{'id': 4, 'name': 'A', 'openalex_id': 'A1', 'last_run': '2026-09-10'}]
        with patch('integrations.openalex.get_author_recent_works', return_value=[]) as get:
            collect_tracking_articles({'tracking': {'enabled': True, 'author_check_days': 3}}, db)
        self.assertEqual(get.call_args.kwargs['from_date'], '2026-09-10')
