# Scope generated UI selectors to repeated regions

- Status: accepted
- Date: 2026-09-01
- Owners: PulseGuard maintainers
- Scope: UI page inspection, generated assertion metadata, selector execution, element picker
- Supersedes: none
- Superseded by: none

## Context

The page inspector previously treated each candidate as one flat selector. On repeated H5 cards this made a title, city/mileage group, or price look like either one fixed text value or an unscoped class selector. The executor then waited only for the first match, so a healthy first card could hide missing or empty fields in later cards. Setup scripts also ran before an unconditional navigation, which discarded explicitly prepared page state such as a user-requested scroll.

## Evidence

| Claim | Kind | Source and environment | Observed | Reference | Freshness / redaction |
| --- | --- | --- | --- | --- | --- |
| The target H5 page renders 20 repeated vehicle cards with repeated title, city/mileage, and price regions | fact | local Chromium against the user-provided target | 2026-09-01 | `https://cartech.autohome.com.cn/usedcarpage` inspection | current live page; no credentials |
| Repeated selectors were executed through `.first` for visibility and non-empty checks | fact | local repository | 2026-09-01 | previous `backend/app/ui_assertions.py` selector branches | current checkout |
| Role locators were reported as unique without a real locator count | fact | local repository | 2026-09-01 | previous `backend/app/ui_scan_script.py` role option generation | current checkout |
| A setup-prepared page was navigated again before inspection or execution | fact | local repository | 2026-09-01 | previous `inspect_ui_page` and `run_structured_ui_check` navigation order | current checkout |

## Decision

1. Generated assertions may carry additive `scope_selector`, `relative_selector`, `match_mode`, and `frame_path` metadata. Existing persisted assertions without these fields retain first-match behavior.
2. Repeated card fields prefer a stable scope plus a stable relative selector and default to `match_mode=every_scope`. A repeated region selector without a field scope defaults to `all`.
3. Every generated locator is recounted by Playwright before it is returned. Zero-match locators are removed; uniqueness, match count, score, and stability are derived from the verified count.
4. Inspection covers the naturally loaded main document, open shadow roots, and child frames. It does not scroll unless the task setup script explicitly does so.
5. A setup script that already navigated to the configured entry URL owns that prepared page state; PulseGuard does not navigate to the same URL a second time.
6. Selector metadata remains persisted for deterministic execution but the element picker displays only the region name and selector value, without implementation-type or score labels.

## Alternatives considered

- Generate only unique selectors for the first card — rejected because it validates one current instance instead of the repeated page contract.
- Convert every field to its current text — rejected because vehicle names, cities, mileage, and prices are dynamic data, not fixed product copy.
- Replace the assertion schema outright — rejected because existing local tasks are a persisted contract and can remain valid with additive metadata.
- Automatically scroll the page before scanning — rejected because it changes page state, can trigger infinite lists, and conflicts with explicit setup ownership.

## Consequences

- Positive: a title, city/mileage group, or price rule can validate every loaded card; selector choices have real match counts; iframe and open-shadow content can be selected; explicit setup state is preserved.
- Negative: inspection performs additional locator counts and therefore uses more browser calls; closed shadow roots remain inaccessible by platform design.
- Compatibility: existing assertions continue to use `first`. New metadata is optional and ignored by older binaries during rollback, while new binaries accept old persisted rows.

## Verification

- Regression coverage exercises explicit setup navigation, repeated scopes, transient counts, duplicate roles, late-DOM candidates, BEM and hashed classes, open shadow roots, and iframes.
- The live target page is inspected without a setup scroll and returns card, name, city/mileage, and price regions with verified repeated match counts.
- Backend suite and frontend production build are required before deployment.

## Revisit when

- PulseGuard adds closed-shadow instrumentation, cross-page workflow capture, virtualized-list traversal, or a new persisted assertion language.
