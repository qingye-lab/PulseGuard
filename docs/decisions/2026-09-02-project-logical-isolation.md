# Project-scoped probe and alert ownership

- Status: accepted
- Date: 2026-09-02
- Owners: PulseGuard maintainers
- Scope: project ownership for checks, derived monitoring data, execution alerts, and public read APIs
- Supersedes: none
- Superseded by: none

## Context

PulseGuard stores checks, runs, monitoring history, and alert configuration in one SQLite-backed instance. Operators need independent project workspaces for probe scripts and execution-alert routing while continuing to share runners, resource pools, environment variables, member identities, backups, and system alerts.

## Evidence

| Claim | Kind | Source and environment | Observed | Reference | Freshness / redaction |
| --- | --- | --- | --- | --- | --- |
| Checks and derived records have no project owner | fact | current repository | 2026-09-02 | `backend/app/storage.py` table definitions | current working tree |
| Execution and system alerts share the global settings record | fact | current repository | 2026-09-02 | `backend/app/defaults.py`, `backend/app/notifier.py` | current working tree |
| Scripts and runtime resources intentionally share one trusted process | constraint | repository contract | 2026-09-02 | `AGENTS.md` | current working tree |
| Existing SQLite data is user-owned and must be migrated with backup and preflight | constraint | repository contract | 2026-09-02 | `AGENTS.md` | current working tree |

## Decision

Every check and every check-derived persisted record has a required integer `project_id`. Project-facing API requests carry `X-PulseGuard-Project-Id`, and storage queries enforce that owner rather than relying on UI filtering. Archived projects remain readable but cannot schedule or execute work.

Execution-alert channels and policies are owned by `project_alert_settings`. Member identities, delivery infrastructure, runners, execution resources, environment variables, and system-alert channels remain global. This is logical separation for a shared trusted operator environment, not a security sandbox or multi-tenant authorization boundary.

Existing instances move forward through one idempotent migration into a renameable default project. New and old application versions do not coexist; rollback restores the old release together with the pre-migration database.

## Alternatives considered

- Tags as project identifiers — rejected because tags do not enforce ownership on detail, history, batch, import, or alert-channel lookups.
- Separate SQLite databases or services per project — rejected because the requested runtime and system configuration remain shared and the added operational cost is unnecessary.
- Security multi-tenancy — deferred because shared trusted Python execution and the absence of user authentication cannot provide that boundary.

## Consequences

- Positive: task configuration, monitoring history, project audits, exports, read-only tokens, and execution alerts cannot accidentally cross project scope through supported APIs.
- Negative: all project-facing callers must send the project header, and legacy configuration bundles require re-export after migration.
- Operational: migration must be rehearsed on a database copy; production cutover is coordinated, and rollback restores both code and database.

## Verification

- Fresh and legacy database migration tests, including idempotence and alert-channel copying.
- Cross-project negative API tests, archived-project scheduling and mutation tests, and project-bound token tests.
- Full backend suite, frontend build, database-copy migration preflight, and rendered desktop/mobile project switching.

## Revisit when

- Projects require mutually untrusted users, independent credentials, isolated scripts, per-project runners, or multi-instance horizontal scaling.
