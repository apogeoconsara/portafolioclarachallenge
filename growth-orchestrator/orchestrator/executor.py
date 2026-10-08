"""External actions with idempotency keys, retries, reconciliation of uncertain outcomes, and fail-closed behaviour.

Outreach is MOCK-ONLY by construction: the only outreach client is MockSystems.send_email, which writes to an
in-memory ledger. There is no code path that sends a real email.

Outreach (first or follow-up) also needs a person. An email is first HELD (`pending_approval`); only a recorded review (`reviewer` set,
status `approved`) lets `send_email` reach the mock. Any other call raises ApprovalRequired before touching the provider.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import state
from .db import one, rows
from .mocks import MockSystems, Timeout
from .retry import Clock, Retrier
from .timeutil import iso


class ApprovalRequired(Exception):
    """An email was about to be sent without a recorded approval. Fail closed: nothing reaches the provider."""


@dataclass
class Exec:
    status: str                       # ok | deferred | failed | conflict | hard_reject | pending | contradictory | no_data
    tags: list = field(default_factory=list)   # retried | reconciled | reread | exhausted | polled
    attempts: int = 0
    detail: dict = field(default_factory=dict)


class Executor:
    def __init__(self, conn, mocks: MockSystems, policy, clock: Clock, audit):
        self.conn, self.mocks, self.policy, self.clock, self.audit = conn, mocks, policy, clock, audit
        self.retrier = Retrier(policy.retry, clock)

    # ---- bookkeeping ---------------------------------------------------------------------------------------------
    def _action(self, key, account_id, contact_id, decision_id, kind, system, status, request=None, send_after=None):
        now = iso(self.clock.now)
        self.conn.execute("INSERT INTO actions (idempotency_key, account_id, contact_id, decision_id, kind, system, status, "
                          "request, send_after, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                          "ON CONFLICT(idempotency_key) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at",
                          (key, account_id, contact_id, decision_id, kind, system, status, json.dumps(request or {}),
                           send_after, now, now))

    def _finish(self, key, status, attempts, response=None):
        self.conn.execute("UPDATE actions SET status=?, attempts=attempts+?, response=?, updated_at=? WHERE idempotency_key=?",
                          (status, attempts, json.dumps(response or {}, default=str), iso(self.clock.now), key))

    def _attempt_log(self, ctx, system, out):
        self.audit.log("external_call", ctx["account_id"], ctx.get("event_id"), ctx.get("delivery_id"), system=system,
                       outcome=out.status, attempts=out.attempts, waits=out.waits, reason=out.reason)

    def _write(self, system, fn, key, ctx) -> Exec:
        """Generic write: retry transient errors; on an uncertain outcome or a write timeout, ask the provider what
        happened (lookup by idempotency key) BEFORE trying again."""
        tags, attempts = [], 0
        try:
            first = fn()
            attempts = 1
        except Timeout:
            attempts = 1
            seen = self.mocks.lookup(system, key).body["applied"]
            self.audit.log("reconcile_lookup", ctx["account_id"], ctx.get("event_id"), ctx.get("delivery_id"), system=system,
                           trigger="timeout", applied=seen)
            if seen:
                return Exec("ok", ["reconciled"], attempts)
            tags.append("reconciled")
            first = None
        if first is not None:
            if first.status == 200 and first.body.get("status") == "unknown":
                seen = self.mocks.lookup(system, key).body["applied"]
                self.audit.log("reconcile_lookup", ctx["account_id"], ctx.get("event_id"), ctx.get("delivery_id"),
                               system=system, trigger="uncertain_outcome", applied=seen)
                if seen:
                    return Exec("ok", ["reconciled"], attempts)
                tags.append("reconciled")
            elif first.status == 200:
                return Exec("ok", [], attempts, {"response": first.body})
            elif first.status == 409:
                return Exec("conflict", [], attempts, {"response": first.body})
            elif first.status == 422 or (400 <= first.status < 500 and first.status != 429):
                return Exec("hard_reject", [], attempts, {"response": first.body})
            else:
                if first.status == 429:
                    self.clock.sleep(float(first.retry_after or 1))
                else:
                    self.clock.sleep(self.retrier._backoff(1, key))
                tags.append("retried")
        out = self.retrier.call(fn, key)
        self._attempt_log(ctx, system, out)
        attempts += out.attempts
        if out.status == "ok":
            return Exec("ok", tags + (["retried"] if attempts > 1 and "reconciled" not in tags else []), attempts)
        if out.status == "uncertain":
            seen = self.mocks.lookup(system, key).body["applied"]
            return Exec("ok" if seen else "failed", tags + ["reconciled"], attempts)
        if out.status == "rejected":
            return Exec("conflict" if out.result.status == 409 else "hard_reject", tags, attempts)
        return Exec("failed", tags + ["exhausted"], attempts, {"reason": out.reason})

    # ---- CRM -------------------------------------------------------------------------------------------------------
    def crm_write(self, account_id, kind, payload, key, ctx, decision_id=None) -> Exec:
        self._action(key, account_id, None, decision_id, kind, "crm", "in_progress", payload)
        r = self._write("crm", lambda: self.mocks.crm_write(account_id, key, payload), key, ctx)
        self._finish(key, {"ok": "succeeded"}.get(r.status, r.status), r.attempts)
        return r

    # ---- outreach (mock email only) ---------------------------------------------------------------------------------
    def hold_for_approval(self, account_id, contact_id, step, decision_id, draft, send_after, origin=None):
        """Park a drafted outreach email until a person approves it. Returns (key, created). An existing action under the same
        key (already held, approved, sent, rejected) is never overwritten: a replay cannot reopen a decided email."""
        key = f"send:{account_id}:{contact_id}:{step}"
        if one(self.conn, "SELECT 1 AS x FROM actions WHERE idempotency_key=?", (key,)):
            return key, False
        self._action(key, account_id, contact_id, decision_id, "email", "send", "pending_approval",
                     {"subject": draft.subject, "body": draft.body, "mode": draft.mode, "step": step,
                      "claims": getattr(draft, "claims", None) or [], "origin": origin or {}}, send_after)
        return key, True

    def review(self, key, status, reviewer, note=""):
        """Record a person's decision on a held email: approved | rejected. Only a held email can be reviewed."""
        cur = self.conn.execute("UPDATE actions SET status=?, reviewer=?, reviewed_at=?, review_note=?, updated_at=? "
                                "WHERE idempotency_key=? AND kind='email' AND status='pending_approval'",
                                (status, reviewer, iso(self.clock.now), note, iso(self.clock.now), key))
        return cur.rowcount == 1

    def mark_scheduled(self, key, send_after):
        self.conn.execute("UPDATE actions SET status='scheduled', send_after=?, updated_at=? "
                          "WHERE idempotency_key=? AND status='approved'", (iso(send_after), iso(self.clock.now), key))

    def send_email(self, account, contact, step, decision_id, draft, ctx) -> Exec:
        key = f"send:{account['account_id']}:{contact['contact_id']}:{step}"
        done = one(self.conn, "SELECT status, reviewer FROM actions WHERE idempotency_key=?", (key,))
        if done and done["status"] == "succeeded":
            return Exec("ok", ["already_sent"], 0)
        if not done or not done["reviewer"] or done["status"] not in ("approved", "scheduled"):
            self.audit.log("send_blocked_no_approval", ctx["account_id"], ctx.get("event_id"), ctx.get("delivery_id"), key=key,
                           status=done["status"] if done else "no_action")
            raise ApprovalRequired(key)
        payload = {"to": contact["email"], "subject": draft.subject, "body": draft.body, "mode": draft.mode}
        self._action(key, account["account_id"], contact["contact_id"], decision_id, "email", "send", "in_progress", payload)
        r = self._write("send", lambda: self.mocks.send_email(account["account_id"], key, payload), key, ctx)
        if r.status == "ok":
            self._finish(key, "succeeded", r.attempts)
            state.record_touch(self.conn, account["account_id"], contact["contact_id"], step, draft.subject, "sequence",
                               iso(self.clock.now))
            day = f"sent:{self.clock.now.date().isoformat()}"
            self.conn.execute("INSERT INTO counters (name, value) VALUES (?, 1) ON CONFLICT(name) DO UPDATE SET value=value+1",
                              (day,))
        elif r.status == "hard_reject":
            self._finish(key, "failed", r.attempts, {"error": "address_rejected"})
            state.mark_contact_invalid(self.conn, account["account_id"], contact["contact_id"])
        else:
            self._finish(key, "failed", r.attempts)
        return r

    def cancel_pending(self, account_id, contact_id=None) -> int:
        q = ("UPDATE actions SET status='cancelled', updated_at=? WHERE account_id=? AND kind='email' "
             "AND status IN ('pending_approval','approved','scheduled')")
        p = [iso(self.clock.now), account_id]
        if contact_id:
            q += " AND contact_id=?"; p.append(contact_id)
        return self.conn.execute(q, p).rowcount

    # ---- enrichment -------------------------------------------------------------------------------------------------
    def enrich(self, account, ctx) -> Exec:
        aid = account["account_id"]
        key = f"enrich:{aid}:{account['enrichment_attempts']}"
        valid = lambda res: isinstance(res.body, dict) and "firmographics" in res.body and "contacts" in res.body
        out = self.retrier.call(lambda: self.mocks.enrich(aid, key), key, is_valid=valid)
        self._attempt_log(ctx, "enrichment", out)
        tags = ["retried"] if out.attempts > 1 else []
        if out.status == "pending":
            job = out.result.body.get("job_id")
            for _ in range(3):                                   # poll within a small budget, never assume "no data"
                self.clock.sleep(30)
                if self.mocks.enrich_poll(aid, job).status != 202:
                    break
            state.record_enrichment_attempt(self.conn, aid)
            return Exec("pending", tags + ["polled"], out.attempts, {"job_id": job})
        if out.status != "ok":
            state.record_enrichment_attempt(self.conn, aid)
            return Exec("failed", tags + ["exhausted"], out.attempts, {"reason": out.reason})
        body = out.result.body
        fm = body.get("firmographics") or {}
        ec = account.get("employee_count")
        contradictory = bool(body.get("warnings")) or fm.get("is_customer") or \
            (ec and fm.get("employee_count") and not 0.2 <= fm["employee_count"] / ec <= 5)
        if contradictory:                                        # do not overwrite the CRM with data that disagrees
            state.record_enrichment_attempt(self.conn, aid)
            return Exec("contradictory", tags, out.attempts, {"warnings": body.get("warnings")})
        if not fm and not body.get("contacts"):
            state.record_enrichment_attempt(self.conn, aid)
            return Exec("no_data", tags, out.attempts)
        changed = state.apply_enrichment(self.conn, aid, body, self.clock.now)
        return Exec("ok", tags, out.attempts, {"changed": changed})

    # ---- calendar -----------------------------------------------------------------------------------------------------
    def book_meeting(self, account_id, payload, key, ctx) -> Exec:
        self._action(key, account_id, None, None, "meeting", "calendar", "in_progress", payload)
        r = self._write("calendar", lambda: self.mocks.calendar_book(account_id, key, payload), key, ctx)
        self._finish(key, {"ok": "succeeded"}.get(r.status, r.status), r.attempts)
        return r
