"""Rule-based AE routing. The AI never chooses an AE."""
from __future__ import annotations

import json
from datetime import datetime

from .timeutil import parse


def available(ae: dict, now: datetime) -> bool:
    return bool(ae["active"]) and (not ae["out_of_office_until"] or parse(ae["out_of_office_until"]) <= now)


def _langs(ae: dict) -> list:
    v = ae["languages"]
    return json.loads(v) if isinstance(v, str) else list(v or [])


def route_ae(account: dict, contact_language: str | None, aes: list[dict], now: datetime):
    """Returns (ae_id | None, reason). Owner -> owner's backup -> territory -> any territory -> nobody."""
    by_id = {a["ae_id"]: a for a in aes}
    owner = account.get("crm_owner_ae_id")
    if owner:
        o = by_id.get(owner)
        if o and available(o, now):
            return owner, "OWNER"
        b = by_id.get(o["backup_ae_id"]) if o else None
        if b and available(b, now):
            return b["ae_id"], "OWNER_BACKUP"
        return None, "NO_AE_AVAILABLE"
    lang = contact_language or "en"

    def best(pool):
        ok = [a for a in pool if available(a, now) and lang in _langs(a) and a["open_accounts"] < a["max_open_accounts"]]
        if not ok:
            return None
        return min(ok, key=lambda a: (a["open_accounts"] / a["max_open_accounts"], a["ae_id"]))["ae_id"]

    hit = best([a for a in aes if a["country"] == account["country"]])
    if hit:
        return hit, "TERRITORY"
    hit = best(aes)
    return (hit, "TERRITORY_FALLBACK") if hit else (None, "NO_AE_AVAILABLE")
