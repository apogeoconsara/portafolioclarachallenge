"""Everyday-language names for actions and reason codes, for the web page. One source; exported to public/data."""
ACTIONS = {
    "contact": ["Outreach email drafted", "Passed every check and is worth a personal email: a first email, or a follow-up if the company already got earlier ones. It is prepared automatically and held until a person approves it. Nothing has been sent: once approved, it is only recorded in a simulated log."],
    "nurture": ["Nurture (slow track)", "Eligible, but a weak fit for now. No email and no AI: it joins the slow nurture track and is scored again at the next event."],
    "wait": ["On hold", "Not the right moment (recently contacted or recently lost). The system waits and checks again."],
    "suppress": ["Do not contact", "Should not receive outreach (opted out, customer, competitor, open deal or poor fit)."],
    "handoff_ae": ["Hand to a sales exec", "Already has an owner, so it goes to the right sales exec instead of an automatic email."],
    "enrich": ["Needs better data first", "Data is missing or outdated, so the system looks it up before doing anything else."],
    "escalate_human": ["A person decides", "Conflicting or unusual situation. The system does not guess."],
}
CODES = {
    "ELIGIBLE": "Passed every check",
    "UNSUBSCRIBED": "Asked to stop receiving emails",
    "HARD_BOUNCE": "Its email address does not exist",
    "SPAM_COMPLAINT": "Marked a previous email as spam",
    "DNC": "Asked not to be contacted",
    "LEGAL_HOLD": "Legal hold on the account",
    "COMPETITOR": "Competitor",
    "STATE_CONFLICT": "The CRM contradicts itself (customer without a won deal, or the reverse)",
    "CUSTOMER": "Already a customer",
    "CHURNED_CUSTOMER": "Former customer",
    "DUPLICATE_ACCOUNT": "Duplicate of an older account",
    "ACTIVE_OPPORTUNITY": "Has a deal in progress",
    "AE_ASSIGNED": "Already assigned to a sales exec",
    "NO_AE_AVAILABLE": "No sales exec available right now",
    "CLOSED_LOST_COOLDOWN": "Lost a deal recently",
    "NOT_ICP": "Too small or not a target industry",
    "RECENT_OUTREACH": "Contacted very recently",
    "SEQUENCE_EXHAUSTED": "Already received the full email sequence",
    "STALE_ENRICHMENT": "Company data is out of date",
    "MISSING_FIRMOGRAPHICS": "Company size or industry is missing",
    "NO_CONTACTS": "No contact person on file",
    "UNVERIFIED_EMAIL_ONLY": "Only unverified email addresses",
    "NO_VALID_EMAIL": "No valid email address",
    "ENRICHMENT_EXHAUSTED": "Data lookups did not fix it",
    "LOW_PRIORITY": "Low priority score",
}

EVENTS = {
    "account_targeted": "A company is added to a target list", "reply_received": "A prospect replies",
    "unsubscribe_received": "Someone unsubscribes", "opportunity_created": "A deal is created",
    "opportunity_stage_changed": "A deal changes stage", "email_bounced": "An email bounces",
    "meeting_booked": "A meeting is booked", "lead_scored": "A lead is scored upstream",
}
VERDICTS = {
    "accept": "Accepted: well-formed and supported by the prospect's own words",
    "accept_with_warning": "Accepted with a warning",
    "override_rule": "The rules overrode the model",
    "reject_retry": "Rejected: malformed output, asked once more",
    "reject_escalate": "Rejected: it claimed things the reply does not say, so a person looks",
    "escalate_low_confidence": "Not confident enough to act: a person looks",
    "no_usable_facts": "No verified fact to mention: generic template, no AI call",
    "skipped": "Skipped",
}
VALIDATION_CODES = {
    "G001": "Opt-out phrase detected in the reply: opt-out always wins, whatever the model said",
    "V010": "The model's suggested action was replaced by the rules",
    "V011": "Confidence below the automatic threshold",
    "V006": "A quoted piece of evidence is not in the reply",
    "V007": "An invented referral",
    "V008": "An invented or past date",
    "V009": "An invented qualification detail",
    "V012": "A prompt injection was followed or missed",
    "V001": "The output did not match the required structure",
}

# "Can we safely automate it?" for each outcome: the question the challenge asks, answered in one line.
AUTOMATION = {
    "contact": ["Automate, then approve", "The system decides and prepares the email on its own. A person approves it before anything is sent."],
    "nurture": ["Automate", "Enrolled in the slow nurture track automatically. No email, no AI, nothing to approve."],
    "wait": ["Automate", "The system holds the account and checks it again by itself."],
    "enrich": ["Automate", "The data lookup runs by itself. Outreach waits for better data."],
    "suppress": ["Automate (it blocks)", "Stopping is safe to automate: the system blocks outreach and nothing is sent."],
    "handoff_ae": ["Hand to a person", "The system routes it to the right sales exec, who takes it from here."],
    "escalate_human": ["Do not automate", "The case is conflicting or unusual, so a person decides."],
}
