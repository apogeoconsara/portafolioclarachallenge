"""Turns frozen reply seeds into realistic labelled reply bodies (greetings, signatures, quoted threads, typos)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from .config import REPLY_LABEL_WEIGHTS
from .reply_seeds import (BY_LABEL, LABEL_ACTION, LABEL_INTEREST, NEEDS_HUMAN_REVIEW, UNSAFE_ACTIONS)
from .names import SDR_NAMES
from .util import wpick

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
GREET = ["", "Hi {first},\n\n", "Hello,\n\n", "Dear {first},\n\n", "Hi!\n\n"]
CLOSE = ["", "\n\nBest,\n{first}", "\n\nBest regards,\n{full}\n{title}\n{company}", "\n\nThanks,\n{first}",
         "\n\nSent from my iPhone", "\n\nRegards"]
QUOTE_HEAD = "On {date}, {sdr} wrote:"
QUOTE_BODY = "Hi {first}, this is {sdr} from Clara. We help companies like {company} control spend with corporate cards."
# Realistic noise: quoted outbound emails carry an unsubscribe footer the classifier must NOT treat as the reply.
QUOTE_FOOT = "If you'd rather not hear from us, reply STOP."
DISCLAIMER = "\n\nNotice: this message is confidential and intended solely for the addressee."
RAW_LABELS = {"empty_or_truncated", "auto_reply", "prompt_injection"}  # keep body raw (no greetings/typos)

# crm states: customer | churned_customer | active_opportunity | ae_assigned | prospect
OPT_OUT_LABELS = {"unsubscribe", "hostile", "mixed_signals"}


def final_action(label: str, crm_state: str) -> str:
    """Expected safe action for a reply once account state is taken into account."""
    if label in OPT_OUT_LABELS:
        return "suppress"  # opt-out always wins, whatever the account state
    base = LABEL_ACTION[label]
    if crm_state in ("customer", "churned_customer"):
        return base if base in ("no_action", "wait") else "escalate_human"  # never a prospecting path
    if crm_state in ("active_opportunity", "ae_assigned") and label in ("info_request", "objection"):
        return "handoff_ae"  # the owning AE answers
    return base


def pick_label(r) -> str:
    return wpick(r, REPLY_LABEL_WEIGHTS)


def pick_seed(r, label: str, lang: str = "en") -> dict:
    return r.choice(BY_LABEL[label])


def _fmt_date(dt: datetime, lang: str = "en") -> str:
    return f"{MONTHS[dt.month - 1]} {dt.day}"


# Words the reference validator reads in the reply text: the referral name (V007), the month of a follow-up date (V008), and
# the country, "budget" and timeline words (V009). A typo on any of them would make a correct extraction "unsupported by
# the text", so they are never altered.
_TIMELINE_WORDS = {"month", "months", "next", "quarter", "semester", "within", "year", "budget"}


def _protected(ref_name: str) -> set[str]:
    from .ai_ref import COUNTRY_WORDS            # imported here: ai_ref itself imports this module
    return ({m.lower() for m in MONTHS} | {w.lower() for w in re.findall(r"[^\W\d_]+", ref_name)} | _TIMELINE_WORDS
            | {w for ws in COUNTRY_WORDS.values() for w in ws})


def _typo(text: str, r, protected: set[str] = frozenset()) -> str:
    words = text.split(" ")
    idx = [i for i, w in enumerate(words)
           if len(re.sub(r"\W", "", w)) >= 5 and not re.search(r"[\d@{<>]", w) and not w.isupper()]
    if not idx:
        return text
    i = r.choice(idx)
    ch = list(words[i])
    pos = r.randrange(1, len(ch) - 2)           # the random draws are made either way, so nothing else in the world moves
    if re.sub(r"\W", "", words[i]).lower() in protected:
        return text
    ch[pos], ch[pos + 1] = ch[pos + 1], ch[pos]
    words[i] = "".join(ch)
    return " ".join(words)


def resolve(seed: dict, occurred: datetime, ref: tuple[str, str]):
    """Replace date/referral tokens. Returns (text, dates, has_ref_name, has_ref_email)."""
    text, dates = seed["text"], []

    def _date(m):
        d = occurred + timedelta(days=int(m.group(1)))
        dates.append(d)
        return _fmt_date(d)

    text = re.sub(r"\{d\+(\d+)\}", _date, text)
    has_name, has_email = "{ref_name}" in text, "{ref_email}" in text
    return text.replace("{ref_name}", ref[0]).replace("{ref_email}", ref[1]), dates, has_name, has_email


def expected_extraction(seed, dates, ref, has_name, has_email):
    return {"interest_level": LABEL_INTEREST[seed["label"]],
            "follow_up_date": dates[-1].date().isoformat() if dates else None,
            "referred_contact": ({"name": ref[0] if has_name else None, "email": ref[1] if has_email else None}
                                 if (has_name or has_email) else None),
            "qualification": seed["qualification"]}


def render_reply(seed: dict, ctx: dict, r) -> dict:
    """ctx: first, full, title, company, occurred (datetime), ref=(name, email), crm_state."""
    lang, label = "en", seed["label"]
    text, dates, has_name, has_email = resolve(seed, ctx["occurred"], ctx["ref"])
    # A typo must not alter the information the model has to extract
    if label not in RAW_LABELS and r.random() < 0.12:
        text = _typo(text, r, _protected(ctx["ref"][0]))

    fmt = {"first": ctx["first"], "full": ctx["full"], "title": ctx["title"], "company": ctx["company"]}
    body = text
    if label not in RAW_LABELS:
        body = r.choice(GREET).format(**fmt) + text + r.choice(CLOSE).format(**fmt)
    if r.random() < 0.08:
        body += DISCLAIMER
    if r.random() < 0.30:
        sdr = r.choice(SDR_NAMES)
        foot = QUOTE_FOOT if r.random() < 0.5 else ""
        quoted = [QUOTE_HEAD.format(date=_fmt_date(ctx["occurred"] - timedelta(days=r.randint(1, 20)), lang),
                                          sdr=sdr),
                  QUOTE_BODY.format(first=ctx["first"], sdr=sdr.split()[0], company=ctx["company"])]
        if foot:
            quoted.append(foot)
        body += "\n\n" + "\n".join("> " + q for q in quoted)

    action = final_action(label, ctx["crm_state"])
    return {
        "text": body, "language": "en", "seed_id": seed["seed_id"], "label": label,
        "difficulty": seed["difficulty"], "is_ambiguous": seed["ambiguous"],
        "extracted": expected_extraction(seed, dates, ctx["ref"], has_name, has_email),
        "expected_action": action, "unsafe_actions": UNSAFE_ACTIONS[label],
        "needs_human_review": label in NEEDS_HUMAN_REVIEW,
    }
