"""Background API paths: fake external services, temporary SQLite, localhost only."""
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.db import Database
from core.batch_tasks import BatchTaskRunner, BatchTaskError, task_view
import web_server


class BackgroundTasksTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / 'test.sqlite'
        self.db = Database(str(self.db_path))
        self.addCleanup(self.db.close)
        self.ids = self.db.save_articles_batch([
            {'title': 'Paper ' + str(i), 'doi': '10.1234/' + str(i), 'abstract': 'a'} for i in range(3)])
        self.runner = BatchTaskRunner(self.db)
        self.config = {'zotero': {'api_key': 'test-secret'}, 'web': {'protect_read': True}}

    def wait(self, runner=None):
        (runner or self.runner)._thread.join(timeout=5)
        self.assertFalse((runner or self.runner)._thread.is_alive())

    def submit(self, processor, **kwargs):
        return self.runner.submit('zotero', self.ids, {}, self.config, processor, **kwargs)

    def test_progress_is_persisted_before_processing_and_config_is_snapshot(self):
        entered, release = threading.Event(), threading.Event()
        def process(kind, ids, opts, config):
            entered.set()
            self.assertTrue(release.wait(3))
            self.assertEqual(config['zotero']['api_key'], 'test-secret')
            row = self.db.get_batch_task(task['task_id'])
            self.assertEqual(row['stats']['current_ids'], ids)
            return [{'id': i, 'ok': True} for i in ids]
        task = self.submit(process)
        self.assertTrue(entered.wait(2))
        self.config['zotero']['api_key'] = 'changed'
        row = self.db.get_batch_task(task['task_id'])
        self.assertEqual(row['status'], 'running')
        self.assertNotIn('test-secret', json.dumps(row))
        self.assertEqual(task_view(row)['completed'], 0)
        release.set()
        self.wait()
        result = task_view(self.db.get_batch_task(task['task_id']))
        self.assertEqual(result['completed'], 3)
        self.assertEqual(result['status'], 'success')

    def test_partial_retry_excludes_success_and_duplicate_retry_is_idempotent(self):
        failed = self.ids[1]
        def process(kind, ids, options, config):
            return [{'id': i, 'ok': i != failed, **({'error': 'offline'} if i == failed else {})} for i in ids]
        original = self.submit(process)
        self.wait()
        self.assertEqual(self.db.get_batch_task(original['task_id'])['status'], 'partial')
        retry = Mock(side_effect=lambda kind, ids, options, config: [{'id': i, 'ok': True} for i in ids])
        child = self.runner.retry(original['task_id'], self.config, retry)
        self.wait()
        self.assertEqual(retry.call_args.args[1], [failed])
        duplicate = self.runner.retry(original['task_id'], self.config, retry)
        self.assertEqual(child['task_id'], duplicate['task_id'])
        self.assertEqual(retry.call_count, 1)

    def test_warned_attachment_is_retryable_without_reprocessing_success(self):
        original = self.submit(lambda kind, ids, opts, cfg: [
            {'id': i, 'ok': True, 'warnings': ['PDF failed'] if i == self.ids[0] else []} for i in ids])
        self.wait()
        retry = Mock(side_effect=lambda k, ids, o, c: [{'id': i, 'ok': True} for i in ids])
        self.runner.retry(original['task_id'], self.config, retry)
        self.wait()
        self.assertEqual(retry.call_args.args[1], [self.ids[0]])

    def test_duplicate_request_id_does_not_schedule_twice_and_busy_is_explicit(self):
        gate, entered = threading.Event(), threading.Event()
        def process(k, ids, o, c):
            entered.set()
            gate.wait(3)
            return [{'id': i, 'ok': True} for i in ids]
        request_id = str(uuid.uuid4())
        task = self.submit(process, request_id=request_id)
        self.assertTrue(entered.wait(2))
        duplicate = self.submit(process, request_id=request_id)
        self.assertEqual(task['task_id'], duplicate['task_id'])
        with self.assertRaises(BatchTaskError) as err:
            self.submit(process)
        self.assertEqual(err.exception.status, 409)
        gate.set()
        self.wait()

    def test_restart_retains_results_and_retries_only_unfinished(self):
        task_id = str(uuid.uuid4())
        self.db.create_batch_task(task_id, 'batch_translate', {
            'ids': self.ids, 'options': {}, 'results': [{'id': self.ids[0], 'ok': True}],
            'current_ids': [self.ids[1]]})
        self.assertEqual(self.db.mark_interrupted_tasks(), 1)
        restarted = BatchTaskRunner(self.db)
        process = Mock(side_effect=lambda k, ids, o, c: [{'id': i, 'ok': True} for i in ids])
        restarted.retry(task_id, self.config, process)
        self.wait(restarted)
        self.assertEqual(process.call_args.args[1], self.ids[1:])

    def test_unknown_inflight_zotero_without_doi_is_not_blindly_retried(self):
        (aid,) = self.db.save_articles_batch([{'title': 'Without DOI'}])
        task_id = str(uuid.uuid4())
        self.db.create_batch_task(task_id, 'batch_zotero', {
            'ids': [aid], 'options': {}, 'results': [], 'current_ids': [aid]})
        self.db.mark_interrupted_tasks()
        with self.assertRaises(BatchTaskError):
            self.runner.retry(task_id, self.config, Mock())

    def test_persistence_failure_prevents_network_calls(self):
        processor = Mock()
        with patch.object(self.db, 'save_batch_task', side_effect=sqlite3.OperationalError('disk full')):
            task = self.submit(processor)
            self.wait()
        processor.assert_not_called()
        self.assertNotEqual(self.db.get_batch_task(task['task_id'])['status'], 'success')

    def test_invalid_or_oversized_submission_never_starts_external_work(self):
        process = Mock()
        for kind, ids in [('zotero', list(range(1, 1002))), ('zotero', []),
                          ('zotero', [True]), ([], [1])]:
            with self.assertRaises(BatchTaskError):
                self.runner.submit(kind, ids, {}, {}, process)
        process.assert_not_called()
        self.assertFalse(self.db.list_batch_tasks())

    def test_invalid_provider_is_rejected_without_starting_work(self):
        for provider in ({'bad': True}, 'unknown'):
            payload, status = web_server._start_background_batch(self.context(), {
                'kind': 'translate', 'ids': self.ids, 'provider': provider})
            self.assertEqual(status, 400)
            self.assertFalse(payload['ok'])
        self.assertFalse(self.db.list_batch_tasks())

    def test_restored_zotero_link_allows_previously_blocked_retry(self):
        (aid,) = self.db.save_articles_batch([{'title': 'Restore association'}])
        task_id = str(uuid.uuid4())
        stats = {'ids': [aid], 'options': {}, 'results': [
            {'id': aid, 'ok': False, 'retryable': False}], 'current_ids': []}
        self.db.create_batch_task(task_id, 'batch_zotero', stats)
        self.db.save_batch_task(task_id, 'failed', 'done', stats)
        self.db.update_article_fields(aid, zotero_key='RESTORED')
        process = Mock(return_value=[{'id': aid, 'ok': True}])
        self.runner.retry(task_id, {}, process)
        self.wait()
        self.assertEqual(process.call_args.args[1], [aid])

    def test_pipeline_state_restore_ignores_background_tasks(self):
        self.submit(lambda k, ids, o, c: [{'id': i, 'ok': True} for i in ids])
        self.wait()
        runner = web_server.TaskRunner({})
        runner.attach_db(self.db)
        self.assertEqual(runner.get_state()['run_count'], 0)

    def context(self):
        def connect():
            c = sqlite3.connect(self.db_path)
            c.row_factory = sqlite3.Row
            return c
        return SimpleNamespace(db=self.db, config=self.config, runner=SimpleNamespace(batch=self.runner),
                               connect_db=connect, api_token='test-token')

    def test_background_translation_reports_missing_and_save_failure(self):
        ctx = self.context()
        def translate(snapshot, request):
            self.db.update_article_fields(self.ids[0], title_zh='中文标题')
            return {'ok': True}, 200
        with patch.object(web_server, '_batch_translate_titles', side_effect=translate):
            results = web_server._process_background_batch(ctx, 'translate', self.ids, {}, self.config)
        self.assertTrue(results[0]['ok'])
        self.assertTrue(all(not r['ok'] for r in results[1:]))

    def test_background_zotero_uses_shared_sync_and_reports_local_save_failure(self):
        client = Mock()
        client.sync_article.side_effect = lambda a, save_key: {
            'key': 'KEY00001', 'ok': save_key('KEY00001'), 'id': a['id'], 'warnings': []}
        with patch('integrations.zotero_client.ZoteroClient', return_value=client), patch.object(
                self.db, 'update_article_fields', return_value=False):
            result = web_server._process_background_batch(self.context(), 'zotero', self.ids[:1], {}, self.config)
        self.assertFalse(result[0]['ok'])

    def test_zotero_client_is_reused_across_progress_updates(self):
        resources = {}
        ctx = self.context()
        client = Mock()
        client.sync_article.side_effect = lambda a, save_key: {'id': a['id'], 'ok': save_key('KEY00001')}
        with patch('integrations.zotero_client.ZoteroClient', return_value=client) as factory:
            for aid in self.ids:
                web_server._process_background_batch(ctx, 'zotero', [aid], {}, self.config, resources)
        factory.assert_called_once()
        self.assertEqual(client.sync_article.call_count, 3)

    def test_http_submission_returns_before_worker_finishes_and_auth_is_shared(self):
        gate, entered = threading.Event(), threading.Event()
        calls = []
        retrying = False
        def process(ctx, kind, ids, options, config, resources=None):
            entered.set()
            calls.extend(ids)
            gate.wait(3)
            return [{'id': i, 'ok': retrying or i != self.ids[1]} for i in ids]
        ctx = self.context()
        server = web_server.ThreadingHTTPServer(('127.0.0.1', 0), web_server.Handler)
        server.context = ctx
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(method, path, body=None, token=True):
            conn = http.client.HTTPConnection(*server.server_address, timeout=2)
            headers = {'Content-Type': 'application/json'}
            if token: headers['X-API-Token'] = 'test-token'
            try:
                conn.request(method, path, json.dumps(body) if body is not None else None, headers)
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally: conn.close()
        try:
            with patch.object(web_server, '_process_background_batch', side_effect=process):
                self.assertEqual(request('POST', '/api/batch-tasks', {'kind': 'zotero', 'ids': self.ids}, False)[0], 401)
                status, data = request('POST', '/api/batch-tasks', {'kind': 'zotero', 'ids': self.ids})
                self.assertEqual(status, 202)
                self.assertTrue(entered.wait(2))
                task_id = data['task_id']
                status, data = request('GET', '/api/batch-tasks/' + task_id)
                self.assertEqual(data['task']['status'], 'running')
                self.assertEqual(request('GET', '/api/batch-tasks', token=False)[0], 401)
                self.assertEqual(request('GET', '/api/batch-tasks/not-found')[0], 404)
                ctx.config['web']['readonly'] = True
                self.assertEqual(request('POST', '/api/batch-tasks/' + task_id + '/retry', {})[0], 403)
                ctx.config['web']['readonly'] = False
                gate.set()
                self.wait()
                self.assertEqual(request('GET', '/api/batch-tasks')[1]['items'][0]['status'], 'partial')
                retrying = True
                status, retry = request('POST', '/api/batch-tasks/' + task_id + '/retry', {})
                self.assertEqual(status, 202)
                self.assertNotEqual(retry['task_id'], task_id)
                self.wait()
                self.assertEqual(calls, self.ids + [self.ids[1]])
                self.assertEqual(request('GET', '/api/batch-tasks')[1]['items'][0]['status'], 'success')
        finally:
            gate.set()
            if self.runner._thread: self.wait()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
