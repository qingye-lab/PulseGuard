from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend.app import storage


INSERT = """INSERT INTO runs(project_id, check_id, check_name, check_type, status,
    started_at, created_at, observation_kind, runner_id, run_group_id, notification_status)
    VALUES (?, 1, ?, ?, ?, '2026-10-22T12:00:00+08:00', '2026-10-22T12:00:00+08:00', ?, ?, ?, ?)"""


class RunPageCountTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(ignore_cleanup_errors=True)))
        self.db = root / 'counts.db'
        self.enterContext(patch.object(storage, 'DB_PATH', self.db))
        self.enterContext(patch.object(storage, 'BACKUPS_DIR', root / 'backups'))
        self.enterContext(patch.object(storage, 'ensure_runtime_dirs'))
        storage.init_db()
        self.project = storage.default_project_id()
        self.other = int(storage.create_project('Another project')['id'])
        with storage._connect() as conn:
            conn.executemany(INSERT, [
                (self.project, 'API one', 'api', 'ok', 'observation', 'local', 'g1', 'sent'),
                (self.project, 'UI two', 'ui', 'failed', 'observation', 'remote', 'g1', 'disabled'),
                (self.project, 'API three', 'api', 'timeout', 'observation', 'remote', 'g2', 'failed'),
                (self.project, 'Draft', 'api', 'ok', 'draft', 'local', '', 'not_required'),
                (self.other, 'Other', 'api', 'ok', 'observation', 'local', '', 'sent'),
            ])

    def assert_totals_match_history(self):
        with storage._connect(read_only=True) as conn:
            expected = [tuple(row) for row in conn.execute(
                'SELECT project_id, observation_kind, COUNT(*) FROM runs GROUP BY project_id, observation_kind ORDER BY 1,2')]
            actual = [tuple(row) for row in conn.execute(
                'SELECT project_id, observation_kind, run_count FROM run_count_totals ORDER BY 1,2')]
        self.assertEqual(actual, expected)

    @contextmanager
    def traced(self, callback=None, authorizer=None):
        original = storage._connect

        @contextmanager
        def connect(**kwargs):
            with original(**kwargs) as conn:
                if callback:
                    conn.set_trace_callback(callback)
                if authorizer:
                    conn.set_authorizer(authorizer)
                yield conn

        with patch.object(storage, '_connect', connect):
            yield

    def test_existing_database_backfills_once_without_changing_records(self):
        with storage._connect() as conn:
            for name in ('insert', 'delete', 'scope'):
                conn.execute('DROP TRIGGER trg_runs_total_' + name)
            conn.execute('DROP TABLE run_count_totals')
            conn.execute('DELETE FROM schema_versions WHERE version=?', (storage.RUN_COUNT_SCHEMA_VERSION,))
            before = [tuple(r) for r in conn.execute('SELECT * FROM runs ORDER BY id')]
        storage.init_db()
        self.assert_totals_match_history()
        statements = []
        with self.traced(statements.append):
            storage.init_db()
        self.assertFalse(any('GROUP BY project_id, observation_kind' in sql for sql in statements))
        with storage._connect(read_only=True) as conn:
            self.assertEqual(before, [tuple(r) for r in conn.execute('SELECT * FROM runs ORDER BY id')])
            plan = ' '.join(row[3] for row in conn.execute(
                'EXPLAIN QUERY PLAN SELECT project_id,observation_kind,COUNT(*) '
                'FROM runs INDEXED BY idx_runs_project_kind_started GROUP BY project_id,observation_kind'))
        self.assertIn('COVERING INDEX', plan)

    def test_common_pages_never_count_history_and_keep_all_scopes_exact(self):
        def deny_count(action, first, second, *_):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_FUNCTION and second.lower() == 'count' else sqlite3.SQLITE_OK

        with self.traced(authorizer=deny_count):
            for filters, total in [({}, 5), ({'project_id': self.project}, 4),
                    ({'observation_kind': 'observation'}, 4),
                    ({'project_id': self.project, 'observation_kind': 'observation'}, 3),
                    ({'project_id': self.other, 'observation_kind': 'draft'}, 0)]:
                page = storage.list_runs_page(filters, page=1, page_size=2)
                self.assertEqual(page['total'], total)
                self.assertEqual(len(page['items']), min(total, 2))
                second = storage.list_runs_page(filters, page=2, page_size=2)
                self.assertEqual(second['total'], total)
                self.assertFalse({r['id'] for r in page['items']} & {r['id'] for r in second['items']})

    def test_filtered_counts_keep_original_exact_semantics(self):
        for extra in [{'status': 'failed'}, {'check_type': 'ui'}, {'notification_status': 'failed'},
                {'runner_id': 'remote'}, {'run_group_id': 'g1'}, {'q': 'API'}, {'check_id': 1},
                {'start': '2026-10-22T11:00:00+08:00'}, {'end': '2026-10-22T11:00:00+08:00'},
                {'trigger': 'legacy'}]:
            filters = {'project_id': self.project, 'observation_kind': 'observation', **extra}
            expected = storage.list_runs(filters, limit=100, summary_only=True)
            page = storage.list_runs_page(filters, page_size=100)
            self.assertEqual(page['total'], len(expected), filters)
            self.assertEqual(page['items'], expected, filters)

    def test_direct_sql_scope_changes_deletes_and_rollbacks_update_counts_atomically(self):
        with self.assertRaisesRegex(RuntimeError, 'rollback'):
            with storage._connect() as conn:
                conn.execute('DELETE FROM runs WHERE observation_kind=?', ('observation',))
                conn.execute('UPDATE runs SET project_id=?,observation_kind=?', (self.other, 'verification'))
                raise RuntimeError('rollback')
        self.assert_totals_match_history()
        self.assertEqual(storage.list_runs_page()['total'], 5)
        with sqlite3.connect(self.db) as conn:
            conn.execute('UPDATE runs SET project_id=?,observation_kind=? WHERE project_id=?',
                         (self.other, 'verification', self.project))
            conn.execute('UPDATE runs SET status=?,logs=?', ('skipped', 'changed payload'))
            conn.execute('DELETE FROM runs WHERE observation_kind=?', ('observation',))
        self.assert_totals_match_history()
        self.assertEqual(storage.list_runs_page({'project_id': self.project})['total'], 0)
        self.assertEqual(storage.list_runs_page({'project_id': self.other, 'observation_kind': 'verification'})['total'], 4)
        with storage._connect() as conn:
            conn.execute('DELETE FROM runs')
        self.assert_totals_match_history()
        self.assertEqual(storage.list_runs_page()['total'], 0)

    def test_missing_trigger_rebuilds_counts_before_serving(self):
        with storage._connect() as conn:
            conn.execute('DROP TRIGGER trg_runs_total_insert')
            conn.execute(INSERT, (self.project, 'Untracked', 'api', 'ok', 'observation', 'local', '', 'sent'))
        storage.init_db()
        self.assert_totals_match_history()
        self.assertEqual(storage.list_runs_page()['total'], 6)

    def test_failed_migration_rolls_back_table_backfill_triggers_and_marker(self):
        with storage._connect() as conn:
            for name in ('insert', 'delete', 'scope'):
                conn.execute('DROP TRIGGER trg_runs_total_' + name)
            conn.execute('DROP TABLE run_count_totals')
            conn.execute('DELETE FROM schema_versions WHERE version=?', (storage.RUN_COUNT_SCHEMA_VERSION,))
        with self.assertRaises(sqlite3.DatabaseError):
            with storage._connect() as conn:
                conn.set_authorizer(lambda action, name, *_: sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_CREATE_TRIGGER and name == 'trg_runs_total_scope' else sqlite3.SQLITE_OK)
                storage._ensure_run_count_totals(conn)
        with storage._connect(read_only=True) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='run_count_totals'").fetchone())
            self.assertIsNone(conn.execute('SELECT 1 FROM schema_versions WHERE version=?', (storage.RUN_COUNT_SCHEMA_VERSION,)).fetchone())
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 5)
        storage.init_db()
        self.assert_totals_match_history()

    def test_separate_writers_and_rollback_cannot_lose_increments(self):
        def write(number):
            with sqlite3.connect(self.db, timeout=5) as conn:
                conn.executemany(INSERT, [(self.project, 'Concurrent', 'api', 'ok', 'observation', 'local', '', 'sent')] * 10)
                if number % 2:
                    conn.rollback()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(write, range(4)))
        self.assert_totals_match_history()
        self.assertEqual(storage.list_runs_page()['total'], 25)

    def test_total_and_page_share_snapshot_across_another_writer_commit(self):
        original = storage._connect
        inserted = False

        class Connection:
            def __init__(self, conn):
                self.conn = conn

            def execute(inner, sql, *args):
                nonlocal inserted
                cursor = inner.conn.execute(sql, *args)
                if 'SELECT COALESCE(SUM(run_count)' in sql and not inserted:
                    inserted = True
                    def write():
                        with sqlite3.connect(self.db) as conn:
                            conn.execute(INSERT, (self.project, 'After snapshot', 'api', 'ok', 'observation', 'local', '', 'sent'))
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        pool.submit(write).result(timeout=2)
                return cursor

        @contextmanager
        def connect(**kwargs):
            with original(**kwargs) as conn:
                yield Connection(conn)

        with patch.object(storage, '_connect', connect):
            page = storage.list_runs_page(page_size=100)
        self.assertTrue(inserted)
        self.assertEqual(page['total'], 5)
        self.assertEqual(len(page['items']), 5)
        self.assertEqual(storage.list_runs_page()['total'], 6)


if __name__ == '__main__':
    unittest.main()
