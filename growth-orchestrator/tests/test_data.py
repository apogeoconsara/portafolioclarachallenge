"""Tests that the synthetic data is trustworthy: deterministic, covers every branch, internally consistent,
free of ground-truth leakage, and that the golden set agrees with the independent oracle.

Run:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from generator import golden, oracle
from generator.build import build_world
from generator.config import SCENARIO_QUOTAS
from generator.export import export_seed, write_world
from generator.profile import load, run_checks
from generator.reply_seeds import BY_LABEL, CORE_IDS, LABEL_ACTION, SEEDS
from generator.util import read_jsonl

SEED, N = 42, 5000
ACTIONS = {"contact", "wait", "enrich", "escalate_human", "handoff_ae", "suppress", "no_action", "update_state", None}
HANDLING = {"process", "ignore_duplicate", "dedupe_by_content", "dead_letter", "process_and_reconcile", "ignore_stale",
            "retry_then_process", "reconcile_before_retry", "dead_letter_and_alert", "defer_to_send_window",
            "reread_and_reevaluate", "poll_then_escalate"}
SEED_DIR = Path(__file__).resolve().parent.parent / "data" / "seed"


class World(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name) / "w"
        cls.world = build_world(SEED, N)
        cls.manifest = write_world(cls.world, cls.dir, SEED, N, sqlite=True)
        cls.data = load(cls.dir)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_deterministic(self):
        with tempfile.TemporaryDirectory() as t:
            m2 = write_world(build_world(SEED, N), Path(t), SEED, N, sqlite=False)
        self.assertEqual(self.manifest["determinism_hash"], m2["determinism_hash"])

    def test_different_seed_differs(self):
        with tempfile.TemporaryDirectory() as t:
            m2 = write_world(build_world(SEED + 1, N), Path(t), SEED + 1, N, sqlite=False)
        self.assertNotEqual(self.manifest["determinism_hash"], m2["determinism_hash"])

    def test_scenario_quotas_are_exact(self):
        got = Counter(t["scenario"] for t in self.data["truth_accounts"])
        total = sum(SCENARIO_QUOTAS.values())
        for k, q in SCENARIO_QUOTAS.items():
            self.assertLessEqual(abs(got[k] - N * q / total), 1, k)

    def test_all_integrity_checks_pass(self):
        failed = [c for c in run_checks(self.data) if not c[1]]
        self.assertEqual(failed, [])

    def test_every_decision_branch_is_covered(self):
        reasons = {r for t in self.data["truth_accounts"] for r in t["reason_codes"]}
        expected = {"ELIGIBLE", "RECENT_OUTREACH", "AE_ASSIGNED", "CUSTOMER", "SEQUENCE_EXHAUSTED", "NOT_ICP",
                    "ACTIVE_OPPORTUNITY", "CLOSED_LOST_COOLDOWN", "CHURNED_CUSTOMER", "DUPLICATE_ACCOUNT",
                    "NO_VALID_EMAIL", "STATE_CONFLICT", "NO_CONTACTS", "STALE_ENRICHMENT", "UNSUBSCRIBED",
                    "MISSING_FIRMOGRAPHICS", "ENRICHMENT_EXHAUSTED", "DNC", "HARD_BOUNCE", "SPAM_COMPLAINT",
                    "COMPETITOR", "UNVERIFIED_EMAIL_ONLY", "LEGAL_HOLD"}
        self.assertEqual(expected - reasons, set())

    def test_every_reply_label_and_perturbation_appears(self):
        self.assertEqual(set(LABEL_ACTION) - {r["label"] for r in self.data["truth_replies"]}, set())
        perts = {t["perturbation"] for t in self.data["truth_events"]}
        self.assertEqual({"exact_duplicate", "semantic_duplicate", "content_duplicate", "delayed", "malformed",
                          "out_of_order_race"} - perts, set())

    def test_every_event_type_is_generated(self):
        types = {e["type"] for e in self.data["events"]}
        self.assertEqual({"account_targeted", "reply_received", "unsubscribe_received", "email_bounced", "meeting_booked",
                          "opportunity_created", "opportunity_stage_changed"} - types, set())

    def test_no_ground_truth_leaks_into_source_tables(self):
        forbidden = {"expected_action", "scenario", "reason_codes", "best_contact_id", "label", "trap", "perturbation",
                     "expected_handling", "wait_until", "usable_for_personalization", "duplicate_of", "race"}
        for table in ("accounts", "contacts", "opportunities", "outreach_history", "suppression", "company_facts",
                      "events", "mock_behavior"):
            keys = set().union(*(r.keys() for r in self.data[table]))
            self.assertEqual(keys & forbidden, set(), table)
        # reply bodies must not contain the label
        for e in self.data["events"]:
            if e["type"] == "reply_received" and isinstance(e["payload"], dict):
                self.assertNotIn("expected", json.dumps(e["payload"]))

    def test_invariants_hold_for_every_account(self):
        idx = oracle.Index(self.data["accounts"], self.data["contacts"], self.data["opportunities"],
                           self.data["outreach_history"], self.data["suppression"])
        acc = {a["account_id"]: a for a in self.data["accounts"]}
        con = {c["contact_id"]: c for c in self.data["contacts"]}
        suppressed_contacts = {s["contact_id"] for s in self.data["suppression"] if s["scope"] == "contact"}
        suppressed_domains = {s["domain"] for s in self.data["suppression"] if s["scope"] == "domain"}
        for t in self.data["truth_accounts"]:
            a = acc[t["account_id"]]
            if t["expected_action"] == "contact":
                c = con[t["best_contact_id"]]
                self.assertEqual(c["email_status"], "valid")
                self.assertNotIn(c["contact_id"], suppressed_contacts)
                self.assertNotIn(a["domain"], suppressed_domains)
                self.assertEqual(a["crm_status"], "prospect")
                self.assertIsNone(a["crm_owner_ae_id"])
            if a["crm_status"] in ("customer", "competitor"):
                self.assertNotEqual(t["expected_action"], "contact")
        # never contact a duplicate or a conflicted account
        for t in self.data["truth_accounts"]:
            if t["scenario"] in ("duplicate_domain", "conflict_state", "customer", "suppressed", "low_icp"):
                self.assertNotEqual(t["expected_action"], "contact")
        self.assertTrue(idx.by_domain)

    def test_race_events_arrive_after_targeting_but_happened_before(self):
        events_by_acc = {}
        for e in self.data["events"]:
            events_by_acc.setdefault(e["account_id"], []).append(e)
        races = [t for t in self.data["truth_accounts"] if t["race"]]
        self.assertGreater(len(races), 0)
        for t in races:
            evs = events_by_acc[t["account_id"]]
            tgt = next(e for e in evs if e["type"] == "account_targeted" and e["source"] == "list_import")
            late = next(e for e in evs if e["type"] in ("opportunity_created", "unsubscribe_received"))
            self.assertLess(late["occurred_at"], tgt["occurred_at"])
            self.assertGreater(late["received_at"], tgt["received_at"])

    def test_replies_are_state_aware(self):
        for r in self.data["truth_replies"]:
            if r["crm_state"] in ("customer", "churned_customer"):
                self.assertNotIn(r["expected_action"], ("contact", "handoff_ae", "enrich"))
            if r["label"] in ("unsubscribe", "hostile", "mixed_signals"):
                self.assertEqual(r["expected_action"], "suppress")  # opt-out always wins
            if r["label"] == "prompt_injection":
                self.assertEqual(r["expected_action"], "escalate_human")
        for e in self.data["events"]:
            if e["type"] == "reply_received" and isinstance(e["payload"], dict):
                body = e["payload"].get("body_text", "")
                self.assertNotRegex(body, r"\{d\+\d+\}|\{ref_(name|email)\}")  # tokens must be resolved

    def test_sqlite_matches_jsonl(self):
        import sqlite3
        con = sqlite3.connect(self.dir / "growth.sqlite")
        for table in ("accounts", "contacts", "events", "company_facts"):
            self.assertEqual(con.execute(f"select count(*) from {table}").fetchone()[0], len(self.data[table]))
        row = con.execute("select payload from events limit 1").fetchone()[0]
        self.assertIsInstance(json.loads(row), (dict, str, type(None)))
        con.close()

    def test_facts_traps_are_labelled(self):
        facts = {f["fact_id"]: f for f in self.data["company_facts"]}
        for t in self.data["truth_facts"]:
            if t["trap"] == "unverified_hypothesis":
                self.assertFalse(facts[t["fact_id"]]["is_verified"])
            self.assertEqual(t["usable_for_personalization"], t["trap"] is None and facts[t["fact_id"]]["is_verified"])


class GoldenSet(unittest.TestCase):
    def test_oracle_agrees_with_every_state_derived_expectation(self):
        checked = 0
        for g in golden.GOLDEN:
            if not g["oracle"]:
                continue
            st = g["state"]
            idx = oracle.Index(st["accounts"], st["contacts"], st["opportunities"], st["outreach_history"], st["suppression"])
            d, e = oracle.decide(st["accounts"][0], idx, aes=st.get("aes")), g["expected"][0]
            self.assertEqual((d["action"], d["reason_codes"], d["best_contact_id"]),
                             (e["action"], e["reason_codes"], e["best_contact_id"]), g["id"])
            if e["wait_until"]:
                self.assertEqual(d["wait_until"], e["wait_until"], g["id"])
            checked += 1
        self.assertGreaterEqual(checked, 30)

    def test_schema(self):
        ids = [g["id"] for g in golden.GOLDEN]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 50)
        tags = {t for g in golden.GOLDEN for t in g["tags"]}
        # the demo requirements: success, duplicate, failure, ambiguous/unsafe AI
        for needed in ("happy_path", "duplicate", "failure", "ambiguous", "unsafe_ai", "race", "malformed", "retry"):
            self.assertIn(needed, tags)
        for g in golden.GOLDEN:
            self.assertEqual(len(g["events"]), len(g["expected"]), g["id"])
            base = {"accounts", "contacts", "opportunities", "outreach_history", "suppression", "company_facts"}
            self.assertTrue(base <= set(g["state"]) <= base | {"aes", "runtime_state"}, g["id"])
            for e in g["expected"]:
                self.assertIn(e["action"], ACTIONS, g["id"])
                self.assertIn(e["handling"], HANDLING, g["id"])
            for ev in g["events"]:
                self.assertTrue({"delivery_id", "event_id", "idempotency_key", "type", "occurred_at", "received_at",
                                 "payload", "schema_version", "source"} <= set(ev), g["id"])

    def test_personalization_expectations_follow_the_trap_rules(self):
        from generator.config import AS_OF
        from generator.util import parse_iso
        from datetime import timedelta
        for g in golden.GOLDEN:
            e = g["expected"][0]
            if "usable_fact_ids" not in e:
                continue
            acc = g["state"]["accounts"][0]
            usable = [f["fact_id"] for f in g["state"]["company_facts"]
                      if f["is_verified"] and AS_OF - parse_iso(f["observed_at"]) <= timedelta(days=365)
                      and acc["name"] in f["text"]
                      and not ("employees" in f["text"] and str(acc["employee_count"] * 10) in f["text"])]
            self.assertEqual(sorted(usable), sorted(e["usable_fact_ids"]), g["id"])


class ReplyCorpus(unittest.TestCase):
    def test_every_label_has_multilingual_coverage(self):
        self.assertEqual(set(BY_LABEL), set(LABEL_ACTION))
        for label, seeds in BY_LABEL.items():
            self.assertGreaterEqual(len(seeds), 7, label)
            self.assertTrue(all(s["lang"] == "en" for s in seeds), label)   # the whole case is in English
        self.assertEqual(len({s["seed_id"] for s in SEEDS}), len(SEEDS))

    def test_eval_suite_is_small_and_representative(self):
        cases = golden.eval_cases()
        replies = [c for c in cases if c["kind"] == "reply_classification" and c["suite"] == "core"]
        self.assertTrue(6 <= len(replies) <= 10)
        labels = {c["expected"]["label"] for c in replies}
        for must in ("interested", "unsubscribe", "ambiguous", "mixed_signals", "prompt_injection", "not_now"):
            self.assertIn(must, labels)
        self.assertEqual(len(CORE_IDS), len(replies))
        for c in replies:
            self.assertNotIn("{", c["input"]["reply_text"])
        self.assertEqual(sum(c["kind"] == "personalization_grounding" for c in cases), 4)


class FrozenSeedFiles(unittest.TestCase):
    """Files committed in data/seed must be exactly what the code produces (no silent drift)."""

    def test_in_sync_with_code(self):
        with tempfile.TemporaryDirectory() as t:
            export_seed(Path(t))
            for name in ("reply_seeds.jsonl", "golden_scenarios.jsonl", "eval_cases.jsonl"):
                self.assertEqual(read_jsonl(SEED_DIR / name), read_jsonl(Path(t) / name), name)
            committed = json.loads((SEED_DIR / "sample" / "manifest.json").read_text())
            fresh = json.loads((Path(t) / "sample" / "manifest.json").read_text())
            self.assertEqual(committed["determinism_hash"], fresh["determinism_hash"])


if __name__ == "__main__":
    unittest.main()
