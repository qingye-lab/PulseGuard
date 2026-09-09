"""Trusted-local script modules used by PulseGuard execution attempts.

The module library is intentionally an execution input, rather than a second
persistence layer.  A caller captures a complete, immutable-in-practice bundle
before dispatch and every attempt validates the same source and hashes again.
Module source is deliberately small: literal constants, descriptions, and
functions.  Imports, top-level calls, classes, and control flow are rejected so
loading a module cannot perform business work.  Function bodies run only after
the check/setup entry point calls ``ctx.modules.use("literal_name")``.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
import inspect
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence


MODULE_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}\Z")
HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
MAX_MODULES = 128
MAX_SOURCE_BYTES = 512 * 1024
MAX_DESCRIPTION_LENGTH = 500


class ModuleBundleError(ValueError):
    """A bundle or module source is malformed or failed its content checks."""


class ScriptStaticError(ValueError):
    """Static script diagnostics prove one concrete error without type-checking Python."""

    def __init__(self, message: str, diagnostics: Sequence[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.diagnostics = list(diagnostics or [])


@dataclass(frozen=True)
class ModuleDefinition:
    name: str
    source: str
    hash: str
    description: str = ""
    exports: tuple[str, ...] = ()

    @property
    def filename(self) -> str:
        return module_virtual_filename(self.name, self.hash)


@dataclass(frozen=True)
class ScriptAnalysis:
    """AST facts and diagnostics for one check/setup source."""

    references: tuple[str, ...]
    functions: tuple[dict[str, Any], ...]
    diagnostics: tuple[dict[str, Any], ...]
    filename: str

    @property
    def valid(self) -> bool:
        return not any(item.get("level") == "error" for item in self.diagnostics)

    def as_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "references": list(self.references),
            "functions": [dict(item) for item in self.functions],
            "diagnostics": [dict(item) for item in self.diagnostics],
            "filename": self.filename,
        }


def normalize_module_source(source: Any) -> str:
    if not isinstance(source, str):
        raise ModuleBundleError("模块源码必须是字符串")
    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    if len(normalized.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ModuleBundleError(f"模块源码不能超过 {MAX_SOURCE_BYTES // 1024}KB")
    return normalized


def source_hash(source: Any) -> str:
    """Return the canonical SHA-256 for module source."""
    return hashlib.sha256(normalize_module_source(source).encode("utf-8")).hexdigest()


def module_virtual_filename(name: str, module_hash: str | None = None) -> str:
    safe_name = str(name or "module").strip() or "module"
    suffix = str(module_hash or "")[:12]
    return f"<pulseguard-module:{safe_name}:{suffix}>"


def script_virtual_filename(
    script_label: str = "check", *, check_id: int | str | None = None, attempt: int | None = None
) -> str:
    label = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(script_label or "check")).strip("-") or "check"
    parts = [f"<pulseguard:{label}"]
    if check_id is not None:
        parts.append(f":task-{check_id}")
    if attempt is not None:
        parts.append(f":attempt-{attempt}")
    return "".join(parts) + ">"


def _module_export_names(tree: ast.Module) -> tuple[str, ...]:
    names: list[str] = []
    declared: tuple[str, ...] | None = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                names.append(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets: Iterable[ast.expr] = node.targets if isinstance(node, ast.Assign) else (node.target,)
            for target in targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    names.append(target.id)
                if isinstance(target, ast.Name) and target.id == "__all__" and _literal_value(node.value):
                    value = ast.literal_eval(node.value)
                    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
                        declared = tuple(dict.fromkeys(item for item in value if item and not item.startswith("_")))
    if declared is not None:
        return declared
    return tuple(dict.fromkeys(names))


def _module_function_specs(tree: ast.Module) -> dict[str, dict[str, Any]]:
    specs: dict[str, dict[str, Any]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        positional = [*node.args.posonlyargs, *node.args.args]
        defaults = len(node.args.defaults)
        kwonly = [item for item in node.args.kwonlyargs]
        kw_defaults = list(node.args.kw_defaults)
        specs[node.name] = {
            "kind": "async" if isinstance(node, ast.AsyncFunctionDef) else "sync",
            "positional": [item.arg for item in positional],
            "positional_only": [item.arg for item in node.args.posonlyargs],
            "required_positional": max(0, len(positional) - defaults),
            "keyword_only": [item.arg for item in kwonly],
            "required_keyword_only": [item.arg for item, default in zip(kwonly, kw_defaults) if default is None],
            "has_varargs": node.args.vararg is not None,
            "has_kwargs": node.args.kwarg is not None,
            "line": int(getattr(node, "lineno", 0) or 0),
        }
    return specs


def _module_export_metadata(tree: ast.Module) -> list[dict[str, Any]]:
    exports = _module_export_names(tree)
    functions = _module_function_specs(tree)
    constants: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    constants.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and not node.target.id.startswith("_"):
            constants.add(node.target.id)
    metadata: list[dict[str, Any]] = []
    for name in exports:
        if name in functions:
            spec = dict(functions[name])
            positional = list(spec.get("positional") or ())
            signature_parts = positional
            if spec.get("has_varargs"):
                signature_parts.append("*args")
            signature_parts.extend(str(item) for item in spec.get("keyword_only") or ())
            if spec.get("has_kwargs"):
                signature_parts.append("**kwargs")
            node = next(
                (item for item in tree.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name),
                None,
            )
            spec.update(
                {
                    "name": name,
                    "signature": f"{name}({', '.join(signature_parts)})",
                    "docstring": (ast.get_docstring(node, clean=True) if node is not None else "") or "",
                    "kind": spec.get("kind") or "sync",
                }
            )
            metadata.append(spec)
        elif name in constants:
            metadata.append({"name": name, "signature": None, "docstring": "", "kind": "constant"})
    return metadata


def _literal_value(node: ast.AST) -> bool:
    """Whether an AST expression can be evaluated without a runtime lookup."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (str, int, float, bool, type(None), bytes))
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_literal_value(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(key is not None and _literal_value(key) and _literal_value(value) for key, value in zip(node.keys, node.values))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return _literal_value(node.operand)
    return False


def _has_load_side_effect(node: ast.AST) -> bool:
    """Check defaults/decorators/annotations, which execute at function load."""
    for child in ast.walk(node):
        if isinstance(
            child,
            (
                ast.Call,
                ast.Await,
                ast.Yield,
                ast.YieldFrom,
                ast.NamedExpr,
                ast.ListComp,
                ast.SetComp,
                ast.DictComp,
                ast.GeneratorExp,
                ast.Lambda,
            ),
        ):
            return True
    return False


def _diagnostic(
    code: str,
    message: str,
    node: ast.AST | None = None,
    *,
    filename: str,
    level: str = "error",
) -> dict[str, Any]:
    return {
        "level": level,
        "code": code,
        "message": message,
        "filename": filename,
        "line": int(getattr(node, "lineno", 0) or 0),
        "column": int(getattr(node, "col_offset", 0) or 0) + 1,
    }


def _parse_source(source: str, filename: str) -> tuple[ast.Module | None, list[dict[str, Any]]]:
    try:
        tree = ast.parse(source, filename=filename, mode="exec")
        # Parsing alone accepts invalid scopes such as break outside a loop.
        # Compile without executing so every source consumer rejects these
        # errors before a shared module can become effective.
        compile(tree, filename, "exec", dont_inherit=True)
        return tree, []
    except SyntaxError as exc:
        return None, [
            {
                "level": "error",
                "code": "syntax_error",
                "message": f"语法错误：{exc.msg}",
                "filename": filename,
                "line": int(exc.lineno or 0),
                "column": int(exc.offset or 0),
            }
        ]


def validate_module_source(source: Any, *, name: str = "module", filename: str | None = None) -> dict[str, Any]:
    """Return AST validation facts for one module without executing it."""
    normalized = normalize_module_source(source)
    virtual = filename or module_virtual_filename(name, source_hash(normalized))
    tree, diagnostics = _parse_source(normalized, virtual)
    if tree is None:
        return {
            "valid": False,
            "name": name,
            "filename": virtual,
            "exports": [],
            "functions": {},
            "export_metadata": [],
            "diagnostics": diagnostics,
        }

    body = list(tree.body)
    for index, node in enumerate(body):
        is_docstring = index == 0 and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        if is_docstring:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.decorator_list:
                diagnostics.append(_diagnostic("load_side_effect", "模块函数禁止使用装饰器，避免加载时执行副作用", node, filename=virtual))
            defaults = [*node.args.defaults, *(item for item in node.args.kw_defaults if item is not None)]
            annotations = [item.annotation for item in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs] if item.annotation is not None]
            if node.args.vararg and node.args.vararg.annotation is not None:
                annotations.append(node.args.vararg.annotation)
            if node.args.kwarg and node.args.kwarg.annotation is not None:
                annotations.append(node.args.kwarg.annotation)
            if node.returns is not None:
                annotations.append(node.returns)
            for expr in [*defaults, *annotations]:
                if _has_load_side_effect(expr):
                    diagnostics.append(_diagnostic("load_side_effect", "模块函数默认值、装饰器和注解禁止执行调用或推导表达式", expr, filename=virtual))
            for nested in ast.walk(node):
                if isinstance(nested, (ast.Import, ast.ImportFrom)):
                    diagnostics.append(_diagnostic("import_forbidden", "模块函数禁止导入其他模块", nested, filename=virtual))
                if isinstance(nested, ast.Call) and _is_modules_use_call(nested):
                    diagnostics.append(_diagnostic("cross_module_reference", "模块禁止跨模块引用，请只在 check/setup 中调用 ctx.modules.use(\"名称\")", nested, filename=virtual))
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            diagnostics.append(_diagnostic("import_forbidden", "公共方法模块禁止导入其他模块", node, filename=virtual))
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets: Iterable[ast.expr] = node.targets if isinstance(node, ast.Assign) else (node.target,)
            if any(not isinstance(target, ast.Name) for target in targets):
                diagnostics.append(_diagnostic("top_level_assignment", "模块顶层只能给名称定义常量，不能修改对象属性或下标", node, filename=virtual))
                continue
            value = node.value
            if value is None or not _literal_value(value):
                diagnostics.append(_diagnostic("top_level_call", "模块顶层只能定义字面量常量，不能执行调用或业务逻辑", node, filename=virtual))
            if isinstance(node, ast.AnnAssign) and node.annotation is not None and _has_load_side_effect(node.annotation):
                diagnostics.append(_diagnostic("load_side_effect", "模块常量注解禁止执行调用或推导表达式", node.annotation, filename=virtual))
            continue
        diagnostics.append(_diagnostic("top_level_statement", "模块顶层只能包含说明、常量和函数定义", node, filename=virtual))

    declared_exports: tuple[str, ...] | None = None
    defined_names = {
        node.name
        for node in body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for node in body:
        if isinstance(node, ast.Assign):
            defined_names.update(
                target.id
                for target in node.targets
                if isinstance(target, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined_names.add(node.target.id)
    for node in body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not _literal_value(node.value):
            continue
        targets: Iterable[ast.expr] = node.targets if isinstance(node, ast.Assign) else (node.target,)
        if not any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets):
            continue
        value = ast.literal_eval(node.value)
        if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
            declared_exports = tuple(dict.fromkeys(item for item in value if item and not item.startswith("_")))
            missing = [item for item in declared_exports if item not in defined_names]
            if missing:
                diagnostics.append(_diagnostic("export_not_found", f"模块导出不存在：{', '.join(missing)}", node, filename=virtual))

    return {
        "valid": not any(item.get("level") == "error" for item in diagnostics),
        "name": name,
        "filename": virtual,
        "exports": list(_module_export_names(tree)),
        "functions": _module_function_specs(tree),
        "export_metadata": _module_export_metadata(tree),
        "diagnostics": diagnostics,
    }


def derive_module_exports(source: Any, *, name: str = "module") -> list[str]:
    """Return exports using the same AST policy used by bundle loading."""
    validation = validate_module_source(source, name=name)
    if not validation["valid"]:
        first = next((item for item in validation["diagnostics"] if item.get("level") == "error"), None)
        raise ModuleBundleError(str(first.get("message") if first else f"模块“{name}”校验失败"))
    return list(validation.get("exports") or ())


def _is_modules_use_call(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "use"
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "modules"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "ctx"
    )


def _function_shape(node: ast.FunctionDef | ast.AsyncFunctionDef, expected: str) -> dict[str, Any]:
    positional = [*node.args.posonlyargs, *node.args.args]
    required_positional = len(positional) - len(node.args.defaults)
    required_keyword_only = sum(item is not None for item in node.args.kw_defaults)
    has_varargs = node.args.vararg is not None or node.args.kwarg is not None
    obvious_error = False
    message = ""
    expected_count = 2 if "setup" in expected else 1
    if required_positional > expected_count:
        obvious_error = True
        message = f"必须定义 {expected}，存在过多必填位置参数"
    elif required_keyword_only:
        obvious_error = True
        message = f"必须定义 {expected}，不能要求额外的必填关键字参数"
    elif len(positional) < expected_count and node.args.vararg is None:
        obvious_error = True
        message = f"必须定义 {expected}，缺少入口参数"
    return {
        "name": node.name,
        "kind": "async" if isinstance(node, ast.AsyncFunctionDef) else "sync",
        "line": int(getattr(node, "lineno", 0) or 0),
        "parameters": len(positional),
        "required_parameters": required_positional + required_keyword_only,
        "has_varargs": has_varargs,
        "expected": expected,
        "obvious_error": obvious_error,
        "message": message,
    }


def analyze_script(
    source: Any,
    *,
    entry_points: Sequence[str] = ("check",),
    module_bundle: Mapping[str, Any] | None = None,
    script_label: str = "脚本",
    filename: str | None = None,
) -> dict[str, Any]:
    """Extract direct module references and definite entry/signature errors.

    This deliberately reports only facts knowable from the AST.  ``*args``,
    ``**kwargs`` and arbitrary Python calls are left to runtime instead of being
    presented as a complete Python type check.
    """
    normalized = normalize_module_source(source)
    virtual = filename or script_virtual_filename(script_label)
    tree, diagnostics = _parse_source(normalized, virtual)
    references: list[str] = []
    functions: list[dict[str, Any]] = []
    if tree is None:
        return ScriptAnalysis(tuple(), tuple(), tuple(diagnostics), virtual).as_dict()

    entry_set = tuple(dict.fromkeys(str(item) for item in entry_points if str(item)))
    definitions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions.setdefault(node.name, node)
            if node.name in entry_set:
                expected = "async def setup(ctx, page)" if node.name == "setup" else "async def check(ctx)"
                shape = _function_shape(node, expected)
                functions.append(shape)
                if shape["kind"] != "async":
                    diagnostics.append(_diagnostic("entry_not_async", f"{script_label}必须定义 {expected}", node, filename=virtual))
                elif shape["obvious_error"]:
                    diagnostics.append(_diagnostic("entry_signature", shape["message"], node, filename=virtual))

    for entry in entry_set:
        if entry not in definitions:
            diagnostics.append(_diagnostic("missing_entry", f"{script_label}必须定义 async def {entry}(...)", None, filename=virtual))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_modules_use_call(node):
            continue
        if len(node.args) != 1 or node.keywords:
            diagnostics.append(_diagnostic("module_use_arguments", "ctx.modules.use 必须只接收一个模块名称参数", node, filename=virtual))
            continue
        argument = node.args[0]
        if not isinstance(argument, ast.Constant) or not isinstance(argument.value, str):
            diagnostics.append(_diagnostic("dynamic_module_name", "ctx.modules.use 的模块名必须是字符串字面量", argument, filename=virtual))
            continue
        name = argument.value.strip()
        if not MODULE_NAME_PATTERN.fullmatch(name):
            diagnostics.append(_diagnostic("invalid_module_name", f"模块名格式无效：{name or '空'}", argument, filename=virtual))
            continue
        references.append(name)

    module_names = set(_bundle_module_names(module_bundle)) if module_bundle is not None else None
    if module_names is not None:
        for name in dict.fromkeys(references):
            if name not in module_names:
                diagnostics.append(_diagnostic("module_not_found", f"公共方法模块不存在：{name}", None, filename=virtual))
        _append_module_call_diagnostics(tree, module_bundle, diagnostics, virtual)

    return ScriptAnalysis(
        tuple(dict.fromkeys(references)),
        tuple(functions),
        tuple(diagnostics),
        virtual,
    ).as_dict()


def _module_specs(bundle: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(bundle, Mapping) or not isinstance(bundle.get("modules"), list):
        return {}
    specs: dict[str, dict[str, Any]] = {}
    for raw in bundle["modules"]:
        if not isinstance(raw, Mapping):
            continue
        name = str(raw.get("name") or "").strip()
        source = raw.get("source")
        if not name or not isinstance(source, str):
            continue
        filename = module_virtual_filename(name, str(raw.get("hash") or source_hash(source)))
        tree, parse_diagnostics = _parse_source(normalize_module_source(source), filename)
        if tree is None or parse_diagnostics:
            continue
        validation = validate_module_source(source, name=name, filename=filename)
        exports = tuple(raw.get("exports") or validation.get("exports") or ())
        specs[name] = {
            "exports": frozenset(exports),
            "functions": validation.get("functions") or _module_function_specs(tree),
        }
    return specs


def _use_module_name(call: ast.Call) -> str | None:
    if not _is_modules_use_call(call) or len(call.args) != 1 or call.keywords:
        return None
    argument = call.args[0]
    return argument.value.strip() if isinstance(argument, ast.Constant) and isinstance(argument.value, str) else None


def _append_module_call_diagnostics(
    tree: ast.Module,
    bundle: Mapping[str, Any] | None,
    diagnostics: list[dict[str, Any]],
    filename: str,
) -> None:
    specs = _module_specs(bundle)
    if not specs:
        return
    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.aliases: dict[str, str] = {}

        def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802 - AST visitor API
            self.visit(node.value)
            module_name = _use_module_name(node.value) if isinstance(node.value, ast.Call) else None
            targets = node.targets
            if module_name and len(targets) == 1 and isinstance(targets[0], ast.Name):
                self.aliases[targets[0].id] = module_name
            else:
                self._forget_targets(targets)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802 - AST visitor API
            self.visit(node.value) if node.value is not None else None
            module_name = _use_module_name(node.value) if isinstance(node.value, ast.Call) else None
            if module_name and isinstance(node.target, ast.Name):
                self.aliases[node.target.id] = module_name
            elif isinstance(node.target, ast.Name):
                self.aliases.pop(node.target.id, None)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802 - AST visitor API
            # Nested function scopes have independent parameter bindings.  They
            # are visited by the outer function's caller loop below.
            return

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802 - AST visitor API
            return

        def _forget_targets(self, targets: Sequence[ast.expr]) -> None:
            for target in targets:
                if isinstance(target, ast.Name):
                    self.aliases.pop(target.id, None)
                    continue
                if isinstance(target, (ast.Attribute, ast.Subscript)):
                    root = target.value
                    while isinstance(root, ast.Attribute):
                        root = root.value
                    if isinstance(root, ast.Name):
                        self.aliases.pop(root.id, None)
                    continue
                for nested in ast.walk(target):
                    if isinstance(nested, ast.Name):
                        self.aliases.pop(nested.id, None)

        def _branch(self, statements: Sequence[ast.stmt], aliases: Mapping[str, str]) -> None:
            self.aliases = dict(aliases)
            for statement in statements:
                self.visit(statement)

        def _clear_after_compound(self) -> None:
            # A branch/loop/exception handler may run zero or one of several
            # paths.  Keeping aliases after it would turn a conditional
            # assignment into a false static proof.  Calls inside each branch
            # are still checked from that branch's own definite assignments.
            self.aliases = {}

        def visit_If(self, node: ast.If) -> None:  # noqa: N802 - AST visitor API
            self.visit(node.test)
            aliases = dict(self.aliases)
            self._branch(node.body, aliases)
            self._branch(node.orelse, aliases)
            self._clear_after_compound()

        def visit_For(self, node: ast.For) -> None:  # noqa: N802 - AST visitor API
            self.visit(node.iter)
            aliases = dict(self.aliases)
            self._forget_targets((node.target,))
            loop_aliases = dict(self.aliases)
            self._branch(node.body, loop_aliases)
            self._branch(node.orelse, aliases)
            self._clear_after_compound()

        def visit_AsyncFor(self, node: ast.AsyncFor) -> None:  # noqa: N802 - AST visitor API
            self.visit_For(node)  # type: ignore[arg-type]

        def visit_While(self, node: ast.While) -> None:  # noqa: N802 - AST visitor API
            self.visit(node.test)
            aliases = dict(self.aliases)
            self._branch(node.body, aliases)
            self._branch(node.orelse, aliases)
            self._clear_after_compound()

        def visit_With(self, node: ast.With) -> None:  # noqa: N802 - AST visitor API
            aliases = dict(self.aliases)
            for item in node.items:
                self.visit(item.context_expr)
                if item.optional_vars is not None:
                    self._forget_targets((item.optional_vars,))
            self._branch(node.body, self.aliases)
            self._clear_after_compound()

        def visit_AsyncWith(self, node: ast.AsyncWith) -> None:  # noqa: N802 - AST visitor API
            self.visit_With(node)  # type: ignore[arg-type]

        def visit_Try(self, node: ast.Try) -> None:  # noqa: N802 - AST visitor API
            aliases = dict(self.aliases)
            self._branch(node.body, aliases)
            for handler in node.handlers:
                handler_aliases = dict(aliases)
                if handler.name:
                    handler_aliases.pop(handler.name, None)
                self._branch(handler.body, handler_aliases)
            self._branch(node.orelse, aliases)
            self._branch(node.finalbody, aliases)
            self._clear_after_compound()

        def visit_Match(self, node: ast.Match) -> None:  # noqa: N802 - AST visitor API
            self.visit(node.subject)
            aliases = dict(self.aliases)
            for case in node.cases:
                self._branch(case.body, aliases)
            self._clear_after_compound()

        def visit_AugAssign(self, node: ast.AugAssign) -> None:  # noqa: N802 - AST visitor API
            self.visit(node.value)
            self._forget_targets((node.target,))

        def visit_Delete(self, node: ast.Delete) -> None:  # noqa: N802 - AST visitor API
            self._forget_targets(node.targets)

        def visit_NamedExpr(self, node: ast.NamedExpr) -> None:  # noqa: N802 - AST visitor API
            self.visit(node.value)
            self._forget_targets((node.target,))

        def visit_Import(self, node: ast.Import) -> None:  # noqa: N802 - AST visitor API
            for alias in node.names:
                self.aliases.pop((alias.asname or alias.name.split(".", 1)[0]), None)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802 - AST visitor API
            for alias in node.names:
                self.aliases.pop(alias.asname or alias.name, None)

        def visit_Global(self, node: ast.Global) -> None:  # noqa: N802 - AST visitor API
            for name in node.names:
                self.aliases.pop(name, None)

        def visit_Nonlocal(self, node: ast.Nonlocal) -> None:  # noqa: N802 - AST visitor API
            for name in node.names:
                self.aliases.pop(name, None)

        def visit_Lambda(self, node: ast.Lambda) -> None:  # noqa: N802 - AST visitor API
            return

        def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802 - AST visitor API
            return

        def visit_ListComp(self, node: ast.ListComp) -> None:  # noqa: N802 - AST visitor API
            return

        def visit_SetComp(self, node: ast.SetComp) -> None:  # noqa: N802 - AST visitor API
            return

        def visit_DictComp(self, node: ast.DictComp) -> None:  # noqa: N802 - AST visitor API
            return

        def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:  # noqa: N802 - AST visitor API
            return

        def check_call(self, node: ast.Call) -> None:
            if not isinstance(node.func, ast.Attribute):
                return
            module_name: str | None = None
            if isinstance(node.func.value, ast.Call):
                module_name = _use_module_name(node.func.value)
            elif isinstance(node.func.value, ast.Name):
                module_name = self.aliases.get(node.func.value.id)
            if not module_name:
                return
            export = node.func.attr
            module_spec = specs.get(module_name)
            if module_spec is None:
                return
            if export not in module_spec["exports"]:
                diagnostics.append(_diagnostic("module_export_missing", f"公共方法模块“{module_name}”未导出：{export}", node.func, filename=filename))
                return
            function_spec = module_spec["functions"].get(export)
            if function_spec is None:
                diagnostics.append(_diagnostic("module_export_not_callable", f"公共方法模块“{module_name}”的导出“{export}”不是函数", node.func, filename=filename))
                return
            diagnostics.extend(_call_shape_diagnostics(node, module_name, export, function_spec, filename))

        def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - AST visitor API
            self.check_call(node)
            self.generic_visit(node)

    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for function in functions:
        visitor = Visitor()
        visitor.aliases = {}
        for statement in function.body:
            visitor.visit(statement)


def _call_shape_diagnostics(
    call: ast.Call,
    module_name: str,
    export: str,
    function_spec: Mapping[str, Any],
    filename: str,
) -> list[dict[str, Any]]:
    # Starred values and **kwargs make the call shape unknowable here.  Let
    # Python perform the real binding and report its runtime error if needed.
    if any(isinstance(arg, ast.Starred) for arg in call.args) or any(keyword.arg is None for keyword in call.keywords):
        return []
    positional_names = list(function_spec.get("positional") or ())
    positional_only = set(function_spec.get("positional_only") or ())
    positional_or_keyword = set(positional_names) - positional_only
    required_count = int(function_spec.get("required_positional") or 0)
    required_names = set(positional_names[:required_count])
    positional_count = len(call.args)
    if positional_count > len(positional_names) and not function_spec.get("has_varargs"):
        return [
            _diagnostic(
                "module_call_arguments",
                f"调用公共方法模块“{module_name}”的“{export}”时传入了过多位置参数",
                call,
                filename=filename,
            )
        ]
    consumed_positional = set(positional_names[:positional_count])
    provided_keywords = [keyword.arg for keyword in call.keywords if keyword.arg is not None]
    if any(name in positional_only for name in provided_keywords):
        return [
            _diagnostic(
                "module_call_arguments",
                f"调用公共方法模块“{module_name}”的“{export}”时不能以关键字传入仅位置参数",
                call,
                filename=filename,
            )
        ]
    provided_keyword_set = set(provided_keywords)
    duplicate_names = sorted(consumed_positional & provided_keyword_set)
    if duplicate_names:
        return [
            _diagnostic(
                "module_call_arguments",
                f"调用公共方法模块“{module_name}”的“{export}”时重复传入参数：{', '.join(duplicate_names)}",
                call,
                filename=filename,
            )
        ]
    missing_positional = sorted(required_names - consumed_positional - (provided_keyword_set & positional_or_keyword))
    if missing_positional:
        return [
            _diagnostic(
                "module_call_arguments",
                f"调用公共方法模块“{module_name}”的“{export}”时缺少必填参数：{', '.join(missing_positional)}",
                call,
                filename=filename,
            )
        ]
    required_keywords = set(function_spec.get("required_keyword_only") or ())
    missing_keywords = sorted(required_keywords - provided_keyword_set)
    if missing_keywords:
        return [
            _diagnostic(
                "module_call_arguments",
                f"调用公共方法模块“{module_name}”的“{export}”时缺少必填参数：{', '.join(missing_keywords)}",
                call,
                filename=filename,
            )
        ]
    if not function_spec.get("has_kwargs"):
        allowed_keywords = positional_or_keyword | set(function_spec.get("keyword_only") or ())
        unknown = sorted(provided_keyword_set - allowed_keywords)
        if unknown:
            return [
                _diagnostic(
                    "module_call_arguments",
                    f"调用公共方法模块“{module_name}”的“{export}”时包含未知参数：{', '.join(unknown)}",
                    call,
                    filename=filename,
                )
            ]
    return []


def validate_script(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return analyze_script(*args, **kwargs)


def _bundle_module_names(bundle: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(bundle, Mapping):
        return []
    modules = bundle.get("modules")
    if not isinstance(modules, list):
        return []
    names: list[str] = []
    for item in modules:
        if isinstance(item, Mapping):
            name = str(item.get("name") or "").strip()
            if name:
                names.append(name)
    return names


def _canonical_module_content(modules: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": str(item["name"]),
            "source": normalize_module_source(item["source"]),
            "hash": str(item["hash"]),
        }
        for item in modules
    ]


def bundle_content_hash(modules: Sequence[Mapping[str, Any]]) -> str:
    canonical = _canonical_module_content(modules)
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_module_item(raw: Any, *, index: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ModuleBundleError(f"第 {index} 个模块格式不正确")
    name = str(raw.get("name") or "").strip()
    if not MODULE_NAME_PATTERN.fullmatch(name):
        raise ModuleBundleError(f"第 {index} 个模块名称格式无效")
    source = normalize_module_source(raw.get("source"))
    actual_hash = source_hash(source)
    supplied_hash = str(raw.get("hash") or "").strip().lower()
    if supplied_hash and supplied_hash != actual_hash:
        raise ModuleBundleError(f"模块“{name}”源码哈希校验失败")
    description = raw.get("description") or ""
    if not isinstance(description, str):
        raise ModuleBundleError(f"模块“{name}”说明必须是字符串")
    description = description.strip()
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise ModuleBundleError(f"模块“{name}”说明过长")
    raw_exports = raw.get("exports")
    if raw_exports is None:
        exports: tuple[str, ...] = ()
    elif isinstance(raw_exports, (list, tuple)) and all(isinstance(item, str) and item.strip() for item in raw_exports):
        exports = tuple(dict.fromkeys(item.strip() for item in raw_exports))
    else:
        raise ModuleBundleError(f"模块“{name}”导出列表格式不正确")
    validation = validate_module_source(source, name=name, filename=module_virtual_filename(name, actual_hash))
    if not validation["valid"]:
        first = next((item for item in validation["diagnostics"] if item.get("level") == "error"), None)
        raise ModuleBundleError(str(first.get("message") if first else f"模块“{name}”校验失败"))
    derived_exports = tuple(validation.get("exports") or ())
    if exports:
        unknown = [item for item in exports if item not in derived_exports]
        if unknown:
            raise ModuleBundleError(f"模块“{name}”导出不存在：{', '.join(unknown)}")
    else:
        exports = derived_exports
    return {
        "name": name,
        "source": source,
        "hash": actual_hash,
        "description": description,
        "exports": list(exports),
        "export_metadata": validation.get("export_metadata") or [],
    }


def normalize_module_bundle(
    bundle: Mapping[str, Any] | None,
    *,
    expected_project_id: int | None = None,
    require_content_hash: bool = False,
) -> dict[str, Any]:
    """Normalize and verify a captured bundle for execution or transport."""
    if bundle is None:
        return {
            "project_id": int(expected_project_id) if expected_project_id else None,
            "release_id": None,
            "release_hash": None,
            "content_hash": bundle_content_hash([]),
            "modules": [],
        }
    if not isinstance(bundle, Mapping):
        raise ModuleBundleError("模块 bundle 必须是对象")
    raw_project_id = bundle.get("project_id")
    project_id: int | None = None
    if raw_project_id not in (None, ""):
        if isinstance(raw_project_id, bool):
            raise ModuleBundleError("模块 bundle 项目归属无效")
        try:
            project_id = int(raw_project_id)
        except (TypeError, ValueError) as exc:
            raise ModuleBundleError("模块 bundle 项目归属无效") from exc
        if project_id <= 0:
            raise ModuleBundleError("模块 bundle 项目归属无效")
    if expected_project_id is not None:
        expected = int(expected_project_id)
        if project_id is not None and project_id != expected:
            raise ModuleBundleError("模块 bundle 不属于当前项目")
        project_id = expected
    raw_modules = bundle.get("modules")
    if raw_modules is None:
        raw_modules = []
    if not isinstance(raw_modules, list):
        raise ModuleBundleError("模块 bundle 的 modules 必须是数组")
    if len(raw_modules) > MAX_MODULES:
        raise ModuleBundleError(f"模块数量不能超过 {MAX_MODULES}")
    modules: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_modules, start=1):
        item = _normalize_module_item(raw, index=index)
        if item["name"] in seen:
            raise ModuleBundleError(f"模块名称重复：{item['name']}")
        seen.add(item["name"])
        modules.append(item)
    content_hash = bundle_content_hash(modules)
    supplied_content_hash = str(bundle.get("content_hash") or "").strip().lower()
    if require_content_hash and not supplied_content_hash:
        raise ModuleBundleError("模块 bundle 缺少 content_hash")
    if supplied_content_hash and supplied_content_hash != content_hash:
        raise ModuleBundleError("模块 bundle content_hash 校验失败")
    raw_release_id = bundle.get("release_id")
    if raw_release_id in (None, ""):
        release_id = None
    else:
        if isinstance(raw_release_id, bool):
            raise ModuleBundleError("模块 release_id 必须是正整数")
        try:
            release_id = int(raw_release_id)
        except (TypeError, ValueError) as exc:
            raise ModuleBundleError("模块 release_id 必须是正整数") from exc
        if release_id <= 0:
            raise ModuleBundleError("模块 release_id 必须是正整数")
    release_hash = bundle.get("release_hash")
    if release_hash in (None, ""):
        release_hash = None
    else:
        release_hash = str(release_hash).strip().lower()
        if not HASH_PATTERN.fullmatch(release_hash):
            raise ModuleBundleError("模块 release_hash 格式无效")
    return {
        "project_id": project_id,
        "release_id": release_id,
        "release_hash": release_hash,
        "content_hash": content_hash,
        "modules": modules,
    }


def validate_module_bundle(
    bundle: Mapping[str, Any] | None,
    *,
    expected_project_id: int | None = None,
    require_content_hash: bool = False,
) -> dict[str, Any]:
    return normalize_module_bundle(
        bundle,
        expected_project_id=expected_project_id,
        require_content_hash=require_content_hash,
    )


def capture_module_bundle(
    project_id: int,
    release_id: int | None,
    modules: Sequence[Mapping[str, Any]],
    *,
    release_hash: str | None = None,
) -> dict[str, Any]:
    """Capture a normalized source snapshot for storage/publication code."""
    return normalize_module_bundle(
        {
            "project_id": project_id,
            "release_id": release_id,
            "release_hash": release_hash,
            "modules": [dict(item) for item in modules],
        },
        expected_project_id=int(project_id),
    )


def module_metadata(bundle: Mapping[str, Any] | None) -> dict[str, Any]:
    normalized = normalize_module_bundle(bundle)
    return {
        "project_id": normalized.get("project_id"),
        "release_id": normalized.get("release_id"),
        "release_hash": normalized.get("release_hash"),
        "content_hash": normalized.get("content_hash"),
        "module_hashes": {item["name"]: item["hash"] for item in normalized.get("modules", [])},
    }


def _safe_builtins() -> MappingProxyType:
    return MappingProxyType(
        {
            "AssertionError": AssertionError,
            "Exception": Exception,
            "RuntimeError": RuntimeError,
            "ValueError": ValueError,
            "TypeError": TypeError,
            "bool": bool,
            "dict": dict,
            "float": float,
            "int": int,
            "isinstance": isinstance,
            "len": len,
            "list": list,
            "max": max,
            "min": min,
            "range": range,
            "set": set,
            "str": str,
            "sum": sum,
            "tuple": tuple,
            "abs": abs,
            "all": all,
            "any": any,
            "enumerate": enumerate,
            "zip": zip,
        }
    )


def _stdlib_namespace() -> dict[str, Any]:
    """Small, explicit standard-library surface for trusted-local functions."""
    return {"asyncio": asyncio, "json": json, "re": re}


def _script_namespace(ctx: Any, modules: "ModuleRuntime") -> dict[str, Any]:
    def log_print(*args: Any, **_: Any) -> None:
        if ctx is not None and hasattr(ctx, "log"):
            ctx.log(" ".join(str(arg) for arg in args))

    return {
        "__builtins__": _safe_builtins(),
        **_stdlib_namespace(),
        "print": log_print,
        "__name__": "pulseguard_script",
    }


def _compile_entry(
    source: str,
    *,
    ctx: Any,
    modules: "ModuleRuntime",
    function_name: str,
    expected_signature: str,
    script_label: str,
    filename: str,
) -> Any:
    analysis = analyze_script(
        source,
        entry_points=(function_name,),
        module_bundle=modules.bundle,
        script_label=script_label,
        filename=filename,
    )
    errors = [item for item in analysis["diagnostics"] if item.get("level") == "error"]
    if errors:
        first = errors[0]
        raise ScriptStaticError(str(first.get("message") or f"{script_label}校验失败"), errors)
    namespace = _script_namespace(ctx, modules)
    try:
        code = compile(source, filename, "exec")
        # One namespace is intentional: top-level helper functions must resolve
        # names defined by the same attempt instead of only safe_globals.
        exec(code, namespace, namespace)
    except SyntaxError as exc:
        diagnostic = {
            "level": "error",
            "code": "syntax_error",
            "message": f"语法错误：{exc.msg}",
            "filename": filename,
            "line": int(exc.lineno or 0),
            "column": int(exc.offset or 0),
        }
        raise ScriptStaticError(diagnostic["message"], [diagnostic]) from exc
    except Exception as exc:
        # Top-level Python is still trusted-local code.  Surface its actual
        # error and source file rather than claiming static type coverage.
        raise ScriptStaticError(f"{script_label}加载失败（{filename}:{getattr(exc, '__traceback__', None) and ''}{exc}）") from exc
    function = namespace.get(function_name)
    if function is None or not inspect.iscoroutinefunction(function):
        raise ScriptStaticError(f"{script_label}必须定义 {expected_signature}")
    args = (ctx, None) if function_name == "setup" else (ctx,)
    try:
        inspect.signature(function).bind(*args)
    except TypeError as exc:
        raise ScriptStaticError(f"{script_label}必须定义 {expected_signature}") from exc
    return function


class ModuleProxy:
    """Read-only access to one attempt-local module namespace."""

    __slots__ = ("_definition", "_namespace", "_exports")

    def __init__(self, definition: ModuleDefinition, namespace: Mapping[str, Any]) -> None:
        object.__setattr__(self, "_definition", definition)
        object.__setattr__(self, "_namespace", namespace)
        object.__setattr__(self, "_exports", frozenset(definition.exports))

    @property
    def name(self) -> str:
        return self._definition.name

    @property
    def description(self) -> str:
        return self._definition.description

    @property
    def hash(self) -> str:
        return self._definition.hash

    @property
    def exports(self) -> tuple[str, ...]:
        return self._definition.exports

    def __getattr__(self, key: str) -> Any:
        if key.startswith("_") or key not in self._exports:
            raise AttributeError(f"模块“{self.name}”未导出：{key}")
        try:
            return self._namespace[key]
        except KeyError as exc:
            raise AttributeError(f"模块“{self.name}”未导出：{key}") from exc

    def __setattr__(self, key: str, value: Any) -> None:
        raise AttributeError("公共方法模块是只读的")


class ModuleRuntime:
    """One attempt-local module collection exposed as ``ctx.modules``."""

    def __init__(self, bundle: Mapping[str, Any] | None = None, *, expected_project_id: int | None = None) -> None:
        normalized = normalize_module_bundle(bundle, expected_project_id=expected_project_id)
        self.bundle: dict[str, Any] = copy.deepcopy(normalized)
        self._definitions: dict[str, ModuleDefinition] = {}
        self._proxies: dict[str, ModuleProxy] = {}
        self._compile_all()

    def _compile_all(self) -> None:
        for item in self.bundle["modules"]:
            definition = ModuleDefinition(
                name=item["name"],
                source=item["source"],
                hash=item["hash"],
                description=item.get("description") or "",
                exports=tuple(item.get("exports") or ()),
            )
            validation = validate_module_source(definition.source, name=definition.name, filename=definition.filename)
            if not validation["valid"]:
                first = next((item for item in validation["diagnostics"] if item.get("level") == "error"), None)
                raise ModuleBundleError(str(first.get("message") if first else f"模块“{definition.name}”校验失败"))
            namespace: dict[str, Any] = {
                "__builtins__": _safe_builtins(),
                "__name__": f"pulseguard_module_{definition.name}",
                "__file__": definition.filename,
                **_stdlib_namespace(),
            }
            try:
                code = compile(definition.source, definition.filename, "exec")
                exec(code, namespace, namespace)
            except Exception as exc:
                raise ModuleBundleError(f"模块“{definition.name}”加载失败（{definition.filename}）：{exc}") from exc
            self._definitions[definition.name] = definition
            self._proxies[definition.name] = ModuleProxy(definition, namespace)

    def use(self, name: str) -> ModuleProxy:
        if not isinstance(name, str):
            raise ModuleBundleError("ctx.modules.use 的模块名必须是字符串字面量")
        normalized_name = name.strip()
        if normalized_name not in self._proxies:
            raise ModuleBundleError(f"公共方法模块不存在：{normalized_name or '空'}")
        return self._proxies[normalized_name]

    def has(self, name: str) -> bool:
        return isinstance(name, str) and name.strip() in self._proxies

    def names(self) -> tuple[str, ...]:
        return tuple(self._definitions)

    def metadata(self) -> dict[str, Any]:
        return module_metadata(self.bundle)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.has(name)


def compile_script_entry(
    source: str,
    ctx: Any,
    *,
    function_name: str = "check",
    expected_signature: str = "async def check(ctx)",
    script_label: str = "脚本",
    module_bundle: Mapping[str, Any] | None = None,
    modules: ModuleRuntime | None = None,
    filename: str | None = None,
    check_id: int | str | None = None,
    attempt: int | None = None,
) -> Any:
    runtime = modules or getattr(ctx, "modules", None)
    if runtime is None:
        runtime = ModuleRuntime(module_bundle, expected_project_id=getattr(ctx, "project_id", None))
    if getattr(ctx, "modules", None) is None:
        ctx.modules = runtime
    virtual = filename or script_virtual_filename(script_label, check_id=check_id, attempt=attempt)
    return _compile_entry(
        normalize_module_source(source),
        ctx=ctx,
        modules=runtime,
        function_name=function_name,
        expected_signature=expected_signature,
        script_label=script_label,
        filename=virtual,
    )


__all__ = [
    "MAX_MODULES",
    "MAX_SOURCE_BYTES",
    "ModuleBundleError",
    "ModuleDefinition",
    "ModuleProxy",
    "ModuleRuntime",
    "ScriptAnalysis",
    "ScriptStaticError",
    "analyze_script",
    "bundle_content_hash",
    "capture_module_bundle",
    "compile_script_entry",
    "derive_module_exports",
    "module_metadata",
    "module_virtual_filename",
    "normalize_module_bundle",
    "normalize_module_source",
    "script_virtual_filename",
    "source_hash",
    "validate_module_bundle",
    "validate_module_source",
    "validate_script",
]
