"""One test per sentence of the challenge's "Scenario" paragraph, run against generated data.

If any of these fails, the data no longer supports something the case says to assume.

Run:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

from generator import golden, policy_data
from generator.build import build_world
from generator.config import AS_OF, OPEN_STAGES, RECENT_OUTREACH_DAYS
from generator.export import write_world
from generator.util import parse_iso, read_jsonl

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "data" / "seed"
SYSTEMS = ("crm", "enrichment", "send", "calendar")


class Scenario(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.w = build_world(42, 5000)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        write_world(cls.w, cls.dir, 42, 5000, sqlite=True)
        cls.contracts = json.loads((SEED / "mock_api_contracts.json").read_text())
        cls.policy = json.loads((SEED / "send_policy.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # "Assume Clara works with CRM, enrichment, outreach and calendar systems."
    def test_crm_system(self):
        w = self.w
        self.assertEqual({a["crm_status"] for a in w.accounts}, {"prospect", "customer", "churned_customer", "competitor"})
        self.assertTrue(w.opportunities)
        self.assertTrue({"opportunity_created", "opportunity_stage_changed"} <= {e["type"] for e in w.events if e["source"] == "crm"})
        self.assertIn("crm", self.policy["api_limits"])
        self.assertIn("crm", self.contracts)

    def test_enrichment_system(self):
        self.assertTrue(self.w.mock_enrichment)
        self.assertEqual({m["variant"] for m in self.w.mock_enrichment}, {"good_contacts", "no_data", "contradictory"})
        self.assertIn("enrichment", self.policy["api_limits"])
        self.assertIn("enrichment", self.contracts)

    def test_outreach_system(self):
        self.assertTrue(self.w.touches)
        self.assertEqual({t["sender_type"] for t in self.w.touches}, {"sequence", "ae", "cs"})
        self.assertTrue(policy_data.templates())
        self.assertIn("send", self.policy["api_limits"])
        self.assertIn("send", self.contracts)

    def test_calendar_system(self):
        self.assertTrue(self.w.calendar)
        self.assertTrue(any(r["free_slots"] for r in self.w.calendar))
        self.assertTrue(any(e["type"] == "meeting_booked" and e["source"] == "calendar" for e in self.w.events))
        self.assertIn("calendar", self.policy["api_limits"])
        self.assertIn("calendar", self.contracts)

    # "Accounts may have multiple contacts and may already be customers, have active opportunities,
    #  recent outreach, an assigned AE or suppression rules."
    def test_multiple_contacts(self):
        n = Counter(c["account_id"] for c in self.w.contacts)
        self.assertGreater(sum(1 for v in n.values() if v >= 2), len(self.w.accounts) * 0.5)
        self.assertGreaterEqual(max(n.values()), 5)

    def test_customers(self):
        self.assertTrue(any(a["crm_status"] == "customer" for a in self.w.accounts))
        self.assertTrue(any(o["stage"] == "closed_won" for o in self.w.opportunities))

    def test_active_opportunities(self):
        self.assertTrue(any(o["stage"] in OPEN_STAGES for o in self.w.opportunities))

    def test_recent_outreach(self):
        recent = [t for t in self.w.touches if t["sender_type"] == "sequence"
                  and AS_OF - parse_iso(t["sent_at"]) < timedelta(days=RECENT_OUTREACH_DAYS)]
        self.assertTrue(recent)

    def test_assigned_ae(self):
        self.assertTrue(any(a["crm_owner_ae_id"] for a in self.w.accounts))
        self.assertTrue(self.w.aes)

    def test_suppression_rules(self):
        self.assertEqual({s["scope"] for s in self.w.suppression}, {"contact", "domain"})
        self.assertGreaterEqual({s["reason"] for s in self.w.suppression},
                                {"unsubscribe", "hard_bounce", "spam_complaint", "dnc_request", "legal_hold"})

    # "Events may arrive duplicated, delayed or out of order."
    def test_events_duplicated_delayed_out_of_order(self):
        kinds = Counter(t["perturbation"] for t in self.w.truth_events)
        for k in ("exact_duplicate", "semantic_duplicate", "content_duplicate", "delayed", "out_of_order_race", "malformed"):
            self.assertGreater(kinds[k], 0, k)
        by = defaultdict(list)
        for e, t in zip(self.w.events, self.w.truth_events):
            if t["perturbation"] != "malformed":
                by[e["account_id"]].append(e["occurred_at"])        # list is in arrival order
        self.assertTrue(any(v != sorted(v) for v in by.values()), "no account had events arrive out of occurrence order")
        late = [e for e, t in zip(self.w.events, self.w.truth_events) if t["perturbation"] != "malformed"
                and parse_iso(e["received_at"]) - parse_iso(e["occurred_at"]) > timedelta(hours=1)]
        self.assertTrue(late)

    # "External APIs may fail, rate-limit requests or return uncertain outcomes."
    def test_every_external_api_can_fail_rate_limit_and_be_uncertain(self):
        failing = ("timeout", "server_error", "transient_error", "malformed", "hard_reject", "slot_conflict", "stale_version")
        for system in SYSTEMS:
            assigned = {m[system] for m in self.w.mock_behavior}
            declared = {k for k in self.contracts[system] if k not in ("purpose",)}
            self.assertEqual(assigned, declared, system)                                   # every behaviour is actually assigned
            self.assertTrue(any(any(f in b for f in failing) for b in assigned), f"{system}: no failure")
            self.assertTrue(any("rate_limit" in b for b in assigned), f"{system}: no rate limit")
            self.assertTrue(any("uncertain" in b for b in assigned), f"{system}: no uncertain outcome")
            for b in declared:
                self.assertIn("expected_handling", self.contracts[system][b], (system, b))

    def test_failure_goldens_exist_for_each_system(self):
        tags = defaultdict(set)
        for g in golden.GOLDEN:
            for t in g["tags"]:
                tags[t].add(g["id"])
        for system_tag in ("crm", "calendar", "enrichment"):
            self.assertTrue(tags[system_tag], system_tag)
        self.assertTrue(tags["rate_limit"] and tags["uncertain_outcome"] and tags["retry"])

    # "AI-generated decisions may also be incomplete, malformed or unsupported by available data."
    def test_ai_outputs_incomplete_malformed_unsupported(self):
        rec = read_jsonl(SEED / "llm_recordings.jsonl")
        variants = {r["variant"].split(":")[0] for r in rec}
        incomplete = {"truncated_json", "missing_field", "empty_output"}
        malformed = {"prose_not_json", "bad_enum", "confidence_out_of_range", "extra_field"}
        unsupported = {"evidence_hallucinated", "hallucinated_referral_email", "date_invented", "date_in_past",
                       "qualification_hallucinated", "fact_id_hallucinated", "unsupported_detail", "invented_fact_when_none"}
        for name, group in (("incomplete", incomplete), ("malformed", malformed), ("unsupported", unsupported)):
            self.assertTrue(group <= variants, (name, group - variants))
        kinds = {r["kind"] for r in rec}
        self.assertEqual(kinds, {"reply_interpretation", "personalization_draft"})

    # "Assume a scale of approximately 50,000 target companies per month."
    def test_scale_is_fifty_thousand_accounts(self):
        self.assertIn("N ?= 50000", (ROOT / "Makefile").read_text())
        main_src = (ROOT / "generator" / "__main__.py").read_text()
        self.assertIn("default=50_000", main_src)
        full = ROOT / "data" / "generated" / "manifest.json"
        if full.exists():
            self.assertEqual(json.loads(full.read_text())["n_accounts"], 50_000)

    def test_burst_arrival_pattern(self):
        """Each monthly list lands within ~20 minutes, which is what stresses queues and rate limits."""
        targeted = [parse_iso(e["occurred_at"]) for e, t in zip(self.w.events, self.w.truth_events)
                    if t["perturbation"] != "malformed" and e["type"] == "account_targeted" and e["source"] == "list_import"]
        window = Counter(int((t - AS_OF).total_seconds() // 1200) for t in targeted)       # 20-minute buckets
        top = sorted(window.values(), reverse=True)[:5]
        self.assertGreater(top[0], 0.1 * len(targeted))

    # "You may use mock APIs, synthetic data, SQLite and a single outreach channel."
    def test_synthetic_mock_sqlite_single_channel(self):
        self.assertTrue(all(a["domain"].endswith(".example") for a in self.w.accounts))     # synthetic, cannot be real
        con = sqlite3.connect(self.dir / "growth.sqlite")
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        self.assertTrue({"accounts", "contacts", "events", "mock_behavior", "outreach_history", "ae_calendar"} <= tables)
        con.close()
        self.assertEqual({t["channel"] for t in self.w.touches}, {"email"})
        self.assertEqual(self.policy["channel"], "email")
        self.assertEqual({e["source"] for e in self.w.events} - {"list_import", "email_provider", "crm", "calendar"}, set())


if __name__ == "__main__":
    unittest.main()
