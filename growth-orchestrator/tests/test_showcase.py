"""The 'Live Demo' flows and the 'Operations' numbers come from the real engine, and tell the truth.

Run:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from orchestrator import plain, showcase

REPO = Path(__file__).resolve().parent.parent.parent
GENERATED = showcase.GENERATED / "manifest.json"
ROOT_DIR = Path(__file__).resolve().parent.parent


class Flows(unittest.TestCase):
    flows = {f["id"]: f for f in showcase.flows_payload()["flows"]}

    def test_every_flow_has_the_seven_stages_in_order(self):
        for f in self.flows.values():
            self.assertEqual([s["id"] for s in f["stages"]], ["event", "state", "rules", "model", "validator", "action", "audit"], f["id"])

    def test_the_rules_overrule_the_model_on_an_opt_out(self):
        f = self.flows["F2"]
        c = f["contradiction"]
        self.assertEqual((c["claude_label"], c["guard"], c["final_action"]), ("interested", True, "suppress"))
        self.assertEqual(f["final"]["action"], "suppress")
        validator = next(s for s in f["stages"] if s["id"] == "validator")
        self.assertEqual(validator["status"], "flag")
        self.assertIn("SIMULATED", f["label"])

    def test_duplicate_delivery_has_no_second_effect(self):
        f = self.flows["F3"]
        self.assertEqual([s["status"] for s in f["stages"][1:5]], ["skipped"] * 4)
        self.assertEqual(len(f["audit"]), 1)

    def test_the_default_flow_shows_an_outreach_email_from_draft_to_the_simulated_log(self):
        action = next(s for s in self.flows["F0"]["stages"] if s["id"] == "action")
        text = " | ".join(action["lines"])
        order = [text.index(x) for x in ("pending approval", "Approved by demo reviewer", "simulated log")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(sum("Email recorded" in l for l in action["lines"]), 1)
        self.assertNotIn("attempts", text)               # a clean send: no retry noise in the default flow

    def test_retried_send_records_exactly_one_email(self):
        action = next(s for s in self.flows["F4"]["stages"] if s["id"] == "action")
        self.assertEqual(sum("Email recorded" in l for l in action["lines"]), 1)
        self.assertTrue(any("2 attempts" in l for l in action["lines"]))

    def test_the_vague_reply_goes_to_a_person(self):
        self.assertEqual(self.flows["F5"]["final"]["action"], "escalate_human")

    def test_published_flows_are_current(self):
        published = json.loads((REPO / "public/data/flows.json").read_text(encoding="utf-8"))
        self.assertEqual(published, json.loads(json.dumps(showcase.flows_payload(), default=str)),
                         "run: python -m orchestrator export-web")

    def test_scenario_companies_are_told_apart_by_the_score(self):
        tiers = {f["id"]: f["account"]["tier"] for f in self.flows.values()}
        self.assertEqual({tiers[k] for k in ("F10", "F1")}, {"A"})
        self.assertEqual(tiers["F9"], "C")
        self.assertEqual(self.flows["F9"]["final"]["action"], "nurture")          # a weaker fit: no email, no AI call
        self.assertGreater(len({f["account"]["name"] for f in self.flows.values()}), 8)
        for f in self.flows.values():                                              # the demo companies are never the bare golden ones
            self.assertNotRegex(f["account"]["name"], r"^Dorada \d+$")

    def test_leads_cover_every_group_and_follow_the_rules(self):
        leads = showcase.leads_payload()["leads"]
        by = {t: [l for l in leads if l["account"]["tier"] == t] for t in "ABC"}
        self.assertEqual({t: len(v) for t, v in by.items()}, {"A": 4, "B": 4, "C": 4})
        for l in leads:
            a = l["account"]
            self.assertEqual(a["score"], sum(a["parts"].values()))
            self.assertEqual(a["tier"], "A" if a["score"] >= a["tier_a"] else "B" if a["score"] >= a["tier_b"] else "C")
            if a["tier"] == "C" and l["decision"]["action"] == "nurture":
                self.assertIsNone(l["draft"])                                      # nurture: no email, no AI call
            if l["draft"] and l["draft"]["ai"]:
                self.assertTrue(l["draft"]["claims"])                              # an AI-written line always cites a fact
                self.assertGreater(l["usable"], 0)

    def test_published_leads_are_current(self):
        published = json.loads((REPO / "public/data/leads.json").read_text(encoding="utf-8"))
        self.assertEqual(published, json.loads(json.dumps(showcase.leads_payload(), default=str)), "run: python -m orchestrator export-web")

    def test_eligibility_and_routing_scenarios(self):
        f = self.flows
        self.assertEqual((f["F11"]["final"]["action"], f["F11"]["final"]["codes"]), ("suppress", ["CUSTOMER"]))
        self.assertEqual(f["F11"]["account"]["crm"]["status"], "customer")
        self.assertEqual((f["F12"]["final"]["action"], f["F12"]["final"]["codes"]), ("suppress", ["ACTIVE_OPPORTUNITY"]))
        self.assertEqual(f["F12"]["account"]["crm"]["open_deals"], 1)
        self.assertEqual(f["F13"]["final"]["action"], "handoff_ae")
        self.assertTrue(any("owner is away" in l for s in f["F13"]["stages"] for l in s["lines"]), "the backup routing should be explained")
        for k in ("F11", "F12"):                                                  # decided by rules alone: no model, no email, no score
            self.assertEqual({s["id"]: s["status"] for s in f[k]["stages"]}["model"], "skipped")

    def test_a_late_unsubscribe_cancels_the_waiting_draft(self):
        f = self.flows["F14"]
        self.assertEqual((f["final"]["action"], f["final"]["codes"]), ("suppress", ["UNSUBSCRIBED"]))
        self.assertEqual(f["account"]["crm"]["drafts_waiting"], 1)                # the draft was still waiting when the opt-out arrived
        self.assertTrue(any("Out of order" in l for l in f["stages"][0]["lines"]))
        self.assertTrue(any("cancelled" in l for l in f["stages"][-2]["lines"]))
        self.assertFalse([c for c in f["calls"] if c["system"] == "send"], "nothing may reach the outreach system")

    def test_a_cut_off_ai_answer_goes_to_a_person(self):
        f = self.flows["F15"]
        self.assertEqual((f["final"]["action"], f["final"]["codes"]), ("escalate_human", ["AI_INVALID_OUTPUT"]))
        by = {s["id"]: s for s in f["stages"]}
        self.assertEqual(by["validator"]["status"], "flag")
        self.assertIn("cut off", by["model"]["headline"])
        self.assertIn("SIMULATED", f["label"])                                    # the defect is a recorded one, never passed off as live

    def test_validator_codes_have_everyday_text(self):
        for c in ("G001", "V010", "V011", "V006"):
            self.assertIn(c, plain.VALIDATION_CODES)


class CrmCoordination(unittest.TestCase):
    flows = {f["id"]: f for f in showcase.flows_payload()["flows"]}

    def test_a_failing_crm_write_is_retried_once_with_the_same_key(self):
        f = self.flows["F6"]
        self.assertEqual([c["status"] for c in f["calls"] if c["system"] == "crm"], [503, 200])
        self.assertEqual(len({c["key"] for c in f["calls"]}), 1)
        self.assertEqual(len(f["ledger"]["crm"]), 1)

    def test_an_unknown_crm_outcome_is_read_back_before_writing_again(self):
        f = self.flows["F7"]
        self.assertEqual([c["op"] for c in f["calls"]], ["write", "lookup", "write"])
        self.assertEqual(len(f["ledger"]["crm"]), 1)
        action = next(s for s in f["stages"] if s["id"] == "action")
        self.assertTrue(any("read back by idempotency key" in l for l in action["lines"]))

    def test_a_version_conflict_triggers_a_re_read_and_a_new_decision(self):
        f = self.flows["F8"]
        self.assertEqual([c["status"] for c in f["calls"] if c["system"] == "crm"], [409, 200])
        action = next(s for s in f["stages"] if s["id"] == "action")
        self.assertTrue(any("re-decided" in l for l in action["lines"]))

    def test_every_flow_is_core_or_crm_and_the_systems_table_is_complete(self):
        self.assertEqual({f["group"] for f in self.flows.values()}, {"core", "crm"})
        self.assertEqual([c["system"] for c in showcase.COORDINATION], ["AI", "CRM", "Outreach"])

    def test_every_outcome_answers_can_we_safely_automate_it(self):
        self.assertEqual(set(plain.AUTOMATION), set(plain.ACTIONS))


class Measurement(unittest.TestCase):
    m = json.loads((REPO / "public/data/measurement.json").read_text(encoding="utf-8"))

    def test_the_numbers_are_internally_consistent(self):
        m = self.m
        self.assertAlmostEqual(m["pipeline"]["diff"], m["pipeline"]["treatment"] - m["pipeline"]["control"], delta=2)
        self.assertEqual(m["pipeline"]["includes_zero"], m["pipeline"]["lo"] <= 0 <= m["pipeline"]["hi"])
        stages = [f["treatment"] for f in m["funnel"]]
        self.assertEqual(stages, sorted(stages, reverse=True))
        self.assertTrue(all(g["status"] in ("ok", "breach") for g in m["guardrails"]))
        self.assertIn("SIMULATED", m["label"])

    def test_guardrails_carry_their_counts_and_an_interval_that_decides_the_state(self):
        for g in self.m["guardrails"]:
            self.assertIn(g["evidence"], ("go", "hold", "stop"), g["name"])
            if "ci_pct" not in g:                      # the ineligible-contact guardrail has a limit of zero events
                self.assertEqual(g["evidence"], "go" if g["events"]["treatment"] == 0 else "stop")
                continue
            lo, hi = g["ci_pct"]
            self.assertLessEqual(lo, 100 * g["events"]["treatment"] / g["contacted"]["treatment"])
            self.assertLessEqual(100 * g["events"]["treatment"] / g["contacted"]["treatment"], hi)
            self.assertEqual(g["evidence"], "go" if hi < g["limit_pct"] else "stop" if lo > g["limit_pct"] else "hold", g["name"])
        spam = next(g for g in self.m["guardrails"] if g["name"] == "Spam complaint rate")
        self.assertEqual((spam["status"], spam["evidence"]), ("breach", "hold"),
                         "a point estimate over the limit with an interval that includes it reads HOLD, not broken")

    def test_it_matches_the_worked_example_in_the_reports(self):
        text = (ROOT_DIR / "data/reports/impact_example.md").read_text(encoding="utf-8")
        self.assertIn(f'USD {self.m["pipeline"]["diff"]:,}', text)
        self.assertIn(f'| **qualified pipeline (USD)** | {self.m["pipeline"]["control"]:,} | {self.m["pipeline"]["treatment"]:,} |', text)

    @unittest.skipUnless(GENERATED.exists(), "50k world not generated (make data)")
    def test_published_measurement_matches_a_fresh_run(self):
        self.assertEqual(self.m, json.loads(json.dumps(showcase.measurement_payload(), default=str)), "run: python -m orchestrator export-overview")


class ChallengeMap(unittest.TestCase):
    cm = json.loads((REPO / "public/data/challenge_map.json").read_text(encoding="utf-8"))
    VIEWS = {"overview", "run", "approvals", "flows", "priority", "automation", "account", "live", "evals", "ops", "stream"}

    def test_every_proof_exists_in_the_repository(self):
        for r in self.cm["rows"]:
            self.assertTrue((ROOT_DIR / r["proof"]).exists(), r["proof"])

    def test_every_row_points_to_a_view_that_exists_on_the_site(self):
        html = (REPO / "public/index.html").read_text(encoding="utf-8")
        for r in self.cm["rows"]:
            self.assertIn(r["view"], self.VIEWS, r)
        for v in self.VIEWS - {"priority", "automation", "account", "live", "evals", "ops", "stream"}:
            self.assertIn(f'data-v="{v}"', html)

    def test_it_covers_the_ten_requirements_and_the_four_demo_cases(self):
        by = {}
        for r in self.cm["rows"]:
            by[r["group"]] = by.get(r["group"], 0) + 1
        self.assertEqual((by["build"], by["demo"]), (10, 4))
        self.assertEqual(set(by), set(self.cm["groups"]))


class ScoringCompare(unittest.TestCase):
    """Changing the scoring: the numbers come from running the real engine once per version."""
    SAMPLE = showcase.SEED_DIR / "sample"

    def _check(self, p):
        for v in p["versions"]:
            r = p["runs"][v["id"]]
            self.assertEqual(sum(r["ready_by_tier"].values()), p["ready"], v)
            self.assertEqual(r["nurture_enrolled"], r["ready_by_tier"].get("C", 0), "every ready tier C company is enrolled in nurture exactly once")
            self.assertEqual(r["versions_in_audit"], [v["id"]], "every logged score names its version")
        for q in p["pairs"]:
            self.assertEqual(sum(q["matrix"].values()), q["ready"])
            self.assertEqual(q["moved"], sum(n for k, n in q["matrix"].items() if k[0] != k[1]))
            d = q["delta"]
            self.assertEqual(d["emails_prepared"], -d["nurture_enrolled"], "a company that leaves nurture gets an outreach email, and the other way round")

    def test_engine_comparison_on_the_sample_world(self):
        p = showcase.scoring_compare_payload(self.SAMPLE)
        self._check(p)
        self.assertEqual([v["id"] for v in p["versions"]], ["v1", "v2"])
        self.assertGreater(p["pairs"][0]["moved"], 0, "the example proposal must move somebody")

    def test_published_comparison_is_consistent(self):
        self._check(json.loads((REPO / "public/data/scoring_compare.json").read_text(encoding="utf-8")))

    def test_the_command_prints_a_report(self):
        import subprocess, sys
        r = subprocess.run([sys.executable, "-m", "orchestrator", "compare-scoring", "v1", "v2", "--world", str(self.SAMPLE)],
                           capture_output=True, text=True, cwd=ROOT_DIR)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ready companies change group", r.stdout)

    @unittest.skipUnless(GENERATED.exists(), "50k world not generated (make data)")
    def test_published_comparison_matches_a_fresh_run(self):
        self.assertEqual(json.loads((REPO / "public/data/scoring_compare.json").read_text(encoding="utf-8")),
                         json.loads(json.dumps(showcase.scoring_compare_payload(), default=str)), "run: python -m orchestrator export-overview")


class Operations(unittest.TestCase):
    ops = json.loads((REPO / "public/data/operations.json").read_text(encoding="utf-8"))

    def test_numbers_add_up(self):
        o = self.ops
        self.assertEqual(o["automated"] + o["human"], o["decided"])
        self.assertEqual(sum(o["handling"].values()), o["events"])
        self.assertEqual(sum(o["ai"]["verdicts"].values()), o["ai"]["calls"])
        self.assertEqual(sum(o["ai"]["by_kind"].values()), o["ai"]["calls"])
        self.assertEqual(o["real_emails_sent"], 0)

    def test_cost_is_calls_times_the_stated_assumption(self):
        a = self.ops["ai"]
        self.assertAlmostEqual(a["cost_usd"], round(a["calls"] * a["cost_per_call_usd"], 2))

    @unittest.skipUnless(GENERATED.exists(), "50k world not generated (make data)")
    def test_published_operations_match_a_fresh_run(self):
        self.assertEqual(self.ops, json.loads(json.dumps(showcase.operations_payload(), default=str)),
                         "run: python -m orchestrator export-overview")


class Approvals(unittest.TestCase):
    ap = json.loads((REPO / "public/data/approvals.json").read_text(encoding="utf-8"))

    def test_batch_is_what_the_page_says(self):
        a = self.ap
        self.assertEqual(len(a["items"]), a["batch"])
        self.assertEqual(sum(a["by_tier"].values()), a["total_prepared"])
        self.assertTrue(all(i["tier"] in ("A", "B") for i in a["items"]))
        self.assertEqual(len({i["id"] for i in a["items"]}), a["batch"])

    def test_every_draft_is_a_complete_email_with_an_opt_out(self):
        for i in self.ap["items"]:
            self.assertIn("reply STOP", i["body"], i["id"])
            self.assertNotIn("[OPENING]", i["body"])
            self.assertNotIn("{", i["body"])
            self.assertEqual(i["mode"] == "personalized", bool(i["claims"]), i["id"])

    def test_totals_match_the_scoring_of_the_50k_summary(self):
        from orchestrator import scoring
        ov = json.loads((REPO / "public/data/overview.json").read_text(encoding="utf-8"))
        by = {"A": 0, "B": 0, "C": 0}
        for g in ov["groups"]:
            feat = {"size": g["size"], "pain": g["pain"], "signals": [None] * g["signals"]}
            by[scoring.score(feat, ov["config"])["tier"]] += g["counts"].get("contact", 0)
        self.assertEqual((by["A"], by["B"]), (self.ap["by_tier"]["A"], self.ap["by_tier"]["B"]))

    @unittest.skipUnless(GENERATED.exists(), "50k world not generated (make data)")
    def test_published_queue_matches_a_fresh_run(self):
        self.assertEqual(self.ap, json.loads(json.dumps(showcase.approvals_payload(), default=str)),
                         "run: python -m orchestrator export-overview")


if __name__ == "__main__":
    unittest.main()

class MonthReplay(unittest.TestCase):
    @unittest.skipUnless(GENERATED.exists(), "50k world not generated (make data)")
    def test_the_replay_ends_on_the_command_center_totals(self):
        r = showcase.replay_payload()
        ov = json.loads((REPO / "public/data/overview.json").read_text(encoding="utf-8"))
        last = dict(zip(r["cols"], r["rows"][-1]))
        self.assertEqual(last["events"], r["total_events"])
        self.assertEqual(sum(last[k] for k in ("email", "nurture", "wait", "lookup", "sales", "review", "blocked")), ov["n_accounts"])
        self.assertEqual(last["sales"], ov["actions"]["handoff_ae"])
        self.assertEqual(last["review"], ov["actions"]["escalate_human"])
        self.assertEqual(last["blocked"], ov["actions"]["suppress"])
        self.assertEqual(last["wait"], ov["actions"]["wait"])
        self.assertEqual(last["email"] + last["nurture"], ov["actions"]["contact"])
        self.assertEqual(json.loads((REPO / "public/data/replay.json").read_text(encoding="utf-8")), json.loads(json.dumps(r)),
                         "run: python -m orchestrator export-overview")
        for a, b in zip(r["rows"], r["rows"][1:]):                  # a running count never goes down
            self.assertTrue(all(y >= x for x, y in zip(a[2:], b[2:])))

