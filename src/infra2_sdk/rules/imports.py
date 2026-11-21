"""Pure AST import-boundary, layer-ordering, and cycle checker."""

from __future__ import annotations

import ast
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LayerRule:
    name: str
    prefixes: tuple[str, ...]
    rank: int
    depends_on: frozenset[str] | None = None

    def matches(self, module: str) -> bool:
        return any(module == p or module.startswith(f"{p}.") for p in self.prefixes)


@dataclass(frozen=True)
class ImportViolation:
    source_module: str
    target_module: str
    kind: str
    detail: str


def _module_name(root: Path, path: Path, prefix: str) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    dotted = ".".join(parts)
    return f"{prefix}.{dotted}" if prefix and dotted else (prefix or dotted)


def _resolve_relative(cur: str, is_pkg: bool, level: int, mod: str | None) -> str:
    anchor = cur.split(".") if is_pkg else cur.split(".")[:-1]
    base = anchor[: max(0, len(anchor) - max(0, level - 1))]
    return ".".join([*base, mod] if mod else base)


def _extract_public_all(tree: ast.Module) -> frozenset[str] | None:
    for node in tree.body:
        targets, val = (
            (node.targets, node.value)
            if isinstance(node, ast.Assign)
            else ([node.target], node.value)
            if isinstance(node, ast.AnnAssign) and node.value is not None
            else ([], None)
        )
        if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets) and isinstance(
            val, (ast.List, ast.Tuple, ast.Set)
        ):
            return frozenset(
                e.value
                for e in val.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            )
    return None


def _is_tc(test: ast.expr) -> bool:
    name = test.id if isinstance(test, ast.Name) else getattr(test, "attr", "")
    return isinstance(name, str) and name.lower() == "type_checking"


def _import_module_call(node: ast.AST) -> ast.Import | None:
    """`importlib.import_module("a.b")` or a bare `import_module("a.b")` as `import a.b`.

    Only a literal absolute name counts. A relative name (".x") or a method of another
    object (`loader.import_module`) is not an edge.
    """
    if not (isinstance(node, ast.Call) and node.args):
        return None
    func, arg = node.func, node.args[0]
    is_call = (isinstance(func, ast.Name) and func.id == "import_module") or (
        isinstance(func, ast.Attribute)
        and func.attr == "import_module"
        and isinstance(func.value, ast.Name)
        and func.value.id == "importlib"
    )
    if (
        is_call
        and isinstance(arg, ast.Constant)
        and isinstance(arg.value, str)
        and not arg.value.startswith(".")
    ):
        return ast.Import(names=[ast.alias(name=arg.value)])
    return None


def _walk_imports(node: ast.AST, *, ignore_tc: bool) -> list[ast.stmt]:
    out: list[ast.stmt] = []
    for child in ast.iter_child_nodes(node):
        if ignore_tc and isinstance(child, ast.If) and _is_tc(child.test):
            branch = ast.Module(body=child.orelse, type_ignores=[])
            out.extend(_walk_imports(branch, ignore_tc=ignore_tc))
        elif isinstance(child, (ast.Import, ast.ImportFrom)):
            out.append(child)
        else:
            if (call := _import_module_call(child)) is not None:
                out.append(call)
            out.extend(_walk_imports(child, ignore_tc=ignore_tc))
    return out


def _best_known(name: str, known: set[str]) -> str | None:
    parts = name.split(".")
    return next(
        (c for idx in range(len(parts), 0, -1) if (c := ".".join(parts[:idx])) in known), None
    )


def _scan(
    base: Path, *, prefix: str, ignore_tc: bool
) -> tuple[
    dict[str, set[str]], dict[str, frozenset[str] | None], list[tuple[str, str, str | None]]
]:
    trees: dict[str, tuple[ast.Module, bool]] = {}
    exports: dict[str, frozenset[str] | None] = {}
    # Hidden directories (.venv, .git, .tox) hold no first-party code.
    files = (
        f
        for f in base.rglob("*.py")
        if not any(
            part.startswith(".") or part == "__pycache__" for part in f.relative_to(base).parts
        )
    )
    for p in sorted(files):
        if mod := _module_name(base, p, prefix):
            is_pkg = p.name == "__init__.py"
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            trees[mod] = (tree, is_pkg)
            if is_pkg:
                exports[mod] = _extract_public_all(tree)
    known = set(trees)
    graph: dict[str, set[str]] = {m: set() for m in known}
    refs: list[tuple[str, str, str | None]] = []
    for mod, (tree, is_pkg) in trees.items():
        for st in _walk_imports(tree, ignore_tc=ignore_tc):
            if isinstance(st, ast.Import):
                for al in st.names:
                    if (dst := _best_known(al.name, known)) and dst != mod:
                        graph[mod].add(dst)
                        refs.append((mod, al.name, None))
            elif isinstance(st, ast.ImportFrom):
                bt = (
                    _resolve_relative(mod, is_pkg, st.level, st.module)
                    if st.level
                    else (st.module or "")
                )
                for al in st.names:
                    cand = f"{bt}.{al.name}" if bt else al.name
                    if (dst := _best_known(cand, known) or _best_known(bt, known)) and dst != mod:
                        graph[mod].add(dst)
                        refs.append((mod, bt, al.name))
    return graph, exports, refs


def scan_module_imports(
    root: str | Path, *, package_prefix: str = "", ignore_type_checking: bool = True
) -> dict[str, set[str]]:
    return _scan(Path(root), prefix=package_prefix, ignore_tc=ignore_type_checking)[0]


def find_dependency_cycle(graph: Mapping[str, set[str]]) -> tuple[str, ...] | None:
    state: dict[str, int] = {}
    stack: list[str] = []

    def dfs(node: str) -> tuple[str, ...] | None:
        state[node] = 1
        stack.append(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt not in graph:
                continue
            st = state.get(nxt, 0)
            if st == 1:
                return (*stack[stack.index(nxt) :], nxt)
            if st == 0 and (cyc := dfs(nxt)) is not None:
                return cyc
        stack.pop()
        state[node] = 2
        return None

    for node in sorted(graph):
        if state.get(node, 0) == 0 and (cyc := dfs(node)) is not None:
            return cyc
    return None


def _layer_for(module: str, layers: Sequence[LayerRule]) -> LayerRule | None:
    hits = [
        (max(len(p) for p in r.prefixes if module == p or module.startswith(f"{p}.")), r)
        for r in layers
        if r.matches(module)
    ]
    return max(hits, key=lambda x: x[0])[1] if hits else None


def check_import_boundaries(
    root: str | Path,
    layers: Sequence[LayerRule],
    *,
    package_prefix: str = "",
    forbid_cycles: bool = True,
    enforce_public_all: bool = False,
    ignore_type_checking: bool = True,
) -> list[ImportViolation]:
    graph, exports, refs = _scan(Path(root), prefix=package_prefix, ignore_tc=ignore_type_checking)
    out: list[ImportViolation] = []
    for src in sorted(graph):
        if (sl := _layer_for(src, layers)) is None:
            continue
        for dst in sorted(graph[src]):
            if (dl := _layer_for(dst, layers)) is None or dl.name == sl.name:
                continue
            if sl.depends_on is not None and dl.name not in sl.depends_on:
                out.append(
                    ImportViolation(
                        src, dst, "forbidden_layer", f"{sl.name!r} may not import {dl.name!r}"
                    )
                )
            elif dl.rank > sl.rank:
                out.append(
                    ImportViolation(
                        src,
                        dst,
                        "layer_rank",
                        f"{sl.name!r} ({sl.rank}) -> {dl.name!r} ({dl.rank})",
                    )
                )
    if enforce_public_all:
        seen: set[tuple[str, str]] = set()
        for src, bt, sym in refs:
            sl = _layer_for(src, layers)
            dl = _layer_for(bt, layers) if bt else None
            if sl is None or dl is None or sl.name == dl.name:
                continue
            pkg = next((p for p in dl.prefixes if bt == p or bt.startswith(f"{p}.")), None)
            if pkg is None or (allowed := exports.get(pkg)) is None:
                continue
            if bt != pkg and (src, bt) not in seen:
                seen.add((src, bt))
                out.append(ImportViolation(src, bt, "deep_import", f"bypasses {pkg}.__all__"))
            elif sym and sym != "*" and sym not in allowed and (src, f"{pkg}.{sym}") not in seen:
                seen.add((src, f"{pkg}.{sym}"))
                out.append(
                    ImportViolation(src, f"{pkg}.{sym}", "private_export", f"not in {pkg}.__all__")
                )
    if forbid_cycles and (cyc := find_dependency_cycle(graph)) is not None:
        out.append(ImportViolation(cyc[0], cyc[1], "cycle", " -> ".join(cyc)))
    return out


__all__ = [
    "ImportViolation",
    "LayerRule",
    "check_import_boundaries",
    "find_dependency_cycle",
    "scan_module_imports",
]
