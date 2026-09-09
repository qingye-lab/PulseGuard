"""Prepare the approved used-car script-module migration.

The default operation is a read-only plan.  ``--apply`` is required before
this script can call the project library/check APIs, and ``--publish`` is an
additional explicit gate for publishing the validated candidate.  The script
never opens PulseGuard's SQLite database and only mutates the named project
through its HTTP API.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Mapping

# Make the repository-root ``backend`` package importable when this file is
# invoked directly (``python scripts/migrate_usedcar_modules.py``).
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.script_modules import (
    analyze_script,
    capture_module_bundle,
    module_metadata,
    source_hash,
    validate_module_source,
)


TARGET_IDS = tuple(range(34, 49))
DYNAMIC_IDS = (34, 40, 41, 42, 43, 44, 45, 46, 47, 48)
STATIC_IDS = tuple(range(35, 40))
REQUEST_COUNTS = {
    34: 1,
    35: 1,
    36: 1,
    37: 1,
    38: 1,
    39: 1,
    40: 2,
    41: 3,
    42: 5,
    43: 3,
    44: 3,
    45: 4,
    46: 5,
    47: 3,
    48: 2,
}
REQUEST_CHAINS = {
    34: ["product/list (target)"],
    35: ["getHeaderData (target)"],
    36: ["activity/get/city/list (target)"],
    37: ["city/list (target)"],
    38: ["carAreaEntry/getEntryItems (target)"],
    39: ["getBannerJson (target)"],
    40: ["product/list (dependency)", "GetAdViewV2 (target)"],
    41: ["product/list (dependency)", "usedcar/sign (dependency)", "getcarinfo (target)"],
    42: [
        "product/list (dependency)",
        "usedcar/sign (dependency)",
        "getcarinfo (dependency)",
        "usedcar/sign (dependency)",
        "getcarextinfo (target)",
    ],
    43: ["product/list (dependency)", "usedcar/sign (dependency)", "getparamtypeitems (target)"],
    44: ["product/list (dependency)", "usedcar/sign (dependency)", "recommendWithSessionId (target)"],
    45: [
        "product/list (dependency)",
        "usedcar/sign (dependency)",
        "getcarinfo (dependency)",
        "newcarpriceintax (target)",
    ],
    46: [
        "product/list (dependency)",
        "usedcar/sign (dependency)",
        "getcarinfo (dependency)",
        "usedcar/sign (dependency)",
        "getloanresult (target)",
    ],
    47: ["product/list (dependency)", "usedcar/sign (dependency)", "order-status (target)"],
    48: ["product/list (dependency)", "usedcar/sign (target)"],
}


class MigrationPlanError(RuntimeError):
    """The reviewed target no longer matches the immutable plan inputs."""


CHECK_UPDATE_FIELDS = (
    "name",
    "type",
    "enabled",
    "interval_seconds",
    "timeout_ms",
    "entry_url",
    "viewport_mode",
    "method",
    "headers_json",
    "body",
    "assertions_json",
    "setup_script",
    "script",
    "tags",
    "alert_policy_json",
    "runner_selection_mode",
    "runner_ids",
    "browser_selection_mode",
    "browser_types",
)


def _check_update_payload(check: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the public PUT payload from a list/detail check response."""

    return {field: copy.deepcopy(check.get(field)) for field in CHECK_UPDATE_FIELDS}


def _default_temp_path(name: str) -> Path:
    return Path(tempfile.gettempdir()) / name


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MigrationPlanError(f"输入文件不存在：{path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationPlanError(f"输入文件不可读：{path}") from exc


def _as_checks(value: Any, label: str) -> list[dict[str, Any]]:
    if isinstance(value, list):
        checks = value
    elif isinstance(value, Mapping):
        checks = value.get("checks") or value.get("items") or value.get("data")
    else:
        checks = None
    if not isinstance(checks, list) or not all(isinstance(item, Mapping) for item in checks):
        raise MigrationPlanError(f"{label} 不是任务列表")
    return [dict(item) for item in checks]


def _payload_by_id(update: Mapping[str, Any]) -> dict[int, dict[str, Any]]:
    items = update.get("checks")
    if not isinstance(items, list):
        raise MigrationPlanError("更新候选缺少 checks 列表")
    result: dict[int, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("payload"), Mapping):
            raise MigrationPlanError("更新候选的 checks 项缺少 payload")
        check_id = int(item.get("id") or 0)
        if check_id in result:
            raise MigrationPlanError(f"更新候选存在重复任务：{check_id}")
        result[check_id] = dict(item["payload"])
    return result


def _canonical_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _assertion_count(value: Any) -> int:
    parsed = _canonical_json(value)
    return len(parsed) if isinstance(parsed, list) else 0


def _config_fingerprint(check: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "name",
        "project_id",
        "type",
        "enabled",
        "interval_seconds",
        "timeout_ms",
        "entry_url",
        "viewport_mode",
        "method",
        "headers_json",
        "body",
        "assertions_json",
        "setup_script",
        "tags",
        "alert_policy_json",
        "runner_selection_mode",
        "runner_ids",
        "browser_selection_mode",
        "browser_types",
    )
    return {field: _canonical_json(check.get(field)) for field in fields}


def _assert_reviewed_inputs(live_value: Any, update: Mapping[str, Any], project_id: int) -> dict[int, dict[str, Any]]:
    live = {int(item.get("id") or 0): item for item in _as_checks(live_value, "live 快照")}
    payloads = _payload_by_id(update)
    if set(live) != set(TARGET_IDS) or set(payloads) != set(TARGET_IDS):
        raise MigrationPlanError(f"本次只允许任务 ID 34–48；得到 live={sorted(live)} update={sorted(payloads)}")
    for check_id in TARGET_IDS:
        current = live[check_id]
        payload = payloads[check_id]
        if int(current.get("project_id") or 0) != project_id:
            raise MigrationPlanError(f"任务 {check_id} 不属于项目 {project_id}")
        if _config_fingerprint(current)["project_id"] != project_id:
            raise MigrationPlanError(f"任务 {check_id} 项目归属无效")
        if source_hash(current.get("script") or "") != source_hash(payload.get("script") or ""):
            raise MigrationPlanError(f"任务 {check_id} 的源脚本哈希与已确认快照不一致")
        payload_snapshot = {**payload, "project_id": project_id}
        if _config_fingerprint(payload_snapshot) != _config_fingerprint(current):
            raise MigrationPlanError(f"任务 {check_id} 更新候选的完整配置与已确认快照不一致")
        if int(payload.get("interval_seconds") or 0) != 60:
            raise MigrationPlanError(f"任务 {check_id} 间隔不是已确认的 60 秒")
        if list(payload.get("runner_ids") or []) != ["local"]:
            raise MigrationPlanError(f"任务 {check_id} 执行节点不是 local")
    return live


def _candidate_scripts() -> dict[int, str]:
    return {
        34: '''async def check(ctx):
    module = ctx.modules.use("usedcar")
    await ctx.http.get(ctx.entry_url, params=module.list_params(ctx), headers=module.HEADERS)
''',
        40: '''async def check(ctx):
    module = ctx.modules.use("usedcar")
    sample = await module.list_sample(ctx)
    await ctx.http.get(ctx.entry_url, params={"spuid": sample["spu"], "skuid": sample["sku"]}, headers=module.HEADERS)
''',
        41: '''async def check(ctx):
    module = ctx.modules.use("usedcar")
    sample = await module.list_sample(ctx)
    sign_data = module.signature_body(ctx, sample)
    sign = await module.sign_for(ctx, "getcarinfo", sign_data)
    await ctx.http.get(ctx.entry_url, params={**sign_data, "_sign": sign}, headers=module.HEADERS)
''',
        42: '''async def check(ctx):
    module = ctx.modules.use("usedcar")
    sample = await module.list_sample(ctx)
    info = await module.fetch_car_info(ctx, sample)
    series = str(info.get("seriesid") or sample["series"])
    if not series:
        ctx.fail("依赖失败：getcarinfo 缺少 seriesid")
    sign_data = {"skuid": sample["sku"], "spuid": sample["spu"], "seriesid": series, "ucuserauth": "", "fqabtest": "X", "ab_version": 1, "_appid": "xls.ios"}
    sign = await module.sign_for(ctx, "getcarextinfo", sign_data)
    await ctx.http.get(ctx.entry_url, params={**sign_data, "_sign": sign}, headers=module.HEADERS)
''',
        43: '''async def check(ctx):
    module = ctx.modules.use("usedcar")
    sample = await module.list_sample(ctx)
    sign_data = {"skuId": module.as_int(ctx, sample["sku"], "skuId"), "spuId": module.as_int(ctx, sample["spu"], "spuId"), "_appid": "xls.ios"}
    sign = await module.sign_for(ctx, "getparamtypeitems", sign_data)
    await ctx.http.get(ctx.entry_url, params={**sign_data, "_sign": sign}, headers=module.HEADERS)
''',
        44: '''async def check(ctx):
    module = ctx.modules.use("usedcar")
    sample = await module.list_sample(ctx)
    sign_data = {"spuid": module.as_int(ctx, sample["spu"], "spuid"), "skuid": module.as_int(ctx, sample["sku"], "skuid"), "pageSize": 30, "pageNumber": 1, "pcpopclub": "", "deviceId": "", "cityId": "0", "reqId": str(int(asyncio.get_running_loop().time() * 1000000)), "_appid": "xls.ios"}
    sign = await module.sign_for(ctx, "recommendWithSessionId", sign_data)
    await ctx.http.get(ctx.entry_url, params={**sign_data, "_sign": sign}, headers=module.HEADERS)
''',
        45: '''async def check(ctx):
    module = ctx.modules.use("usedcar")
    sample = await module.list_sample(ctx)
    info = await module.fetch_car_info(ctx, sample, ("pid", "cid", "specid"))
    params = {"pid": module.as_int(ctx, info.get("pid"), "pid"), "cid": module.as_int(ctx, info.get("cid"), "cid"), "specid": module.as_int(ctx, info.get("specid"), "specid"), "_appid": "xls.ios", "personalizedPush": "", "deviceid": "", "userid": ""}
    await ctx.http.get(ctx.entry_url, params=params, headers=module.HEADERS)
''',
        46: '''async def check(ctx):
    module = ctx.modules.use("usedcar")
    sample = await module.list_sample(ctx)
    info = await module.fetch_car_info(ctx, sample)
    finance_price = str(info.get("price") or sample["price"] or "")
    if not finance_price:
        ctx.fail("依赖失败：getcarinfo 缺少 price，无法准备月供请求")
    sign_data = {"skuid": sample["sku"], "spuid": sample["spu"], "dpbl": "30", "pyqs": "36", "ab_version": "1", "finance_price": finance_price, "_appid": "xls.ios"}
    sign = await module.sign_for(ctx, "getloanresult", sign_data)
    await ctx.http.get(ctx.entry_url, params={**sign_data, "_sign": sign}, headers=module.HEADERS)
''',
        47: '''async def check(ctx):
    module = ctx.modules.use("usedcar")
    sample = await module.list_sample(ctx)
    sign_data = {"spuid": sample["spu"], "skuid": sample["sku"], "_appid": "xls.ios"}
    sign = await module.sign_for(ctx, "order-status", sign_data)
    await ctx.http.get(ctx.entry_url, params={**sign_data, "_sign": sign}, headers=module.HEADERS)
''',
        48: '''async def check(ctx):
    module = ctx.modules.use("usedcar")
    sample = await module.list_sample(ctx)
    await ctx.http.post(ctx.entry_url, json=module.signature_body(ctx, sample), headers=module.HEADERS)
''',
    }


def build_verification_matrix(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Describe module usage, assertion count, and config identity per probe."""

    bundle = plan.get("module_bundle")
    if not isinstance(bundle, Mapping) or not isinstance(bundle.get("modules"), list):
        raise MigrationPlanError("迁移计划缺少模块 bundle")
    module_items = [item for item in bundle["modules"] if isinstance(item, Mapping)]
    if len(module_items) != 1 or str(module_items[0].get("name") or "") != "usedcar":
        raise MigrationPlanError("迁移计划必须包含唯一 usedcar 模块")
    module = module_items[0]
    module_hash = str(module.get("hash") or "")
    dynamic_metadata = module_metadata(bundle)
    checks = {item.get("id"): item for item in plan.get("checks", []) if isinstance(item, Mapping)}
    review_index = plan.get("review_index")
    if not isinstance(review_index, Mapping):
        raise MigrationPlanError("迁移计划缺少 review_index")
    matrix: dict[str, dict[str, Any]] = {}
    for check_id in TARGET_IDS:
        candidate = checks.get(check_id)
        if not isinstance(candidate, Mapping) or not isinstance(candidate.get("payload"), Mapping):
            raise MigrationPlanError(f"迁移计划缺少任务 {check_id} candidate payload")
        reviewed = review_index.get(str(check_id))
        if not isinstance(reviewed, Mapping):
            raise MigrationPlanError(f"迁移计划缺少任务 {check_id} 配置指纹")
        uses_module = check_id in DYNAMIC_IDS
        matrix[str(check_id)] = {
            "module_refs": ["usedcar"] if uses_module else [],
            "module_hashes": {"usedcar": module_hash} if uses_module else {},
            "module_metadata": dynamic_metadata if uses_module else None,
            "assertion_count": _assertion_count(candidate["payload"].get("assertions_json")),
            "config_fingerprint": copy.deepcopy(reviewed.get("config_fingerprint") or {}),
            "script_hash": str(reviewed.get("script_hash") or ""),
        }
    return {
        "target_ids": list(TARGET_IDS),
        "dynamic_ids": list(DYNAMIC_IDS),
        "static_ids": list(STATIC_IDS),
        "assertion_rule_count": sum(int(item["assertion_count"]) for item in matrix.values()),
        "checks": matrix,
    }


def _build_plan(live_value: Any, update: Mapping[str, Any], module_source: str, project_id: int) -> dict[str, Any]:
    live = _assert_reviewed_inputs(live_value, update, project_id)
    scripts = _candidate_scripts()
    module_validation = validate_module_source(module_source, name="usedcar", filename="<candidate:usedcar>")
    if not module_validation.get("valid"):
        raise MigrationPlanError(f"usedcar 模块校验失败：{module_validation.get('diagnostics')}")
    bundle = capture_module_bundle(
        project_id,
        None,
        [{"name": "usedcar", "source": module_source, "description": "二手车探活公共方法"}],
        release_hash=None,
    )
    static_validation: dict[int, dict[str, Any]] = {}
    payloads = _payload_by_id(update)
    candidate_checks: list[dict[str, Any]] = []
    for check_id in TARGET_IDS:
        payload = copy.deepcopy(payloads[check_id])
        if check_id in scripts:
            payload["script"] = scripts[check_id]
            analysis = analyze_script(
                scripts[check_id],
                entry_points=("check",),
                module_bundle=bundle,
                script_label=f"task-{check_id}",
            )
            static_validation[check_id] = {
                "valid": bool(analysis.get("valid")),
                "references": analysis.get("references", []),
                "diagnostics": analysis.get("diagnostics", []),
            }
            if not analysis.get("valid"):
                raise MigrationPlanError(f"任务 {check_id} 静态校验失败：{analysis.get('diagnostics')}")
            if "ctx.entry_url" not in scripts[check_id]:
                raise MigrationPlanError(f"任务 {check_id} 的目标请求没有使用 ctx.entry_url")
        else:
            if payload.get("script") != "":
                raise MigrationPlanError(f"静态任务 {check_id} 脚本必须保持为空")
        candidate_checks.append({"id": check_id, "payload": payload})
    plan = {
        "schema": "pulseguard.usedcar.api-rule-migration.v1",
        "generated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "source": {
            "project_id": project_id,
            "remote_mutation": False,
        },
        "policy": {
            "module": "usedcar",
            "dynamic_scripts": "module prepares dependencies; target request remains last",
            "static_checks": "35-39 preserve configured request and empty scripts",
            "manual_snapshot_duration_compensation": "removed; runner owns request/task timings",
            "optional_data": "type/business availability checks preserve empty optional data acceptance",
            "preserve_ids": list(TARGET_IDS),
            "preserve_history": True,
            "preserve_interval_seconds": 60,
            "preserve_notifications": True,
            "preserve_nodes": ["local"],
            "remote_write": False,
        },
        "module_bundle": bundle,
        "request_count_baseline": REQUEST_COUNTS,
        "request_chains": REQUEST_CHAINS,
        "checks": candidate_checks,
        "static_validation": static_validation,
        "review_index": {
            str(check_id): {
                "name": live[check_id].get("name"),
                "project_id": project_id,
                "script_hash": source_hash(live[check_id].get("script") or ""),
                "config_fingerprint": _config_fingerprint(live[check_id]),
                "enabled": payloads[check_id].get("enabled"),
                "interval_seconds": payloads[check_id].get("interval_seconds"),
                "timeout_ms": payloads[check_id].get("timeout_ms"),
                "runner_selection_mode": payloads[check_id].get("runner_selection_mode"),
                "runner_ids": payloads[check_id].get("runner_ids"),
                "alert_policy_json": payloads[check_id].get("alert_policy_json"),
                "tags": payloads[check_id].get("tags"),
                "script_changed_to_module": check_id in scripts,
                "static_script_preserved": check_id in STATIC_IDS and payloads[check_id].get("script") == "",
            }
            for check_id in TARGET_IDS
        },
    }
    plan["verification_matrix"] = build_verification_matrix(plan)
    return plan


def _write_plan(
    plan: Mapping[str, Any],
    output_dir: Path,
    live_value: Any,
    module_source: str,
    *,
    live_path: Path,
    update_path: Path,
    remote_path: Path | None,
) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    bundle = plan["module_bundle"]
    metadata = module_metadata(bundle)
    module_json = {
        "schema": "pulseguard.script-module.candidate.v1",
        "generated_at": plan["generated_at"],
        "project_id": plan["source"]["project_id"],
        "release_id": None,
        "release_hash": None,
        "module": bundle["modules"][0],
        "metadata": metadata,
        "validation": {"valid": True, "export_metadata": bundle["modules"][0].get("export_metadata", []), "diagnostics": []},
    }
    module_json_path = output_dir / "pg-usedcar-module-candidate.json"
    module_json_path.write_text(json.dumps(module_json, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    module_source_path = output_dir / "pg-usedcar-module-candidate.py"
    module_source_path.write_text(module_source, encoding="utf-8")
    migration = copy.deepcopy(dict(plan))
    migration["source"].update(
        {
            "local_live_snapshot": str(live_path),
            "local_update_payload": str(update_path),
            "remote_readback": str(remote_path) if remote_path else None,
        }
    )
    migration_path = output_dir / "pg-usedcar-rules-migration-candidate.json"
    migration_path.write_text(json.dumps(migration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    live_checks = _as_checks(live_value, "live 快照")
    rollback = {
        "schema": "pulseguard.usedcar.api-rule-rollback.v1",
        "generated_at": plan["generated_at"],
        "project_id": plan["source"]["project_id"],
        "remote_mutation": False,
        "checks": [copy.deepcopy(item) for item in sorted(live_checks, key=lambda item: int(item.get("id") or 0)) if int(item.get("id") or 0) in TARGET_IDS],
    }
    rollback_path = output_dir / "pg-usedcar-rules-rollback-v1.json"
    rollback_path.write_text(json.dumps(rollback, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    scripts = _candidate_scripts()
    for check_id, script in scripts.items():
        (output_dir / f"pg-usedcar-module-script-{check_id:02d}.py").write_text(script, encoding="utf-8")
    return {
        "module_source": str(module_source_path),
        "module_json": str(module_json_path),
        "migration_json": str(migration_path),
        "rollback_json": str(rollback_path),
    }


def _write_report(
    plan: Mapping[str, Any],
    output_dir: Path,
    paths: Mapping[str, str],
    *,
    live_path: Path,
    update_path: Path,
    remote_path: Path | None,
    remote_status: str,
) -> Path:
    bundle = plan["module_bundle"]
    matrix = plan.get("verification_matrix") or build_verification_matrix(plan)
    lines = [
        "# Used-car module migration candidate",
        "",
        f"- Prepared at: {plan['generated_at']} (Asia/Shanghai)",
        f"- Remote readback: {remote_status}",
        "- Remote operation during plan generation: read-only; no PulseGuard write endpoint or database mutation.",
        f"- Local inputs: `{live_path}` and `{update_path}`.",
        f"- Candidate bundle: project `{plan['source']['project_id']}`, release_id `null`, release_hash `null`, content_hash `{bundle['content_hash']}`.",
        f"- Module source SHA-256: `{source_hash(bundle['modules'][0]['source'])}`.",
        "",
        "## Candidate artifacts",
        "",
        f"- Module source: `{paths['module_source']}`",
        f"- Module bundle metadata: `{paths['module_json']}`",
        f"- Rule migration payload: `{paths['migration_json']}`",
        f"- Independent rollback snapshot: `{paths['rollback_json']}`",
        "",
        "## Preservation contract",
        "",
        "- IDs 34–48, project scope, names, enabled state, history-related persisted fields, 60-second interval, timeout, alert policy, tags, and `runner_ids=[\"local\"]` remain bound to the reviewed input payload.",
        "- Static checks 35–39 retain their configured request and empty script.",
        "- Dynamic checks 34 and 40–48 use `ctx.modules.use(\"usedcar\")`; each target request uses `ctx.entry_url` and remains the final call.",
        "- Direct request-snapshot URL writes and manual timing compensation were removed; RunContext owns request/task duration and last-response capture.",
        "",
        "## Request baseline",
        "",
        "| Task | Requests | Candidate chain |",
        "|---:|---:|---|",
    ]
    for check_id in TARGET_IDS:
        lines.append(f"| {check_id} | {REQUEST_COUNTS[check_id]} | " + " → ".join(f"`{item}`" for item in REQUEST_CHAINS[check_id]) + " |")
    lines += [
        "",
        "## Verification matrix",
        "",
        f"- Target scope: {len(matrix['target_ids'])} probes; assertion rules: {matrix['assertion_rule_count']}.",
        "- Dynamic probes 34 and 40–48 carry `usedcar` module metadata and its source hash; static probes 35–39 carry no module metadata.",
        "- Every probe has a reviewed full configuration fingerprint in the plan; the remote verifier rechecks it before draft debug.",
        "- After deployment run `uv run python scripts/verify_usedcar_remote.py --base-url <base-url> --project-id 2 --plan <migration-json>`; it runs 15 positive manual checks, then sends 15 reverse cases only to `POST /api/checks/debug`, and records the scheduled poll evidence.",
        "",
        "| Task | Module refs | Module hashes | Assertions | Config fingerprint |",
        "|---:|---|---|---:|---|",
    ]
    for check_id in TARGET_IDS:
        item = matrix["checks"][str(check_id)]
        refs = ", ".join(item["module_refs"]) or "none"
        hashes = ", ".join(f"{name}:{value}" for name, value in item["module_hashes"].items()) or "none"
        lines.append(
            f"| {check_id} | `{refs}` | `{hashes}` | {item['assertion_count']} | present |"
        )
    lines += [
        "",
        "## Verification",
        "",
        "- The module and all migrated scripts passed the canonical AST validation; static validation failures: none.",
        "- Post-deploy verification first runs all 15 candidates through `POST /api/checks/{id}/run` and checks each request/assertion count plus frozen `diagnostics.modules` metadata, then uses `POST /api/checks/debug` for all 15 negative payloads; the reverse phase does not PUT checks, publish, enable/disable, or update health status.",
        "- Scheduled evidence uses `GET /api/checks?type=api` and `GET /api/runs?type=api&limit=500`; after a 60-second lower bound it requires a new finished `status=ok` run for every enabled ID with matching request/assertion counts, module metadata, `trigger=scheduled`, `observation_kind=observation`, and `affects_health=true`.",
        "- Apply mode is intentionally opt-in and uses `PUT /api/projects/{project_id}/library/draft` and `PUT /api/checks/{id}` through HTTP; it never edits SQLite directly.",
        "- Rollback data is written before any future apply and contains the selected live definitions for IDs 34–48.",
    ]
    report_path = output_dir / "pg-usedcar-migration-report.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path


def _api_json(base_url: str, path: str, project_id: int, *, method: str = "GET", payload: Any = None) -> Any:
    url = base_url.rstrip("/") + path
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-PulseGuard-Project-Id": str(project_id),
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise MigrationPlanError(f"API {method} {path} 返回 HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise MigrationPlanError(f"API {method} {path} 读取失败") from exc


def _remote_readback(base_url: str, project_id: int, destination: Path) -> Any:
    value = _api_json(base_url, "/api/checks?type=api", project_id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return value


def _apply(plan: Mapping[str, Any], base_url: str, project_id: int, *, publish: bool) -> dict[str, Any]:
    if not publish:
        raise MigrationPlanError(
            "--apply 必须同时指定 --publish；先发布无引用模块，再逐项 debug/手动验证任务，避免 live 任务引用未发布模块"
        )

    # Re-read before mutation so an old local snapshot cannot overwrite a
    # changed project.  A source hash alone is insufficient: a reviewed URL,
    # assertion, runner, or alert policy change must also abort the apply.
    remote = _api_json(base_url, "/api/checks?type=api", project_id)
    review_index = plan.get("review_index")
    if not isinstance(review_index, Mapping):
        raise MigrationPlanError("迁移计划缺少 review_index")
    remote_checks = {int(item.get("id") or 0): item for item in _as_checks(remote, "远端任务")}
    if set(remote_checks) != set(TARGET_IDS):
        raise MigrationPlanError(f"远端任务集合已变化：{sorted(remote_checks)}")
    candidate_checks = {int(item["id"]): item["payload"] for item in plan["checks"]}
    if set(candidate_checks) != set(TARGET_IDS):
        raise MigrationPlanError("迁移计划的任务集合已变化")
    original_payloads: dict[int, dict[str, Any]] = {}
    for check_id in TARGET_IDS:
        remote_item = remote_checks[check_id]
        reviewed = review_index.get(str(check_id))
        if not isinstance(reviewed, Mapping):
            raise MigrationPlanError(f"任务 {check_id} 缺少 reviewed 配置指纹")
        actual_script_hash = source_hash(remote_item.get("script") or "")
        if actual_script_hash != str(reviewed.get("script_hash") or ""):
            raise MigrationPlanError(f"任务 {check_id} 的源脚本哈希已变化，已中止")
        actual_fingerprint = _config_fingerprint(remote_item)
        if actual_fingerprint != reviewed.get("config_fingerprint"):
            raise MigrationPlanError(f"任务 {check_id} 的完整配置指纹已变化，已中止")
        original_payloads[check_id] = _check_update_payload(remote_item)

    def _require_check_snapshot(
        check_id: int,
        *,
        script_hash: str,
        config_fingerprint: Mapping[str, Any],
        phase: str,
    ) -> dict[str, Any]:
        current_value = _api_json(base_url, f"/api/checks/{check_id}", project_id)
        if not isinstance(current_value, Mapping):
            raise MigrationPlanError(f"任务 {check_id} {phase}读取结果无效")
        current = dict(current_value)
        if source_hash(current.get("script") or "") != script_hash:
            raise MigrationPlanError(f"任务 {check_id} {phase}源脚本已变化，已中止")
        if _config_fingerprint(current) != dict(config_fingerprint):
            raise MigrationPlanError(f"任务 {check_id} {phase}完整配置已变化，已中止")
        return current

    library = _api_json(base_url, f"/api/projects/{project_id}/library", project_id)
    if not isinstance(library, Mapping) or not isinstance(library.get("modules"), list):
        raise MigrationPlanError("远端公共方法返回缺少可合并的 modules 列表，拒绝覆盖")
    published_library = library.get("published")
    if library.get("modules") or (
        isinstance(published_library, Mapping) and published_library.get("modules")
    ):
        raise MigrationPlanError(
            "远端公共方法草稿或已发布版本非空；本次首次迁移拒绝隐式合并/覆盖，请先人工审核库版本"
        )
    expected_revision = int(library.get("draft_revision") or 0)
    module = plan["module_bundle"]["modules"][0]
    merged_modules = [
        {"name": module["name"], "source": module["source"], "description": module.get("description", "")}
    ]
    draft = _api_json(
        base_url,
        f"/api/projects/{project_id}/library/draft",
        project_id,
        method="PUT",
        payload={"expected_revision": expected_revision, "modules": merged_modules},
    )
    candidate_revision = int(draft.get("draft_revision") or 0)
    if candidate_revision < 1:
        raise MigrationPlanError("远端未返回有效公共方法草稿 revision")

    # Validate and publish the module while no live check refers to it.  This
    # creates a safe, independently useful release before any check PUT.
    validation = _api_json(
        base_url,
        f"/api/projects/{project_id}/library/validate",
        project_id,
        method="POST",
        payload={"candidate_revision": candidate_revision, "include_disabled": False},
    )
    validation_id = int(validation.get("validation_id") or 0)
    if validation_id <= 0:
        raise MigrationPlanError("远端未返回公共方法验证 ID")
    validation_result: dict[str, Any] = {}
    for _ in range(120):
        validation_result = dict(
            _api_json(
                base_url,
                f"/api/projects/{project_id}/library/validations/{validation_id}",
                project_id,
            )
        )
        status = str(validation_result.get("status") or "")
        if status not in {"pending", "queued", "running"}:
            break
        time.sleep(1)
    if str(validation_result.get("status") or "") != "succeeded":
        raise MigrationPlanError("公共方法验证未成功，未发布候选")
    published = _api_json(
        base_url,
        f"/api/projects/{project_id}/library/publish",
        project_id,
        method="POST",
        payload={"candidate_revision": candidate_revision, "validation_id": validation_id},
    )

    # The first live reference is a canary.  Every candidate is debugged before
    # its PUT, and the canary is then manually run before the remaining changes.
    ordered_ids = [41, *[check_id for check_id in DYNAMIC_IDS if check_id != 41]]
    updated_ids: list[int] = []
    attempted_ids: list[int] = []

    def _require_ok(result: Any, label: str) -> None:
        status = str(result.get("status") or "") if isinstance(result, Mapping) else ""
        if status != "ok":
            raise MigrationPlanError(f"{label} 未成功：status={status or 'unknown'}")

    try:
        for check_id in ordered_ids:
            debug_result = _api_json(
                base_url,
                "/api/checks/debug",
                project_id,
                method="POST",
                payload=candidate_checks[check_id],
            )
            _require_ok(debug_result, f"任务 {check_id} 候选 debug")
            reviewed = review_index[str(check_id)]
            _require_check_snapshot(
                check_id,
                script_hash=str(reviewed.get("script_hash") or ""),
                config_fingerprint=reviewed.get("config_fingerprint") or {},
                phase="debug后写入前",
            )
            attempted_ids.append(check_id)
            _api_json(
                base_url,
                f"/api/checks/{check_id}",
                project_id,
                method="PUT",
                payload=candidate_checks[check_id],
            )
            updated_ids.append(check_id)
            if check_id == 41:
                manual_result = _api_json(
                    base_url,
                    "/api/checks/41/run",
                    project_id,
                    method="POST",
                )
                _require_ok(manual_result, "任务 41 手动运行")
    except Exception as exc:
        rollback_errors: list[str] = []
        for check_id in reversed(attempted_ids):
            try:
                candidate_payload = candidate_checks[check_id]
                candidate_snapshot = {**candidate_payload, "project_id": project_id}
                current_value = _api_json(base_url, f"/api/checks/{check_id}", project_id)
                if not isinstance(current_value, Mapping):
                    raise MigrationPlanError(f"任务 {check_id} 回滚前读取结果无效")
                current = dict(current_value)
                original_review = review_index[str(check_id)]
                if (
                    source_hash(current.get("script") or "") == str(original_review.get("script_hash") or "")
                    and _config_fingerprint(current) == original_review.get("config_fingerprint")
                ):
                    # The PUT may have failed before the server committed;
                    # there is nothing to restore.
                    continue
                if (
                    source_hash(current.get("script") or "") != source_hash(candidate_payload.get("script") or "")
                    or _config_fingerprint(current) != _config_fingerprint(candidate_snapshot)
                ):
                    raise MigrationPlanError(f"任务 {check_id} 回滚前检测到并行修改，拒绝覆盖")
                _api_json(
                    base_url,
                    f"/api/checks/{check_id}",
                    project_id,
                    method="PUT",
                    payload=original_payloads[check_id],
                )
            except Exception as rollback_exc:  # pragma: no cover - requires API outage during rollback
                rollback_errors.append(f"{check_id}: {rollback_exc}")
        suffix = f"；回滚失败：{', '.join(rollback_errors)}" if rollback_errors else "；已回滚已修改任务"
        if isinstance(exc, MigrationPlanError):
            raise MigrationPlanError(f"{exc}{suffix}") from exc
        raise MigrationPlanError(f"迁移写入失败：{exc}{suffix}") from exc

    return {
        "project_id": project_id,
        "candidate_revision": candidate_revision,
        "validation_id": validation_id,
        "updated_check_ids": updated_ids,
        "validation_status": validation_result.get("status"),
        "published": bool(published),
        "rollback_on_failure": True,
    }


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare or explicitly apply the reviewed used-car module migration")
    parser.add_argument("--project-id", type=int, default=None, help="Target project; required with --apply")
    parser.add_argument("--live-snapshot", type=Path, default=_default_temp_path("pg-live-applied-rules.json"))
    parser.add_argument("--update-payload", type=Path, default=_default_temp_path("pg-usedcar-rules-update.json"))
    parser.add_argument("--module-source", type=Path, default=Path(__file__).resolve().parent / "examples" / "usedcar_module.py")
    parser.add_argument("--output-dir", type=Path, default=Path(tempfile.gettempdir()))
    parser.add_argument("--remote-url", help="Read-only remote base URL for a fresh plan readback")
    parser.add_argument("--base-url", help="PulseGuard base URL used only with --apply")
    parser.add_argument("--apply", action="store_true", help="Write the named project through HTTP APIs")
    parser.add_argument("--publish", action="store_true", help="Publish after --apply validation succeeds")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv or sys.argv[1:]))
    if args.publish and not args.apply:
        raise MigrationPlanError("--publish 必须与 --apply 一起使用")
    if args.apply and not args.publish:
        raise MigrationPlanError("--apply 必须同时指定 --publish；不允许留下引用未发布模块的 live 任务")
    if args.apply and (args.project_id is None or not args.base_url):
        raise MigrationPlanError("--apply 必须同时指定 --project-id 和 --base-url")
    project_id = int(args.project_id or 2)
    live_value = _read_json(args.live_snapshot)
    update_value = _read_json(args.update_payload)
    if not isinstance(update_value, Mapping):
        raise MigrationPlanError("更新候选必须是 JSON Object")
    module_source = args.module_source.read_text(encoding="utf-8")
    plan = _build_plan(live_value, update_value, module_source, project_id)
    remote_path: Path | None = None
    remote_status = "not requested"
    if args.remote_url:
        remote_path = args.output_dir / "pg-usedcar-remote-live-readback.json"
        remote_value = _remote_readback(args.remote_url, project_id, remote_path)
        _assert_reviewed_inputs(remote_value, update_value, project_id)
        remote_status = f"read-only GET succeeded; saved to {remote_path}"
    paths = _write_plan(
        plan,
        args.output_dir,
        live_value,
        module_source,
        live_path=args.live_snapshot,
        update_path=args.update_payload,
        remote_path=remote_path,
    )
    report = _write_report(
        plan,
        args.output_dir,
        paths,
        live_path=args.live_snapshot,
        update_path=args.update_payload,
        remote_path=remote_path,
        remote_status=remote_status,
    )
    apply_result = None
    if args.apply:
        apply_result = _apply(plan, args.base_url, project_id, publish=bool(args.publish))
    print(
        json.dumps(
            {
                "mode": "apply" if args.apply else "plan",
                "project_id": project_id,
                "module_hash": source_hash(module_source),
                "content_hash": plan["module_bundle"]["content_hash"],
                "report": str(report),
                "artifacts": paths,
                "apply": apply_result,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MigrationPlanError as exc:
        print(f"migration plan aborted: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
