# Local frontend runtimes and bounded SQLite concurrency

- Status: accepted
- Date: 2026-09-04
- Owners: PulseGuard maintainers
- Scope: console dependency loading, database connection lifetime, retention cleanup, backup restoration
- Supersedes: none

## Context

PulseGuard remains one trusted local/LAN instance using SQLite. The remaining performance work must improve actual resource loading and database responsiveness without replacing the component system, weakening retained-data guarantees, or adding phone viewport support.

## Evidence

- The previous production build forced all Ant Design modules into an entry dependency. Its static JavaScript/CSS closure was 459,424 gzip bytes; the monitoring route including shared dependencies was 905,519 gzip bytes.
- `@ant-design/plots` dispatches through a full chart registry even when importing its Line component. PulseGuard monitoring only needs latency lines/points, axes, legend, and tooltips.
- The lazy editor wrapper previously relied on the Monaco loader's remote CDN default. A small wrapper chunk did not mean the editor was local or available offline.
- `storage.py` used one RLock for reads and writes. Retention selected every expired record, including unfinished runs, and cleared only one of the two check-status run references.
- [SQLite WAL documentation](https://sqlite.org/wal.html) defines simultaneous readers and a serialized writer, stable per-transaction snapshots, and the checkpoint cost of long read transactions.
- Local verification runtime: Python 3.12.10 / SQLite 3.49.1. SQLite's official WAL-reset advisory recommends a fixed version (3.51.3+, or the documented 3.50.7 / 3.44.6 backports). This task does not replace the host's Python installation.

## Decision

1. Keep Ant Design and native route splitting. Measure the complete static-import closure, not an isolated chunk. Use the existing G2 implementation's documented on-demand registration for the latency chart; keep the visible chart available immediately.
2. Bundle Monaco ESM with Python/JSON support and same-origin editor/JSON workers inside the existing lazy editor boundary. Do not fetch its runtime from a public CDN, and do not preload it on overview/list navigation.
3. Separate database connection lifecycle from writer serialization. Normal connections share a maintenance gate; init, migration, and restoration take exclusive access. Audited read paths use read-only URI connections, `query_only`, and explicit snapshot transactions. Writers and their commits/closing remain serialized. Acquisition order is maintenance gate before writer lock.
4. Keep immediate stale-status semantics at existing read entry points. Select expired candidates in SQL and recheck the predicate under the writer boundary before updating; do not replace the contract with an unbounded stale cache.
5. Clean historical runs in bounded batches using the terminal-only `idx_runs_cleanup_terminal(created_at, id)` index. Each transaction selects one terminal-ID set, archives that exact set, clears both status references, and deletes that set. Partial progress is resumable without duplicate summary accumulation. Unfinished runs survive retention. Artifact filesystem cleanup remains outside database transactions.
6. Restore on the main node under exclusive database access from safety backup through initialization, with compensation if initialization fails. The runtime pauses new run admissions and scheduling, waits for accepted work (including shielded local children and worker executions) to finish, then restores and reloads runtime settings. Timeout returns 409 without restoring. Cancellation cannot reopen admission while restoration is still running. Existing deployment pauses are respected. Failed compensation or runtime reload returns an actionable 503 and leaves task execution paused for operator recovery; never resume automatically after an unverified recovery.
7. Follow [PRODUCT.md](../../PRODUCT.md) and [DESIGN.md](../../DESIGN.md): desktop/laptop console only; phone compatibility is excluded. H5 probe viewports remain supported.

## Alternatives considered

- PostgreSQL or distributed locks: unnecessary for the current single-instance contract.
- Removing all locks or mechanically treating every `get`/`list` as read-only: unsafe because some getters expire runner credentials or initialize alert settings.
- Only renaming/splitting forced entry chunks: does not reduce the bytes a cold route loads.
- Eager Monaco or CDN-only Monaco: respectively increases ordinary navigation cost or depends on public internet access.
- One giant cleanup transaction, or archive-all followed by paged deletion: respectively monopolizes the writer or duplicates archives after partial failure.
- Automatic VACUUM or host Python replacement: operational changes outside this development task; require a separate maintenance decision.

## Consequences

- Existing API fields, project ownership, table columns, and persisted data formats remain unchanged; runtime status gains additive maintenance/drain signals. The only schema addition is a rebuildable partial cleanup index, installed after required columns exist. No data conversion or mixed-version adapter is introduced.
- Editors consume local assets only when opened; their initial download still costs time. G2 on-demand registrations require a rendered regression check when upgrading the chart dependency.
- WAL read transactions must stay short and must never enclose network calls or awaits. The maintenance gate is process-local: do not run multiple application processes against one database or place it on a network filesystem.
- This change serializes writable connections/checkpoints and opens readers in read-only mode; it is not a substitute for deploying a patched SQLite runtime. Do not run external writable SQLite tools concurrently.
- Unrelated pre-existing settings/token read-modify-write workflows are not redesigned here.
- API/UI inspection previews are diagnostic operations, not persisted run admissions. They remain available; their active contexts retain the resource pool's existing lifetime protection. This is not an execution sandbox for user-supplied scripts.
- No live database cleanup, compaction, restore, schema migration, or deployment is executed by this development task.

## Verification

Required evidence is tracked in [performance optimization verification](../performance-optimization.md): production build closure metrics, desktop rendered chart/editor checks, isolated-database snapshot/writer/maintenance tests, atomic batch cleanup and retry tests, and the complete repository backend test gate.

## Rollback

For the code-only rollout, restore the previous complete application build; the additional index is compatible with prior code, and table columns/data formats are unchanged. Before deployment, rehearse `init_db()` on an application-consistent copy of the active database to validate index creation against actual volume; synthetic tests do not prove that production step. Before any real database restoration or deployment, retain an application-consistent backup as required by repository rules. Restoring historical data is an explicit operation; retention deletion is not undone by reverting code.

## Revisit when

- PulseGuard becomes multi-process/multi-instance, serves untrusted tenants, or requires high-volume analytics.
- Metrics show long-lived snapshots, growing WAL files, or writer starvation.
- Monaco/G2 upgrades change package paths or registration contracts.
- A separate approved product decision adds phone-console support.
