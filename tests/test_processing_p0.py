import unittest
from core.db import Database
from processing import admission_decision, build_queue, task_status_and_error, validate_budget

DAY = '2026-09-13'
CFG = {'scheduler': {'timezone': 'Asia/Shanghai'}, 'fetcher': {'date_filter_days': 3}}


class TaskStatusTests(unittest.TestCase):
    def test_source_truncation_alone_is_success(self):
        stats = {"source_incomplete": 2, "sources": [
            {"source_id": 17, "name": "ChemCatChem", "success": True, "complete": False},
            {"source_id": 21, "name": "Angew. Chem. Int. Ed.", "success": True, "complete": False},
        ]}
        self.assertEqual(task_status_and_error(stats), ("success", ""))

    def test_partial_reasons_include_source_names_and_incomplete_note(self):
        stats = {
            "source_failed": 1, "source_incomplete": 1,
            "sources": [
                {"source_id": 2, "name": "Nature Materials", "success": False, "complete": False},
                {"source_id": 21, "name": "Angew. Chem. Int. Ed.", "success": True, "complete": False},
            ],
        }
        status, error = task_status_and_error(stats)
        self.assertEqual(status, "partial")
        self.assertIn("来源抓取失败×1（如：Nature Materials）", error)
        self.assertIn("来源未抓全×1（如：Angew. Chem. Int. Ed.", error)
        self.assertIn("下轮自动续采", error)

    def test_push_delivery_failure_is_partial(self):
        status, error = task_status_and_error({"push_results": {"email": False}})
        self.assertEqual(status, "partial")
        self.assertIn("投递失败", error)

    def test_all_scores_failed_is_failed(self):
        status, _error = task_status_and_error(
            {"score_attempted": 25, "scored_ok": 0, "scored_failed": 25})
        self.assertEqual(status, "failed")

    def test_score_error_summaries_include_samples(self):
        stats = {"scored_failed": 3,
                 "score_errors": {"LLMError": {"count": 3, "samples": ["Paper A"]}}}
        status, error = task_status_and_error(stats)
        self.assertEqual(status, "partial")
        self.assertIn("LLMError×3（如：Paper A）", error)

    def test_db_and_analysis_failures_have_reasons(self):
        stats = {"db_errors": 2, "analyzed_failed": 4}
        status, error = task_status_and_error(stats)
        self.assertEqual(status, "partial")
        self.assertIn("数据库写入失败×2", error)
        self.assertIn("解读失败×4", error)


class ProcessingTests(unittest.TestCase):
    def test_dates_and_timezone(self):
        cases = {'2026-09-11': 'eligible', '2026-09-13': 'eligible',
                 '2026-09-10': 'outside_window', '2026-09-14': 'invalid_date',
                 '': 'needs_date', '2026': 'needs_date', '2026-09': 'needs_date',
                 'nonsense': 'invalid_date', '2026-02-30': 'invalid_date',
                 '2026-09-10T16:01:00Z': 'eligible',
                 '2026-09-13T16:01:00Z': 'invalid_date'}
        for raw, expected in cases.items():
            for source in ('rss', 'openalex_query', 'crossref_journal', 'arxiv'):
                with self.subTest(raw=raw, source=source):
                    result = admission_decision({'pub_date': raw, 'discovered_via': source}, DAY, CFG)
                    self.assertEqual(result['decision'], expected)
                    self.assertEqual((result['window_start'], result['window_end']), ('2026-09-11', DAY))

    def test_round_robin_id_dedup_retry_quota(self):
        rows = [{'id': i, 'journal': str(i % 4), 'processing_status': 'eligible',
                 'score_status': 'failed' if i < 500 else '', 'queued_at': '2026-09-11'} for i in range(2000)]
        selected = build_queue(rows + rows, CFG, 100)
        self.assertEqual(len(selected), 100)
        self.assertEqual(len({r['id'] for r in selected}), 100)
        self.assertEqual(sum(r['score_status'] == 'failed' for r in selected), 25)
        self.assertEqual({r['journal'] for r in selected}, {'0', '1', '2', '3'})
        self.assertEqual(selected, build_queue(list(reversed(rows)), CFG, 100))

    def test_old_unreviewed_and_admitted_deferred(self):
        db = Database(':memory:')
        self.addCleanup(db.close)
        ids = db.save_articles_batch([
            {'title': 'missing date'}, {'title': 'historical', 'pub_date': '2020-01-01'},
            {'title': 'new eligible', 'pub_date': DAY}, {'title': 'other eligible', 'pub_date': DAY}])
        cfg = {**CFG, 'processing': {'max_score_articles_per_run': 1}}
        queue = db.get_processing_queue(cfg, DAY)
        self.assertEqual(queue['score_queue_total'], 2)
        self.assertEqual(len(queue['selected']), 1)
        db.update_article_fields(queue['selected'][0]['id'], score_status='ok', processed=1)
        following = db.get_processing_queue(cfg, '2026-09-30')
        self.assertEqual(len(following['selected']), 1)
        self.assertNotEqual(queue['selected'][0]['id'], following['selected'][0]['id'])
        self.assertEqual([r['processing_status'] for r in db.get_articles_by_ids(ids[:2])], ['unreviewed', 'unreviewed'])

    def test_budget_validation(self):
        for invalid in (0, -1, 'nope', None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_budget({'processing': {'max_score_articles_per_run': invalid}})
        self.assertEqual(validate_budget({}), 100)
        self.assertEqual(validate_budget({}, True), 30)
