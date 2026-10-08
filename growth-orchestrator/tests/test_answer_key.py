"""Every difference between the engine and the answer key must be intentional and explainable, never a data error.

Three things were fixed in the generator and are pinned here:
  * a typo must not alter the words the validator reads (referral name, month of a date, country / budget / timeline words);
  * an injected calendar conflict is an expected outcome (escalate, never double-book), marked as such in the key;
  * on a freshly generated world the engine and the key agree on every decision, including the reason codes, the best
    contact and the wait date of each account_targeted;
  * an account that needs enrichment keeps no best contact on its decision; the contact enrichment finds goes to the email.

Run:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import random
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from generator import build, replies
from generator.build import build_world
from generator.export import write_world
from orchestrator import db, scenario
from orchestrator.ai.fixture import FixtureLLM, reply_output
from orchestrator.engine import Orchestrator
from orchestrator.timeutil import parse

SEED = 42


class TyposNeverTouchWhatTheValidatorReads(unittest.TestCase):
    TEXT = "Thanks, but we are out until October 8; please ask Diego Moreno within three months about the budget in Brazil."
    TOKENS = ("October", "Diego", "Moreno", "within", "months", "budget", "Brazil")

    def test_protected_words_are_never_altered_and_the_random_draws_are_the_same(self):
        protected, altered_before = replies._protected("Diego Moreno"), 0
        for seed in range(500):
            r_old, r_new = random.Random(seed), random.Random(seed)
            old = replies._typo(self.TEXT, r_old)                     # the previous behaviour: nothing protected
            new = replies._typo(self.TEXT, r_new, protected)
            self.assertEqual(r_old.getstate(), r_new.getstate(), "the random stream must not move: nothing else in the world changes")
            for tok in self.TOKENS:
                self.assertIn(tok, new, f"seed {seed}: {tok} was altered")
            altered_before += any(tok not in old for tok in self.TOKENS)
        self.assertGreater(altered_before, 0, "the old behaviour did alter these words, so this test would have caught it")


class GeneratedWorld(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.world = build_world(SEED, 5000)

    def test_every_reply_text_supports_what_the_key_says_it_extracts(self):
        text = {e["event_id"]: re.sub(r"\s+", " ", e["payload"]["body_text"]).lower() for e in self.world.events
                if e["type"] == "reply_received" and isinstance(e.get("payload"), dict) and e["payload"].get("body_text")}
        checked = 0
        for t in self.world.truth_replies:
            body, x = text.get(t["event_id"]), t.get("extracted") or {}
            if body is None:
                continue
            if x.get("follow_up_date"):
                y, m, d = x["follow_up_date"].split("-")
                month = replies.MONTHS[int(m) - 1].lower()
                self.assertIn(f"{month} {int(d)}", body, t["event_id"])
            rc = x.get("referred_contact")
            if rc:
                for k in ("name", "email"):
                    if rc.get(k):
                        self.assertIn(rc[k].lower(), body, f"{t['event_id']} {k}")
            checked += 1
        self.assertGreater(checked, 100)

    def test_nothing_is_marked_as_an_injected_failure_unless_its_calendar_is_set_to_fail(self):
        failing = {m["account_id"] for m in self.world.mock_behavior if m["calendar"] == "slot_conflict"}
        for t in self.world.truth_events:
            if t.get("injected_failure"):
                self.assertEqual(t["type"], "meeting_booked")
                self.assertIn(t["account_id"], failing)
        for t in self.world.truth_events:
            if t["type"] == "meeting_booked" and t["expected_handling"] == "process" and t["account_id"] in failing \
                    and t["reason_codes"] != ["NO_AE_AVAILABLE"]:
                self.assertEqual((t["expected_action"], t["reason_codes"]), ("escalate_human", ["CALENDAR_CONFLICT"]))


class InjectedCalendarConflict(unittest.TestCase):
    def test_a_calendar_conflict_is_the_expected_outcome_when_the_calendar_is_set_to_fail(self):
        with mock.patch.object(build, "CALENDAR_BEHAVIORS", [("slot_conflict", 100)]):
            w = build_world(SEED, 5000)
        meetings = [t for t in w.truth_events if t["type"] == "meeting_booked" and t["expected_handling"] == "process"]
        self.assertGreater(len(meetings), 0)
        checked = 0
        for t in meetings:
            if t["reason_codes"] == ["NO_AE_AVAILABLE"]:       # there is no AE to book with: that is decided first
                continue
            self.assertEqual((t["expected_action"], t["reason_codes"], t.get("injected_failure")),
                             ("escalate_human", ["CALENDAR_CONFLICT"], "calendar_slot_conflict"))
            checked += 1
        self.assertGreater(checked, 0)


class EnrichThenContact(unittest.TestCase):
    def test_the_decision_keeps_its_own_contact_and_the_contact_found_by_enrichment_goes_to_the_email(self):
        """G074: the account needs enrichment, enrichment finds a contact, the engine decides again and drafts an email.
        The decision taken on the event has no best contact (the golden says so); the one found afterwards is the recipient."""
        g = next(x for x in scenario.load_golden() if x["id"] == "G074")
        orch, results = scenario.run(g)
        r = results[-1]
        self.assertEqual((r.action, r.final_action), ("enrich", "contact"))
        self.assertIsNone(r.best_contact_id)
        self.assertTrue(r.email and r.email["to"])
        final = db.one(orch.conn, "SELECT best_contact_id FROM decisions WHERE decided_by='rules:after_execution' ORDER BY decision_id DESC LIMIT 1")
        self.assertIsNotNone(final["best_contact_id"])


class EngineAgreesWithTheKey(unittest.TestCase):
    def test_on_a_fresh_world_every_decision_equals_the_key(self):
        """A world generated now (not the committed sample), run through the real engine in delivery order."""
        with tempfile.TemporaryDirectory() as tmp:
            w = build_world(SEED, 3000)
            write_world(w, Path(tmp), SEED, 3000, sqlite=False)
            conn = db.connect()
            db.load_world_dir(conn, Path(tmp))
            by_id = {e["event_id"]: e for e in w.events}
            answers = {by_id[r["event_id"]]["payload"]["body_text"]: reply_output(r["label"], by_id[r["event_id"]]["payload"]["body_text"],
                                                                                 r.get("extracted"))
                       for r in w.truth_replies if r["event_id"] in by_id}
            orch = Orchestrator(conn, llm=FixtureLLM(answers), as_of=parse("2026-10-01T16:00:00Z"))
            truth = {t["delivery_id"]: t for t in w.truth_events}
            bad = []
            for e in w.events:
                r = orch.process(e)
                t = truth.get(e.get("delivery_id")) or {}
                if r.action != t.get("expected_action"):
                    bad.append((e["type"], e.get("account_id"), "action", r.action, t.get("expected_action")))
                elif e["type"] == "account_targeted" and t.get("expected_handling") == "process":
                    got = (sorted(r.reason_codes), r.best_contact_id, r.wait_until)
                    want = (sorted(t["reason_codes"]), t["best_contact_id"], t["wait_until"])
                    if got != want:
                        bad.append((e["type"], e.get("account_id"), "reason codes / best contact / wait date", got, want))
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
