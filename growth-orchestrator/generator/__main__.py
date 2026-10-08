"""CLI:  python -m generator <build|seed|profile|all> [options]  (run from growth-orchestrator/)"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .build import build_world
from .export import export_csv, export_seed, write_world
from .profile import profile_dir

ROOT = Path(__file__).resolve().parent.parent


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="generator", description=__doc__)
    ap.add_argument("command", choices=["build", "seed", "profile", "impact", "csv", "all"])
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--n", type=int, default=50_000, help="number of accounts (target companies)")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "generated")
    ap.add_argument("--no-sqlite", action="store_true")
    a = ap.parse_args(argv)

    if a.command in ("build", "all"):
        t = time.time()
        w = build_world(a.seed, a.n)
        m = write_world(w, a.out, a.seed, a.n, sqlite=not a.no_sqlite)
        print(f"built {a.n:,} accounts in {time.time() - t:.1f}s -> {a.out}")
        for k, v in m["files"].items():
            print(f"  {k:34s}{v['rows']:>10,}")
        print(f"  determinism_hash {m['determinism_hash']}")
    if a.command in ("csv", "all"):
        n = export_csv(a.out, a.out / "csv")
        print(f"csv export: {len(n)} files -> {a.out / 'csv'}")
    if a.command in ("seed", "all"):
        counts = export_seed(ROOT / "data" / "seed")
        print("seed artefacts:", {k: v for k, v in counts.items() if k != "sample"})
    if a.command in ("impact", "all"):
        from .impact_report import write
        p = write(a.out, ROOT / "data" / "reports" / "impact_example.md")
        print(f"impact example written -> {p.relative_to(ROOT)}")
    if a.command in ("profile", "all"):
        md, checks = profile_dir(a.out, ROOT / "data" / "reports" / "data_profile.md")
        failed = [c for c in checks if not c[1]]
        print(f"profile written ({len(checks) - len(failed)}/{len(checks)} checks passed)")
        for name, _, detail in failed:
            print(f"  FAIL {name}: {detail}")
        return 1 if failed else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
