"""The answer key for `account_targeted` is stateful: it is judged on the account's state at the moment the event is processed.

`truth_accounts` is a snapshot at AS_OF. Events delivered earlier in the stream (a deal closing lost, an unsubscribe, a hard
bounce, an opt-out reply) change that state, so a later `account_targeted` must be judged on the state after them. These tests
check that the corrections are exactly those, that nothing else moved, and that the policy itself was not touched.

Run:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import ast
import copy
import json
import unittest
from pathlib import Path

from generator import oracle, stateful
from generator.build import build_world
from generator.config import LOST_COOLDOWN_DAYS
from orchestrator.policy import SEED_DIR

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = SEED_DIR / "sample"
STATE_CHANGING = {"unsubscribe_received", "email_bounced", "reply_received", "opportunity_created", "opportunity_stage_changed"}
KEY_FIELDS = ("expected_action", "reason_codes", "best_contact_id", "wait_until")


def _jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


class CommittedSampleKey(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.events = _jsonl(SAMPLE / "events.jsonl")
        cls.truth = _jsonl(SAMPLE / "truth" / "truth_events.jsonl")
        cls.accounts = {t["account_id"]: t for t in _jsonl(SAMPLE / "truth" / "truth_accounts.jsonl")}
        cls.by_delivery = {e["delivery_id"]: e for e in cls.events}
        cls.order = {e["delivery_id"]: i for i, e in enumerate(cls.events)}

    def test_every_correction_is_attributable_to_an_earlier_state_change_on_the_same_account(self):
        corrected = [t for t in self.truth if t.get("state_changed_by")]
        self.assertGreater(len(corrected), 0)
        for t in corrected:
            self.assertEqual(t["type"], "account_targeted", t["delivery_id"])
            for d in t["state_changed_by"]:
                ev = self.by_delivery[d]
                self.assertEqual(ev["account_id"], t["account_id"], d)
                self.assertIn(ev["type"], STATE_CHANGING, d)
                self.assertLess(self.order[d], self.order[t["delivery_id"]], f"{d} must be delivered before {t['delivery_id']}")

    def test_without_a_state_change_the_key_is_still_the_snapshot(self):
        checked = 0
        for t in self.truth:
            if t["type"] != "account_targeted" or t["expected_handling"] != "process" or t.get("state_changed_by"):
                continue
            snap = self.accounts[t["account_id"]]
            for k in KEY_FIELDS:
                self.assertEqual(t[k], snap[k], f"{t['account_id']}.{k}")
            checked += 1
        self.assertGreater(checked, 400)

    def test_a_deal_that_closes_lost_before_the_account_is_targeted(self):
        row = next(t for t in self.truth if t["account_id"] == "acc_000015" and t["type"] == "account_targeted")
        self.assertEqual((row["expected_action"], row["reason_codes"]), ("handoff_ae", ["AE_ASSIGNED"]))
        (cause,) = row["state_changed_by"]
        ev = self.by_delivery[cause]
        self.assertEqual((ev["type"], ev["payload"]["to_stage"]), ("opportunity_stage_changed", "closed_lost"))
        self.assertEqual(self.accounts["acc_000015"]["expected_action"], "suppress")      # the snapshot still says: open deal

    def test_an_unsubscribe_that_arrives_before_the_account_is_targeted(self):
        row = next(t for t in self.truth if t["account_id"] == "acc_000134" and t["type"] == "account_targeted")
        self.assertEqual((row["expected_action"], row["reason_codes"]), ("suppress", ["UNSUBSCRIBED"]))
        self.assertTrue(row["state_changed_by"])


class StatefulReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.world = build_world(42, 1500)

    def test_replaying_again_changes_nothing(self):
        rows = list(zip(self.world.events, self.world.truth_events))
        self.assertEqual(stateful.correct_truth(self.world, rows), 0)

    def test_the_replay_does_not_touch_the_generated_tables(self):
        w = copy.deepcopy(self.world)
        before = (copy.deepcopy(w.opportunities), copy.deepcopy(w.touches), copy.deepcopy(w.suppression))
        stateful.correct_truth(w, list(zip(w.events, w.truth_events)))
        self.assertEqual((w.opportunities, w.touches, w.suppression), before)

    def test_only_account_targeted_rows_with_an_earlier_change_are_ever_corrected(self):
        order = {t["delivery_id"]: i for i, t in enumerate(self.world.truth_events)}
        for t in self.world.truth_events:
            if "state_changed_by" in t:
                self.assertEqual(t["type"], "account_targeted")
                self.assertTrue(all(order[d] < order[t["delivery_id"]] for d in t["state_changed_by"]))


class PolicyAndIndependence(unittest.TestCase):
    def test_the_stateful_key_does_not_import_the_engine(self):
        tree = ast.parse((ROOT / "generator" / "stateful.py").read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            names = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""] if isinstance(n, ast.ImportFrom) else []
            for name in names:
                self.assertFalse(name.split(".")[0] == "orchestrator", f"generator/stateful.py imports {name}")

    def test_the_policy_was_not_changed_by_making_the_key_stateful(self):
        """Pinned on purpose: whether a closed-lost account may go straight back to its AE (rule 7 before rule 8) is a
        business decision that is being reviewed separately. Changing it is a deliberate edit of this test, not a side effect."""
        self.assertEqual(LOST_COOLDOWN_DAYS, 90)
        src = (ROOT / "generator" / "oracle.py").read_text(encoding="utf-8")
        self.assertLess(src.index('"ACTIVE_OPPORTUNITY"'), src.index('"AE_ASSIGNED"'))
        self.assertLess(src.index('"AE_ASSIGNED"'), src.index('"CLOSED_LOST_COOLDOWN"'))
        policy = (ROOT / "data" / "POLICY.md").read_text(encoding="utf-8")
        self.assertIn("Closed-lost opportunity < 90 days ago", policy)


if __name__ == "__main__":
    unittest.main()
