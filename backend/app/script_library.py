"""Storage facing adapters for the runtime owned script module contract.

`script_modules` owns source normalization, AST policy, export discovery and
hashing. This module only adapts those facts to project storage/API payloads
and computes task reference lists.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .script_modules import (
    ModuleBundleError as ModuleLibraryError,
    analyze_script,
    bundle_content_hash,
    capture_module_bundle,
    derive_module_exports,
    normalize_module_bundle,
    normalize_module_source,
    source_hash,
    validate_module_source,
)


def _export_metadata(source: str, name: str) -> list[dict[str, Any]]:
    # The runtime owns the export policy.  Calling its canonical derivation
    # first keeps this storage adapter from accidentally accepting a metadata
    # shape that the executor would reject.
    names = derive_module_exports(source, name=name)
    validation = validate_module_source(source, name=name)
    if not validation.get("valid"):
        errors = [item for item in validation.get("diagnostics", []) if item.get("level") == "error"]
        first = errors[0] if errors else {}
        raise ModuleLibraryError(str(first.get("message") or f"模块“{name}”校验失败"))
    metadata = validation.get("export_metadata")
    if not isinstance(metadata, list):
        raise ModuleLibraryError("运行时未返回有效的模块导出元数据")
    by_name = {
        str(item.get("name")): dict(item)
        for item in metadata
        if isinstance(item, Mapping) and item.get("name")
    }
    result: list[dict[str, Any]] = []
    for export_name in names:
        item = by_name.get(str(export_name))
        if item is None:
            raise ModuleLibraryError(f"运行时未返回模块导出“{export_name}”的元数据")
        item["name"] = str(export_name)
        item.setdefault("signature", None)
        item.setdefault("docstring", "")
        result.append(item)
    return result


def normalize_module(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ModuleLibraryError("模块必须是 JSON Object")
    # Omit client exports so the runtime derives them from source.
    candidate = {
        "name": raw.get("name"),
        "source": raw.get("source"),
        "description": raw.get("description") or "",
    }
    normalized = normalize_module_bundle({"modules": [candidate]})["modules"][0]
    item = deepcopy(dict(normalized))
    item["exports"] = _export_metadata(str(item["source"]), str(item["name"]))
    return item


def normalize_modules(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ModuleLibraryError("modules 必须是 JSON Array")
    modules: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        normalized = normalize_module(item)
        name = str(normalized["name"])
        if name in seen:
            raise ModuleLibraryError(f"模块名称重复：{name}")
        seen.add(name)
        modules.append(normalized)
    return sorted(modules, key=lambda item: str(item["name"]))


def build_bundle(
    project_id: int,
    release_id: int | None,
    modules: Any,
    *,
    release_hash: str | None = None,
    candidate_revision: int | None = None,
) -> dict[str, Any]:
    normalized = normalize_modules(modules)
    runtime_modules = [
        {
            **item,
            "exports": [
                str(export.get("name") if isinstance(export, Mapping) else export)
                for export in item.get("exports", [])
            ],
        }
        for item in normalized
    ]
    bundle = capture_module_bundle(int(project_id), release_id, runtime_modules, release_hash=release_hash)
    result = deepcopy(dict(bundle))
    result["project_id"] = int(project_id)
    result["release_id"] = release_id
    # Keep the canonical runtime module shape returned by capture_module_bundle.
    # The API-facing metadata shape is produced separately by modules_for_api.
    # A candidate has no release id yet, but its full source hash still binds
    # every validation and publish check. Runtime currently returns that hash
    # as content_hash; keep release_hash equal to the same opaque value until
    # an immutable version receives its id.
    if not result.get("release_hash"):
        result["release_hash"] = str(result.get("content_hash") or bundle_content_hash(normalized))
    if candidate_revision is not None:
        result["candidate_revision"] = int(candidate_revision)
    return result


def scan_script_references(source: Any, *, script_label: str = "script") -> list[dict[str, Any]]:
    analysis = analyze_script(
        normalize_module_source(source),
        entry_points=(),
        script_label=script_label,
    )
    refs = [{"name": name, "script": script_label} for name in analysis.get("references", [])]
    for diagnostic in analysis.get("diagnostics", []):
        if diagnostic.get("level") == "error":
            refs.append(
                {
                    "name": "",
                    "script": script_label,
                    "line": diagnostic.get("line", 0),
                    "column": diagnostic.get("column", 0),
                    "error": diagnostic.get("message") or "脚本静态校验失败",
                    "code": diagnostic.get("code"),
                }
            )
    return refs


def scan_check_references(check: dict[str, Any]) -> list[dict[str, Any]]:
    refs = scan_script_references(check.get("setup_script"), script_label="setup_script")
    refs.extend(scan_script_references(check.get("script"), script_label="script"))
    return refs


def referenced_module_names(check: dict[str, Any]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for item in scan_check_references(check):
        name = str(item.get("name") or "")
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def static_reference_diagnostics(checks: list[dict[str, Any]], modules: Any) -> list[dict[str, Any]]:
    normalized = normalize_modules(modules)
    names = {str(item["name"]) for item in normalized}
    runtime_modules = [
        {
            **item,
            "exports": [
                str(export.get("name") if isinstance(export, Mapping) else export)
                for export in item.get("exports", [])
            ],
        }
        for item in normalized
    ]
    diagnostics: list[dict[str, Any]] = []
    for check in checks:
        check_id = int(check.get("id") or 0)
        check_name = str(check.get("name") or check_id)
        for label, source in (("setup_script", check.get("setup_script")), ("script", check.get("script"))):
            if not str(source or "").strip():
                continue
            analysis = analyze_script(
                normalize_module_source(source),
                entry_points=(),
                module_bundle={"modules": runtime_modules},
                script_label=label,
            )
            for item in analysis.get("diagnostics", []):
                if item.get("level") != "error":
                    continue
                diagnostics.append({"check_id": check_id, "check_name": check_name, **dict(item)})
            for ref in scan_script_references(source, script_label=label):
                name = str(ref.get("name") or "")
                if name and name not in names:
                    diagnostics.append(
                        {
                            "check_id": check_id,
                            "check_name": check_name,
                            **ref,
                            "code": "module_not_found",
                            "error": f"引用的模块不存在：{name}",
                        }
                    )
    return diagnostics


def modules_for_api(modules: Any) -> list[dict[str, Any]]:
    return [
        {
            "name": item["name"],
            "source": item["source"],
            "description": item.get("description", ""),
            "exports": item.get("exports", []),
            "hash": item.get("hash") or source_hash(item["source"]),
        }
        for item in normalize_modules(modules)
    ]


__all__ = [
    "ModuleLibraryError",
    "build_bundle",
    "bundle_content_hash",
    "derive_module_exports",
    "modules_for_api",
    "normalize_module",
    "normalize_modules",
    "referenced_module_names",
    "scan_check_references",
    "source_hash",
    "static_reference_diagnostics",
]
