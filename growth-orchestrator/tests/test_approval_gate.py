"""No first outreach can execute without a person's approval: Draft -> Pending approval -> Approved -> Mock send.

The default engine has no approver, so every outreach email stops at `pending_approval`. These tests drive the real engine
that way (no AutoApprover) and check the gate from both sides: nothing reaches the mock send before approval, and what
is approved is re-checked, sent exactly once and attributed to a named reviewer.

Run:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import ast
import json
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from orchestrator import db
from orchestrator.ai import draft as ai_draft
from orchestrator.ai.fixture import FixtureLLM, reply_output
from orchestrator.engine import Orchestrator
from orchestrator.executor import ApprovalRequired
from orchestrator.scenario import build, load_golden
from orchestrator.timeutil import parse

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "seed" / "sample"
GOLDEN = {g["id"]: g for g in load_golden()}
AS_OF = parse("2026-10-01T16:00:00Z")


def _held(gid):
    """A fresh engine with NO approver, after the scenario's events: the outreach email is waiting for a person."""
    g = GOLDEN[gid]
    orch = build(g)
    results = [orch.process(e) for e in g["events"]]
    return g, orch, results


def _sends(orch):
    return len(orch.mocks.ledger["send"])


def _actions(orch):
    return db.rows(orch.conn, "SELECT * FROM actions WHERE kind='email'")


def _audit_kinds(orch):
    return [a["kind"] for a in orch.audit.trail()]


class NothingSendsWithoutApproval(unittest.TestCase):
    def test_a_clean_prospect_is_drafted_and_held_not_sent(self):
        g, orch, (r,) = _held("G001")
        self.assertEqual(r.action, "contact")
        self.assertEqual(r.email["status"], "pending_approval")
        self.assertEqual(_sends(orch), 0)
        self.assertEqual([c for c in orch.mocks.calls if c[:2] == ("send", "send_email")], [])
        (a,) = _actions(orch)
        self.assertEqual((a["status"], a["reviewer"]), ("pending_approval", None))
        self.assertEqual(len(orch.pending_approvals()), 1)
        # the draft is not outreach: no touch is recorded until it is really (mock) sent
        self.assertEqual(db.rows(orch.conn, "SELECT * FROM outreach_history WHERE sender_type='sequence'"), [])
        self.assertIn("email_pending_approval", _audit_kinds(orch))
        self.assertNotIn("email_approved", _audit_kinds(orch))

    def test_the_executor_refuses_to_send_an_email_nobody_approved(self):
        g, orch, (r,) = _held("G001")
        account, contact = orch._account(r.account_id), orch._contact(r.best_contact_id)
        dr = ai_draft.DraftResult("Subject", "Body", "generic", "x")
        with self.assertRaises(ApprovalRequired):
            orch.ex.send_email(account, contact, 1, None, dr, {"account_id": r.account_id})
        self.assertEqual(_sends(orch), 0)
        self.assertIn("send_blocked_no_approval", _audit_kinds(orch))
        # nor can an email with no held action at all be sent
        with self.assertRaises(ApprovalRequired):
            orch.ex.send_email(account, contact, 9, None, dr, {"account_id": r.account_id})
        # a rejected draft stays blocked
        key = orch.pending_approvals()[0]["key"]
        orch.reject(key, "ana (reviewer)", "not now")
        with self.assertRaises(ApprovalRequired):
            orch.ex.send_email(account, contact, 1, None, dr, {"account_id": r.account_id})
        self.assertEqual(_sends(orch), 0)

    def test_the_only_call_sites_of_the_mock_send_are_gated(self):
        """Structural check: `send_email` is reached only from `_release` (right after a recorded review) and `run_due`
        (which only picks up emails a person approved); both go through the executor's own approval check."""
        found = set()
        for p in (ROOT / "orchestrator").rglob("*.py"):
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                for n in ast.walk(fn):
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "send_email":
                        found.add((p.name, fn.name))
        self.assertEqual(found, {("executor.py", "send_email"), ("engine.py", "_release"), ("engine.py", "run_due")})

    def test_replaying_the_events_does_not_hold_a_second_draft(self):
        g, orch, _ = _held("G001")
        for e in g["events"]:
            orch.process(e)
        self.assertEqual(len(orch.pending_approvals()), 1)
        self.assertEqual(_sends(orch), 0)


class ApprovalReleasesExactlyOnce(unittest.TestCase):
    def test_approve_sends_once_and_names_the_reviewer(self):
        g, orch, _ = _held("G001")
        key = orch.pending_approvals()[0]["key"]
        out = orch.approve(key, "ana (reviewer)", now=AS_OF)
        self.assertEqual((out["status"], out["reviewer"]), ("sent_mock", "ana (reviewer)"))
        self.assertEqual(_sends(orch), 1)
        (a,) = _actions(orch)
        self.assertEqual((a["status"], a["reviewer"]), ("succeeded", "ana (reviewer)"))
        self.assertTrue(a["reviewed_at"])
        kinds = _audit_kinds(orch)
        self.assertLess(kinds.index("email_pending_approval"), kinds.index("email_approved"))
        self.assertLess(kinds.index("email_approved"), kinds.index("email_ok"))
        approved = next(a for a in orch.audit.trail() if a["kind"] == "email_approved")
        self.assertEqual(approved["detail"]["reviewer"], "ana (reviewer)")
        # the send is the first touch with this contact, recorded only now
        self.assertEqual(len(db.rows(orch.conn, "SELECT * FROM outreach_history WHERE sender_type='sequence'")), 1)

    def test_approving_twice_does_not_send_twice(self):
        g, orch, _ = _held("G001")
        key = orch.pending_approvals()[0]["key"]
        orch.approve(key, "ana (reviewer)", now=AS_OF)
        again = orch.approve(key, "bruno (reviewer)", now=AS_OF)
        self.assertEqual((again["status"], again["current"]), ("not_pending", "succeeded"))
        self.assertEqual(_sends(orch), 1)
        self.assertEqual(_actions(orch)[0]["reviewer"], "ana (reviewer)")          # the first decision stands

    def test_reject_sends_nothing_and_cannot_be_approved_afterwards(self):
        g, orch, _ = _held("G001")
        key = orch.pending_approvals()[0]["key"]
        out = orch.reject(key, "ana (reviewer)", "wrong contact", now=AS_OF)
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(orch.approve(key, "bruno (reviewer)", now=AS_OF)["status"], "not_pending")
        self.assertEqual(_sends(orch), 0)
        self.assertEqual(orch.pending_approvals(), [])
        rejected = next(a for a in orch.audit.trail() if a["kind"] == "email_rejected")
        self.assertEqual((rejected["detail"]["reviewer"], rejected["detail"]["reason"]), ("ana (reviewer)", "wrong contact"))

    def test_a_reviewer_identity_is_required(self):
        g, orch, _ = _held("G001")
        key = orch.pending_approvals()[0]["key"]
        for who in ("", "   ", None):
            with self.assertRaises(ValueError):
                orch.approve(key, who, now=AS_OF)
        self.assertEqual(_sends(orch), 0)
        self.assertEqual(len(orch.pending_approvals()), 1)

    def test_an_unknown_key_changes_nothing(self):
        g, orch, _ = _held("G001")
        self.assertEqual(orch.approve("send:nope:nope:1", "ana (reviewer)", now=AS_OF)["status"], "not_found")
        self.assertEqual(_sends(orch), 0)


class ApprovalIsRecheckedAtApprovalTime(unittest.TestCase):
    def test_an_unsubscribe_that_arrives_while_the_draft_waits_cancels_it(self):
        g, orch, (r,) = _held("G090")                       # Thursday night: would go out Friday 09:00
        unsub = dict(g["events"][0], delivery_id="dlv_g090_u", event_id="evt_g090_u", idempotency_key="key_g090_u",
                     type="unsubscribe_received", contact_id=r.best_contact_id, occurred_at="2026-10-02T04:00:00Z",
                     received_at="2026-10-02T04:00:01Z", payload={"method": "link_click", "thread_id": None})
        orch.process(unsub)
        key = _actions(orch)[0]["idempotency_key"]
        self.assertEqual(orch.approve(key, "ana (reviewer)", now=parse("2026-10-02T09:00:00Z"))["status"], "not_pending")
        self.assertEqual(_actions(orch)[0]["status"], "cancelled")
        self.assertEqual(_sends(orch), 0)

    def test_a_deal_that_opens_while_the_draft_waits_blocks_the_approval(self):
        g, orch, (r,) = _held("G001")
        orch.conn.execute("INSERT INTO opportunities (opportunity_id, account_id, amount_usd, owner_ae_id, lost_reason, "
                          "closed_at, stage, created_at) VALUES ('opp_late', ?, 5000, NULL, NULL, NULL, 'discovery', ?)",
                          (r.account_id, "2026-10-01T15:00:00Z"))
        key = orch.pending_approvals()[0]["key"]
        out = orch.approve(key, "ana (reviewer)", now=AS_OF)
        self.assertEqual((out["status"], out["now_decision"], out["reason_codes"]), ("cancelled", "suppress", ["ACTIVE_OPPORTUNITY"]))
        self.assertEqual(_sends(orch), 0)
        self.assertEqual(_actions(orch)[0]["reviewer"], None)                      # an approval that was blocked is not an approval
        self.assertIn("email_approval_blocked", _audit_kinds(orch))

    def test_an_approved_email_still_waits_for_its_send_window_and_goes_out_once(self):
        g, orch, (r,) = _held("G090")
        key = orch.pending_approvals()[0]["key"]
        out = orch.approve(key, "ana (reviewer)", now=parse(g["events"][0]["received_at"]))     # Thursday night in Mexico
        self.assertEqual((out["status"], out["send_after"]), ("scheduled", "2026-10-02T15:00:00Z"))
        self.assertEqual(_sends(orch), 0)
        self.assertEqual(orch.run_due(parse("2026-10-02T14:59:00Z")), [])
        self.assertEqual([o["status"] for o in orch.run_due(parse("2026-10-02T15:00:00Z"))], ["ok"])
        self.assertEqual(orch.run_due(parse("2026-10-02T16:00:00Z")), [])
        self.assertEqual(_sends(orch), 1)
        self.assertEqual(_actions(orch)[0]["reviewer"], "ana (reviewer)")


    def test_a_scheduled_row_without_a_reviewer_is_never_sent(self):
        """Even if a row were forced to `scheduled` by hand, the scheduler ignores it and the executor would refuse it."""
        g, orch, (r,) = _held("G090")
        orch.conn.execute("UPDATE actions SET status='scheduled' WHERE kind='email'")
        self.assertEqual(orch.run_due(parse("2026-10-02T15:00:00Z")), [])
        self.assertEqual(_sends(orch), 0)


class FailuresAfterApprovalStillBehave(unittest.TestCase):
    def test_a_503_on_the_send_is_retried_and_ends_with_one_email(self):
        g, orch, _ = _held("G070")                         # the email API answers 503 once
        self.assertEqual(_sends(orch), 0)
        out = orch.approve(orch.pending_approvals()[0]["key"], "ana (reviewer)", now=AS_OF)
        self.assertEqual(out["status"], "sent_mock")
        self.assertEqual(_sends(orch), 1)
        self.assertEqual(sum(1 for c in orch.mocks.calls if c[:2] == ("send", "send_email")),
                         g["expected"][0]["expected_send_attempts"])


class DefaultEngineOnTheSampleStream(unittest.TestCase):
    """All 561 sample deliveries through the default engine: nothing is sent until people approve."""

    @classmethod
    def setUpClass(cls):
        cls.conn = db.connect()
        db.load_world_dir(cls.conn, SAMPLE)
        events = [json.loads(l) for l in (SAMPLE / "events.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        by_id = {e["event_id"]: e for e in events}
        truth = [json.loads(l) for l in (SAMPLE / "truth" / "truth_replies.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        answers = {by_id[r["event_id"]]["payload"]["body_text"]: reply_output(r["label"], by_id[r["event_id"]]["payload"]["body_text"],
                                                                            r.get("extracted"))
                   for r in truth if r["event_id"] in by_id}
        cls.touches_before = cls.conn.execute("SELECT COUNT(*) FROM outreach_history WHERE sender_type='sequence'").fetchone()[0]
        cls.orch = Orchestrator(cls.conn, llm=FixtureLLM(answers), as_of=AS_OF)
        cls.results = [cls.orch.process(e) for e in events]

    # unittest runs tests in name order and these two share one engine: check the untouched state first (1), then approve (2)
    def test_1_zero_emails_reach_the_mock_send_without_approval(self):
        self.assertEqual(self.orch.mocks.ledger["send"], {})
        self.assertEqual([c for c in self.orch.mocks.calls if c[:2] == ("send", "send_email")], [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outreach_history WHERE sender_type='sequence'").fetchone()[0],
                         self.touches_before)
        states = {a["status"] for a in _actions(self.orch)}
        self.assertTrue(states <= {"pending_approval", "cancelled"}, states)
        self.assertTrue(all(a["reviewer"] is None for a in _actions(self.orch)))
        self.assertGreater(len(self.orch.pending_approvals()), 0)

    def test_2_approving_everything_sends_each_email_once_with_a_reviewer(self):
        pending = self.orch.pending_approvals()
        outs = [self.orch.approve(p["key"], "test reviewer", now=AS_OF) for p in pending]
        sent = [o for o in outs if o["status"] == "sent_mock"]
        self.assertLessEqual(len(sent), len(pending))
        self.assertEqual(_sends(self.orch), len(sent))
        for a in _actions(self.orch):
            if a["status"] == "succeeded":
                self.assertEqual(a["reviewer"], "test reviewer")
        self.assertEqual([self.orch.approve(p["key"], "test reviewer", now=AS_OF)["status"] for p in pending],
                         ["not_pending"] * len(pending))
        self.assertEqual(_sends(self.orch), len(sent))


class ApprovalsOverHttp(unittest.TestCase):
    """`python -m orchestrator serve` end to end: webhooks in, held drafts out, a named reviewer releases one."""

    @classmethod
    def setUpClass(cls):
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0))
            port = sk.getsockname()[1]
        cls.base = f"http://127.0.0.1:{port}"
        cls.proc = subprocess.Popen([sys.executable, "-m", "orchestrator", "serve", "--port", str(port)], cwd=ROOT,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                urllib.request.urlopen(cls.base + "/approvals", timeout=1).read()
                break
            except (urllib.error.URLError, ConnectionError, OSError):
                time.sleep(0.25)
        else:
            cls.proc.kill()
            raise RuntimeError("the local server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.kill()
        cls.proc.wait()

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(), method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_hold_then_approve_then_the_decision_stands(self):
        events = [json.loads(l) for l in (SAMPLE / "events.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        for e in events:
            self.assertEqual(self.call("POST", "/webhook", e)[0], 200)
            pending = self.call("GET", "/approvals")[1]["pending"]
            if len(pending) >= 2:
                break
        self.assertGreaterEqual(len(pending), 2)
        first, second = (urllib.parse.quote(p["key"], safe="") for p in pending[:2])
        self.assertEqual(self.call("POST", f"/approvals/{first}/approve", {}), (400, {"error": "reviewer_required"}))
        self.assertEqual(len(self.call("GET", "/approvals")[1]["pending"]), len(pending))      # still held
        code, out = self.call("POST", f"/approvals/{first}/approve", {"reviewer": "ana (reviewer)"})
        self.assertEqual((code, out["reviewer"]), (200, "ana (reviewer)"))
        self.assertIn(out["status"], ("sent_mock", "scheduled"))
        self.assertEqual(self.call("POST", f"/approvals/{first}/approve", {"reviewer": "bruno"})[1]["status"], "not_pending")
        self.assertEqual(self.call("POST", f"/approvals/{second}/reject", {"reviewer": "ana (reviewer)", "reason": "x"})[1]["status"],
                         "rejected")
        self.assertEqual(self.call("POST", "/approvals/x/delete", {"reviewer": "a"})[0], 404)


if __name__ == "__main__":
    unittest.main()
