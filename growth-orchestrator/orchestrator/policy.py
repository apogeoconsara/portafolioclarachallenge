"""Business thresholds, send rules and AI limits. Read from JSON so changing an assumption is a config change."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

SEED_DIR = Path(__file__).resolve().parent.parent / "data" / "seed"


@dataclass
class Policy:
    recent_outreach_days: int
    sequence_max_touches: int
    sequence_window_days: int
    sequence_cooldown_days: int
    stale_enrichment_days: int
    lost_cooldown_days: int
    icp_min_employees: int
    max_enrich_attempts: int
    open_stages: tuple
    non_icp_industries: tuple
    function_score: dict
    seniority_score: dict
    ai_confidence_min_auto: float
    send: dict = field(default_factory=dict)          # send_policy.json

    @classmethod
    def load(cls, seed_dir: Path = SEED_DIR) -> "Policy":
        d = json.loads((seed_dir / "decision_policy.json").read_text(encoding="utf-8"))
        send = json.loads((seed_dir / "send_policy.json").read_text(encoding="utf-8"))
        return cls(recent_outreach_days=d["recent_outreach_days"], sequence_max_touches=d["sequence_max_touches"],
                   sequence_window_days=d["sequence_window_days"], sequence_cooldown_days=d["sequence_cooldown_days"],
                   stale_enrichment_days=d["stale_enrichment_days"], lost_cooldown_days=d["lost_cooldown_days"],
                   icp_min_employees=d["icp_min_employees"], max_enrich_attempts=d["max_enrich_attempts"],
                   open_stages=tuple(d["open_stages"]), non_icp_industries=tuple(d["non_icp_industries"]),
                   function_score=d["function_score"], seniority_score=d["seniority_score"],
                   ai_confidence_min_auto=d["ai_confidence_min_auto"], send=send)

    # --- send rules -----------------------------------------------------------------------
    @property
    def retry(self) -> dict:
        return self.send["retry"]

    @property
    def daily_cap(self) -> int:
        return self.send["caps"]["daily_send_cap_total"]

    def utc_offset(self, country: str) -> int:
        return self.send["timezones"][country]["utc_offset_hours"]
