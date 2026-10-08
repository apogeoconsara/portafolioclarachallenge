"""A stateful answer key: what an `account_targeted` event should decide, given everything delivered before it.

`truth_accounts` is a snapshot: it says what the oracle decides for each account at AS_OF. The event stream then keeps
changing that state: a deal closes lost, a contact unsubscribes, an email bounces hard, a reply opts the contact out. An
`account_targeted` event delivered AFTER one of those must be judged on the state it will find, not on the snapshot.

This module replays the stream in delivery order (the order a consumer processes it) and applies the effects the policy
documents (data/POLICY.md, "events"):

  unsubscribe_received          -> that contact is suppressed
  email_bounced (hard)          -> that contact is suppressed
  reply_received (opt-out)      -> that contact is suppressed; any reply marks the contact's earlier touches as replied
  opportunity_created           -> the opportunity exists, with the stage it was created in
  opportunity_stage_changed     -> the opportunity takes the new stage (closed stages are stamped with the event time)

and, for each `account_targeted` that follows at least one such effect on the same account, asks the oracle again. It
never changes the policy or its rule order, and it never touches an event whose account saw no earlier change.
It does not import the engine: the oracle stays an independent derivation.
"""
from __future__ import annotations

import copy
from collections import defaultdict

from . import oracle
from .config import AS_OF
from .util import parse_iso

PROCESSED = ("process", "process_and_reconcile")      # duplicates, stale and malformed deliveries change nothing
KEYS = ("action", "reason_codes", "best_contact_id", "wait_until")
TRUTH_KEYS = {"action": "expected_action", "reason_codes": "reason_codes", "best_contact_id": "best_contact_id",
              "wait_until": "wait_until"}


class StreamState:
    def __init__(self, w):
        # Copies of the tables the stream changes: the world itself must stay exactly as generated.
        self.accounts = {a["account_id"]: a for a in w.accounts}
        self.idx = oracle.Index(w.accounts, w.contacts, copy.deepcopy(w.opportunities), copy.deepcopy(w.touches),
                                copy.deepcopy(w.suppression))
        self.aes = w.aes
        self.changed_by = defaultdict(list)            # account_id -> delivery ids whose effect was applied

    # ---- effects -----------------------------------------------------------------------------------------------
    def _suppress(self, aid, cid, reason, delivery_id, at):
        self.idx.supp_by_account[aid].append({"suppression_id": f"sup_stream_{delivery_id}", "scope": "contact", "reason": reason,
                                              "account_id": aid, "contact_id": cid, "email": None, "domain": None,
                                              "source": "stream", "added_at": at})

    def apply(self, env: dict, truth: dict) -> bool:
        """Apply one processed delivery's effect on state. Returns True if the state the oracle reads changed."""
        aid, cid, p, kind = env.get("account_id"), env.get("contact_id"), env.get("payload") or {}, env.get("type")
        if aid not in self.accounts:
            return False
        done = False
        if kind == "unsubscribe_received" and cid:
            self._suppress(aid, cid, "unsubscribe", env["delivery_id"], env["occurred_at"])
            done = True
        elif kind == "email_bounced" and cid and p.get("bounce_type") == "hard":
            self._suppress(aid, cid, "hard_bounce", env["delivery_id"], env["occurred_at"])
            done = True
        elif kind == "reply_received" and cid:
            for t in self.idx.touches.get(aid, []):
                if t["contact_id"] == cid and t["status"] != "replied":
                    t["status"] = "replied"
                    done = True
            if truth.get("expected_action") == "suppress":
                self._suppress(aid, cid, "unsubscribe", env["delivery_id"], env["occurred_at"])
                done = True
        elif kind == "opportunity_created" and p.get("opportunity_id"):
            self.idx.opps[aid].append({"opportunity_id": p["opportunity_id"], "account_id": aid, "amount_usd": 0,
                                       "owner_ae_id": p.get("owner_ae_id"), "lost_reason": None, "closed_at": None,
                                       "stage": p.get("stage", "discovery"), "created_at": env["occurred_at"]})
            done = True
        elif kind == "opportunity_stage_changed" and p.get("opportunity_id"):
            for o in self.idx.opps.get(aid, []):
                if o["opportunity_id"] == p["opportunity_id"]:
                    o["stage"] = p["to_stage"]
                    if p["to_stage"] in ("closed_won", "closed_lost"):
                        o["closed_at"] = env["occurred_at"]
                    done = True
        if done:
            self.changed_by[aid].append(env["delivery_id"])
        return done

    def decide(self, aid: str) -> dict:
        return oracle.decide(self.accounts[aid], self.idx, AS_OF, self.aes)


def correct_truth(w, rows: list[tuple[dict, dict]]) -> int:
    """Re-derive the expected decision of every `account_targeted` that follows a state change on its account.

    `rows` are (envelope, truth) in delivery order. Truth entries are corrected in place; a corrected entry also lists the
    deliveries that changed the state (`state_changed_by`) so the difference from the account snapshot is auditable.
    Returns how many expectations changed."""
    st, changed = StreamState(w), 0
    for env, truth in rows:
        if truth.get("expected_handling") not in PROCESSED or env.get("type") != truth.get("type"):
            continue
        if env["type"] == "account_targeted":
            before = st.changed_by.get(env["account_id"])
            if not before:
                continue
            got = st.decide(env["account_id"])
            if any(got.get(k) != truth.get(TRUTH_KEYS[k]) for k in KEYS):
                for k in KEYS:
                    truth[TRUTH_KEYS[k]] = got.get(k)
                truth["state_changed_by"] = list(before)
                changed += 1
        else:
            st.apply(env, truth)
    return changed
