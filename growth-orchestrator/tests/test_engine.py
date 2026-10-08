"""The orchestrator end to end: every golden scenario through the real engine, effect counts against the mock ledgers,
replayed deliveries, the full sample event stream against the truth file, scheduled sends, and the no-real-email
guarantee.

Run:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import ast
import json
import unittest
from collections import Counter
from pathlib import Path

from orchestrator import db
from orchestrator.ai.fixture import FixtureLLM, reply_output
from orchestrator.ai.llm import UnavailableLLM
from orchestrator.engine import AutoApprover, Orchestrator
from orchestrator.scenario import build, load_golden, run
from orchestrator.timeutil import parse

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "seed" / "sample"
AS_OF = parse("2026-10-01T16:00:00Z")
GOLDEN = load_golden()
# These tests check what happens AFTER an outreach email is approved (retries, windows, duplicates, races), so a harness
# approves each held draft on the spot. The default engine, with nobody approving, is tested in test_approval_gate.py.
HARNESS = AutoApprover("scenario harness (test)")
FIELDS = ["action", "handling", "best_contact_id", "wait_until", "route_to_ae_id", "route_reason", "final_action",
          "final_reason_codes", "send_after", "scope", "contact_id", "cancel_pending_outreach", "usable_fact_ids",
          "needs_human_review", "automation_allowed", "extracted"]


def _jsonl(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


class GoldenThroughEngine(unittest.TestCase):
    def test_every_golden_scenario(self):
        for g in GOLDEN:
            with self.subTest(g["id"]):
                orch, results = run(g, approver=HARNESS)
                self.assertEqual(len(results), len(g["expected"]))
                for i, (r, x) in enumerate(zip(results, g["expected"])):
                    d = r.to_dict()
                    for k in FIELDS:
                        if k in x and (x[k] not in (None, []) or k in ("action", "handling", "automation_allowed")):
                            self.assertEqual(d.get(k), x[k], f"{g['id']}[{i}].{k}")
                    # the label people assigned (or the rule) is always among the reason codes
                    self.assertTrue(set(x.get("reason_codes") or []) <= set(d["reason_codes"]), f"{g['id']}[{i}]")
                    if d["action"] is not None:
                        self.assertNotIn(d["action"], x.get("unsafe_actions", []), g["id"])
                        self.assertNotIn(d["final_action"], x.get("unsafe_actions", []), g["id"])

    def test_effect_counts_against_the_mock_ledgers(self):
        for g in GOLDEN:
            x = g["expected"][0]
            orch, results = run(g, approver=HARNESS)
            led, calls = orch.mocks.ledger, orch.mocks.calls
            tasks = [p for p in led["crm"].values() if p.get("kind") == "ae_handoff_task"]
            with self.subTest(g["id"]):
                if "expected_emails_sent" in x:
                    self.assertEqual(len(led["send"]), x["expected_emails_sent"])
                if "expected_emails_sent_max" in x:
                    self.assertLessEqual(len(led["send"]), x["expected_emails_sent_max"])
                if "expected_send_attempts" in x:
                    self.assertEqual(sum(1 for c in calls if c[0] == "send" and c[1] == "send_email"),
                                     x["expected_send_attempts"])
                if "expected_crm_tasks_created" in x:
                    self.assertEqual(len(tasks), x["expected_crm_tasks_created"])
                if "expected_crm_tasks_created_max" in x:
                    self.assertLessEqual(len(tasks), x["expected_crm_tasks_created_max"])
                if "expected_meetings_created" in x:
                    self.assertEqual(len(led["calendar"]), x["expected_meetings_created"])
                if "expected_meetings_created_max" in x:
                    self.assertLessEqual(len(led["calendar"]), x["expected_meetings_created_max"])
                if "expected_contact_marked_invalid" in x:
                    c = db.one(orch.conn, "SELECT email_status FROM contacts WHERE contact_id=?",
                               (x["expected_contact_marked_invalid"],))
                    self.assertNotEqual(c["email_status"], "valid")
                if x.get("handling") == "reconcile_before_retry":
                    self.assertTrue(any(c[1] == "lookup" for c in calls), "must look up before retrying")

    def test_uncertain_send_never_double_sends_either_way(self):
        """The mock applies an uncertain send for some keys and not others; both branches end with one email."""
        g = next(s for s in GOLDEN if s["id"] == "G072")
        for suffix in ("", "_x", "_y", "_z"):
            s = json.loads(json.dumps(g))
            for t in ("accounts", "contacts"):
                for row in s["state"][t]:
                    row["account_id"] += suffix
                    if t == "contacts":
                        row["contact_id"] += suffix
            for e in s["events"]:
                e["account_id"] += suffix
            orch, (r,) = run(s, approver=HARNESS)
            self.assertEqual(len(orch.mocks.ledger["send"]), 1, suffix)
            self.assertEqual(r.handling, "reconcile_before_retry")

    def test_every_processed_event_is_audited_and_escalations_are_queued(self):
        for g in GOLDEN:
            orch, results = run(g, approver=HARNESS)
            for r in results:
                trail = orch.audit.trail(event_id=r.event_id)
                self.assertTrue(any(t["kind"] == "event_received" for t in trail), g["id"])
                if r.handling not in ("ignore_duplicate", "dead_letter", "dedupe_by_content", "ignore_stale"):
                    self.assertTrue(any(t["kind"] == "decision" for t in trail), g["id"])
                if "escalate_human" in (r.action, r.final_action):
                    q = orch.conn.execute("SELECT COUNT(*) FROM review_queue WHERE account_id=?", (r.account_id,)).fetchone()[0]
                    self.assertGreater(q, 0, g["id"])


class Idempotency(unittest.TestCase):
    def test_replaying_every_delivery_changes_nothing(self):
        for g in GOLDEN:
            orch = build(g, approver=HARNESS)
            for e in g["events"]:
                orch.process(e)
            before = {k: dict(v) for k, v in orch.mocks.ledger.items()}
            n_dec = orch.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
            again = [orch.process(e) for e in g["events"]]
            with self.subTest(g["id"]):
                self.assertEqual({k: dict(v) for k, v in orch.mocks.ledger.items()}, before)
                self.assertEqual(orch.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], n_dec)
                self.assertTrue(all(r.handling == "ignore_duplicate" for r in again if r.delivery_id))


class SampleStream(unittest.TestCase):
    """All 561 sample deliveries (duplicates, garbage, races, replies) against the independently generated truth."""

    @classmethod
    def setUpClass(cls):
        cls.conn = db.connect()
        db.load_world_dir(cls.conn, SAMPLE)
        cls.events = _jsonl(SAMPLE / "events.jsonl")
        truth = {t["delivery_id"]: t for t in _jsonl(SAMPLE / "truth" / "truth_events.jsonl")}
        by_id = {e["event_id"]: e for e in cls.events}
        answers = {by_id[r["event_id"]]["payload"]["body_text"]: reply_output(r["label"], by_id[r["event_id"]]["payload"]["body_text"],
                                                                            r.get("extracted"))
                   for r in _jsonl(SAMPLE / "truth" / "truth_replies.jsonl") if r["event_id"] in by_id}
        # the truth is computed on the snapshot, so the stream is replayed "as of" the snapshot time
        cls.orch = Orchestrator(cls.conn, llm=FixtureLLM(answers), as_of=AS_OF, approver=HARNESS)
        cls.results = [(e, cls.orch.process(e), truth[e["delivery_id"]]) for e in cls.events]

    def test_handling_and_action_match_the_truth(self):
        intake = {"ignore_duplicate", "dead_letter", "dedupe_by_content", "ignore_stale", "process_and_reconcile"}
        bad = []
        for e, r, t in self.results:
            h_ok = r.handling == t["expected_handling"] or (t["expected_handling"] == "process" and r.handling not in intake)
            if not h_ok or r.action != t.get("expected_action"):
                bad.append((t.get("scenario"), e["account_id"], t.get("expected_action"), r.action))
        # The key is stateful (generator/stateful.py): an account_targeted is judged on the state left by the events delivered
        # before it, as the engine does. There is no known divergence left on the sample.
        self.assertEqual(bad, [])

    def test_safety_invariants(self):
        sent = [json.loads(a["request"]) | a for a in db.rows(self.conn, "SELECT * FROM actions WHERE kind='email' AND status='succeeded'")]
        keys = Counter(a["idempotency_key"] for a in sent)
        self.assertTrue(all(v == 1 for v in keys.values()))
        self.assertEqual(len(sent), len(self.orch.mocks.ledger["send"]))
        for a in sent:
            self.assertIn("reply STOP", a["body"])                          # every email carries the opt-out line
            sup = db.rows(self.conn, "SELECT * FROM suppression WHERE (contact_id=? OR (account_id=? AND scope<>'contact'))",
                          (a["contact_id"], a["account_id"]))
            # suppressions known before the run are never emailed; one that arrived late (race) is flagged to a human
            self.assertEqual([s for s in sup if not s["suppression_id"].startswith("sup_o_")], [], a["idempotency_key"])
            if sup:
                flagged = self.conn.execute("SELECT COUNT(*) FROM review_queue WHERE account_id=? AND reason_codes LIKE "
                                            "'%SENT_BEFORE_LATE_FACT%'", (a["account_id"],)).fetchone()[0]
                self.assertGreater(flagged, 0, a["idempotency_key"])
        per_account = Counter(a["account_id"] for a in sent)
        self.assertLessEqual(max(per_account.values()), 1)                  # never two emails to one account in a run
        customers = {r["account_id"] for r in db.rows(self.conn, "SELECT account_id FROM accounts WHERE crm_status IN "
                                                                 "('customer','churned_customer','competitor')")}
        self.assertFalse(customers & set(per_account))
        dl = self.conn.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0]
        self.assertGreaterEqual(dl, 6)


class ScheduledSends(unittest.TestCase):
    def test_deferred_email_is_redecided_and_cancelled_by_a_later_unsubscribe(self):
        g = next(s for s in GOLDEN if s["id"] == "G090")                   # Thursday night: deferred to Friday 09:00
        orch, (r,) = run(g, approver=HARNESS)
        self.assertEqual(r.email["status"], "scheduled")
        self.assertEqual(orch.mocks.ledger["send"], {})
        unsub = dict(g["events"][0], delivery_id="dlv_g090_u", event_id="evt_g090_u", idempotency_key="key_g090_u",
                     type="unsubscribe_received", contact_id=r.best_contact_id, occurred_at="2026-10-02T04:00:00Z",
                     received_at="2026-10-02T04:00:01Z", payload={"method": "link_click", "thread_id": None})
        orch.process(unsub)
        orch.run_due(parse("2026-10-02T15:00:00Z"))
        self.assertEqual(orch.mocks.ledger["send"], {})

    def test_deferred_email_goes_out_once_when_the_window_opens(self):
        g = next(s for s in GOLDEN if s["id"] == "G090")
        orch, (r,) = run(g, approver=HARNESS)
        self.assertEqual(orch.run_due(parse("2026-10-02T14:59:00Z")), [])
        out = orch.run_due(parse("2026-10-02T15:00:00Z"))
        self.assertEqual([o["status"] for o in out], ["ok"])
        self.assertEqual(orch.run_due(parse("2026-10-02T16:00:00Z")), [])
        self.assertEqual(len(orch.mocks.ledger["send"]), 1)


class AIDegradesSafely(unittest.TestCase):
    def test_without_a_model_replies_escalate_and_opt_outs_still_win(self):
        for g in GOLDEN:
            if not any(e.get("type") == "reply_received" for e in g["events"]):
                continue
            orch, results = run(g, llm=UnavailableLLM(), approver=HARNESS)
            for e, r in zip(g["events"], results):
                if e.get("type") != "reply_received":
                    continue
                with self.subTest(g["id"]):
                    self.assertIn(r.action, ("escalate_human", "suppress"))
                    if r.action == "suppress":
                        self.assertIn("OPT_OUT_GUARD", r.reason_codes)

    def test_without_a_model_outreach_uses_the_generic_template(self):
        g = next(s for s in GOLDEN if s["id"] == "G080")
        orch, (r,) = run(g, llm=UnavailableLLM(), approver=HARNESS)
        self.assertEqual(r.email["mode"], "generic")
        self.assertNotIn("I saw that", r.email["body"])


class NoRealEmail(unittest.TestCase):
    """Outreach is mock-only by construction: no module can open an SMTP connection or call an email API, and the
    only outbound HTTP client in the package is the LLM client."""

    def test_no_email_capable_imports(self):
        banned = {"smtplib", "email", "imaplib", "poplib", "requests", "httpx", "aiohttp", "http.client", "socket"}
        for p in (ROOT / "orchestrator").rglob("*.py"):
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                names = [a.name for a in n.names] if isinstance(n, ast.Import) else \
                        [n.module or ""] if isinstance(n, ast.ImportFrom) else []
                for name in names:
                    self.assertFalse(name in banned or name.split(".")[0] in {"smtplib", "imaplib", "poplib"},
                                     f"{p.name} imports {name}")
                    if name.startswith("urllib"):
                        self.assertEqual(p.name, "llm.py", f"{p.name} must not make network calls")

    def test_llm_client_only_talks_to_the_model_api(self):
        src = (ROOT / "orchestrator" / "ai" / "llm.py").read_text(encoding="utf-8")
        self.assertIn("api.anthropic.com", src)
        self.assertNotRegex(src, r"(?i)smtp|sendgrid|mailgun|ses\.amazonaws|postmark|/send")


if __name__ == "__main__":
    unittest.main()
