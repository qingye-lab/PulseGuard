# PulseGuard Repository Rules

## Scope

This file defines repository-specific workflow rules for PulseGuard. Higher-priority system, developer, and tool instructions still apply.

## Branch Lines

This repository publishes to two remotes that carry **two different product lines**. They are not two progress levels of one line; they diverged at `388e375` and were both rewritten afterwards, so the same work appears on each side under different commit hashes.

| Remote | Line | Branding | Purpose |
| --- | --- | --- | --- |
| `origin` | Public | **Stripped** | GitHub release. No company, team, or internal host identity anywhere in content. |
| `corp` | Internal | **Preserved** | Internal GitLab deployment. May carry internal-only identity. |

- `origin` — `https://github.com/liyanqing90/PulseGuard.git`
- `corp` — `https://git.corpautohome.com/liyanqing/PulseGuard.git`

The two lines are kept in sync by content, not by history. Do not assume a commit on one side has a mirror on the other, and do not merge the lines into each other.

### Branding rule

Brand identity is a **build-time deployment setting**, never a literal in source that ships to both lines. Each surface has one knob, and both default to empty:

| Surface | Setting | Unset behaviour |
| --- | --- | --- |
| Console team badge | `VITE_APP_TEAM_NAME` (frontend build arg) | Badge is omitted from the layout |
| `GET /api/health` `team` field | `PULSEGUARD_TEAM_NAME` (backend env) | Field is an empty string |

Both are plumbed through `Dockerfile` build args so a single source tree produces either build. Never reintroduce a hardcoded company or team name — such as `新零售测试团队` — into source, tests, fixtures, or committed configuration.

A public build must be verifiable as unbranded before release:

```sh
cd frontend && npm run build          # no VITE_APP_TEAM_NAME set
grep -ri "新零售测试团队" frontend/dist backend/app   # must return nothing
curl -s http://127.0.0.1:8787/api/health             # "team" must be empty
```

`backend/tests/test_main.py` guards the `PULSEGUARD_TEAM_NAME` default, so a source literal reaching a release build fails the suite.

### Adding a change to both lines

1. Land the change on the internal line first, where it can be exercised against the real deployment.
2. Re-apply it to the public line as a **new commit**. Do not cherry-pick the internal commit if it carries branding; the cherry-pick will drag the identity across.
3. Run the unbranded verification above on the public side.
4. If the same work must exist on both, keep commit messages identical so the pairs stay recognisable in `git log`, and accept that the hashes will differ.

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
