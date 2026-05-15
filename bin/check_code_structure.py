#!/usr/bin/env python3
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Dict, List


ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "config" / "architecture_baseline.json"


def _py_files() -> List[Path]:
    files: List[Path] = []
    for base in (ROOT / "app", ROOT / "tools"):
        if base.exists():
            files.extend([p for p in base.rglob("*.py") if p.is_file()])
    return files


def _read_baseline() -> dict:
    if not BASELINE_PATH.exists():
        return {}
    try:
        return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main() -> int:
    files = _py_files()
    file_rows: List[Dict] = []
    func_rows: List[Dict] = []

    for p in files:
        src = p.read_text(encoding="utf-8", errors="ignore")
        loc = len(src.splitlines())
        funcs = 0
        try:
            tree = ast.parse(src)
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    funcs += 1
                    end = getattr(n, "end_lineno", n.lineno)
                    ln = max(1, int(end) - int(n.lineno) + 1)
                    func_rows.append(
                        {
                            "file": str(p.relative_to(ROOT)),
                            "name": n.name,
                            "line": int(n.lineno),
                            "len": ln,
                        }
                    )
        except Exception:
            pass
        file_rows.append({"file": str(p.relative_to(ROOT)), "loc": loc, "funcs": funcs})

    files_over_1200 = [r for r in file_rows if int(r["loc"]) > 1200]
    funcs_over_200 = [r for r in func_rows if int(r["len"]) > 200]
    file_rows.sort(key=lambda x: int(x["loc"]), reverse=True)
    funcs_over_200.sort(key=lambda x: int(x["len"]), reverse=True)

    print("[arch-structure] py_files:", len(files))
    print("[arch-structure] files_over_1200:", len(files_over_1200))
    print("[arch-structure] funcs_over_200:", len(funcs_over_200))

    print("[arch-structure] top_files:")
    for r in file_rows[:15]:
        print(f"  - {int(r['loc']):5d} LOC | {r['file']}")

    print("[arch-structure] longest_functions:")
    for r in funcs_over_200[:20]:
        print(f"  - {int(r['len']):4d} lines | {r['file']}::{r['name']}:{r['line']}")

    baseline = _read_baseline().get("structure", {})
    max_files_1200 = int(baseline.get("max_files_over_1200", len(files_over_1200)))
    max_funcs_200 = int(baseline.get("max_functions_over_200", len(funcs_over_200)))
    enforce = str(baseline.get("enforce", 0)).strip() in {"1", "true", "yes", "on"}

    failed = False
    if len(files_over_1200) > max_files_1200:
        msg = f"[arch-structure] {'FAIL' if enforce else 'WARN'}: files_over_1200 {len(files_over_1200)} > baseline {max_files_1200}"
        print(msg)
        failed = failed or enforce
    if len(funcs_over_200) > max_funcs_200:
        msg = f"[arch-structure] {'FAIL' if enforce else 'WARN'}: funcs_over_200 {len(funcs_over_200)} > baseline {max_funcs_200}"
        print(msg)
        failed = failed or enforce
    if not failed:
        print("[arch-structure] OK")
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

