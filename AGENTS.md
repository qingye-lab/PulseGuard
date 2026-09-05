# PulseGuard Repository Rules

## Scope

This file defines repository-specific workflow rules for PulseGuard. Higher-priority system, developer, and tool instructions still apply.

## Architecture Boundaries

- PulseGuard is a single-instance local or LAN UI/API probe console.
- Keep SQLite as the default persistence layer unless the deployment model changes to multi-instance, multi-user, or high-volume historical analytics.
- SQLite must run with explicit connection closing and WAL mode.
- User-defined Python probe scripts are a trusted-local-tool feature, not a security sandbox.
- Do not disable environment proxy inheritance for HTTP clients; support common proxy schemes through dependencies instead.

## Backend Workflow

- Use `uv` as the backend dependency manager.
- `pyproject.toml` and `uv.lock` are the backend dependency source of truth.
- Do not reintroduce `backend/requirements.txt`.
- Run backend tests from the repository root:

```sh
uv run python -m unittest discover -s backend/tests -p 'test_*.py' -v
```

- Run the backend locally from the repository root:

```sh
uv run uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8787 --reload
```

## Frontend Workflow

- Use npm with `frontend/package-lock.json`.
- Run frontend checks from `frontend/`:

```sh
npm ci
npm run build
```

- Keep heavy editor/debugging code lazily loaded. Monaco must not be eagerly imported into route/list entry chunks.
- Prefer route-level lazy loading for page modules when adding new pages.
- Use Ant Design as the primary mature open-source component system unless a feature has a concrete reason to use another library.
- Keep product-level visual tokens in `frontend/src/designSystem.ts` and `frontend/src/styles.css`; do not scatter one-off hex colors or gradients through page components.
- Import mature Ant Design primitives directly for controls, surfaces, data display, overlays, and feedback: `Button`, `Menu`, `Typography`, `Tag`, `Card`, `Statistic`, `Table`, `Form`, `Drawer`, `Modal`, `Tooltip`, `message`, and `notification`.
- Do not create local wrapper components for primitive controls such as Button, Tag, Card, Typography, or Menu.
- Shared design code is limited to tokens, CSS constraints, and business status mapping helpers. Reusable React components should encapsulate real product workflows, not restyle AntD primitives.
- Promote repeated page-local UI patterns to business components only when they carry domain behavior or data transformation.
- Route feedback by scenario: transient action results use AntD `message`; control-level context uses `Tooltip`; detailed or focused guidance uses `Popover`, `Drawer`, or `Modal`; page-level `Alert` is reserved for blocking errors.
- Do not add persistent informational callouts inside routine work surfaces.
- This is an operations console, not a marketing page. Avoid hero sections, decorative eyebrow labels, ornamental gradients, invented badges, and filler copy.
- Inputs, buttons, menus, and clickable rows must have stable focus states and accessible labels or visible text.
- Treat `PRODUCT.md` and `DESIGN.md` as the UI source of truth before changing product surfaces.
- The console targets desktop/laptop browsers; phone viewport compatibility, phone-specific navigation, and phone visual acceptance are out of scope. This does not restrict UI probes' H5 page mode or mobile target viewports.
- Do not use Inter as the default or only UI font. Use the functional UI, heading, numeric, and code font stacks from `frontend/src/styles.css`.
- Do not use bounce or elastic easing. In particular, do not introduce `cubic-bezier(0.12, 0.4, 0.29, 1.46)`.
- Do not animate layout properties such as `width`, `height`, `left`, or `right` for routine UI state. Prefer opacity, transform, color, and border-color.

## Verification Gates

Before handing off changes that touch code, run the smallest relevant set:

- Backend changes: `uv run python -m unittest discover -s backend/tests -p 'test_*.py' -v`
- Frontend changes: `cd frontend && npm run build`
- Docker/dependency changes: verify `uv lock` is current and Dockerfile consumes `uv.lock`
- Backend tests must not read `data/pulseguard.db`. Point `PULSEGUARD_DB_PATH` to an initialized temporary database when the production database may contain deployment or runtime state.

State any skipped gate and the reason.

## Docker Deployment

- Treat `data/` and `reports/` as user-owned persistent data. Record database fingerprints and create an application-consistent backup before recreating the container.
- Run host-side SQLite fingerprint checks and container API checks sequentially. Concurrent access to the bind-mounted database can cause transient lock-related API failures.
- Before deploying a schema change, copy the active SQLite database to a temporary path and run `storage.init_db()` against that copy. Fresh-database tests do not prove that an existing database can migrate.
- Add columns before creating indexes or queries that reference them. Cover each migration with a legacy-schema regression test.
- Run the standard `docker compose build` first. A quiet BuildKit interval can be an image download; wait for the command's final result before declaring it blocked.
- External base images are required only for a cold full build or a missing dependency layer. Check local application/base images and BuildKit cache before designing a fallback, but do not reuse a stale dependency layer when `pyproject.toml`, `uv.lock`, or frontend lock files changed.
- Do not recreate the running container until the new image has built successfully and the database-copy migration preflight has passed.
