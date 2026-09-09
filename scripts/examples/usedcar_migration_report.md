# Used-car module migration plan

This directory contains the reviewed `usedcar` module source and the
reproducible plan generator.  The generator is read-only by default:

```powershell
uv run python scripts/migrate_usedcar_modules.py --project-id 2 `
  --remote-url http://115.190.188.243:8787
```

The plan accepts only the reviewed task set 34–48, verifies the original
script hash and full check configuration before every future write, preserves
the static 35–39 requests, and keeps the dynamic request counts at
34=1, 40=2, 41=3, 42=5, 43=3, 44=3, 45=4, 46=5, 47=3, 48=2.  Dynamic target
requests use each task's `ctx.entry_url`; the module provides only shared
dependency methods and constants.

After deployment, run the readback verifier with the generated migration JSON:

```powershell
uv run python scripts/verify_usedcar_remote.py `
  --base-url http://115.190.188.243:8787 --project-id 2 `
  --plan C:\Users\Qa\AppData\Local\Temp\pg-usedcar-rules-migration-candidate.json
```

It first runs all 15 candidates through their normal manual run endpoints and
checks their request/assertion counts and frozen `diagnostics.modules`
metadata.  It then runs all 15 negative
candidates through draft debug with an impossible status assertion, proving
each result is a health-neutral draft.  A concurrent normal scheduled
observation may update health fields during that phase; the verifier ties such
changes to new local scheduled run evidence instead of treating them as draft
mutation.  Finally it waits at least the 60-second task interval before
collecting all 15 new local scheduled observations from the checks and runs
endpoints.

`--apply` is intentionally gated by `--publish`.  Apply mode first merges and
validates the module draft, publishes it while no task references it, then
debugs each candidate, updates canary 41, manually runs 41, and proceeds with
the remaining dynamic tasks.  Any failure restores already changed tasks from
the fresh readback.  It writes through PulseGuard HTTP APIs only and never
opens the SQLite database.  The rollback snapshot and generated request
payloads are written to the selected output directory before a future apply.
