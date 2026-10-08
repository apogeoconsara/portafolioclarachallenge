"""Writes a built World to JSONL (+ SQLite) with a manifest, and exports the frozen seed files."""
from __future__ import annotations

import csv
import json
import shutil
import sqlite3
import sys
from pathlib import Path

from . import ai_ref, golden, impact, llm_fixtures, policy_data
from .build import World, build_world
from . import config as cfg
from .config import AS_OF
from .demo import ASSUMPTION_DRILLS, DEMO_FLOWS
from .reply_seeds import SEEDS
from .util import file_sha, read_jsonl, sha, write_jsonl

TABLES = [  # (file/table name, World attribute, primary key, indexes)
    ("aes", "aes", "ae_id", []),
    ("ae_calendar", "calendar", None, ["ae_id", "date"]),
    ("accounts", "accounts", "account_id", ["domain"]),
    ("contacts", "contacts", "contact_id", ["account_id"]),
    ("opportunities", "opportunities", "opportunity_id", ["account_id"]),
    ("outreach_history", "touches", "touch_id", ["account_id"]),
    ("suppression", "suppression", "suppression_id", ["account_id", "domain"]),
    ("company_facts", "facts", "fact_id", ["account_id"]),
    ("mock_behavior", "mock_behavior", "account_id", []),
    ("mock_enrichment", "mock_enrichment", "account_id", []),
    ("events", "events", "delivery_id", ["received_at", "idempotency_key", "account_id"]),
    ("experiment_assignments", "arms", "account_id", ["arm", "domain"]),
    ("experiment_sim_outcomes", "sim", "account_id", ["arm"]),
]
TRUTH = [("truth_accounts", "truth_accounts"), ("truth_events", "truth_events"),
         ("truth_facts", "truth_facts"), ("truth_replies", "truth_replies")]
JSON_COLS = {"payload", "response", "languages", "free_slots"}


def _sql_type(rows, col):
    for r in rows:
        v = r.get(col)
        if v is not None:
            return "INTEGER" if isinstance(v, (bool, int)) else "REAL" if isinstance(v, float) else "TEXT"
    return "TEXT"


def write_world(w: World, out: Path, seed: int, n: int, sqlite: bool = True) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, attr, _pk, _idx in TABLES:
        p = out / f"{name}.jsonl"
        files[f"{name}.jsonl"] = {"rows": write_jsonl(p, getattr(w, attr)), "sha256": file_sha(p)}
    for name, attr in TRUTH:
        p = out / "truth" / f"{name}.jsonl"
        files[f"truth/{name}.jsonl"] = {"rows": write_jsonl(p, getattr(w, attr)), "sha256": file_sha(p)}
    manifest = {"seed": seed, "n_accounts": n, "as_of": AS_OF.isoformat(), "python": sys.version.split()[0],
                "files": files, "determinism_hash": sha(*[f["sha256"] for f in files.values()], n=32),
                "note": "truth/ holds ground-truth labels. The orchestrator must NEVER read it; only tests/evals do."}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if sqlite:
        load_sqlite(w, out / "growth.sqlite")
    return manifest


def load_sqlite(w: World, path: Path) -> None:
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    for name, attr, pk, idx in TABLES:
        rows = getattr(w, attr)
        cols = list(rows[0].keys())
        ddl = ", ".join(f"{c} {_sql_type(rows, c)}{' PRIMARY KEY' if c == pk else ''}" for c in cols)  # pk None -> no key
        con.execute(f"CREATE TABLE {name} ({ddl})")
        con.executemany(
            f"INSERT INTO {name} VALUES ({','.join('?' * len(cols))})",
            ([json.dumps(r[c], ensure_ascii=False) if c in JSON_COLS else
              (int(r[c]) if isinstance(r[c], bool) else r[c]) for c in cols] for r in rows))
        for c in idx:
            con.execute(f"CREATE INDEX idx_{name}_{c} ON {name}({c})")
    con.commit()
    con.close()


def export_seed(out: Path, sample_n: int = 500, seed: int = 42) -> dict:
    """Frozen, reviewable artefacts that live in git: reply seeds, golden set, eval cases, small sample world."""
    out.mkdir(parents=True, exist_ok=True)
    counts = {
        "reply_seeds.jsonl": write_jsonl(out / "reply_seeds.jsonl", SEEDS),
        "golden_scenarios.jsonl": write_jsonl(out / "golden_scenarios.jsonl", golden.GOLDEN),
        "eval_cases.jsonl": write_jsonl(out / "eval_cases.jsonl", golden.eval_cases()),
    }
    seed_files = write_policy_and_ai_seed(out)
    counts.update(seed_files)
    sample = out / "sample"
    if sample.exists():
        shutil.rmtree(sample)
    w = build_world(seed, sample_n)
    manifest = write_world(w, sample, seed, sample_n, sqlite=False)
    export_csv(sample, sample / "csv")
    counts["sample"] = {k: v["rows"] for k, v in manifest["files"].items()}
    return counts


def write_policy_and_ai_seed(out: Path) -> dict:
    """Policy / templates / AI contracts / recorded model outputs / impact assumptions / demo script."""
    cases = golden.eval_cases()
    recs = llm_fixtures.build_recordings(cases)
    dump = lambda p, o: p.write_text(json.dumps(o, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    dump(out / "send_policy.json", policy_data.SEND_POLICY)
    dump(out / "decision_policy.json", decision_policy())
    dump(out / "mock_api_contracts.json", policy_data.MOCK_API_CONTRACTS)
    dump(out / "ai_schemas.json", llm_fixtures.schemas_doc())
    dump(out / "funnel_assumptions.json", impact.ASSUMPTIONS)
    dump(out / "demo_flows.json", {"flows": DEMO_FLOWS, "assumption_drills": ASSUMPTION_DRILLS})
    return {"outreach_templates.jsonl": write_jsonl(out / "outreach_templates.jsonl", policy_data.templates()),
            "llm_recordings.jsonl": write_jsonl(out / "llm_recordings.jsonl", recs),
            "send_policy.json": 1, "decision_policy.json": 1, "mock_api_contracts.json": 1, "ai_schemas.json": 1, "funnel_assumptions.json": 1, "demo_flows.json": 1}


def export_csv(src: Path, out: Path) -> dict:
    """One CSV per JSONL table (UTF-8 with BOM so Excel reads accents; nested values as JSON text)."""
    counts = {}
    for f in sorted(src.glob("*.jsonl")) + sorted((src / "truth").glob("*.jsonl")):
        rows = read_jsonl(f)
        if not rows:
            continue
        cols, seen = [], set()
        for r in rows:
            for k in r:
                if k not in seen:
                    seen.add(k)
                    cols.append(k)
        dest = out / ("truth" if f.parent.name == "truth" else "") / (f.stem + ".csv")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            for r in rows:
                w.writerow([json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                            for v in (r.get(c) for c in cols)])
        counts[str(dest.relative_to(out))] = len(rows)
    return counts


def decision_policy() -> dict:
    """Thresholds the orchestrator reads at run time (single source: generator/config.py). Changing an assumption = edit
    this JSON, no code change."""
    return {
        "note": "ASSUMPTIONS. Read by orchestrator/policy.py; mirrored in data/POLICY.md",
        "recent_outreach_days": cfg.RECENT_OUTREACH_DAYS, "sequence_max_touches": cfg.SEQUENCE_MAX_TOUCHES,
        "sequence_window_days": cfg.SEQUENCE_WINDOW_DAYS, "sequence_cooldown_days": cfg.SEQUENCE_COOLDOWN_DAYS,
        "stale_enrichment_days": cfg.STALE_ENRICHMENT_DAYS, "lost_cooldown_days": cfg.LOST_COOLDOWN_DAYS,
        "icp_min_employees": cfg.ICP_MIN_EMPLOYEES, "max_enrich_attempts": cfg.MAX_ENRICH_ATTEMPTS,
        "open_stages": list(cfg.OPEN_STAGES),
        "non_icp_industries": [i[0] for i in cfg.INDUSTRIES if not i[3]],
        "function_score": cfg.FUNCTION_SCORE, "seniority_score": cfg.SENIORITY_SCORE,
        "ai_confidence_min_auto": 0.75,
    }
