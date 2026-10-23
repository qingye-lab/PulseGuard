from __future__ import annotations

from contextlib import contextmanager
import inspect
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from backend.app import main, storage


class RunPageFilterContractTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        self.enterContext(patch.object(storage, 'DB_PATH', root / 'filters.db'))
        self.enterContext(patch.object(storage, 'BACKUPS_DIR', root / 'backups'))
        self.enterContext(patch.object(storage, 'ensure_runtime_dirs'))
        storage.init_db()
        self.project = storage.default_project_id()
        self.other = int(storage.create_project('Other project')['id'])
        base = dict(project_id=self.project, observation_kind='observation', check_id=1,
                    check_name="Needle 'quoted' name", check_type='api', status='failed',
                    notification_status='failed', runner_id='remote', run_group_id='g1',
                    trigger='schedule', started_at='2026-10-22T12:00:00+08:00',
                    created_at='2026-10-22T12:00:00+08:00', logs='x' * 16384)
        variants = [{}, {'status': 'timeout'}, {}, {'check_type': 'ui'}, {'status': 'ok'},
                    {'notification_status': 'sent'}, {'runner_id': 'local'}, {'run_group_id': 'g2'},
                    {'trigger': 'manual'}, {'check_id': 2}, {'check_name': 'Other name'},
                    {'started_at': '2026-10-21T12:00:00+08:00'},
                    {'started_at': '2026-10-23T12:00:00+08:00'},
                    {'project_id': self.other}, {'observation_kind': 'draft'}]
        self.rows = []
        with storage._connect() as conn:
            for extra in variants:
                row = {**base, **extra}
                cursor = conn.execute('INSERT INTO runs(' + ','.join(row) + ') VALUES ('
                                      + ','.join('?' for _ in row) + ')', list(row.values()))
                self.rows.append({**row, 'id': cursor.lastrowid})
        self.options = {'check_type': 'api', 'status': 'failed', 'notification_status': 'failed',
                        'runner_id': 'remote', 'run_group_id': 'g1', 'trigger': 'schedule',
                        'check_id': 1, 'q': 'Needle', 'start': '2026-10-22T12:00:00+08:00',
                        'end': '2026-10-22T12:01:00+08:00'}

    def expected(self, filters):
        def matches(row):
            for key, value in filters.items():
                if key == 'q':
                    if value.lower() not in row['check_name'].lower():
                        return False
                elif key == 'start':
                    if row['started_at'] < value:
                        return False
                elif key == 'end':
                    if row['started_at'] > value:
                        return False
                elif key == 'status' and value == 'failed':
                    if row[key] not in ('failed', 'timeout'):
                        return False
                elif row[key] != value:
                    return False
            return True
        return sorted((r for r in self.rows if matches(r)), key=lambda r: (r['started_at'], r['id']), reverse=True)

    def test_every_api_filter_is_in_the_performance_contract(self):
        public = set(inspect.signature(main.runs_page).parameters) - {'page', 'page_size'}
        public.remove('type')
        public.add('check_type')
        self.assertEqual(public, set(self.options) | {'project_id', 'observation_kind'})

    def test_all_1024_filter_combinations_are_exact_and_scan_only_metadata(self):
        keys = list(self.options)
        with storage._connect(read_only=True) as conn:
            for mask in range(1 << len(keys)):
                filters = {'project_id': self.project, 'observation_kind': 'observation',
                           **{key: self.options[key] for i, key in enumerate(keys) if mask & (1 << i)}}
                with self.subTest(mask=mask):
                    expected = self.expected(filters)
                    for page in (1, 2):
                        actual = storage.list_runs_page(filters, page=page, page_size=2)
                        self.assertEqual(actual['total'], len(expected))
                        self.assertEqual([r['id'] for r in actual['items']],
                                         [r['id'] for r in expected[(page - 1) * 2:page * 2]])
                        self.assertTrue(all('logs' not in r for r in actual['items']))
                    clauses, params = storage._run_filter_clauses(filters)
                    where = ' WHERE ' + ' AND '.join(clauses)
                    if mask:
                        count_plan = ' '.join(r[3] for r in conn.execute(
                            'EXPLAIN QUERY PLAN ' + storage._run_count_select(where), params))
                        self.assertIn('USING COVERING INDEX ' + storage.RUN_PAGE_INDEX, count_plan)
                    page_plan = ' '.join(r[3] for r in conn.execute(
                        'EXPLAIN QUERY PLAN ' + storage._run_page_select(filters, where), [*params, 2, 0]))
                    self.assertIn('MATERIALIZE page', page_plan)
                    self.assertIn('USING COVERING INDEX', page_plan)
                    self.assertIn('SEARCH r USING INTEGER PRIMARY KEY', page_plan)

    def test_optional_scopes_values_and_page_boundaries_preserve_results(self):
        for scope in ({}, {'project_id': self.project}, {'observation_kind': 'draft'},
                      {'project_id': self.other, 'observation_kind': 'observation'}):
            for extra in ({}, {'notification_status': 'failed'}, {'q': 'Needle'}, {'runner_id': 'remote'}):
                filters = {**scope, **extra}
                expected = self.expected(filters)
                for page_size in (1, 100):
                    actual = storage.list_runs_page(filters, page_size=page_size)
                    self.assertEqual(actual['total'], len(expected), filters)
                    self.assertEqual([r['id'] for r in actual['items']], [r['id'] for r in expected[:page_size]])
                self.assertEqual(storage.list_runs_page(filters, page=100)['items'], [])

    def test_notification_filter_uses_real_api_shape_and_unmatched_filter_does_not_fetch(self):
        client = TestClient(main.app)
        response = client.get('/api/runs-page?notification_status=failed&observation_kind=observation&page=1&page_size=12',
                              headers={'X-PulseGuard-Project-Id': str(self.project)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['total'], len(self.expected(
            {'project_id': self.project, 'observation_kind': 'observation', 'notification_status': 'failed'})))
        self.assertTrue(all(r['notification_status'] == 'failed' for r in response.json()['items']))
        with patch.object(storage, '_run_page_select', side_effect=AssertionError('Empty results must not scan twice')):
            page = storage.list_runs_page({'project_id': self.project, 'notification_status': 'missing'})
        self.assertEqual((page['total'], page['items']), (0, []))

    def test_filter_updates_and_rollbacks_keep_index_and_precise_results_in_sync(self):
        first = self.rows[0]['id']
        with storage._connect() as conn:
            conn.execute("UPDATE runs SET notification_status='sent',runner_id='changed',check_name='Renamed' WHERE id=?", (first,))
        actual = storage.list_runs_page({'project_id': self.project, 'runner_id': 'changed', 'q': 'Renamed',
                                        'notification_status': 'sent'})
        self.assertEqual([r['id'] for r in actual['items']], [first])
        with self.assertRaises(RuntimeError):
            with storage._connect() as conn:
                conn.execute('DELETE FROM runs WHERE id=?', (first,))
                raise RuntimeError('rollback')
        self.assertEqual(storage.list_runs_page({'runner_id': 'changed'})['total'], 1)

    def test_legacy_index_is_replaced_once_without_changing_any_run(self):
        with storage._connect() as conn:
            conn.execute('DROP INDEX ' + storage.RUN_PAGE_INDEX)
            conn.execute('CREATE INDEX ' + storage.RUN_PAGE_INDEX
                         + ' ON runs(project_id,observation_kind,check_type,status,started_at DESC,id DESC)')
            conn.execute('DELETE FROM schema_versions WHERE version=?', (storage.RUN_PAGE_SCHEMA_VERSION,))
            before = [tuple(r) for r in conn.execute('SELECT * FROM runs ORDER BY id')]
        storage.init_db(recover_interrupted=False)
        with storage._connect(read_only=True) as conn:
            self.assertEqual(before, [tuple(r) for r in conn.execute('SELECT * FROM runs ORDER BY id')])
            self.assertEqual(tuple(r[2] for r in conn.execute('PRAGMA index_info(' + storage.RUN_PAGE_INDEX + ')')),
                             storage.RUN_PAGE_INDEX_COLUMNS)
        statements = []
        original = storage._connect

        @contextmanager
        def traced(**kwargs):
            with original(**kwargs) as conn:
                conn.set_trace_callback(statements.append)
                yield conn

        with patch.object(storage, '_connect', traced):
            storage.init_db(recover_interrupted=False)
        self.assertFalse(any(sql.startswith(('CREATE INDEX idx_runs_project_page_filter_started',
                                             'DROP INDEX idx_runs_project_page_filter_started')) for sql in statements))

    def test_failed_index_rebuild_rolls_back_index_and_migration_marker(self):
        with storage._connect() as conn:
            conn.execute('DROP INDEX ' + storage.RUN_PAGE_INDEX)
            conn.execute('CREATE INDEX ' + storage.RUN_PAGE_INDEX + ' ON runs(project_id,started_at)')
            conn.execute('DELETE FROM schema_versions WHERE version=?', (storage.RUN_PAGE_SCHEMA_VERSION,))
        with self.assertRaises(sqlite3.DatabaseError):
            with storage._connect() as conn:
                conn.set_authorizer(lambda action, name, *_: sqlite3.SQLITE_DENY
                                    if action == sqlite3.SQLITE_CREATE_INDEX and name == storage.RUN_PAGE_INDEX else sqlite3.SQLITE_OK)
                storage._ensure_run_page_index(conn)
        with storage._connect(read_only=True) as conn:
            self.assertEqual(tuple(r[2] for r in conn.execute('PRAGMA index_info(' + storage.RUN_PAGE_INDEX + ')')),
                             ('project_id', 'started_at'))
            self.assertIsNone(conn.execute('SELECT 1 FROM schema_versions WHERE version=?',
                                           (storage.RUN_PAGE_SCHEMA_VERSION,)).fetchone())
