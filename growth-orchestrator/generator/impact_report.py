"""Measurement-plan worked example on the SIMULATED experiment (all numbers rest on impact.ASSUMPTIONS)."""
from __future__ import annotations

import math
from pathlib import Path

from . import impact
from .impact import per_1000
from .util import read_jsonl


def _rate(rows, field, denom_field=None):
    d = [r for r in rows if (r[denom_field] if denom_field else True)]
    return sum(r[field] for r in d) / max(1, len(d))


def _wilson(k, n, z=1.96):
    if n == 0:
        return 0.0, 1.0
    p, d = k / n, 1 + z * z / n
    c, a = p + z * z / (2 * n), z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (c - a) / d, (c + a) / d


def render(d: Path, aa_runs: int = 40) -> str:
    accounts = read_jsonl(d / "accounts.jsonl")
    truth = read_jsonl(d / "truth" / "truth_accounts.jsonl")
    arms = read_jsonl(d / "experiment_assignments.jsonl")
    sim = read_jsonl(d / "experiment_sim_outcomes.jsonl")
    seed = read_jsonl_manifest_seed(d)
    T = [r for r in sim if r["arm"] == "treatment"]
    C = [r for r in sim if r["arm"] == "control"]
    A = impact.ASSUMPTIONS
    md = ["# Measurement plan: worked example (SIMULATED)\n",
          "> **Not evidence.** Every outcome below is generated from the assumptions in `data/seed/funnel_assumptions.json`. "
          "It shows *how* we would measure incremental qualified pipeline (unit, metric, guardrails, power, A/A sanity check), "
          "not that the system produces it. Replace the assumptions with Clara's measured baseline before concluding anything.\n",
          "## Design\n",
          f"* **Unit / randomization:** {A['unit']}. Stratified by country × employee band × prior-touch, ranked by hash within each stratum (so the split is balanced and reproducible).",
          f"* **Arms:** control = {A['arms']['control']}; treatment = {A['arms']['treatment']}.",
          f"* **Analysis:** {A['design']['analysis']}, attribution window {A['design']['attribution_window_days']} days, {A['design']['duration_weeks']} weeks.",
          f"* **Primary metric:** {A['primary_metric']}.",
          f"* **Guardrails:** {A['guardrails']}.\n",
          "## Funnel (per 1,000 targeted accounts, ITT)\n",
          "| stage | control | treatment |\n|---|---|---|"]
    for label, f in (("contacted", "contacted"), ("delivered", "delivered"), ("replied", "replied"),
                     ("positive reply", "positive_reply"), ("SQL", "sql"), ("opportunity", "opportunity")):
        md.append(f"| {label} | {per_1000(C, f):,.1f} | {per_1000(T, f):,.1f} |")
    diff, lo, hi = impact.bootstrap_diff(T, C, "pipeline_usd", seed=1)
    md.append(f"| **qualified pipeline (USD)** | {per_1000(C, 'pipeline_usd'):,.0f} | {per_1000(T, 'pipeline_usd'):,.0f} |")
    verdict = ("The interval excludes 0." if lo > 0 or hi < 0 else
               "**The interval includes 0: even with a ~2x effect assumed, one month of 50k accounts is not enough to call the primary metric.** "
               "This is why the plan pairs it with a leading metric and a longer horizon.")
    md.append(f"\n**Incremental qualified pipeline per 1,000 targeted accounts: USD {diff:,.0f} (95% bootstrap CI {lo:,.0f} to {hi:,.0f}).** {verdict}\n")
    md.append("Where the lift comes from in this simulation: **coverage**, not better copy. "
              f"Eligible accounts reached: control {_rate([r for r in C if r['eligible']], 'contacted'):.0%} vs treatment "
              f"{_rate([r for r in T if r['eligible']], 'contacted'):.0%}; the per-touch reply rate is assumed *lower* for AI-assisted outreach "
              f"({A['treatment']['p_reply']:.1%} vs {A['control']['p_reply']:.1%}). If real coverage gains are smaller, the incremental pipeline shrinks accordingly.\n")
    md.append("## Guardrails\n\n| guardrail | control | treatment | 95% interval (treatment) | limit | status |\n|---|---|---|---|---|---|")
    g = A["guardrails"]
    for name, f, lim in (("unsubscribe rate", "unsubscribed", g["unsubscribe_rate_max"]),
                         ("spam complaint rate", "complaint", g["spam_complaint_rate_max"]),
                         ("hard bounce rate", "hard_bounce", g["hard_bounce_rate_max"])):
        t, c = _rate(T, f, "contacted"), _rate(C, f, "contacted")
        lo, hi = _wilson(sum(r[f] for r in T if r["contacted"]), sum(1 for r in T if r["contacted"]))
        md.append(f"| {name} | {c:.2%} | {t:.2%} | {lo:.2%} to {hi:.2%} | ≤ {lim:.2%} | {'GO' if hi < lim else ('PAUSE' if lo > lim else 'HOLD')} |")
    vt, vc = sum(r["violation"] for r in T), sum(r["violation"] for r in C)
    md.append(f"| policy violations (contacted an ineligible account) | {vc} | {vt} | n/a | 0 in treatment | {'GO' if vt == 0 else 'PAUSE'} |")
    sql_c = sum(r["sql"] for r in C) or 1
    sql_t = sum(r["sql"] for r in T) or 1
    ue = A["unit_economics_usd"]
    cost_c = sum(r["sdr_minutes"] for r in C) / 60 * ue["sdr_cost_per_hour"]
    cost_t = sum(r["sdr_minutes"] for r in T) / 60 * ue["sdr_cost_per_hour"] + len(T) * (ue["llm_cost_per_account"] + ue["enrichment_cost_per_account"])
    md.append(f"\nCost per SQL (SDR time + AI + enrichment): control USD {cost_c / sql_c:,.0f} vs treatment USD {cost_t / sql_t:,.0f}. "
              f"Median hours to first touch: control {sorted(r['hours_to_first_touch'] for r in C if r['hours_to_first_touch'])[len([r for r in C if r['hours_to_first_touch']]) // 2]:.0f}h vs "
              f"treatment {sorted(r['hours_to_first_touch'] for r in T if r['hours_to_first_touch'])[len([r for r in T if r['hours_to_first_touch']]) // 2]:.0f}h.\n")
    # power
    pc = sum(r["sql"] for r in C) / len(C)
    md.append("## Power: how many accounts per arm to detect a lift in SQL rate\n")
    md.append(f"Baseline SQL rate per targeted account in this simulation: **{pc:.3%}** (rare event, so power is expensive).\n")
    md.append("| relative lift | accounts per arm | months at 50k targeted/month (25k per arm) |\n|---|---|---|")
    for lift in (0.10, 0.25, 0.50, 1.00, 2.00):
        n = impact.sample_size_two_proportions(pc, pc * (1 + lift))
        md.append(f"| +{lift:.0%} | {n:,} | {n / 25_000:.1f} |")
    md.append("\nImplication: with ~50k accounts/month, only large lifts are detectable in one month; small improvements need longer runs "
              "or a more sensitive leading metric (positive replies per contacted account) reported alongside the primary one.\n")
    # A/A
    md.append("## A/A sanity check (no true difference)\n")
    rej = 0
    for k in range(aa_runs):
        aa = impact.simulate_outcomes(seed + 1000 + k, accounts, truth, arms, effect={})
        t_, c_ = [r for r in aa if r["arm"] == "treatment"], [r for r in aa if r["arm"] == "control"]
        p = impact.exact_rate_p_value(sum(r["sql"] for r in t_), len(t_), sum(r["sql"] for r in c_), len(c_))
        rej += p < 0.05
    md.append(f"{aa_runs} A/A re-simulations (identical behaviour in both arms), tested with an **exact conditional test for rare "
              f"counts** (a normal approximation is anti-conservative with only ~15 SQLs per arm): **{rej} false positives at α=0.05** "
              f"(expected ≈ {0.05 * aa_runs:.1f}). A split that rejects far more often would signal a broken randomization.\n")
    # balance
    by = {}
    for r in arms:
        by.setdefault(r["stratum"], [0, 0])[r["arm"] == "treatment"] += 1
    worst = max(abs(a - b) for a, b in by.values())
    md.append("## Randomization check\n")
    md.append(f"{len(by)} strata; largest control/treatment imbalance in any stratum: **{worst} accounts**. "
              f"Overall: control {len(C):,} / treatment {len(T):,}. Accounts sharing a domain always share an arm "
              f"(verified in the data tests).\n")
    return "\n".join(md)


def read_jsonl_manifest_seed(d: Path) -> int:
    import json
    return json.loads((d / "manifest.json").read_text(encoding="utf-8"))["seed"]


def write(d: Path, out: Path, aa_runs: int = 40) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(d, aa_runs), encoding="utf-8")
    return out
