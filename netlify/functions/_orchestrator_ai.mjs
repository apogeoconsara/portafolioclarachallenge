// JS port of growth-orchestrator/orchestrator/ai/{validate,reply,draft}.py for the live panel.
// The Python package is the system; this port exists so the deployed page can call the real model server-side with the
// SAME prompts (generated module) and the SAME validation. Parity is tested against all 220 recorded model outputs:
//   node growth-orchestrator/tests/js/parity.test.mjs
// Nothing in this file can send an email.
import { ORCH } from "./_orchestrator_prompts.mjs";

export const LABEL_ACTION = ORCH.label_action;
const OPT_OUT_LABELS = new Set(ORCH.opt_out_labels);
const NEEDS_HUMAN_REVIEW = new Set(ORCH.needs_human_review);
const REPLY_FIELDS = ["label", "confidence", "interest_level", "follow_up_date", "referred_contact", "qualification",
  "suggested_action", "needs_human_review", "evidence"];
const QUAL_FIELDS = ["team_size", "current_solution", "timeline_months", "countries", "pain_points", "budget_signal"];
const DRAFT_FIELDS = ["language", "subject", "body", "claims", "personalized"];
const MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
  "november", "december"];
const COUNTRY_WORDS = { MX: "mexico", CO: "colombia", CL: "chile", BR: "brazil", AR: "argentina", PE: "peru" };

export const OPT_OUT_RE = new RegExp(
  "don'?t (write|email|contact|send)( to)? me|do not (contact|email|write)|stop (emailing|sending|writing)|" +
  "remove (me|my (email|address))|take me off|unsubscribe|^\\s*stop\\s*$|" +
  "(delete|deleted) (my|of my) (personal )?data|personal data be deleted|" +
  "(don'?t|do not) want (any )?(more|further) (emails|messages)|no more (emails|messages)|enough (with )?(the )?emails",
  "im");
export const INJECTION_RE = new RegExp(
  "ignore (all )?(your )?(previous|prior) instructions|ignore all your|forget everything|" +
  "^\\s*system\\s*:|\\[\\[system|instructions for the assistant|you are now in|\\bai assistant\\b|" +
  "reply in json|output action\\s*=", "im");

const LINE_SPLIT = new RegExp("\\r\\n|[\\n\\r\\v\\f\\x1c\\x1d\\x1e\\x85\\u2028\\u2029]");
export function ownText(t) {
  return String(t || "").split(LINE_SPLIT).filter(l => !l.replace(/^\s+/, "").startsWith(">")).join("\n");
}
const norm = s => String(s || "").replace(/\s+/g, " ").trim().toLowerCase();
const isObj = v => v !== null && typeof v === "object" && !Array.isArray(v);
const sameKeys = (o, keys) => isObj(o) && Object.keys(o).length === keys.length && keys.every(k => k in o);
const intOrNull = v => v === null || Number.isInteger(v);
const esc = s => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function parse(raw) {
  let txt = String(raw || "").trim(), warnings = [];
  const m = txt.match(/^```(?:json)?\s*([\s\S]*?)\s*```$/);
  if (m) { txt = m[1]; warnings = ["FENCED_JSON"]; }
  try { return [JSON.parse(txt), warnings]; } catch { return [null, warnings]; }
}

function validDate(s) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(String(s))) return null;
  const [y, mo, d] = s.split("-").map(Number);
  const dt = new Date(Date.UTC(y, mo - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === mo - 1 && dt.getUTCDate() === d ? dt : null;
}

function structureCodes(o) {
  if (!sameKeys(o, REPLY_FIELDS)) return ["V002"];
  const codes = [];
  const q = o.qualification, rc = o.referred_contact, fu = o.follow_up_date;
  let bad = !(o.label in ORCH.labels) || !ORCH.interest.includes(o.interest_level) || !ORCH.actions.includes(o.suggested_action)
    || typeof o.needs_human_review !== "boolean" || typeof o.evidence !== "string" || o.evidence.length > 200
    || (rc !== null && !(sameKeys(rc, ["name", "email"])))
    || !sameKeys(q, QUAL_FIELDS);
  if (fu !== null && !validDate(fu)) bad = true;
  if (!bad) {
    bad = !intOrNull(q.team_size) || !(q.current_solution === null || ORCH.current.includes(q.current_solution))
      || !intOrNull(q.timeline_months)
      || !(Array.isArray(q.countries) && q.countries.every(c => typeof c === "string" && /^[A-Z]{2}$/.test(c)))
      || !(Array.isArray(q.pain_points) && q.pain_points.every(p => ORCH.pains.includes(p)))
      || !(q.budget_signal === null || ORCH.budget.includes(q.budget_signal));
  }
  if (bad) codes.push("V003");
  const c = o.confidence;
  if (typeof c !== "number" || !(c >= 0 && c <= 1)) codes.push("V005");
  return codes;
}

const USABLE = new Set(["accept", "accept_with_warning", "override_rule"]);

export function validateReply(raw, replyText, receivedDate /* YYYY-MM-DD */, confidenceMin = ORCH.confidence_min) {
  const R = (verdict, codes, warnings, final_action, output = null, label = null, confidence = null) =>
    ({ verdict, codes: [...new Set(codes)].sort(), warnings, final_action, output, label, confidence, usable: USABLE.has(verdict) });
  const [o, warnings] = parse(raw);
  if (o === null) return R("reject_retry", ["V001"], warnings, "retry_then_escalate_human");
  const sc = structureCodes(o);
  if (sc.length) return R("reject_retry", sc, warnings, "retry_then_escalate_human", o);
  const text = ownText(replyText), low = text.toLowerCase(), label = o.label, sem = [];
  if (o.evidence) { if (!norm(text).includes(norm(o.evidence))) sem.push("V006"); }
  else if (label !== "empty_or_truncated") sem.push("V006");
  const rc = o.referred_contact;
  if (rc && ((rc.email && !low.includes(rc.email.toLowerCase())) || (rc.name && !low.includes(rc.name.toLowerCase())))) sem.push("V007");
  if (o.follow_up_date !== null) {
    const d = validDate(o.follow_up_date), rd = validDate(receivedDate);
    if (d <= rd || !norm(text).includes(`${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`)) sem.push("V008");
  }
  const q = o.qualification;
  if ((q.team_size !== null && !new RegExp(`(?<!\\d)${q.team_size}(?!\\d)`).test(text))
    || q.countries.some(c => !low.includes(COUNTRY_WORDS[c] || "~") && !new RegExp(`\\b${esc(c)}\\b`).test(text))
    || (q.budget_signal && !low.includes("budget"))
    || (q.timeline_months !== null && !/\d+\s*months?|next (month|quarter|year)|within|semester|quarter|this year/.test(low))) sem.push("V009");
  if (INJECTION_RE.test(text) && label !== "prompt_injection") sem.push("V012");
  if (sem.length) return R("reject_escalate", sem, warnings, "escalate_human", o, label, o.confidence);
  const base = LABEL_ACTION[label];
  if (OPT_OUT_RE.test(text) && !OPT_OUT_LABELS.has(label))
    return R("override_rule", ["G001", ...(o.suggested_action !== "suppress" ? ["V010"] : [])], warnings, "suppress", o, label, o.confidence);
  if (o.confidence < confidenceMin) return R("escalate_low_confidence", ["V011"], warnings, "escalate_human", o, label, o.confidence);
  if (o.suggested_action !== base) return R("override_rule", ["V010"], warnings, base, o, label, o.confidence);
  return R(warnings.length ? "accept_with_warning" : "accept", [], warnings, base, o, label, o.confidence);
}

export function finalAction(labelAction, label, crmState) {
  if (labelAction === "suppress") return "suppress";
  if (crmState === "customer" || crmState === "churned_customer")
    return ["no_action", "wait"].includes(labelAction) ? labelAction : "escalate_human";
  if ((crmState === "active_opportunity" || crmState === "ae_assigned") && ["info_request", "objection"].includes(label)) return "handoff_ae";
  return labelAction;
}

const words = s => new Set((String(s || "").toLowerCase().match(/[a-z]{5,}/g) || []));
const digits = s => new Set(String(s).match(/\d+/g) || []);

export function validateDraft(raw, facts, usableIds, contactLanguage, rules = ORCH.content_rules) {
  const [o, warnings] = parse(raw);
  if (o === null) return { verdict: "reject_retry", codes: ["P001"], template_mode: "retry", output: null, warnings };
  const ok = sameKeys(o, DRAFT_FIELDS) && ["es", "pt", "en"].includes(o.language) && typeof o.subject === "string"
    && typeof o.body === "string" && typeof o.personalized === "boolean" && Array.isArray(o.claims)
    && o.claims.every(c => sameKeys(c, ["text", "fact_id"]) && typeof c.text === "string");
  if (!ok) return { verdict: "reject_retry", codes: ["P002"], template_mode: "retry", output: null, warnings };
  const byId = Object.fromEntries(facts.map(f => [f.fact_id, f]));
  const body = o.body, codes = [];
  if (o.claims.length > rules.max_claims_per_email) codes.push("P014");
  for (const c of o.claims) {
    const fid = c.fact_id;
    if (!fid) { codes.push("P003"); continue; }
    if (!(fid in byId)) { codes.push("P004"); continue; }
    if (!usableIds.has(fid)) codes.push("P005");
    const ft = byId[fid].text, cw = words(c.text), fw = words(ft), fd = digits(ft);
    const newDigits = [...digits(c.text)].some(d => !fd.has(d));
    const unsupported = [...cw].filter(w => !fw.has(w)).length / Math.max(1, cw.size);
    if (newDigits || unsupported > 0.34) codes.push("P006");
    if (!norm(body).includes(norm(c.text))) codes.push("P013");
  }
  const text = o.subject + "\n" + body;
  if (rules.forbidden_claims.some(r => new RegExp(r.regex, "i").test(text))) codes.push("P007");
  if (!body.includes(rules.unsubscribe_footer[contactLanguage])) codes.push("P008");
  if (body.split(/\s+/).filter(Boolean).length > rules.max_body_words) codes.push("P009");
  if (o.language !== contactLanguage) codes.push("P010");
  if ((o.personalized || o.claims.length) && !usableIds.size) codes.push("P011");
  if (o.subject.length > rules.max_subject_chars) codes.push("P012");
  if (codes.length) return { verdict: "fallback_generic", codes: [...new Set(codes)].sort(), template_mode: "generic", output: o, warnings };
  return { verdict: "accept", codes: [], template_mode: o.claims.length ? "personalized" : "generic", output: o, warnings };
}

// ---- deterministic fact filter (rules, no AI) --------------------------------------------------------------------
export function usableFacts(facts, account, nowIso) {
  const now = Date.parse(nowIso);
  return facts.filter(f => {
    if (!f.is_verified) return false;
    if (now - Date.parse(f.observed_at) > 365 * 86400000) return false;
    if (!f.text.includes(account.name)) return false;
    const m = f.text.match(/(\d+)\s+employees/);
    if (m && account.employee_count && !(Number(m[1]) / account.employee_count >= 0.5 && Number(m[1]) / account.employee_count <= 2)) return false;
    return true;
  });
}

export function render(tpl, firstName, company, sender, footer, opening = "") {
  const fmt = { first_name: firstName, company, sender_name: sender, unsubscribe_footer: footer,
    personalized_opening: opening ? opening + "\n\n" : "" };
  const f = s => s.replace(/\{(\w+)\}/g, (_, k) => fmt[k] ?? "");
  return [f(tpl.subject), f(tpl.body)];
}

export function replyUserMessage(replyText, receivedDate, ctx) {
  return `Reply date: ${receivedDate}\nCompany: ${ctx.company || ""}\nAccount state: ${ctx.crm_state || "prospect"}\n` +
    `Subject of our last email: ${ctx.last_touch_subject || ""}\n<reply>\n${replyText}\n</reply>`;
}

export function draftUserMessage(subject, bodyWithPlaceholder, facts) {
  const lines = facts.map(f => `- ${f.fact_id}: ${f.text}`).join("\n") || "(no usable facts)";
  return `Template subject: ${subject}\nTemplate body:\n${bodyWithPlaceholder}\n\nVerified facts:\n${lines}`;
}

// ---- the two AI capabilities, end to end (llm = async (system, user, tool, maxTokens) => {raw, ...}) -------------
const EMPTY = { interest_level: "unclear", follow_up_date: null, referred_contact: null,
  qualification: { team_size: null, current_solution: null, timeline_months: null, countries: [], pain_points: [], budget_signal: null } };

export async function interpretReply(llm, replyText, receivedDate, ctx) {
  const text = ownText(replyText);
  if (!text.trim()) return { action: "escalate_human", reason_codes: ["EMPTY_REPLY"], verdict: "skipped", attempts: [], extracted: EMPTY, used_ai: false };
  const attempts = [];
  let v = null;
  for (let i = 0; i < 2; i++) {
    let resp;
    try { resp = await llm(ORCH.reply.system, replyUserMessage(text, receivedDate, ctx), ORCH.reply.tool, ORCH.reply.max_tokens); }
    catch (e) {
      if (OPT_OUT_RE.test(text)) return { action: "suppress", reason_codes: ["OPT_OUT_GUARD", "LLM_UNAVAILABLE"], verdict: "llm_unavailable", attempts, extracted: EMPTY, used_ai: false, error: String(e.message || e) };
      return { action: "escalate_human", reason_codes: ["LLM_UNAVAILABLE"], verdict: "llm_unavailable", attempts, extracted: EMPTY, used_ai: false, error: String(e.message || e) };
    }
    v = validateReply(resp.raw, text, receivedDate);
    attempts.push({ ...resp, verdict: v.verdict, codes: v.codes });
    if (v.verdict !== "reject_retry") break;
  }
  const o = v.output || {};
  const extracted = v.usable ? Object.fromEntries(Object.keys(EMPTY).map(k => [k, o[k] ?? EMPTY[k]])) : EMPTY;
  const common = { verdict: v.verdict, label: v.label, confidence: v.confidence, violation_codes: v.codes, extracted, attempts, used_ai: true };
  if (v.verdict === "reject_retry") return { action: "escalate_human", reason_codes: ["AI_INVALID_OUTPUT"], needs_human_review: true, ...common };
  if (v.verdict === "reject_escalate") return { action: "escalate_human", reason_codes: ["AI_UNSUPPORTED_BY_TEXT"], needs_human_review: true, ...common };
  if (v.verdict === "escalate_low_confidence") return { action: "escalate_human", reason_codes: ["AI_LOW_CONFIDENCE", v.label.toUpperCase()], needs_human_review: true, ...common };
  const action = finalAction(v.final_action, v.label, ctx.crm_state || "prospect");
  return { action, reason_codes: [v.label.toUpperCase(), ...(v.codes.includes("G001") ? ["OPT_OUT_GUARD"] : [])],
    needs_human_review: NEEDS_HUMAN_REVIEW.has(v.label) || v.codes.includes("G001"), ...common };
}

export async function composeDraft(llm, account, contact, facts, step, sender, nowIso) {
  const rules = ORCH.content_rules;
  const tpl = ORCH.templates.find(t => t.language === "en" && t.step === Math.min(step, 4));
  const footer = rules.unsubscribe_footer.en;
  const [gSubject, gBody] = render(tpl, contact.first_name, account.name, sender, footer);
  const usable = usableFacts(facts, account, nowIso);
  const ids = usable.map(f => f.fact_id);
  const generic = (verdict, codes, attempts = [], used = false) =>
    ({ subject: gSubject, body: gBody, mode: "generic", verdict, codes, claims: [], usable_fact_ids: ids, attempts, used_ai: used });
  if (!usable.length || !tpl.body.includes("{personalized_opening}")) return generic(usable.length ? "step_without_opening" : "no_usable_facts", []);
  const [, placeholderBody] = render(tpl, contact.first_name, account.name, sender, footer, "[OPENING]");
  const attempts = [];
  let v = null;
  for (let i = 0; i < 2; i++) {
    let resp;
    try { resp = await llm(ORCH.draft.system, draftUserMessage(gSubject, placeholderBody, usable), ORCH.draft.tool, ORCH.draft.max_tokens); }
    catch (e) { return generic("llm_unavailable", ["LLM_UNAVAILABLE"], attempts); }
    v = validateDraft(resp.raw, facts, new Set(ids), "en", rules);
    attempts.push({ ...resp, verdict: v.verdict, codes: v.codes });
    if (v.verdict !== "reject_retry") break;
  }
  if (v.verdict !== "accept") return generic(v.verdict, v.codes, attempts, true);
  const o = v.output;
  let stripped = o.body;
  for (const c of o.claims) stripped = stripped.replace(new RegExp("I saw that " + esc(c.text) + "\\s*", "g"), "");
  if (stripped.replace(/\s+/g, " ").trim() !== gBody.replace(/\s+/g, " ").trim() || o.subject !== gSubject)
    return generic("fallback_generic", ["P015_TEMPLATE_MODIFIED"], attempts, true);
  return { subject: o.subject, body: o.body, mode: v.template_mode, verdict: "accept", codes: [], claims: o.claims, usable_fact_ids: ids, attempts, used_ai: true };
}
