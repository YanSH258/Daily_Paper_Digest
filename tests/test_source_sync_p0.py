import sys
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from core.fetcher import JournalFetcher
from main import dedupe_batch
from core.tracking import collect_tracking_articles
from integrations import openalex


class SourceSyncP0Tests(TestCase):
    def setUp(self):
        # Module-level cooldowns are process state; never leak between tests.
        from integrations import arxiv, openalex
        for module in (arxiv, openalex):
            module._cooldown_until = 0.0

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
            self.assertLessEqual(max(waited), oa.MAX_COOLDOWN_SECONDS + 1)
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
            self.assertIn('openalex rate-limit cooldown', f.source_results[1]['error'])
        finally:
            oa._cooldown_until = old

    def test_arxiv_uses_rss_while_api_cools_down(self):
        """Query-API cooldown must not disable the (static) RSS feed."""
        import feedparser
        from integrations import arxiv
        old = arxiv._cooldown_until
        arxiv._cooldown_until = __import__('time').monotonic() + 120
        try:
            f = JournalFetcher({'journals': [
                {'id': 3, 'name': 'AX1', 'source_type': 'arxiv', 'query': 'cat:cond-mat.mtrl-sci'},
                {'id': 4, 'name': 'AX2', 'source_type': 'arxiv', 'query': 'cat:cond-mat.mtrl-sci'}]})
            empty = feedparser.parse(b'<rss version="2.0"><channel><title>t</title></channel></rss>')
            with patch('integrations.arxiv.fetch_rss', return_value=empty) as rss, \
                 patch('integrations.arxiv.fetch_feed',
                       side_effect=AssertionError('query API must not be called')) as feed:
                f.fetch_all()
            self.assertTrue(rss.called)
            feed.assert_not_called()
            self.assertEqual(len(f.source_results), 2)
            self.assertTrue(all(r['success'] for r in f.source_results))
        finally:
            arxiv._cooldown_until = old

    def test_arxiv_api_fallback_skips_when_cooling_down(self):
        from integrations import arxiv
        old = arxiv._cooldown_until
        arxiv._cooldown_until = __import__('time').monotonic() + 120
        try:
            f = JournalFetcher({'journals': [
                {'id': 3, 'name': 'AX1', 'source_type': 'arxiv', 'query': 'cat:x'},
                {'id': 5, 'name': 'AX5', 'source_type': 'arxiv', 'query': 'cat:y'}]})
            with patch('integrations.arxiv.fetch_rss', side_effect=RuntimeError('rss down')), \
                 patch('integrations.arxiv.fetch_feed',
                       side_effect=AssertionError('must not fetch during cooldown')) as feed:
                f.fetch_all()
            feed.assert_not_called()
            self.assertEqual(len(f.source_results), 2)
            self.assertFalse(any(r['success'] for r in f.source_results))
            self.assertIn('rate-limit cooldown', f.source_results[1]['error'])
        finally:
            arxiv._cooldown_until = old

    def test_arxiv_rss_single_request_per_category_and_replace_dropped(self):
        import feedparser
        xml = b"""<rss version="2.0" xmlns:arxiv="http://arxiv.org/schemas/atom" xmlns:dc="http://purl.org/dc/elements/1.1/">
        <channel><title>cond-mat.mtrl-sci</title>
        <item><title>Fresh MLIP paper</title><link>https://arxiv.org/abs/2609.00001</link>
          <pubDate>Tue, 15 Sep 2026 00:00:00 -0400</pubDate><dc:creator>A. Author</dc:creator>
          <arxiv:announce_type>new</arxiv:announce_type>
          <description>arXiv:2609.00001v1 Announce Type: new Abstract: A machine learning potential study of interfaces.</description></item>
        <item><title>Old paper replaced</title><link>https://arxiv.org/abs/2001.00002</link>
          <pubDate>Tue, 15 Sep 2026 00:00:00 -0400</pubDate><dc:creator>B. Author</dc:creator>
          <arxiv:announce_type>replace</arxiv:announce_type>
          <description>arXiv:2001.00002v3 Announce Type: replace Abstract: Updated version of an old paper.</description></item>
        <item><title>Unrelated catalysis work</title><link>https://arxiv.org/abs/2609.00003</link>
          <pubDate>Tue, 15 Sep 2026 00:00:00 -0400</pubDate><dc:creator>C. Author</dc:creator>
          <arxiv:announce_type>new</arxiv:announce_type>
          <description>arXiv:2609.00003v1 Announce Type: new Abstract: A study of solid catalysts.</description></item>
        </channel></rss>"""
        f = JournalFetcher({'_run_date': '2026-09-15', 'fetcher': {'date_filter_days': 3}, 'journals': [
            {'id': 35, 'name': 'AX-cat', 'source_type': 'arxiv', 'query': 'cat:cond-mat.mtrl-sci'},
            {'id': 36, 'name': 'AX-kw', 'source_type': 'arxiv',
             'query': 'cat:cond-mat.mtrl-sci AND (all:"machine learning potential")'}]})
        with patch('integrations.arxiv.fetch_rss',
                   return_value=feedparser.parse(xml)) as rss, \
             patch('integrations.arxiv.fetch_feed',
                   side_effect=AssertionError('RSS path must not call the query API')):
            rows = f.fetch_all()
        self.assertEqual(rss.call_count, 1, 'one RSS request must cover both subscriptions')
        titles = sorted(r['title'] for r in rows)
        self.assertIn('Fresh MLIP paper', titles)
        self.assertIn('Unrelated catalysis work', titles)      # category subscription keeps it
        self.assertNotIn('Old paper replaced', titles)          # replace announcements dropped
        self.assertTrue(all(r['journal'] == 'cond-mat.mtrl-sci' for r in rows))
        self.assertTrue(all(r.get('date_source') == 'arxiv_rss_announcement' for r in rows))
        self.assertTrue(all(not (r.get('abstract') or '').startswith('arXiv:') for r in rows))
        self.assertEqual({r['source_id'] for r in f.source_results}, {35, 36})
        self.assertTrue(all(r['success'] for r in f.source_results))

    @patch('integrations.arxiv.time.sleep')
    @patch('integrations.arxiv._session.get')
    def test_arxiv_429_capped_backoff_then_success(self, get, sleep):
        from integrations import arxiv
        old = arxiv._cooldown_until
        old_interval, old_last = arxiv.MIN_INTERVAL_SECONDS, arxiv._last_request_at
        arxiv._cooldown_until, arxiv.MIN_INTERVAL_SECONDS, arxiv._last_request_at = 0.0, 0.0, None
        try:
            ok = Mock(status_code=200, headers={})
            ok.__enter__ = Mock(return_value=ok)
            ok.__exit__ = Mock(return_value=False)
            ok.content = b'<feed/>'
            throttled = Mock(status_code=429, headers={'Retry-After': '9000'})
            throttled.__enter__ = Mock(return_value=throttled)
            throttled.__exit__ = Mock(return_value=False)
            get.side_effect = [throttled, ok]
            feed = arxiv.fetch_feed('https://export.arxiv.org/api/query?x', timeout=5)
            self.assertEqual(len(feed.entries), 0)
            waited = [c.args[0] for c in sleep.call_args_list if c.args]
            self.assertTrue(waited)
            self.assertLessEqual(max(waited), arxiv.MAX_COOLDOWN_SECONDS + 1)
        finally:
            arxiv._cooldown_until = old
            arxiv.MIN_INTERVAL_SECONDS, arxiv._last_request_at = old_interval, old_last

    def test_source_groups_run_concurrently(self):
        import threading
        from time import sleep as real_sleep
        finished = {}
        started = threading.Event()
        f = JournalFetcher({'journals': [
            {'id': 1, 'name': 'AX', 'source_type': 'arxiv', 'query': 'cat:x'},
            {'id': 2, 'name': 'OA', 'source_type': 'openalex', 'query': 'chem'}]})
        def slow_arxiv(entries, results):
            started.set()
            real_sleep(1.0)
            finished['arxiv'] = __import__('time').monotonic()
        def fast_openalex(journal):
            if not started.wait(timeout=5):
                raise AssertionError('openalex group blocked behind arxiv group')
            finished['openalex'] = __import__('time').monotonic()
            return []
        with patch.object(JournalFetcher, '_fetch_arxiv_group', side_effect=slow_arxiv), \
             patch.object(JournalFetcher, '_fetch_openalex_source', side_effect=fast_openalex):
            f.fetch_all()
        # OpenAlex completes while arXiv is still sleeping: groups overlap.
        self.assertLess(finished['openalex'], finished['arxiv'])

    @patch('integrations.arxiv.fetch_rss', side_effect=RuntimeError('rss down'))
    @patch('integrations.arxiv.fetch_feed')
    def test_arxiv_sources_merged_into_one_request(self, fetch_feed, _rss):
        import feedparser
        fetch_feed.return_value = feedparser.parse(
            b'<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title></feed>')
        f = JournalFetcher({'_run_date': '2026-09-15', 'fetcher': {'date_filter_days': 3}, 'journals': [
            {'id': 35, 'name': 'AX1', 'source_type': 'arxiv', 'query': 'cat:cond-mat.mtrl-sci'},
            {'id': 36, 'name': 'AX2', 'source_type': 'arxiv',
             'query': 'cat:cond-mat.mtrl-sci AND all:"machine learning potential"'}]})
        f.fetch_all()
        self.assertEqual(fetch_feed.call_count, 1, 'merged arXiv sources must issue one request')
        url = fetch_feed.call_args.args[0]
        self.assertIn('%28cat%3Acond-mat.mtrl-sci%29', url)          # first query, parenthesised
        self.assertIn('%20OR%20', url)                                # OR-joined
        self.assertIn('submittedDate', url)
        self.assertEqual({r['source_id'] for r in f.source_results}, {35, 36})
        self.assertTrue(all(r['success'] for r in f.source_results))

    @patch('integrations.arxiv.time.sleep')
    @patch('integrations.arxiv._session.get')
    def test_arxiv_request_pacing_respects_min_interval(self, get, sleep):
        from integrations import arxiv
        old_interval = arxiv.MIN_INTERVAL_SECONDS
        old_last = arxiv._last_request_at
        old_cd = arxiv._cooldown_until
        arxiv.MIN_INTERVAL_SECONDS, arxiv._cooldown_until = 3.0, 0.0
        try:
            ok = Mock(status_code=200, headers={}, content=b'<feed/>')
            ok.__enter__ = Mock(return_value=ok)
            ok.__exit__ = Mock(return_value=False)
            get.return_value = ok
            arxiv.fetch_feed('https://export.arxiv.org/api/query?x', timeout=5)
            arxiv._last_request_at = __import__('time').monotonic()   # pretend just fetched
            arxiv.fetch_feed('https://export.arxiv.org/api/query?y', timeout=5)
            paced = [c.args[0] for c in sleep.call_args_list if c.args]
            self.assertTrue(any(2 < w <= 3.1 for w in paced), paced)
        finally:
            arxiv.MIN_INTERVAL_SECONDS, arxiv._last_request_at, arxiv._cooldown_until = old_interval, old_last, old_cd

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

    def test_test_feed_window_count_reports_zero_when_undated(self):
        import feedparser
        f = JournalFetcher({'fetcher': {'date_filter_days': 3}})
        undated = feedparser.parse(b'<rss version="2.0"><channel><title>x</title>'
                                   b'<item><title>a</title></item>'
                                   b'<item><title>b</title></item></channel></rss>')
        with patch.object(f, '_fetch_rss', return_value=undated):
            result = f.test_feed('http://x', 'DEFAULT')
        self.assertEqual(result['count'], 2)
        self.assertEqual(result['window_count'], 0)   # never fall back to the total

    def test_dedupe_catches_same_title_with_different_dois(self):
        from main import dedupe_batch
        first = {'doi': '10.1063/5.0329286', 'title': 'Same paper', 'url': 'https://doi.org/10.1063/5.0329286'}
        figshare = {'doi': '10.60893/figshare.apl.c.1', 'title': 'Same paper',
                    'url': 'https://doi.org/10.60893/figshare.apl.c.1'}
        self.assertEqual(len(dedupe_batch([first, figshare])), 1)
        self.assertEqual(len(dedupe_batch([figshare, first])), 1)
        unrelated = {'doi': '10.t/other', 'title': 'Different work', 'url': 'https://x/other'}
        self.assertEqual(len(dedupe_batch([first, unrelated])), 2)
