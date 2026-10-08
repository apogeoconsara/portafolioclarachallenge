"""Data profile + validation checks over a generated directory (independent of the builder's in-memory state)."""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

from . import oracle
from .config import AS_OF, SCENARIO_QUOTAS
from .util import parse_iso, read_jsonl

FILES = ["aes", "ae_calendar", "experiment_assignments", "experiment_sim_outcomes", "accounts", "contacts", "opportunities", "outreach_history", "suppression", "company_facts",
         "mock_behavior", "mock_enrichment", "events"]
TRUTH = ["truth_accounts", "truth_events", "truth_facts", "truth_replies"]


def load(d: Path) -> dict:
    data = {f: read_jsonl(d / f"{f}.jsonl") for f in FILES}
    data.update({t: read_jsonl(d / "truth" / f"{t}.jsonl") for t in TRUTH})
    data["manifest"] = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    return data


def run_checks(data: dict) -> list[tuple[str, bool, str]]:
    """(name, passed, detail). Used by the profile report and by the unit tests."""
    out = []

    def check(name, ok, detail=""):
        out.append((name, bool(ok), detail))

    acc = {a["account_id"]: a for a in data["accounts"]}
    con = {c["contact_id"]: c for c in data["contacts"]}
    touches = {t["touch_id"]: t for t in data["outreach_history"]}
    check("account ids unique", len(acc) == len(data["accounts"]))
    check("contact ids unique", len(con) == len(data["contacts"]))
    check("delivery ids unique", len({e["delivery_id"] for e in data["events"]}) == len(data["events"]))
    check("FK contacts -> accounts", all(c["account_id"] in acc for c in data["contacts"]))
    check("FK opportunities -> accounts", all(o["account_id"] in acc for o in data["opportunities"]))
    check("FK touches -> contacts", all(t["contact_id"] in con and t["account_id"] in acc for t in data["outreach_history"]))
    check("FK suppression -> accounts", all(s["account_id"] in acc for s in data["suppression"]))
    check("FK facts -> accounts", all(f["account_id"] in acc for f in data["company_facts"]))
    ok_ids = {e["delivery_id"] for e, t in zip(data["events"], data["truth_events"]) if t["perturbation"] != "malformed"}
    check("valid events reference existing accounts", all(
        e["account_id"] in acc for e in data["events"] if e["delivery_id"] in ok_ids))
    check("events sorted by received_at (ingestion order)",
          all(a["received_at"] <= b["received_at"] for a, b in zip(data["events"], data["events"][1:])))
    check("truth_events aligned 1:1 with events",
          [e["delivery_id"] for e in data["events"]] == [t["delivery_id"] for t in data["truth_events"]])
    check("every company domain is reserved (.example)", all(a["domain"].endswith(".example") for a in data["accounts"]))
    emails = [c["email"] for c in data["contacts"] if c["email"]]
    check("every email domain is reserved (.example) or malformed on purpose", all(
        e.split("@")[-1].endswith(".example") or "@" not in e or e.count("@") != 1 or e.endswith(".") or e.endswith("@") or " " in e
        or not e.split("@")[-1].count(".")
        for e in emails))
    check("touches sent before the snapshot", all(parse_iso(t["sent_at"]) <= AS_OF for t in touches.values()))
    # replies: thread exists and arrives after the touch
    bad = 0
    for e in data["events"]:
        if e["delivery_id"] in ok_ids and e["type"] == "reply_received" and e["payload"].get("in_reply_to_touch_id"):
            t = touches.get(e["payload"]["in_reply_to_touch_id"])
            if t is None or parse_iso(e["occurred_at"]) <= parse_iso(t["sent_at"]):
                bad += 1
    check("replies reference a real earlier touch", bad == 0, f"{bad} bad")
    # duplicates share keys
    keys = defaultdict(set)
    for e, t in zip(data["events"], data["truth_events"]):
        if t["perturbation"] in ("exact_duplicate", "semantic_duplicate"):
            keys[t["duplicate_of"]].add(e["idempotency_key"])
    orig = {e["delivery_id"]: e["idempotency_key"] for e in data["events"]}
    check("exact/semantic duplicates reuse the original idempotency key",
          all(k == {orig[o]} for o, k in keys.items()))
    # oracle vs scenario truth
    idx = oracle.Index(data["accounts"], data["contacts"], data["opportunities"], data["outreach_history"], data["suppression"])
    truth = {t["account_id"]: t for t in data["truth_accounts"]}
    mism = 0
    for a in data["accounts"]:
        d, t = oracle.decide(a, idx, aes=data["aes"]), truth[a["account_id"]]
        if (d["action"], d["reason_codes"], d["best_contact_id"], d["wait_until"]) != \
                (t["expected_action"], t["reason_codes"], t["best_contact_id"], t["wait_until"]):
            mism += 1
    check("independent oracle agrees with scenario truth for every account", mism == 0, f"{mism} mismatches")
    # AEs, routing, calendar, experiment
    aes = {a["ae_id"]: a for a in data["aes"]}
    check("AE backups exist, share the country and differ from the AE",
          all(a["backup_ae_id"] and a["backup_ae_id"] != a["ae_id"] and aes[a["backup_ae_id"]]["country"] == a["country"]
              for a in aes.values()))
    check("account owners exist as AEs", all(a["crm_owner_ae_id"] in aes for a in data["accounts"] if a["crm_owner_ae_id"]))
    bad = 0
    for t in data["truth_accounts"]:
        if t["expected_action"] == "handoff_ae":
            owner = aes[acc[t["account_id"]]["crm_owner_ae_id"]]
            bad += not (t["route_to_ae_id"] in (owner["ae_id"], owner["backup_ae_id"]) and oracle.available(aes[t["route_to_ae_id"]]))
    check("account handoffs go to an available owner or its backup", bad == 0, f"{bad} bad")
    bad = 0
    for r in data["truth_replies"]:
        if r["route_to_ae_id"]:
            ae, a = aes[r["route_to_ae_id"]], acc[r["account_id"]]
            owner = a["crm_owner_ae_id"]
            ok = oracle.available(ae) and ((ae["ae_id"] in (owner, aes[owner]["backup_ae_id"])) if owner else
                                           (con[r["contact_id"]]["language"] in ae["languages"]
                                            and ae["open_accounts"] < ae["max_open_accounts"]))
            bad += not ok
    check("reply handoffs respect availability, language and capacity", bad == 0, f"{bad} bad")
    check("calendar: weekdays only, nobody inactive, no slots while on leave", all(
        parse_iso(r["date"] + "T00:00:00Z").weekday() < 5 and aes[r["ae_id"]]["active"]
        and not (r["free_slots"] and aes[r["ae_id"]]["out_of_office_until"]
                 and r["date"] < aes[r["ae_id"]]["out_of_office_until"][:10]) for r in data["ae_calendar"]))
    arm = {r["account_id"]: r["arm"] for r in data["experiment_assignments"]}
    dom_arms = defaultdict(set)
    for a in data["accounts"]:
        dom_arms[a["domain"]].add(arm[a["account_id"]])
    check("every account has an arm", len(arm) == len(acc))
    check("accounts sharing a domain share an arm (no contamination)", all(len(v) == 1 for v in dom_arms.values()))
    share = sum(v == "treatment" for v in arm.values()) / max(1, len(arm))
    check("arms balanced overall (±1.5 pts)", abs(share - 0.5) <= 0.015, f"treatment share {share:.3f}")
    check("simulated outcomes are flagged and funnel-consistent", all(
        r["simulated"] and (not r["sql"] or r["positive_reply"]) and (not r["positive_reply"] or r["replied"])
        and (not r["replied"] or r["delivered"]) and (not r["delivered"] or r["contacted"]) for r in data["experiment_sim_outcomes"]))
    # replies truth
    check("every reply event has a label", len(data["truth_replies"]) == sum(
        1 for e, t in zip(data["events"], data["truth_events"]) if e["type"] == "reply_received" and t["perturbation"] != "malformed"
        and e["delivery_id"] in ok_ids and t.get("label") and t["perturbation"] in ("none", "delayed", "out_of_order_race")))
    return out


def _table(rows, headers):
    s = "| " + " | ".join(headers) + " |\n|" + "|".join("---" for _ in headers) + "|\n"
    return s + "\n".join("| " + " | ".join(str(c) for c in r) + " |" for r in rows) + "\n"


def _dist(counter, total=None, top=None):
    total = total or sum(counter.values())
    items = counter.most_common(top)
    return [(k, v, f"{100 * v / total:.1f}%") for k, v in items]


def render(data: dict, checks: list) -> str:
    m = data["manifest"]
    accs, truth = data["accounts"], data["truth_accounts"]
    n = len(accs)
    md = [f"# Data profile\n\nseed `{m['seed']}` · accounts `{n:,}` · as-of `{m['as_of']}` · python `{m['python']}`  \n"
          f"determinism hash: `{m['determinism_hash']}`\n"]
    md.append("## Volumes\n\n" + _table([(k, f"{v['rows']:,}") for k, v in m["files"].items()], ["file", "rows"]))
    md.append("## Validation checks\n\n" + _table([("PASS" if ok else "**FAIL**", name, d) for name, ok, d in checks],
                                                    ["", "check", "detail"]))

    sc = Counter(t["scenario"] for t in truth)
    tot_q = sum(SCENARIO_QUOTAS.values())
    md.append("## Scenario quotas (exact by construction)\n\n" + _table(
        [(k, f"{100 * SCENARIO_QUOTAS[k] / tot_q:.1f}%", sc[k], f"{100 * sc[k] / n:.2f}%") for k in SCENARIO_QUOTAS],
        ["scenario", "target", "accounts", "actual"]))
    cov = Counter((t["scenario"], t["expected_action"]) for t in truth)
    acts = sorted({t["expected_action"] for t in truth})
    md.append("## Coverage matrix: scenario × expected action on `account_targeted`\n\n" + _table(
        [(s, *[cov.get((s, a), 0) or "" for a in acts]) for s in SCENARIO_QUOTAS], ["scenario", *acts]))
    sub = Counter((t["scenario"], t["sub"]) for t in truth if t["sub"])
    md.append("## Sub-scenarios\n\n" + _table([(f"{s}/{u}", c) for (s, u), c in sorted(sub.items())], ["sub-scenario", "accounts"]))
    reasons = Counter(r for t in truth for r in t["reason_codes"])
    md.append("## Reason codes\n\n" + _table(_dist(reasons), ["reason", "accounts", "share"]))

    md.append("## Accounts\n\n**Country**\n\n" + _table(_dist(Counter(a["country"] for a in accs)), ["country", "n", "share"]))
    md.append("**Employee band**\n\n" + _table(_dist(Counter(a["employee_band"] for a in accs)), ["band", "n", "share"]))
    md.append("**Industry (top 8)**\n\n" + _table(_dist(Counter(a["industry"] for a in accs), top=8), ["industry", "n", "share"]))
    md.append("**CRM status / enrichment status**\n\n" + _table(_dist(Counter(a["crm_status"] for a in accs)), ["crm_status", "n", "share"])
              + "\n" + _table(_dist(Counter(a["enrichment_status"] for a in accs)), ["enrichment", "n", "share"]))

    cs = data["contacts"]
    per = Counter(Counter(c["account_id"] for c in cs).values())
    md.append("## Contacts\n\n" + f"{len(cs):,} contacts; accounts with 0 contacts: {n - sum(per.values()):,}\n\n"
              + _table(sorted((k, v) for k, v in per.items()), ["contacts / account", "accounts"]))
    md.append(_table(_dist(Counter(c["email_status"] for c in cs)), ["email_status", "n", "share"]))
    md.append(_table(_dist(Counter(c["function"] for c in cs)), ["function", "n", "share"]))
    md.append(_table(_dist(Counter(c["language"] for c in cs)), ["language", "n", "share"]))

    md.append("## Relationship / history\n\n" + _table([
        ("opportunities", len(data["opportunities"])), ("outreach touches", len(data["outreach_history"])),
        ("suppression entries", len(data["suppression"])),
        ("accounts with ≥1 touch", len({t["account_id"] for t in data["outreach_history"]})),
        ("accounts with an open opportunity", len({o["account_id"] for o in data["opportunities"] if o["stage"] in
                                                   ("discovery", "demo", "proposal", "negotiation")})),
        ("accounts owned by an AE", sum(1 for a in accs if a["crm_owner_ae_id"]))], ["measure", "value"]))
    md.append(_table(_dist(Counter(s["reason"] for s in data["suppression"])), ["suppression reason", "n", "share"]))

    ev, te = data["events"], data["truth_events"]
    md.append("## Event stream\n\n" + f"{len(ev):,} deliveries over {len({e['occurred_at'][:10] for e in ev if e['occurred_at'][:2] == '20' and e['occurred_at'][5:7] != '13'}):,} days\n\n"
              + _table(_dist(Counter(e["type"] for e in ev)), ["type", "n", "share"]))
    pert = Counter(t["perturbation"] for t in te)
    md.append("**Perturbations (ground truth)**\n\n" + _table(_dist(pert), ["perturbation", "n", "share of deliveries"]))
    md.append(_table(_dist(Counter(t["malformed_kind"] for t in te if t["perturbation"] == "malformed")), ["malformed kind", "n", "share"]))
    md.append(_table(_dist(Counter(t["expected_handling"] for t in te)), ["expected handling", "n", "share"]))
    late = sum(1 for e in ev if e["occurred_at"][:2] == "20" and e["occurred_at"][5:7] != "13"
               and parse_iso(e["received_at"]) - parse_iso(e["occurred_at"]) > timedelta(hours=1))
    md.append(f"Deliveries received >1h after they occurred: **{late:,}** ({100 * late / len(ev):.1f}%). "
              f"Targeting events arrive in weekly bursts of ~20 minutes (rate-limit / queue stress).\n")

    rp = data["truth_replies"]
    md.append("## Replies (AI input)\n\n" + f"{len(rp):,} labelled replies\n\n"
              + _table(_dist(Counter(r["label"] for r in rp)), ["label", "n", "share"]))
    md.append(_table(_dist(Counter(r["language"] for r in rp)), ["language", "n", "share"]))
    md.append(_table(_dist(Counter(r["difficulty"] for r in rp)), ["difficulty", "n", "share"]))
    md.append(_table(_dist(Counter(r["expected_action"] for r in rp)), ["expected action (state-aware)", "n", "share"]))
    md.append(f"Ambiguous: {sum(r['is_ambiguous'] for r in rp):,} · needs human review: {sum(r['needs_human_review'] for r in rp):,}\n")

    fc = Counter(f["trap"] or "clean" for f in data["truth_facts"])
    usable = sum(f["usable_for_personalization"] for f in data["truth_facts"])
    md.append("## Company facts (personalization grounding)\n\n" + f"{len(data['company_facts']):,} facts · usable for personalization: {usable:,} ({100 * usable / max(1, len(data['company_facts'])):.1f}%)\n\n"
              + _table(_dist(fc), ["trap", "n", "share"]))

    aes = data["aes"]
    act = [a for a in aes if a["active"]]
    md.append("## AEs, routing and calendar\n\n" + f"{len(aes)} AEs · active {len(act)} · on leave at the snapshot "
              f"{sum(1 for a in aes if a['out_of_office_until'])} · at/over capacity "
              f"{sum(1 for a in act if a['open_accounts'] >= a['max_open_accounts'])} · calendar rows {len(data['ae_calendar']):,}\n\n"
              + _table(_dist(Counter(t["route_reason"] for t in data["truth_accounts"] if t["route_reason"])), ["account handoff route", "n", "share"])
              + "\n" + _table(_dist(Counter(r["route_reason"] for r in rp if r["route_reason"])), ["reply handoff route", "n", "share"]))
    arms = Counter(r["arm"] for r in data["experiment_assignments"])
    md.append("## Experiment arms (simulated outcomes, see impact_example.md)\n\n" + _table(_dist(arms), ["arm", "accounts", "share"]))
    mb = data["mock_behavior"]
    md.append("## Mock API behaviours (deterministic per account)\n\n"
              + "\n".join(_table(_dist(Counter(b[k] for b in mb)), [k, "n", "share"]) for k in ("enrichment", "send", "calendar", "crm")))
    me = Counter(x["variant"] for x in data["mock_enrichment"])
    md.append("**Mock enrichment payload variants**\n\n" + _table(_dist(me), ["variant", "n", "share"]))
    return "\n".join(md)


def profile_dir(d: Path, out_md: Path | None = None) -> tuple[str, list]:
    data = load(d)
    checks = run_checks(data)
    md = render(data, checks)
    if out_md:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(md, encoding="utf-8")
    return md, checks
