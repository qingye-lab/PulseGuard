from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location('systemd_release', SCRIPTS / 'systemd_release.py')
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class SystemdReleaseTests(unittest.TestCase):
    def test_start_submits_both_units_before_waiting_for_api_readiness(self):
        events = []
        with patch.object(release, 'service', side_effect=lambda action: events.append(action)), \
             patch.object(release, 'api', side_effect=lambda *a, **k: events.append('health') or {'status': 'ok'}):
            release.start_services()
        self.assertEqual(events, ['start', 'health', 'is-active'])

    def test_partial_swap_restores_all_originals_and_removes_new_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, work = Path(temporary) / 'app', Path(temporary) / 'release'
            files = {}
            for name, old in [('backend/app/first.py', b'old first'), ('scripts/new.py', None),
                              ('backend/app/last.py', b'old last')]:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                if old is not None:
                    target.write_bytes(old)
                staged = work / 'stage' / name
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_bytes(b'new candidate')
                files[name] = {'baseline': release.digest(target) if old is not None else None,
                               'sha256': release.digest(staged)}
            originals = release.capture_originals(root, work, files)
            atomic_copy = release.atomic_copy

            def fail_last(source, target):
                if target.name == 'last.py':
                    raise OSError('injected partial swap failure')
                atomic_copy(source, target)

            with patch.object(release, 'atomic_copy', side_effect=fail_last):
                with self.assertRaisesRegex(OSError, 'partial swap'):
                    release.replace_files(root, work, files)
            self.assertEqual((root / 'backend/app/first.py').read_bytes(), b'new candidate')
            self.assertTrue((root / 'scripts/new.py').exists())
            release.restore_files(root, work, originals)
            self.assertEqual((root / 'backend/app/first.py').read_bytes(), b'old first')
            self.assertEqual((root / 'backend/app/last.py').read_bytes(), b'old last')
            self.assertFalse((root / 'scripts/new.py').exists())

    def test_rollback_attempts_other_files_after_one_restore_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, work = Path(temporary) / 'app', Path(temporary) / 'work'
            originals = {'backend/app/a.py': {'existed': True, 'sha256': 'a'},
                         'backend/app/b.py': {'existed': True, 'sha256': 'b'}}
            with patch.object(release, 'atomic_copy', side_effect=OSError('disk failure')) as copy:
                with self.assertRaisesRegex(RuntimeError, 'Incomplete source rollback'):
                    release.restore_files(root, work, originals)
            self.assertEqual(copy.call_count, 2)

    def test_manifest_cannot_replace_runtime_data(self):
        for name in ('data/pulseguard.db', '../backend/main.py', '/opt/pulseguard/.env'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.validate_files({name: {}})

    def test_console_checks_real_source_and_project_filters(self):
        calls = []

        def api(path, **kwargs):
            calls.append((path, kwargs))
            if path == '/api/health':
                return {'status': 'ok'}
            return {'total': 0, 'items': [], 'page': 1, 'page_size': 12}

        with patch.object(release, 'api', side_effect=api):
            result = release.verify_console([1, 2])
        self.assertEqual(len(result), 10)
        for path, kwargs in calls[1:]:
            self.assertIn('observation_kind=observation', path)
            self.assertIn(kwargs['project_id'], (1, 2))

    def test_http_failure_and_wrong_project_are_fatal_acceptance_errors(self):
        for response in (RuntimeError('HTTP 400'), {'total': 1, 'page': 1, 'page_size': 12,
                'items': [{'project_id': 2, 'observation_kind': 'observation'}]},
                {'total': 1, 'page': 1, 'page_size': 12, 'items': []}):
            def api(path, **kwargs):
                if path == '/api/health':
                    return {'status': 'ok'}
                if isinstance(response, Exception):
                    raise response
                return response
            with self.subTest(response=response), patch.object(release, 'api', side_effect=api):
                with self.assertRaises(RuntimeError):
                    release.verify_console([1])

    def test_backup_completion_follows_acceptance_and_does_not_roll_back_on_prune_failure(self):
        events = []

        def api(path, **kwargs):
            if path == '/api/settings':
                return {'database_backup_retention': 2}
            if path == '/api/projects':
                return [{'id': 1, 'status': 'active'}]
            raise AssertionError(path)

        def complete(*args, **kwargs):
            events.append('verify_backup' if kwargs.get('dry_run') else 'complete_backup')
            if not kwargs.get('dry_run'):
                raise OSError('retention unavailable')

        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(release, 'api', side_effect=api), \
             patch.object(release.deployment_backups, '_manifest', return_value={'state': 'pending'}), \
             patch.object(release.deployment_backups, 'complete', side_effect=complete), \
             patch.object(release, 'capture_originals', return_value={}), \
             patch.object(release, 'fingerprint', return_value={}), \
             patch.object(release, 'drain'), patch.object(release, 'settle'), \
             patch.object(release, 'service'), patch.object(release, 'start_services'), \
             patch.object(release, 'replace_files'), patch.object(release, 'restore_files') as restore, \
             patch.object(release, 'verify_console', side_effect=lambda _: events.append('accept_pages') or []):
            with self.assertRaisesRegex(OSError, 'retention unavailable'):
                release.apply(Path(temporary), Path(temporary), {'id': 'a' * 32, 'files': {}})
        self.assertEqual(events, ['verify_backup', 'accept_pages', 'complete_backup'])
        restore.assert_not_called()


if __name__ == '__main__':
    unittest.main()
