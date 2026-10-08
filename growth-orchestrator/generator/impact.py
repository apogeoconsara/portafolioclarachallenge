"""Business-impact data: funnel assumptions, a randomized experiment design, and a SIMULATED experiment.

Everything numeric here is an ASSUMPTION or a simulation under assumptions: it demonstrates the measurement method
(unit of randomization, primary metric, guardrails, power, A/A check) and is NOT evidence that the system works.
Replace the assumptions with Clara's actual baseline funnel before drawing any conclusion.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from statistics import NormalDist

from .util import rng, sha

FUNNEL_STAGES = ["targeted", "contacted", "delivered", "replied", "positive_reply", "sql", "opportunity", "pipeline_usd"]

ASSUMPTIONS = {
    "status": "ASSUMPTIONS - illustrative, to be replaced with Clara's measured baseline",
    "unit": "targeted account (cluster = email domain, so duplicate accounts share an arm)",
    "arms": {
        "control": "current process: SDRs research, write and send by hand; capacity-limited coverage",
        "treatment": "orchestrator: rules decide eligibility, AI interprets replies / drafts grounded copy, humans handle escalations",
    },
    "control": {
        "coverage_of_eligible": 0.30,          # share of eligible accounts SDRs actually reach in the month (capacity bound)
        "p_delivered": 0.965, "p_reply": 0.040, "p_positive_given_reply": 0.35, "p_sql_given_positive": 0.45,
        "p_opp_given_sql": 0.50, "p_unsubscribe": 0.004, "p_complaint": 0.0008, "p_hard_bounce": 0.025,
        "policy_violation_rate_on_ineligible": 0.030,   # SDRs mail a customer / suppressed / AE-owned account by mistake
        "median_hours_to_first_touch": 96.0,
        "sdr_minutes_per_contacted_account": 18.0,
    },
    "treatment": {
        "coverage_of_eligible": 0.97,
        "p_delivered": 0.975, "p_reply": 0.034,          # per-touch reply rate assumed LOWER than hand-written
        "p_positive_given_reply": 0.35, "p_sql_given_positive": 0.40, "p_opp_given_sql": 0.50,
        "p_unsubscribe": 0.004, "p_complaint": 0.0008, "p_hard_bounce": 0.012,
        "policy_violation_rate_on_ineligible": 0.0,
        "median_hours_to_first_touch": 2.0,
        "sdr_minutes_per_contacted_account": 1.5,          # human time on escalations only
    },
    "pipeline_usd_by_band": {"11-50": [9000, 0.8], "51-200": [18000, 0.8], "201-500": [32000, 0.8],
                             "501-1000": [55000, 0.8], "1000+": [90000, 0.9]},   # [lognormal median, sigma]
    "unit_economics_usd": {"sdr_cost_per_hour": 22.0, "llm_cost_per_account": 0.004, "enrichment_cost_per_account": 0.05,
                           "email_cost_per_send": 0.001},
    "primary_metric": "qualified_pipeline_usd_per_1000_targeted_accounts (SQL-accepted pipeline created within 60 days)",
    "secondary_metrics": ["sql_per_1000_targeted", "reply_rate_per_contacted", "positive_reply_rate", "cost_per_sql",
                          "median_hours_to_first_touch", "sdr_hours_per_sql"],
    "guardrails": {"unsubscribe_rate_max": 0.006, "spam_complaint_rate_max": 0.001, "hard_bounce_rate_max": 0.03,
                   "ae_sql_acceptance_rate_min_vs_control": 0.95, "policy_violations": "must be 0 in treatment",
                   "ai_unsafe_action_rate": "must be 0; any unsafe action pauses autonomy"},
    "design": {"randomization": "stratified by country x employee band x prior-touch flag, ranked by hash within stratum, alternating",
               "allocation": "50/50", "duration_weeks": 8, "analysis": "intention-to-treat on all targeted accounts",
               "attribution_window_days": 60, "contamination_control": "assignment by domain cluster; AEs see no arm labels",
               "novelty_check": "compare weeks 1-2 vs 5-8", "pre_registered": True,
               "stop_rules": "each week every guardrail is read with its 95% interval: GO when the interval is below the limit, HOLD when it touches it, PAUSE when it is entirely above the limit or the limit is broken two weeks in a row; a policy violation or an unsafe AI action pauses at once"},
}


def stratum(a: dict, has_prior_touch: bool) -> str:
    return f"{a['country']}|{a['employee_band'] or 'NA'}|{'T' if has_prior_touch else 'N'}"


def assign_arms(seed: int, accounts: list[dict], touched: set[str]) -> list[dict]:
    """Stratified, domain-clustered 50/50 assignment. Duplicate accounts (same domain) always share an arm."""
    by_stratum = defaultdict(set)
    for a in accounts:
        by_stratum[stratum(a, a["account_id"] in touched)].add(a["domain"])
    arm_of_domain = {}
    for st, domains in by_stratum.items():
        ranked = sorted(domains, key=lambda d: sha(seed, "arm", st, d, n=16))
        for i, d in enumerate(ranked):
            arm_of_domain[d] = "treatment" if i % 2 else "control"
    return [{"account_id": a["account_id"], "domain": a["domain"], "arm": arm_of_domain[a["domain"]],
             "stratum": stratum(a, a["account_id"] in touched),
             "assignment_hash": sha(seed, "arm", a["domain"], n=10)} for a in accounts]


def simulate_outcomes(seed: int, accounts: list[dict], truth_accounts: list[dict], arms: list[dict],
                      effect: dict | None = None) -> list[dict]:
    """One row per targeted account. `simulated: true` always. `effect` overrides treatment assumptions (use {} for A/A)."""
    arm = {r["account_id"]: r["arm"] for r in arms}
    truth = {t["account_id"]: t for t in truth_accounts}
    a_by = {a["account_id"]: a for a in accounts}
    A = ASSUMPTIONS
    out = []
    for aid, a in a_by.items():
        r = rng(seed, f"sim:{aid}")
        side = arm[aid]
        p = dict(A[side])
        if effect is not None and side == "treatment":
            p = dict(A["control"]) if effect == {} else {**p, **effect}
        eligible = truth[aid]["expected_action"] == "contact"
        row = {"account_id": aid, "arm": side, "eligible": eligible, "contacted": False, "violation": False,
               "delivered": False, "replied": False, "positive_reply": False, "sql": False, "opportunity": False,
               "pipeline_usd": 0, "unsubscribed": False, "complaint": False, "hard_bounce": False,
               "hours_to_first_touch": None, "sdr_minutes": 0.0, "simulated": True}
        if eligible:
            row["contacted"] = r.random() < p["coverage_of_eligible"]
        elif r.random() < p["policy_violation_rate_on_ineligible"]:
            row["contacted"] = row["violation"] = True
        if row["contacted"]:
            row["hours_to_first_touch"] = round(r.lognormvariate(math.log(p["median_hours_to_first_touch"]), 0.6), 1)
            row["sdr_minutes"] = p["sdr_minutes_per_contacted_account"]
            row["delivered"] = r.random() < p["p_delivered"]
            row["hard_bounce"] = r.random() < p["p_hard_bounce"]
            row["unsubscribed"] = r.random() < p["p_unsubscribe"]
            row["complaint"] = r.random() < p["p_complaint"]
            if row["delivered"] and not row["violation"]:
                row["replied"] = r.random() < p["p_reply"]
                row["positive_reply"] = row["replied"] and r.random() < p["p_positive_given_reply"]
                row["sql"] = row["positive_reply"] and r.random() < p["p_sql_given_positive"]
                row["opportunity"] = row["sql"] and r.random() < p["p_opp_given_sql"]
                if row["sql"]:
                    med, sig = A["pipeline_usd_by_band"].get(a["employee_band"], A["pipeline_usd_by_band"]["51-200"])
                    row["pipeline_usd"] = int(r.lognormvariate(math.log(med), sig))
        out.append(row)
    return out


# ------------------------------------------------------------------------------------------ analysis helpers
def per_1000(rows, field):
    return 1000 * sum(r[field] for r in rows) / max(1, len(rows))


def bootstrap_diff(rows_t, rows_c, field="pipeline_usd", n_boot=400, seed=0):
    """Difference in per-1000-targeted-account totals with a percentile bootstrap CI (clusters = accounts here)."""
    r = random.Random(seed)
    vt = [x[field] for x in rows_t]
    vc = [x[field] for x in rows_c]
    diffs = []
    for _ in range(n_boot):
        st = sum(vt[r.randrange(len(vt))] for _ in range(len(vt)))
        sc = sum(vc[r.randrange(len(vc))] for _ in range(len(vc)))
        diffs.append(1000 * (st / len(vt) - sc / len(vc)))
    diffs.sort()
    return per_1000(rows_t, field) - per_1000(rows_c, field), diffs[int(0.025 * n_boot)], diffs[int(0.975 * n_boot) - 1]


def sample_size_two_proportions(p_c: float, p_t: float, alpha=0.05, power=0.8) -> int:
    """Accounts needed PER ARM to detect p_c vs p_t (two-sided, normal approximation)."""
    z = NormalDist().inv_cdf
    num = (z(1 - alpha / 2) + z(power)) ** 2 * (p_c * (1 - p_c) + p_t * (1 - p_t))
    return math.ceil(num / (p_t - p_c) ** 2)


def two_prop_p_value(x_t, n_t, x_c, n_c) -> float:
    p = (x_t + x_c) / (n_t + n_c)
    se = math.sqrt(p * (1 - p) * (1 / n_t + 1 / n_c)) or 1e-12
    z = (x_t / n_t - x_c / n_c) / se
    return 2 * (1 - NormalDist().cdf(abs(z)))


def exact_rate_p_value(x_t: int, n_t: int, x_c: int, n_c: int) -> float:
    """Exact conditional test for two Poisson rates (rare events). Given m = x_t + x_c events in total, under H0
    x_t ~ Binomial(m, n_t / (n_t + n_c)); two-sided p = P(outcomes no more likely than the observed one).
    Preferred over the normal approximation when counts are small (e.g. ~15 SQLs per arm)."""
    m, q = x_t + x_c, n_t / (n_t + n_c)
    if m == 0:
        return 1.0
    pmf = [math.comb(m, k) * q ** k * (1 - q) ** (m - k) for k in range(m + 1)]
    return min(1.0, sum(p for p in pmf if p <= pmf[x_t] * (1 + 1e-9)))
