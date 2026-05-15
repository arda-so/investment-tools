#!/usr/bin/env python3
from __future__ import annotations

import ast
import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
BASELINE_PATH = ROOT / "config" / "architecture_baseline.json"


def _module_name(py_path: Path) -> str:
    rel = py_path.with_suffix("").relative_to(APP_DIR)
    return "app." + ".".join(rel.parts)


def _load_modules() -> Dict[Path, str]:
    out: Dict[Path, str] = {}
    for p in APP_DIR.rglob("*.py"):
        if p.is_file():
            out[p] = _module_name(p)
    return out


def _collapse_to_known(module: str, known: Set[str]) -> str:
    cur = module
    while cur and cur not in known:
        parts = cur.split(".")
        cur = ".".join(parts[:-1]) if len(parts) > 1 else ""
    return cur


def _graph(modules: Dict[Path, str]) -> Dict[str, Set[str]]:
    known = set(modules.values())
    edges: Dict[str, Set[str]] = defaultdict(set)
    for p, mod in modules.items():
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom):
                m = n.module or ""
                if m.startswith("app."):
                    t = _collapse_to_known(m, known)
                    if t and t != mod:
                        edges[mod].add(t)
            elif isinstance(n, ast.Import):
                for a in n.names:
                    m = a.name
                    if m.startswith("app."):
                        t = _collapse_to_known(m, known)
                        if t and t != mod:
                            edges[mod].add(t)
    return edges


def _scc(graph: Dict[str, Set[str]]) -> List[List[str]]:
    index = 0
    stack: List[str] = []
    on_stack: Set[str] = set()
    indices: Dict[str, int] = {}
    low: Dict[str, int] = {}
    comps: List[List[str]] = []

    def strong(v: str) -> None:
        nonlocal index
        indices[v] = index
        low[v] = index
        index += 1
        stack.append(v)
        on_stack.add(v)
        for w in graph.get(v, set()):
            if w not in indices:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], indices[w])
        if low[v] == indices[v]:
            c: List[str] = []
            while True:
                w = stack.pop()
                on_stack.remove(w)
                c.append(w)
                if w == v:
                    break
            comps.append(c)

    nodes = set(graph.keys())
    for targets in graph.values():
        nodes.update(targets)
    for v in sorted(nodes):
        if v not in indices:
            strong(v)
    return [c for c in comps if len(c) > 1]


def _read_baseline() -> dict:
    if not BASELINE_PATH.exists():
        return {}
    try:
        return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> int:
    modules = _load_modules()
    graph = _graph(modules)
    cycles = _scc(graph)
    cycles.sort(key=len, reverse=True)

    print("[arch-cycles] modules:", len(modules))
    print("[arch-cycles] cycle_groups:", len(cycles))
    for comp in cycles[:10]:
        print("  - size", len(comp), ":", ", ".join(sorted(comp)))

    baseline = _read_baseline().get("import_cycles", {})
    max_groups = int(baseline.get("max_cycle_groups", len(cycles)))
    enforce = str(baseline.get("enforce", 0)).strip() in {"1", "true", "yes", "on"}
    if enforce and len(cycles) > max_groups:
        print(
            f"[arch-cycles] FAIL: cycle_groups {len(cycles)} > baseline {max_groups}",
        )
        return 2
    if len(cycles) > max_groups:
        print(
            f"[arch-cycles] WARN: cycle_groups {len(cycles)} > baseline {max_groups} (enforcement disabled)",
        )
    else:
        print("[arch-cycles] OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

