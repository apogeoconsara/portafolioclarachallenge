// Live AI for the Growth Orchestrator page (the site's home page).
//
// Calls the real model (Anthropic Messages API, forced tool use) with the SAME prompts as the Python engine
// (_orchestrator_prompts.mjs is generated from it), validates the output with the parity-tested JS port of the
// validators, and lets deterministic rules choose the action. The key is read server-side from ANTHROPIC_API_KEY and
// never returned. This function has no email client and cannot send email: drafts are returned for display only.
//
// POST {task: "status"}
// POST {task: "reply", reply_text, crm_state?, company?, received_date?}
// POST {task: "draft", case_id, facts?}           facts: optional edited facts for that case's account (max 5)
// POST {task: "eval_case", case_id}               one of the 18 live eval cases, scored
import { ORCH } from "./_orchestrator_prompts.mjs";
import { CASES } from "./_orchestrator_cases.mjs";
import { composeDraft, interpretReply } from "./_orchestrator_ai.mjs";

const MAX_BODY = 20_000, MAX_REPLY = 2000, MAX_FACTS = 5, MAX_FACT_CHARS = 300;
const RATE_PER_MIN = 30;
const CRM_STATES = ["prospect", "customer", "churned_customer", "active_opportunity", "ae_assigned"];
const AS_OF = "2026-10-01T16:00:00Z";
const hits = new Map();                       // best-effort per-instance rate limit

const json = (status, body) => new Response(JSON.stringify(body), {
  status, headers: { "content-type": "application/json", "cache-control": "no-store" } });

function limited(ip) {
  const now = Date.now(), arr = (hits.get(ip) || []).filter(t => now - t < 60_000);
  arr.push(now); hits.set(ip, arr);
  return arr.length > RATE_PER_MIN;
}

function makeLLM(apiKey, model) {
  return async (system, user, tool, maxTokens) => {
    const t0 = Date.now();
    let last;
    for (let attempt = 0; attempt < 2; attempt++) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 9000);
      try {
        const res = await fetch("https://api.anthropic.com/v1/messages", {
          method: "POST", signal: ctrl.signal,
          headers: { "content-type": "application/json", "x-api-key": apiKey, "anthropic-version": "2023-06-01" },
          body: JSON.stringify({ model, max_tokens: maxTokens, temperature: 0, system, messages: [{ role: "user", content: user }],
            tools: [tool], tool_choice: { type: "tool", name: tool.name } }),
        });
        if ([429, 500, 502, 503, 529].includes(res.status) && attempt === 0) { last = new Error(`HTTP ${res.status}`); continue; }
        if (!res.ok) throw new Error(`Anthropic API error ${res.status}`);
        const data = await res.json();
        let raw = "";
        for (const b of data.content || []) {
          if (b.type === "tool_use") { raw = JSON.stringify(b.input); break; }
          if (b.type === "text") raw = b.text || "";
        }
        const u = data.usage || {};
        return { raw, model: data.model || model, mode: "live", latency_ms: Date.now() - t0,
          input_tokens: u.input_tokens || 0, output_tokens: u.output_tokens || 0 };
      } catch (e) { last = e; if (attempt === 0 && e.name === "AbortError") continue; throw e; }
      finally { clearTimeout(timer); }
    }
    throw last;
  };
}

const QUAL = ["team_size", "current_solution", "timeline_months", "countries", "pain_points", "budget_signal"];
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

async function evalCase(llm, c) {
  if (c.kind === "reply_classification") {
    const i = c.input, e = c.expected;
    const r = await interpretReply(llm, i.reply_text, i.received_at.slice(0, 10), i.account_context);
    const x = r.extracted || {}, q = x.qualification || {}, qe = e.extracted.qualification;
    const fields = { interest_level: x.interest_level === e.extracted.interest_level,
      follow_up_date: x.follow_up_date === e.extracted.follow_up_date,
      referred_contact: same(x.referred_contact || null, e.extracted.referred_contact || null) };
    for (const k of QUAL) fields["qualification." + k] = Array.isArray(qe[k]) ? same([...(q[k] || [])].sort(), [...qe[k]].sort()) : q[k] === qe[k];
    return { case_id: c.case_id, kind: "reply", expected_label: e.label, label: r.label ?? null, label_correct: r.label === e.label,
      expected_action: e.action, action: r.action, action_correct: r.action === e.action,
      unsafe: (e.unsafe_actions || []).includes(r.action), verdict: r.verdict, violation_codes: r.violation_codes || [],
      confidence: r.confidence ?? null, fields_correct: Object.values(fields).filter(Boolean).length,
      fields_total: Object.keys(fields).length, field_misses: Object.keys(fields).filter(k => !fields[k]),
      latency_ms: r.attempts.reduce((s, a) => s + (a.latency_ms || 0), 0), raw_outputs: r.attempts.map(a => a.raw) };
  }
  const i = c.input, e = c.expected;
  const d = await composeDraft(llm, i.account, i.contact, i.facts, 1, "Valeria Montes", AS_OF);
  const cited = d.claims.map(x => x.fact_id);
  const grounded = cited.every(f => e.usable_fact_ids.includes(f));
  return { case_id: c.case_id, kind: "draft", expected_usable_fact_ids: e.usable_fact_ids, mode: d.mode, verdict: d.verdict,
    codes: d.codes, cited_fact_ids: cited, grounded, unsafe: !grounded, model_called: d.attempts.length > 0,
    subject: d.subject, body: d.body, latency_ms: d.attempts.reduce((s, a) => s + (a.latency_ms || 0), 0),
    raw_outputs: d.attempts.map(a => a.raw) };
}

export default async (req) => {
  if (req.method !== "POST") return json(405, { error: "POST only" });
  const raw = await req.text();
  if (raw.length > MAX_BODY) return json(413, { error: "Request too large" });
  let body;
  try { body = JSON.parse(raw); } catch { return json(400, { error: "Invalid JSON body" }); }
  const apiKey = process.env.ANTHROPIC_API_KEY;
  const model = (process.env.ORCH_MODEL || ORCH.default_model).trim();
  if (body.task === "status") return json(200, { configured: Boolean(apiKey), model, sends_email: false });
  const ip = req.headers.get("x-nf-client-connection-ip") || req.headers.get("x-forwarded-for") || "anon";
  if (limited(ip)) return json(429, { error: "Too many requests for this demo; wait a minute." });
  if (!apiKey) return json(503, { error: "ANTHROPIC_API_KEY is not configured on this site, so the live model is off. The recorded runs still work." });
  const llm = makeLLM(apiKey, model);
  try {
    if (body.task === "reply") {
      const text = typeof body.reply_text === "string" ? body.reply_text.slice(0, MAX_REPLY) : "";
      const crm_state = CRM_STATES.includes(body.crm_state) ? body.crm_state : "prospect";
      const date = /^\d{4}-\d{2}-\d{2}$/.test(body.received_date || "") ? body.received_date : AS_OF.slice(0, 10);
      const ctx = { company: String(body.company || "Dorada Demo").slice(0, 80), crm_state,
        last_touch_subject: "Spend management for " + String(body.company || "Dorada Demo").slice(0, 80) };
      const r = await interpretReply(llm, text, date, ctx);
      return json(200, { task: "reply", model, crm_state, received_date: date, ...r, email_sent: false });
    }
    const c = CASES.find(x => x.case_id === body.case_id);
    if (!c) return json(400, { error: "Unknown case_id" });
    if (body.task === "draft") {
      if (c.kind !== "personalization_grounding") return json(400, { error: "Not a personalization case" });
      let facts = c.input.facts;
      if (Array.isArray(body.facts)) {
        facts = body.facts.slice(0, MAX_FACTS).map((f, n) => ({
          fact_id: `fct_edit_${n + 1}`, account_id: c.input.account.account_id, type: "edited",
          text: String(f.text || "").slice(0, MAX_FACT_CHARS), source_name: "edited in demo", source_url: null,
          observed_at: /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(f.observed_at || "") ? f.observed_at : "2026-09-01T16:00:00Z",
          is_verified: f.is_verified === true, confidence: 0.9 }));
      }
      // The Live Demo can ask about the company on screen: only its display name, size and the contact's first name are taken, never a rule.
      const company = typeof body.company === "string" && body.company.trim() ? body.company.trim().slice(0, 80) : null;
      const employees = Number.isFinite(body.employees) && body.employees > 0 ? Math.round(body.employees) : null;
      const first = typeof body.contact_first_name === "string" && body.contact_first_name.trim() ? body.contact_first_name.trim().slice(0, 40) : null;
      const account = company ? { ...c.input.account, name: company, employee_count: employees ?? c.input.account.employee_count } : c.input.account;
      const contact = first ? { ...c.input.contact, first_name: first } : c.input.contact;
      const d = await composeDraft(llm, account, contact, facts, 1, "Valeria Montes", AS_OF);
      return json(200, { task: "draft", model, case_id: c.case_id, facts, ...d, email_sent: false,
        note: "Display only: this function has no email client." });
    }
    if (body.task === "eval_case") return json(200, { task: "eval_case", model, ...(await evalCase(llm, c)) });
    return json(400, { error: "Unknown task" });
  } catch (err) {
    return json(502, { error: String(err && err.message || err).slice(0, 300) });
  }
};
