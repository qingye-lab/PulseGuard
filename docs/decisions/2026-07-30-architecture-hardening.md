# Harden worker updates, manual transport, run evidence, and delivery gates

- Status: accepted
- Date: 2026-07-30
- Owners: PulseGuard maintainers
- Scope: worker updater, child-runner transport, runner-failure persistence, systemd deployment, worker-image CI
- Supersedes: none
- Superseded by: none

## Context

PulseGuard is a trusted local/LAN operations console, but four boundaries still allowed avoidable risk: updater image allowlists used raw string prefixes, manual child runners silently defaulted to plaintext HTTP, runner/system failures were returned but deleted from run history, and two delivery paths did not consistently enforce the repository lockfile and verification gates. Administrator identity is intentionally outside this decision and remains unchanged.

## Evidence

| Claim | Kind | Source and environment | Observed | Reference | Freshness / redaction |
| --- | --- | --- | --- | --- | --- |
| A repository-prefix lookalike could pass the updater allowlist | fact | local repository | 2026-07-30 | `backend/app/worker_updater.py` previous `_target_image` implementation | current checkout |
| Manual runner requests carry bearer tokens and execution payloads to the configured address | fact | local repository | 2026-07-30 | `backend/app/main.py::_worker_request`, `backend/app/runner.py::_call_remote_runner` | current checkout; no secret values recorded |
| Runner/system results were discarded after execution | fact | local repository | 2026-07-30 | previous `storage.discard_incomplete_run` call sites in `backend/app/runner.py` | current checkout |
| The systemd path reconstructed dependencies from direct requirements, while worker CI published before repository tests | fact | local repository | 2026-07-30 | `scripts/deploy-china-systemd.ps1`, `.github/workflows/docker-worker.yml` | current checkout |

## Decision

The following invariants are owned at their authoritative boundaries:

1. Worker updater image entries match an exact normalized repository, or an explicitly configured namespace ending in `/` or `/*`; tags and digests do not change repository identity.
2. New or changed manual HTTP child runners require `allow_insecure_http=true`. Existing persisted manual HTTP runners receive that value once when the additive column is introduced. HTTPS and Relay remain usable without this acknowledgement.
3. Runner/system failures finish their existing run row with `failure_kind=runner` and `affects_health=false`. They remain queryable, never update target health, and system-alert delivery state is stored separately on the run.
4. The systemd path installs a pinned `uv`, syncs with `uv.lock`, creates an application-consistent database backup, and runs migration preflight on a copy before stopping services. Worker-image publication depends on isolated backend and frontend verification.

## Alternatives considered

- Reject all manual HTTP immediately — rejected because it would break existing trusted-LAN installations and the documented manual fallback.
- Treat raw allowlist values as arbitrary prefixes — rejected because repository-prefix lookalikes cross the updater trust boundary.
- Store system failures in a separate event table only — deferred because the existing run schema already owns execution correlation, runner metadata, retention, and notification fields.
- Continue generating deployment requirements from `pyproject.toml` — rejected because it bypasses the repository dependency lock contract.

## Consequences

- Positive: updater targets cannot escape an exact repository through a lookalike prefix; accidental plaintext runner setup is blocked; operational failures retain correlation evidence; published worker images are gated by tests and builds.
- Negative: new manual HTTP API clients must send one additional boolean; system-failure rows increase retained run volume; systemd deployment bootstraps a pinned `uv`.
- Operational: the database change is additive. Old code ignores the new column, while rollback can restore the recorded pre-deploy SQLite backup if migration or startup fails. Existing manual HTTP rows remain enabled during coexistence.

## Verification

- Focused updater, runner-route/storage/config-transfer, and runner suites passed.
- The isolated full backend suite passed: 361 tests against a temporary initialized database.
- `npm run build` passed in `frontend/`.
- PowerShell parsing and `bash -n` parsing of the rendered remote deployment payload passed.
- Workflow YAML parsing, `uv lock --check`, `git diff --check`, and an adversarial diff review passed.
- No deployment or publication was performed as part of this decision.

## Revisit when

- PulseGuard removes manual direct runners, adopts mandatory TLS for every child connection, moves to multi-instance persistence, or introduces signed image provenance beyond repository allowlisting.
