import unittest
from datetime import date
from unittest.mock import Mock, patch
from integrations.crossref import journal_works
from core.fetcher import JournalFetcher


class CrossrefSourceTests(unittest.TestCase):
    def test_normalization_and_dates(self):
        rows=[{'DOI':'10.1/ABC','title':['A <i>paper</i>'], 'container-title':['JCP'],
               'abstract':'<jats:p>Real abstract</jats:p>', 'author':[{'given':'A','family':'B'}],
               'published-online':{'date-parts':[[2026,9,12]]},
               'published-print':{'date-parts':[[2026,10,1]]}}]
        reply=Mock();reply.json.return_value={'message':{'items':rows+rows}}
        with patch('integrations.crossref.requests.get',return_value=reply) as get:
            articles=journal_works('0021-9606',today=date(2026,9,13))
        self.assertEqual(len(articles),1)
        self.assertEqual(articles[0]['abstract'],'Real abstract')
        self.assertEqual(articles[0]['pub_date'],'2026-09-12')
        self.assertFalse(articles[0]['has_fulltext'])
        self.assertIn('until-pub-date:2026-09-13',get.call_args.kwargs['params']['filter'])
        reply.close.assert_called_once()

    def test_failure_reports_unhealthy(self):
        cfg={'journals':[{'id':1,'name':'JCP','source_type':'crossref','query':'0021-9606'}]}
        health=Mock()
        with patch('integrations.crossref.requests.get',side_effect=RuntimeError('503')):
            self.assertEqual(JournalFetcher(cfg).fetch_all(health),[])
        self.assertFalse(health.call_args.args[1])

    def test_dispatch_without_fulltext_or_openalex(self):
        cfg={'journals':[{'name':'JCP','source_type':'crossref','query':'0021-9606'}]}
        with patch('integrations.crossref.journal_works_page',return_value=([{'doi':'10.1063/a','journal':'Original'}], 1)) as get:
            result=JournalFetcher(cfg).fetch_all()
        self.assertEqual(result[0]['journal'],'JCP')

    def test_invalid_issn_does_not_request(self):
        with patch('integrations.crossref.requests.get') as get:
            with self.assertRaises(ValueError):journal_works('not-a-journal')
        get.assert_not_called()

    def test_rss_missing_date_is_excluded_from_daily_processing(self):
        fetcher = JournalFetcher({'fetcher': {'date_filter_days': 3}})
        feed = Mock(); feed.entries = [{}, {}]
        with patch.object(fetcher, '_fetch_rss', return_value=feed), patch.object(
            fetcher, '_parse_entry', side_effect=[{'title': 'Unknown date', 'pub_date': ''},
                                                 {'title': 'Old', 'pub_date': '2000-01-01'}]):
            rows = fetcher._fetch_journal({'name': 'DD', 'rss': 'http://feeds.rsc.org/rss/dd'})
        self.assertEqual(rows[0]['title'], 'Unknown date')
        self.assertTrue(rows[0]['quarantine'])
