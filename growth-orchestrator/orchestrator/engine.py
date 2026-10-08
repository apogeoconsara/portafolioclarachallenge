"""The orchestrator: event -> state -> decision -> AI/rules -> action -> audit.

One webhook delivery goes through `Orchestrator.process(env)`:
  1. intake (ingest.check): duplicates, garbage and stale updates never reach the business logic;
  2. the event updates state (suppression, opportunities, replies, meetings);
  3. deterministic rules decide (rules.decide), or, for replies, the LLM interprets and rules decide the action;
  4. the executor performs the action against the MOCK systems with idempotency keys, retries and reconciliation. An outreach
     email (first or follow-up) is only DRAFTED here: it is held as `pending_approval` and goes out only after a person calls `approve()`;
  5. every step is written to the audit log, and the outcome is returned as an EventResult.

Outreach is mock-only: the executor's only email client is MockSystems.send_email (an in-memory ledger), and the
executor refuses to call it for an email nobody approved: Draft -> Pending approval -> Approved -> Mock send.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from . import ingest, rules, state
from .ai import draft as ai_draft
from . import scoring
from .ai import reply as ai_reply
from .ai.llm import UnavailableLLM
from .audit import Audit
from .db import one, rows
from .executor import Exec, Executor
from .mocks import MockSystems
from .policy import Policy
from .retry import Clock
from .routing import route_ae
from .timeutil import iso, parse
from .windows import next_day_window, next_send_time

SENDERS = ["Valeria Montes", "Andres Quiroga", "Lucia Barrientos", "Mateo Salinas"]

# When several things happened while handling one event, the most informative handling tag wins.
HANDLING_PRECEDENCE = ["dead_letter_and_alert", "poll_then_escalate", "reread_and_reevaluate", "reconcile_before_retry",
                       "retry_then_process", "process_and_reconcile", "defer_to_send_window", "process"]
TAG_HANDLING = {"exhausted": "dead_letter_and_alert", "polled": "poll_then_escalate", "reread": "reread_and_reevaluate",
                "reconciled": "reconcile_before_retry", "retried": "retry_then_process", "late": "process_and_reconcile",
                "deferred": "defer_to_send_window"}


@dataclass
class EventResult:
    delivery_id: str | None
    event_id: str | None
    type: str | None
    account_id: str | None
    handling: str
    action: str | None = None                 # the decision taken on this event
    reason_codes: list = field(default_factory=list)
    best_contact_id: str | None = None
    wait_until: str | None = None
    route_to_ae_id: str | None = None
    route_reason: str | None = None
    final_action: str | None = None           # the outcome after executing (differs when execution changed state)
    final_reason_codes: list = field(default_factory=list)
    send_after: str | None = None
    scope: str | None = None
    contact_id: str | None = None
    cancel_pending_outreach: bool = False
    extracted: dict | None = None
    needs_human_review: bool = False
    usable_fact_ids: list = field(default_factory=list)
    ai: dict | None = None                    # reply interpretation / draft summary (verdict, label, codes, mode)
    email: dict | None = None                 # the mock email (subject/body/mode/status) if one was produced
    effects: list = field(default_factory=list)
    intake_reason: str = ""

    @property
    def automation_allowed(self) -> bool:
        return self.action is not None and self.action != "escalate_human"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["automation_allowed"] = self.automation_allowed
        return d


@dataclass
class AutoApprover:
    """Approves every held outreach email on the spot, under a named identity.

    ONLY for harnesses that test what happens after approval (retries, send windows, duplicates, races). The engine's
    default is no approver at all: an outreach email waits until a person calls `Orchestrator.approve()`."""
    reviewer: str


class Orchestrator:
    def __init__(self, conn, policy: Policy | None = None, llm=None, mocks: MockSystems | None = None, templates=None,
                 as_of=None, score_version=None, approver: AutoApprover | None = None):
        self.approver = approver
        self.conn = conn
        self.as_of = as_of            # replay mode: evaluate every event as of this instant instead of its received_at
        self.policy = policy or Policy.load()
        self.llm = llm or UnavailableLLM()
        self.mocks = mocks or MockSystems(conn)
        self.templates = templates or ai_draft.load_templates()
        self.score_cfg = scoring.load_config(score_version)
        self.audit = Audit(conn)
        self.content_rules = self.policy.send["content_rules"]

    # ================================================================================================================
    def process(self, env) -> EventResult:
        e = env if isinstance(env, dict) else {}
        now = parse(e["received_at"]) if isinstance(e.get("received_at"), str) and _valid(e["received_at"]) else None
        self.clock = Clock(self.as_of or now or parse("2026-10-01T16:00:00Z"))
        self.audit.clock = self.clock
        self.ex = Executor(self.conn, self.mocks, self.policy, self.clock, self.audit)
        intake = ingest.check(self.conn, env)
        r = EventResult(e.get("delivery_id"), e.get("event_id"), e.get("type"), e.get("account_id"), intake.handling,
                        intake_reason=intake.reason)
        ctx = {"account_id": e.get("account_id"), "event_id": e.get("event_id"), "delivery_id": e.get("delivery_id")}
        self.audit.log("event_received", **ctx, type=e.get("type"), source=e.get("source"), handling=intake.handling,
                       reason=intake.reason)
        if intake.handling != "process":
            if intake.handling == "dead_letter":
                self.conn.execute("INSERT INTO dead_letters (delivery_id, reason, raw, created_at) VALUES (?,?,?,?)",
                                  (e.get("delivery_id"), intake.reason, json.dumps(env, default=str)[:50_000],
                                   iso(self.clock.now)))
            ingest.record(self.conn, env, intake, intake.handling)
            self.conn.commit()
            return r
        tags: list = []
        try:
            handler = getattr(self, "_on_" + e["type"])
            handler(e, r, ctx, tags)
        except Exception as exc:                                      # fail closed: never half-act silently
            self.conn.rollback()
            self.conn.execute("INSERT INTO dead_letters (delivery_id, reason, raw, created_at) VALUES (?,?,?,?)",
                              (e.get("delivery_id"), f"internal_error:{type(exc).__name__}", json.dumps(env, default=str),
                               iso(self.clock.now)))
            self.audit.log("internal_error", **ctx, error=repr(exc))
            self._review(e["account_id"], e["event_id"], ["INTERNAL_ERROR"], {"error": repr(exc)})
            r.handling, r.action, r.reason_codes = "dead_letter_and_alert", "escalate_human", ["INTERNAL_ERROR"]
            ingest.record(self.conn, env, intake, "failed")
            self.conn.commit()
            return r
        handled = {TAG_HANDLING[t] for t in tags if t in TAG_HANDLING} | {"process"}
        r.handling = min(handled, key=HANDLING_PRECEDENCE.index)
        if r.final_action is None:
            r.final_action, r.final_reason_codes = r.action, list(r.reason_codes)
        ingest.record(self.conn, env, intake, "processed")
        self.audit.log("event_done", **ctx, handling=r.handling, action=r.action, final_action=r.final_action,
                       reason_codes=r.reason_codes, final_reason_codes=r.final_reason_codes)
        self.conn.commit()
        return r

    # ---- helpers -----------------------------------------------------------------------------------------------------
    def _account(self, aid):
        return one(self.conn, "SELECT * FROM accounts WHERE account_id=?", (aid,))

    def _contact(self, cid):
        return one(self.conn, "SELECT * FROM contacts WHERE contact_id=?", (cid,)) if cid else None

    def _is_late(self, e) -> bool:
        """A fact that happened BEFORE something we already acted on for this account, but reached us after it:
        those earlier decisions were taken without it and must be reconciled."""
        return one(self.conn, "SELECT 1 AS x FROM inbox WHERE account_id=? AND status='processed' AND occurred_at > ?",
                   (e["account_id"], e["occurred_at"])) is not None

    def _record_decision(self, e, action, codes, decided_by="rules", **kw) -> int:
        cur = self.conn.execute(
            "INSERT INTO decisions (account_id, event_id, delivery_id, trigger_type, action, reason_codes, best_contact_id, "
            "wait_until, route_to_ae_id, route_reason, automation_allowed, state_version, decided_by, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (e["account_id"], e["event_id"], e["delivery_id"], e["type"], action, json.dumps(codes), kw.get("best_contact_id"),
             kw.get("wait_until"), kw.get("route_to_ae_id"), kw.get("route_reason"), int(action != "escalate_human"),
             state.version(self.conn, e["account_id"]), decided_by, iso(self.clock.now)))
        self.audit.log("decision", e["account_id"], e["event_id"], e["delivery_id"], action=action, reason_codes=codes,
                       decided_by=decided_by, **{k: v for k, v in kw.items() if v is not None})
        return cur.lastrowid

    def _review(self, aid, event_id, codes, payload):
        self.conn.execute("INSERT INTO review_queue (account_id, event_id, reason_codes, payload, created_at) VALUES (?,?,?,?,?)",
                          (aid, event_id, json.dumps(codes), json.dumps(payload, default=str), iso(self.clock.now)))
        self.audit.log("human_review_queued", aid, event_id, None, reason_codes=codes)

    def _crm(self, e, kind, payload, ctx, suffix="") -> Exec:
        key = f"crm:{e['idempotency_key']}{suffix}"
        x = self.ex.crm_write(e["account_id"], kind, {"kind": kind, **payload}, key, ctx)
        return x

    def _log_ai(self, ctx, kind, attempts, verdict, codes, final_action):
        for a in attempts or []:
            self.conn.execute(
                "INSERT INTO ai_calls (kind, model, mode, input_hash, raw_output, verdict, violation_codes, final_action, "
                "latency_ms, input_tokens, output_tokens, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (kind, a.get("model"), a.get("mode"), None, a.get("raw"), a.get("verdict"), json.dumps(a.get("codes")),
                 final_action, a.get("latency_ms", 0), a.get("input_tokens", 0), a.get("output_tokens", 0),
                 iso(self.clock.now)))
        self.audit.log("ai_call", **ctx, task=kind, calls=len(attempts or []), verdict=verdict,
                       codes=codes, final_action=final_action)

    def _apply_decision(self, d: rules.Decision, r: EventResult):
        r.action, r.reason_codes = d.action, list(d.reason_codes)
        r.best_contact_id, r.wait_until = d.best_contact_id, d.wait_until
        r.route_to_ae_id, r.route_reason = d.route_to_ae_id, d.route_reason

    # ---- run a rules decision end to end ---------------------------------------------------------------------------
    def _decide_and_act(self, e, r, ctx, tags):
        aid, now = e["account_id"], self.clock.now
        d = rules.decide(self.conn, aid, now, self.policy)
        self._apply_decision(d, r)
        did = self._record_decision(e, d.action, d.reason_codes, best_contact_id=d.best_contact_id,
                                    wait_until=d.wait_until, route_to_ae_id=d.route_to_ae_id, route_reason=d.route_reason)
        # the CRM records the decision (AE handoff = a task); on a stale-version conflict, re-read and re-decide
        x = self._crm_for_decision(e, d, ctx)
        if x.status == "conflict":
            tags.append("reread")
            self.audit.log("crm_conflict_reread", **ctx)
            d = rules.decide(self.conn, aid, self.clock.now, self.policy)
            self._apply_decision(d, r)
            x = self._crm_for_decision(e, d, ctx)
        tags += x.tags
        if x.status not in ("ok",):
            return self._exhausted(e, r, ctx, tags, "CRM_WRITE_FAILED")
        r.effects.append({"system": "crm", "kind": "ae_handoff_task" if d.action == "handoff_ae" else "decision_note",
                          "status": x.status, "attempts": x.attempts})
        if d.action == "escalate_human":
            self._review(aid, e["event_id"], d.reason_codes, {"decision": d.action})
        elif d.action == "contact":
            sc = self._score(aid)
            self.audit.log("score", **ctx, score=sc["score"], tier=sc["tier"], parts=sc["parts"], version=self.score_cfg["version"])
            if self.score_cfg["gate_enabled"] and sc["tier"] == "C":
                self._nurture(e, r, ctx, tags, d)
            else:
                self._contact_flow(e, r, ctx, tags, did, d.best_contact_id)
        elif d.action == "enrich":
            self._enrich_flow(e, r, ctx, tags, did)

    def _score(self, aid) -> dict:
        facts = rows(self.conn, "SELECT * FROM company_facts WHERE account_id=?", (aid,))
        return scoring.score(scoring.features(self._account(aid), facts, self.score_cfg), self.score_cfg)

    def _nurture(self, e, r, ctx, tags, d):
        """Eligible but low priority: no outreach email and no model call. Only records the enrolment (nothing is sent)."""
        tags.append("nurture")
        r.effects.append({"system": "nurture", "kind": "enrolled", "status": "ok", "attempts": 1})
        self.audit.log("nurture_enrolled", **ctx, version=self.score_cfg["version"])
        self._final(e, r, "nurture", ["LOW_PRIORITY"], best_contact_id=d.best_contact_id)

    def _crm_for_decision(self, e, d, ctx) -> Exec:
        if d.action == "handoff_ae":
            return self._crm(e, "ae_handoff_task", {"ae_id": d.route_to_ae_id, "route_reason": d.route_reason,
                                                    "reason_codes": d.reason_codes}, ctx)
        return self._crm(e, "decision_note", {"action": d.action, "reason_codes": d.reason_codes}, ctx)

    def _exhausted(self, e, r, ctx, tags, code):
        tags.append("exhausted")
        self.conn.execute("INSERT INTO dead_letters (delivery_id, reason, raw, created_at) VALUES (?,?,?,?)",
                          (e["delivery_id"], code, json.dumps(e, default=str), iso(self.clock.now)))
        self.audit.log("alert", **ctx, code=code)
        self._review(e["account_id"], e["event_id"], [code], {})
        self._final(e, r, "escalate_human", [code])

    def _final(self, e, r, action, codes, **kw):
        r.final_action, r.final_reason_codes = action, list(codes)
        self._record_decision(e, action, codes, decided_by="rules:after_execution", **kw)

    # ---- outreach ------------------------------------------------------------------------------------------------
    def _contact_flow(self, e, r, ctx, tags, decision_id, contact_id):
        aid, now = e["account_id"], self.clock.now
        account, contact = self._account(aid), self._contact(contact_id)
        step = 1 + len(rows(self.conn, "SELECT 1 FROM outreach_history WHERE account_id=? AND sender_type='sequence'", (aid,)))
        facts = rows(self.conn, "SELECT * FROM company_facts WHERE account_id=?", (aid,))
        sender = SENDERS[int(hashlib.sha256(aid.encode()).hexdigest(), 16) % len(SENDERS)]
        dr = ai_draft.compose(self.llm, account, contact, facts, min(step, 4), sender, now, self.content_rules, self.templates)
        r.usable_fact_ids = dr.usable_fact_ids
        r.ai = {"task": "draft", "mode": dr.mode, "verdict": dr.verdict, "codes": dr.codes, "claims": dr.claims,
                "used_ai": dr.used_ai}
        if dr.attempts:
            self._log_ai(ctx, "draft", dr.attempts, dr.verdict, dr.codes, dr.mode)
        self.audit.log("draft", **ctx, mode=dr.mode, verdict=dr.verdict, codes=dr.codes, usable_fact_ids=dr.usable_fact_ids)
        when = self._send_time(account, now, ctx)
        r.send_after = iso(when) if when > now else iso(now)
        r.email = {"to": contact["email"], "subject": dr.subject, "body": dr.body, "mode": dr.mode, "step": step,
                   "status": "pending_approval"}
        # Draft -> Pending approval. Nothing below this line can send unless a person (or a test harness) approves.
        key, created = self.ex.hold_for_approval(aid, contact_id, step, decision_id, dr, r.send_after,
                                                 origin={"event_id": e["event_id"], "delivery_id": e["delivery_id"]})
        r.effects.append({"system": "approval", "kind": "held", "key": key, "status": "pending_approval" if created else "already_decided"})
        self.audit.log("email_pending_approval", **ctx, key=key, send_after=r.send_after, created=created)
        if not created or self.approver is None:
            if not created:
                r.email["status"] = "already_decided"
            return
        self.ex.review(key, "approved", self.approver.reviewer, "auto-approved by a test harness")
        self.audit.log("email_approved", **ctx, key=key, reviewer=self.approver.reviewer)
        r.effects.append({"system": "approval", "kind": "approved", "reviewer": self.approver.reviewer})
        kind, x = self._release(key, account, contact, step, dr, decision_id, now, ctx, when)
        if kind == "scheduled":
            tags.append("deferred")
            r.email["status"] = "scheduled"
            r.effects.append({"system": "send", "kind": "email_scheduled", "key": key, "send_after": iso(when)})
            return
        tags += x.tags
        r.email["status"] = {"ok": "sent_mock"}.get(x.status, x.status)
        r.effects.append({"system": "send", "kind": "email", "status": x.status, "attempts": x.attempts})
        if x.status == "hard_reject":                 # the address is dead: mark it and decide again on fresh state
            d = rules.decide(self.conn, aid, self.clock.now, self.policy)
            self._final(e, r, d.action, d.reason_codes, best_contact_id=d.best_contact_id)
            if d.action == "enrich":
                self._queue_enrichment(e, ctx, "after_hard_reject")
        elif x.status != "ok":
            self._exhausted(e, r, ctx, tags, "SEND_FAILED")

    def _send_time(self, account, now, ctx):
        """When an approved email may go out: the country's send window, pushed to the next day if the daily cap is spent."""
        when = next_send_time(now, account["country"], self.policy)
        sent_today = self._counter(f"sent:{now.date().isoformat()}")
        if sent_today >= self.policy.daily_cap:
            when = next_day_window(now, account["country"], self.policy)
            self.audit.log("daily_cap_reached", **ctx, sent_today=sent_today, cap=self.policy.daily_cap)
        return when

    def _release(self, key, account, contact, step, dr, decision_id, now, ctx, when=None):
        """Approved -> waits for its window (`scheduled`) or goes to the mock send now. The only caller of the executor's send."""
        when = when or self._send_time(account, now, ctx)
        if when > now:
            self.ex.mark_scheduled(key, when)
            self.audit.log("email_scheduled", **ctx, key=key, send_after=iso(when))
            return "scheduled", None
        x = self.ex.send_email(account, contact, step, decision_id, dr, ctx)
        self.audit.log("email_" + x.status, **ctx, attempts=x.attempts, tags=x.tags)
        return "sent", x

    # ---- human approval ------------------------------------------------------------------------------------------------
    def _begin(self, now):
        self.clock = Clock(now)
        self.audit.clock = self.clock
        self.ex = Executor(self.conn, self.mocks, self.policy, self.clock, self.audit)

    def pending_approvals(self, limit: int | None = None) -> list[dict]:
        """Outreach emails (first or follow-up) waiting for a person: the draft, who it is for and when it could go out."""
        q = "SELECT * FROM actions WHERE kind='email' AND status='pending_approval' ORDER BY action_id"
        out = []
        for a in rows(self.conn, q + (f" LIMIT {int(limit)}" if limit else "")):
            req = json.loads(a["request"])
            out.append({"key": a["idempotency_key"], "account_id": a["account_id"], "contact_id": a["contact_id"],
                        "step": req["step"], "subject": req["subject"], "body": req["body"], "mode": req["mode"],
                        "claims": req.get("claims", []),
                        "send_after": a["send_after"], "created_at": a["created_at"]})
        return out

    def approve(self, key, reviewer, now=None, note="") -> dict:
        """Pending approval -> Approved -> Mock send (or `scheduled` until the send window opens). The reviewer is recorded.
        Eligibility is decided again now: a suppression, reply or open deal that arrived while the draft waited blocks it."""
        return self._review_held(key, "approved", reviewer, now, note)

    def reject(self, key, reviewer, reason="", now=None) -> dict:
        return self._review_held(key, "rejected", reviewer, now, reason)

    def _review_held(self, key, verdict, reviewer, now, note) -> dict:
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError("a reviewer identity is required to approve or reject")
        self._begin(now or self.as_of or datetime.now(timezone.utc))
        now = self.clock.now
        a = one(self.conn, "SELECT * FROM actions WHERE idempotency_key=? AND kind='email' AND system='send'", (key,))
        if a is None:
            return {"key": key, "status": "not_found"}
        if a["status"] != "pending_approval":
            return {"key": key, "status": "not_pending", "current": a["status"]}
        req = json.loads(a["request"])
        origin = req.get("origin") or {}
        ctx = {"account_id": a["account_id"], "event_id": origin.get("event_id"), "delivery_id": origin.get("delivery_id")}
        if verdict == "rejected":
            self.ex.review(key, "rejected", reviewer, note)
            self.audit.log("email_rejected", **ctx, key=key, reviewer=reviewer, reason=note)
            self.conn.commit()
            return {"key": key, "status": "rejected", "reviewer": reviewer}
        d = rules.decide(self.conn, a["account_id"], now, self.policy)
        if d.action != "contact" or d.best_contact_id != a["contact_id"]:
            self.conn.execute("UPDATE actions SET status='cancelled', review_note=?, updated_at=? WHERE idempotency_key=?",
                              ("not eligible at approval: " + ",".join(d.reason_codes), iso(now), key))
            self.audit.log("email_approval_blocked", **ctx, key=key, reviewer=reviewer, now_decision=d.action,
                           reason_codes=d.reason_codes)
            self.conn.commit()
            return {"key": key, "status": "cancelled", "now_decision": d.action, "reason_codes": d.reason_codes}
        self.ex.review(key, "approved", reviewer, note)
        self.audit.log("email_approved", **ctx, key=key, reviewer=reviewer)
        account, contact = self._account(a["account_id"]), self._contact(a["contact_id"])
        dr = ai_draft.DraftResult(req["subject"], req["body"], req["mode"], "approved")
        kind, x = self._release(key, account, contact, req["step"], dr, a["decision_id"], now, ctx)
        if kind == "scheduled":
            out = {"key": key, "status": "scheduled", "reviewer": reviewer,
                   "send_after": one(self.conn, "SELECT send_after FROM actions WHERE idempotency_key=?", (key,))["send_after"]}
        else:
            out = {"key": key, "status": {"ok": "sent_mock"}.get(x.status, x.status), "reviewer": reviewer,
                   "attempts": x.attempts, "tags": x.tags}
            if x.status == "hard_reject":
                self._review(a["account_id"], ctx["event_id"], ["EMAIL_ADDRESS_REJECTED"], {"key": key})
            elif x.status != "ok":
                self.audit.log("alert", **ctx, code="SEND_FAILED")
                self._review(a["account_id"], ctx["event_id"], ["SEND_FAILED"], {"key": key})
        self.conn.commit()
        return out

    def _counter(self, name) -> int:
        v = self.conn.execute("SELECT value FROM counters WHERE name=?", (name,)).fetchone()
        return v[0] if v else 0

    def run_due(self, now) -> list[dict]:
        """Dispatch scheduled emails whose send_after has arrived. Each one is RE-DECIDED first: a suppression,
        reply or opportunity that arrived in the meantime cancels it."""
        self.clock = Clock(now)
        self.audit.clock = self.clock
        self.ex = Executor(self.conn, self.mocks, self.policy, self.clock, self.audit)
        out = []
        # only emails a person approved can be scheduled; the reviewer filter makes that explicit here as well
        for a in rows(self.conn, "SELECT * FROM actions WHERE status='scheduled' AND kind='email' AND reviewer IS NOT NULL "
                                 "AND send_after<=? ORDER BY send_after, action_id", (iso(now),)):
            ctx = {"account_id": a["account_id"], "event_id": None, "delivery_id": None}
            d = rules.decide(self.conn, a["account_id"], now, self.policy)
            if d.action != "contact" or d.best_contact_id != a["contact_id"]:
                self.conn.execute("UPDATE actions SET status='cancelled', updated_at=? WHERE action_id=?",
                                  (iso(now), a["action_id"]))
                self.audit.log("scheduled_email_cancelled", **ctx, key=a["idempotency_key"], now_decision=d.action,
                               reason_codes=d.reason_codes)
                out.append({"key": a["idempotency_key"], "status": "cancelled", "reason_codes": d.reason_codes})
                continue
            if self._counter(f"sent:{now.date().isoformat()}") >= self.policy.daily_cap:
                out.append({"key": a["idempotency_key"], "status": "still_deferred"})
                continue
            req = json.loads(a["request"])
            dr = ai_draft.DraftResult(req["subject"], req["body"], req["mode"], "scheduled")
            x = self.ex.send_email(self._account(a["account_id"]), self._contact(a["contact_id"]), req["step"],
                                   a["decision_id"], dr, ctx)
            out.append({"key": a["idempotency_key"], "status": x.status, "tags": x.tags})
        self.conn.commit()
        return out

    # ---- enrichment ------------------------------------------------------------------------------------------------
    def _queue_enrichment(self, e, ctx, why):
        key = f"enrich_queued:{e['account_id']}:{e['idempotency_key']}"
        self.ex._action(key, e["account_id"], None, None, "enrichment", "enrichment", "queued", {"why": why})
        self.audit.log("enrichment_queued", **ctx, why=why)

    def _enrich_flow(self, e, r, ctx, tags, decision_id):
        aid = e["account_id"]
        x = self.ex.enrich(self._account(aid), ctx)
        tags += x.tags
        r.effects.append({"system": "enrichment", "status": x.status, "attempts": x.attempts})
        self.audit.log("enrichment_" + x.status, **ctx, attempts=x.attempts, **x.detail)
        if x.status == "failed":
            return self._exhausted(e, r, ctx, tags, "ENRICHMENT_EXHAUSTED")
        if x.status == "pending":
            self._review(aid, e["event_id"], ["ENRICHMENT_UNCERTAIN"], x.detail)
            return self._final(e, r, "escalate_human", ["ENRICHMENT_UNCERTAIN"])
        if x.status == "contradictory":
            self._review(aid, e["event_id"], ["ENRICHMENT_CONTRADICTORY"], x.detail)
            return self._final(e, r, "escalate_human", ["ENRICHMENT_CONTRADICTORY"])
        d = rules.decide(self.conn, aid, self.clock.now, self.policy)       # state changed: decide again
        self._final(e, r, d.action, d.reason_codes, best_contact_id=d.best_contact_id, wait_until=d.wait_until,
                    route_to_ae_id=d.route_to_ae_id, route_reason=d.route_reason)
        if d.action == "contact":
            # `best_contact_id` stays what it was for the decision taken on this event (none for an enrich decision, as the
            # goldens declare); the contact found by enrichment is recorded in the final decision and in the email's recipient.
            self._contact_flow(e, r, ctx, tags, decision_id, d.best_contact_id)
        elif d.action == "escalate_human":
            self._review(aid, e["event_id"], d.reason_codes, {})
        elif d.action == "enrich":                    # still missing data: do not loop inside one event
            self._queue_enrichment(e, ctx, "still_incomplete")

    # ================================================================================================================
    # event handlers
    def _on_account_targeted(self, e, r, ctx, tags):
        self._decide_and_act(e, r, ctx, tags)

    def _late_then(self, e, r, ctx, tags, cancel_contact=None):
        if self._is_late(e):
            tags.append("late")
            n = self.ex.cancel_pending(e["account_id"], cancel_contact)
            r.cancel_pending_outreach = True
            sent = rows(self.conn, "SELECT idempotency_key FROM actions WHERE account_id=? AND kind='email' AND "
                                   "status='succeeded'", (e["account_id"],))
            self.audit.log("late_event_reconciled", **ctx, cancelled_pending=n, already_sent=[s["idempotency_key"] for s in sent])
            if sent:   # something already went out on stale knowledge: a human must know
                self._review(e["account_id"], e["event_id"], ["SENT_BEFORE_LATE_FACT"], {"sent": [s["idempotency_key"] for s in sent]})

    def _on_unsubscribe_received(self, e, r, ctx, tags):
        cid = e.get("contact_id")
        state.add_suppression(self.conn, e["account_id"], "unsubscribe", "contact", cid, "unsubscribe_event", self.clock.now)
        r.cancel_pending_outreach = self.ex.cancel_pending(e["account_id"], cid) > 0 or r.cancel_pending_outreach
        self._late_then(e, r, ctx, tags, cid)
        r.action, r.reason_codes, r.scope, r.contact_id = "suppress", ["UNSUBSCRIBED"], "contact", cid
        self._record_decision(e, "suppress", r.reason_codes)
        tags += self._crm(e, "decision_note", {"action": "suppress", "reason_codes": r.reason_codes, "contact_id": cid}, ctx).tags

    def _on_email_bounced(self, e, r, ctx, tags):
        cid, bt = e.get("contact_id"), e["payload"].get("bounce_type")
        r.scope, r.contact_id = "contact", cid
        if bt == "hard":
            state.add_suppression(self.conn, e["account_id"], "hard_bounce", "contact", cid, "bounce_event", self.clock.now)
            self.ex.cancel_pending(e["account_id"], cid)
            r.action, r.reason_codes = "suppress", ["HARD_BOUNCE"]
        else:
            r.action, r.reason_codes = "wait", ["SOFT_BOUNCE"]
            r.wait_until = iso(self.clock.now + timedelta(days=2))
        self._record_decision(e, r.action, r.reason_codes, wait_until=r.wait_until)
        tags += self._crm(e, "decision_note", {"action": r.action, "reason_codes": r.reason_codes, "contact_id": cid}, ctx).tags

    def _on_opportunity_created(self, e, r, ctx, tags):
        p = e["payload"]
        state.upsert_opportunity(self.conn, e["account_id"], p["opportunity_id"], p.get("stage", "discovery"),
                                 p.get("owner_ae_id"), created_at=e["occurred_at"])
        self.conn.execute("INSERT OR REPLACE INTO opp_versions (opportunity_id, last_event_at) VALUES (?,?)",
                          (p["opportunity_id"], e["occurred_at"]))
        self._late_then(e, r, ctx, tags)
        self.ex.cancel_pending(e["account_id"])
        self._decide_and_act(e, r, ctx, tags)
        if "late" in tags:
            r.cancel_pending_outreach = True

    def _on_opportunity_stage_changed(self, e, r, ctx, tags):
        p = e["payload"]
        closed = e["occurred_at"] if p["to_stage"] in ("closed_won", "closed_lost") else None
        state.upsert_opportunity(self.conn, e["account_id"], p["opportunity_id"], p["to_stage"], p.get("owner_ae_id"),
                                 closed_at=closed, created_at=e["occurred_at"])
        self.conn.execute("INSERT OR REPLACE INTO opp_versions (opportunity_id, last_event_at) VALUES (?,?)",
                          (p["opportunity_id"], e["occurred_at"]))
        r.action, r.reason_codes = "update_state", ["OPP_STAGE_CHANGED"]
        self._record_decision(e, r.action, r.reason_codes)

    def _on_meeting_booked(self, e, r, ctx, tags):
        aid, p = e["account_id"], e["payload"]
        account, contact = self._account(aid), self._contact(e.get("contact_id"))
        self.ex.cancel_pending(aid)                       # a meeting stops the sequence
        ae, why = route_ae(account, contact["language"] if contact else None, rows(self.conn, "SELECT * FROM aes"),
                           self.clock.now)
        x = self.ex.book_meeting(aid, {"calendar_event_id": p.get("calendar_event_id"), "start_at": p.get("start_at"),
                                       "ae_id": ae}, f"cal:{p.get('calendar_event_id')}", ctx)
        tags += x.tags
        r.effects.append({"system": "calendar", "status": x.status, "attempts": x.attempts})
        if x.status == "conflict":
            r.action, r.reason_codes = "escalate_human", ["CALENDAR_CONFLICT"]
            self._record_decision(e, r.action, r.reason_codes)
            return self._review(aid, e["event_id"], r.reason_codes, {"calendar_event_id": p.get("calendar_event_id")})
        if x.status != "ok":
            r.action, r.reason_codes = "handoff_ae", ["MEETING_BOOKED"]
            self._record_decision(e, r.action, r.reason_codes)
            return self._exhausted(e, r, ctx, tags, "CALENDAR_FAILED")
        r.action, r.reason_codes, r.route_to_ae_id, r.route_reason = "handoff_ae", ["MEETING_BOOKED"], ae, why
        self._record_decision(e, r.action, r.reason_codes, route_to_ae_id=ae, route_reason=why)
        c = self._crm(e, "ae_handoff_task", {"ae_id": ae, "route_reason": why, "reason_codes": r.reason_codes}, ctx)
        tags += c.tags

    def _on_reply_received(self, e, r, ctx, tags):
        aid, cid = e["account_id"], e.get("contact_id")
        account = self._account(aid)
        text = e["payload"].get("body_text", "")
        last = one(self.conn, "SELECT subject FROM outreach_history WHERE account_id=? ORDER BY sent_at DESC LIMIT 1", (aid,))
        opps = rows(self.conn, "SELECT stage FROM opportunities WHERE account_id=?", (aid,))
        crm_state = (account["crm_status"] if account["crm_status"] in ("customer", "churned_customer") else
                     "active_opportunity" if any(o["stage"] in self.policy.open_stages for o in opps) else
                     "ae_assigned" if account["crm_owner_ae_id"] else "prospect")
        res = ai_reply.interpret_reply(self.llm, text, self.clock.now,
                                       {"company": account["name"], "crm_state": crm_state,
                                        "last_touch_subject": last["subject"] if last else ""},
                                       self.policy.ai_confidence_min_auto)
        self._log_ai(ctx, "reply", res.attempts, res.verdict, res.violation_codes, res.action)
        self.conn.execute("UPDATE outreach_history SET status='replied' WHERE account_id=? AND contact_id=?", (aid, cid))
        state.bump(self.conn, aid)
        r.extracted, r.needs_human_review = res.extracted, res.needs_human_review
        r.ai = {"task": "reply", "label": res.label, "confidence": res.confidence, "verdict": res.verdict,
                "codes": res.violation_codes, "used_ai": res.used_ai, "crm_state": crm_state}
        r.action, r.reason_codes, r.contact_id = res.action, list(res.reason_codes), cid
        kw = {}
        if res.action == "suppress":                      # opt-out: contact-level suppression + stop everything pending
            state.add_suppression(self.conn, aid, "unsubscribe", "contact", cid, "reply", self.clock.now)
            r.cancel_pending_outreach = self.ex.cancel_pending(aid, cid) > 0
            r.scope = "contact"
        elif res.action == "handoff_ae":
            contact = self._contact(cid)
            ae, why = route_ae(account, contact["language"] if contact else None, rows(self.conn, "SELECT * FROM aes"),
                               self.clock.now)
            if ae is None:
                r.action, r.reason_codes = "escalate_human", r.reason_codes + ["NO_AE_AVAILABLE"]
            r.route_to_ae_id, r.route_reason = ae, why
            kw = {"route_to_ae_id": ae, "route_reason": why}
            self.ex.cancel_pending(aid)
        elif res.action == "wait":
            fu = res.extracted.get("follow_up_date")
            r.wait_until = f"{fu}T15:00:00Z" if fu else iso(self.clock.now + timedelta(days=7))
            kw = {"wait_until": r.wait_until}
            self.ex.cancel_pending(aid, cid)
        elif res.action == "enrich":
            ref = res.extracted.get("referred_contact")
            if ref and ref.get("email"):                  # the referral is unverified until enrichment confirms it
                n = self.conn.execute("SELECT COUNT(*) FROM contacts WHERE account_id=?", (aid,)).fetchone()[0] + 1
                first, _, last_n = (ref.get("name") or "").partition(" ")
                self.conn.execute("INSERT OR IGNORE INTO contacts (contact_id, account_id, first_name, last_name, email, "
                                  "email_status, title, function, seniority, language, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                  (f"con_r_{aid}_{n}", aid, first, last_n, ref["email"], "unverified", None, None, None,
                                   "en", iso(self.clock.now)))
                state.bump(self.conn, aid)
            self._queue_enrichment(e, ctx, "referral")
        self._record_decision(e, r.action, r.reason_codes, decided_by="ai+rules" if res.used_ai else "rules", **kw)
        if r.action == "escalate_human" or res.needs_human_review:
            self._review(aid, e["event_id"], r.reason_codes, {"label": res.label, "verdict": res.verdict,
                                                              "codes": res.violation_codes, "extracted": res.extracted})
        kind = "ae_handoff_task" if r.action == "handoff_ae" else "decision_note"
        c = self._crm(e, kind, {"action": r.action, "reason_codes": r.reason_codes, "extracted": res.extracted, **kw}, ctx)
        tags += c.tags
        r.effects.append({"system": "crm", "kind": kind, "status": c.status, "attempts": c.attempts})


def _valid(s) -> bool:
    try:
        parse(s)
        return True
    except (TypeError, ValueError):
        return False
