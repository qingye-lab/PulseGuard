"""Run backend tests in a fresh runtime, never against the configured instance."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pattern", default="test_*.py", help="unittest filename pattern, e.g. test_runner.py")
    parser.add_argument("--require-browser", action="store_true", help="fail instead of skipping when Chromium is missing")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)

    with tempfile.TemporaryDirectory(prefix="pulseguard-tests-") as temporary:
        root = Path(temporary)
        # Import-time config must not inherit live paths, tokens or node roles.
        for key in list(os.environ):
            if key.startswith("PULSEGUARD_"):
                del os.environ[key]
        os.environ.update({
            "PULSEGUARD_DATA_DIR": str(root / "data"),
            "PULSEGUARD_DB_PATH": str(root / "data" / "test.db"),
            "PULSEGUARD_REPORTS_DIR": str(root / "reports"),
            "PULSEGUARD_BACKUPS_DIR": str(root / "backups"),
            "PULSEGUARD_REQUIRE_BROWSER_TESTS": "1" if args.require_browser else "0",
        })
        from backend.app import storage

        storage.init_db()
        suite = unittest.defaultTestLoader.discover(str(ROOT / "backend" / "tests"), pattern=args.pattern)
        if suite.countTestCases() == 0:
            parser.error(f"no tests matched {args.pattern!r}")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
