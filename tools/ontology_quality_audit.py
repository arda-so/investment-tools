#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

# Ensure repo root is on sys.path when running as a script.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import app_env  # noqa: F401  # ensures .env loader side effects
from app.services.ontology_quality_service import audit_ontology_quality


def main() -> int:
    p = argparse.ArgumentParser(description="Audit ontology entity/relationship quality (Postgres-only).")
    p.add_argument("--top", type=int, default=25, help="Top noisy companies to include.")
    p.add_argument("--write-report", action="store_true", help="Write markdown report under reports/.")
    args = p.parse_args()

    out = audit_ontology_quality(limit_top=max(1, int(args.top or 25)))
    print(json.dumps(out, ensure_ascii=True, indent=2))
    if not bool(args.write_report):
        return 0 if bool(out.get("ok")) else 1

    report_dir = ROOT / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = str(out.get("asof") or "").replace(":", "").replace("-", "").replace("T", "_")
    if not ts:
        ts = "unknown"
    path = report_dir / f"ontology_quality_audit_{ts}.md"
    lines: list[str] = []
    lines.append("# Ontology Quality Audit")
    lines.append(f"asof: {out.get('asof')}")
    lines.append("")
    lines.append(f"- company_total: {out.get('company_total')}")
    lines.append(f"- noisy_company_count: {out.get('noisy_company_count')}")
    lines.append(f"- noisy_company_ratio: {out.get('noisy_company_ratio')}")
    lines.append(f"- relationship_total: {out.get('relationship_total')}")
    lines.append(f"- orphan_company_count: {out.get('orphan_company_count')}")
    lines.append(f"- low_confidence_relationship_count: {out.get('low_confidence_relationship_count')}")
    lines.append("")
    lines.append("## Top Noisy Companies")
    for r in list(out.get("top_noisy_companies") or []):
        lines.append(
            f"- id={int(r.get('id') or 0)} name={str(r.get('name') or '').strip()} normalized={str(r.get('normalized_name') or '').strip()}"
        )
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    print(f"report_path={path}")
    return 0 if bool(out.get("ok")) else 1


if __name__ == "__main__":
    raise SystemExit(main())
