"""World parameters. Policy thresholds mirror data/POLICY.md (the ground-truth decision spec)."""
from __future__ import annotations

from datetime import datetime, timezone

# State snapshot; the event stream covers the next month. 16:00 UTC is inside the Mon-Fri 09:00-18:00 send window in all
# six countries (10:00 Mexico City ... 13:00 Sao Paulo), so events at the snapshot time are sendable.
AS_OF = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)
STREAM_DAYS = 31
SCHEMA_VERSION = "1.0"

# Monthly target lists drop weekly; each drop is a burst (stresses rate limits / queues).
LIST_DROP_DAYS = [0, 7, 14, 21, 28]
LIST_WEIGHTS = [25, 25, 20, 15, 15]
LIST_BURST_MINUTES = 20

# ---- Policy thresholds -------------------------------------------------------------------
RECENT_OUTREACH_DAYS = 14
SEQUENCE_MAX_TOUCHES = 4
SEQUENCE_WINDOW_DAYS = 180
SEQUENCE_COOLDOWN_DAYS = 120
STALE_ENRICHMENT_DAYS = 90
LOST_COOLDOWN_DAYS = 90
ICP_MIN_EMPLOYEES = 11
MAX_ENRICH_ATTEMPTS = 2
OPEN_STAGES = ("discovery", "demo", "proposal", "negotiation")

# ---- Primary scenario quotas (% of accounts). Exact, not random, so rare cases always appear. ----
SCENARIO_QUOTAS = {
    "clean_prospect": 38.0,
    "partial_contact_suppression": 2.0,  # best contact unsubscribed, another contact still eligible
    "race_late_opportunity": 0.5,        # opportunity created before targeting but delivered after
    "race_late_unsubscribe": 0.5,        # unsubscribe happened before targeting but delivered after
    "customer": 8.0,
    "churned_customer": 2.0,
    "active_opportunity": 3.0,
    "ae_assigned": 10.0,
    "recent_outreach": 14.0,
    "max_touches_no_reply": 5.0,
    "suppressed": 2.5,
    "missing_data": 5.0,
    "duplicate_domain": 2.0,
    "low_icp": 4.0,
    "conflict_state": 1.5,
    "closed_lost_recent": 2.0,
}

# ---- Geography ----------------------------------------------------------------------------
# (code, name, name_origin, weight, legal suffixes, cities). name_origin only picks regional first/last names;
# every contact writes and reads English (the case and the presentation are in English).
COUNTRIES = [
    ("MX", "Mexico", "es", 36, ["S.A. de C.V.", "S.A.P.I. de C.V.", "S. de R.L. de C.V."],
     ["Mexico City", "Monterrey", "Guadalajara", "Queretaro", "Puebla", "Tijuana", "Leon"]),
    ("CO", "Colombia", "es", 20, ["S.A.S.", "Ltda.", "S.A."],
     ["Bogota", "Medellin", "Cali", "Barranquilla", "Bucaramanga"]),
    ("BR", "Brazil", "pt", 20, ["Ltda.", "S.A.", "EIRELI"],
     ["Sao Paulo", "Rio de Janeiro", "Belo Horizonte", "Curitiba", "Porto Alegre"]),
    ("CL", "Chile", "es", 10, ["SpA", "S.A.", "Ltda."],
     ["Santiago", "Valparaiso", "Concepcion"]),
    ("AR", "Argentina", "es", 8, ["S.A.", "S.R.L."],
     ["Buenos Aires", "Cordoba", "Rosario"]),
    ("PE", "Peru", "es", 6, ["S.A.C.", "S.A."],
     ["Lima", "Arequipa", "Trujillo"]),
]

# (name, weight, name words, icp_ok)
INDUSTRIES = [
    ("Logistics & Transportation", 9, ["Logistics", "Freight", "Cargo"], True),
    ("Agribusiness", 8, ["Agro", "Farms", "Harvest"], True),
    ("Retail & Commerce", 11, ["Trading", "Retail", "Mercantile"], True),
    ("Manufacturing", 11, ["Industries", "Manufacturing", "Plastics"], True),
    ("Technology & Software", 10, ["Tech", "Systems", "Digital"], True),
    ("Healthcare", 7, ["Health", "Clinic", "Medical"], True),
    ("Construction & Real Estate", 8, ["Builders", "Realty", "Developments"], True),
    ("Food & Beverage", 7, ["Foods", "Beverages", "Gourmet"], True),
    ("Professional Services", 8, ["Consulting", "Advisors", "Services"], True),
    ("Education", 3, ["Education", "Institute", "Academy"], True),
    ("Energy & Mining", 4, ["Energy", "Mining", "Petrochemicals"], True),
    ("Tourism & Hospitality", 4, ["Hotels", "Tourism", "Travel"], True),
    ("Marketing & Media", 4, ["Media", "Marketing", "Studio"], True),
    ("Government & Public Sector", 1.5, ["Municipality", "Secretariat", "Public Institute"], False),
    ("Nonprofit", 1.5, ["Foundation", "Association", "NGO"], False),
]

# (band, min, max, weight)
BANDS = [
    ("1-10", 1, 10, 12),
    ("11-50", 11, 50, 28),
    ("51-200", 51, 200, 32),
    ("201-500", 201, 500, 16),
    ("501-1000", 501, 1000, 8),
    ("1000+", 1001, 8000, 4),
]

FREE_EMAIL_DOMAINS = ["gmail.example", "hotmail.example", "outlook.example", "yahoo.example"]
ROLE_INBOXES = ["info", "contacto", "finanzas", "compras", "administracion", "ventas"]

# Contact attribute weights
EMAIL_STATUS_WEIGHTS = [("valid", 78), ("unverified", 8), ("role_based", 5), ("catchall", 3),
                        ("invalid_syntax", 2), ("missing", 2), ("free_provider", 2)]
FUNCTION_WEIGHTS = [("finance", 35), ("operations", 15), ("procurement", 12), ("it", 8),
                    ("hr", 8), ("executive", 14), ("other", 8)]
SENIORITY_WEIGHTS = [("c_level", 12), ("vp", 8), ("director", 22), ("manager", 30), ("ic", 28)]
N_CONTACTS_WEIGHTS = [(1, 30), (2, 30), (3, 20), (4, 12), (5, 8)]

# Ranking used to choose the best contact (corporate cards -> finance/procurement buyers first)
FUNCTION_SCORE = {"finance": 5, "procurement": 4, "executive": 4, "operations": 3, "it": 2, "hr": 1, "other": 0}
SENIORITY_SCORE = {"c_level": 5, "vp": 4, "director": 3, "manager": 2, "ic": 1}

# ---- Perturbation rates (event stream) ---------------------------------------------------------
RATE_EXACT_DUP = 0.030      # same event_id + key re-delivered
RATE_SEMANTIC_DUP = 0.015   # new event_id, same idempotency_key
RATE_CONTENT_DUP = 0.005    # different source/key, same content (e.g. CRM mirror of a provider event)
RATE_DELAYED = 0.025        # received long after occurred
RATE_MALFORMED = 0.010      # extra corrupted deliveries

# ---- Mock external API behaviours (assigned deterministically per account) ----------------------
ENRICH_BEHAVIORS = [("ok", 87), ("timeout_once", 4), ("rate_limit_once", 3),
                    ("server_error_persistent", 2), ("malformed_response", 3), ("uncertain_outcome", 1)]
SEND_BEHAVIORS = [("ok", 93), ("transient_error_then_ok", 3), ("rate_limit_then_ok", 2),
                  ("uncertain_outcome", 1.2), ("hard_reject", 0.8)]
CALENDAR_BEHAVIORS = [("ok", 92), ("slot_conflict", 4), ("timeout_then_ok", 1.5), ("rate_limit_then_ok", 1),
                      ("uncertain_outcome", 1), ("server_error_persistent", 0.5)]
CRM_BEHAVIORS = [("ok", 94), ("transient_error_then_ok", 2.5), ("rate_limit_then_ok", 1.5),
                 ("uncertain_outcome", 1), ("stale_version_conflict", 1)]

# ---- Replies --------------------------------------------------------------------------------
REPLY_LABEL_WEIGHTS = [
    ("interested", 14), ("info_request", 5.5), ("objection", 12), ("not_now", 13),
    ("wrong_person", 8), ("unsubscribe", 9), ("out_of_office", 10), ("auto_reply", 8),
    ("hostile", 3), ("ambiguous", 8), ("mixed_signals", 5), ("prompt_injection", 2.5),
    ("empty_or_truncated", 2),
]
