"""One local entry point for quick, full and browser verification."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("quick", "full", "ui"), nargs="?", default="quick")
    parser.add_argument("--backend-pattern", default="test_*.py")
    args = parser.parse_args()
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if npm is None:
        parser.error("npm is required; install the frontend dependencies with npm ci first")
    log_dir = Path(tempfile.mkdtemp(prefix="pulseguard-verify-", dir=os.getenv("RUNNER_TEMP")))
    print(f"Verification logs: {log_dir}", flush=True)
    backend = [sys.executable, str(ROOT / "scripts" / "test_backend.py"), "--pattern", args.backend_pattern]
    steps: list[tuple[str, list[str], Path]] = []
    if args.mode != "ui":
        if args.mode == "full":
            backend.append("--require-browser")
        steps.append(("backend", backend, ROOT))
        steps.append(("frontend-tests", [npm, "test"], ROOT / "frontend"))
    if args.mode == "quick":
        steps.append(("typecheck", [npm, "run", "typecheck"], ROOT / "frontend"))
    else:
        steps.append(("frontend-build", [npm, "run", "build"], ROOT / "frontend"))
        steps.append(("browser-smoke", [sys.executable, str(ROOT / "scripts" / "smoke_ui.py"), "--output", str(log_dir / "browser")], ROOT))
    for name, command, cwd in steps:
        log = log_dir / f"{name}.log"
        started = time.perf_counter()
        print(f"Running {name}...", flush=True)
        with log.open("w", encoding="utf-8") as output:
            result = subprocess.run(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT, env={**os.environ, "PYTHONUTF8": "1"})
        elapsed = time.perf_counter() - started
        status = "PASS" if result.returncode == 0 else "FAIL"
        print(f"{status} {name}: {elapsed:.1f}s — {log}", flush=True)
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()
        print("\n".join(tail[-6:] if result.returncode == 0 else tail[-45:]), flush=True)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
