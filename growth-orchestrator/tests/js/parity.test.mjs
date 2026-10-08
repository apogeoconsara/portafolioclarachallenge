// Parity of the JS validators (netlify/functions/_orchestrator_ai.mjs) with the Python ones, over all 220 recorded
// model outputs and the 4 personalization cases. Python matches the same expectations (tests/test_ai_and_rules.py).
// Run: node growth-orchestrator/tests/js/parity.test.mjs
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { validateReply, validateDraft, usableFacts, finalAction } from "../../../netlify/functions/_orchestrator_ai.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const seed = join(here, "..", "..", "data", "seed");
const jsonl = p => readFileSync(p, "utf8").split("\n").filter(Boolean).map(JSON.parse);
const cases = Object.fromEntries(jsonl(join(seed, "eval_cases.jsonl")).map(c => [c.case_id, c]));
const AS_OF = "2026-10-01T16:00:00Z";
let pass = 0, fail = 0;
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
for (const r of jsonl(join(seed, "llm_recordings.jsonl"))) {
  const c = cases[r.case_id];
  let v;
  if (r.kind === "reply_interpretation") {
    v = validateReply(r.model_output_raw, c.input.reply_text, c.input.received_at.slice(0, 10));
    if (v.usable) {
      const act = finalAction(v.final_action, v.label, c.input.account_context.crm_state);
      if ((c.expected.unsafe_actions || []).includes(act) !== Boolean(r.expected.final_action_unsafe)) { fail++; console.log("UNSAFE MISMATCH", r.recording_id); continue; }
    }
  } else {
    const usable = new Set(usableFacts(c.input.facts, c.input.account, AS_OF).map(f => f.fact_id));
    v = validateDraft(r.model_output_raw, c.input.facts, usable, c.input.contact.language);
  }
  if (v.verdict === r.expected.verdict && eq([...v.codes].sort(), [...r.expected.violation_codes].sort())) pass++;
  else { fail++; console.log("MISMATCH", r.recording_id, v.verdict, v.codes, "expected", r.expected.verdict, r.expected.violation_codes); }
}
for (const c of Object.values(cases).filter(c => c.kind === "personalization_grounding")) {
  const got = usableFacts(c.input.facts, c.input.account, AS_OF).map(f => f.fact_id);
  if (eq(got, c.expected.usable_fact_ids)) pass++; else { fail++; console.log("USABLE MISMATCH", c.case_id, got); }
}
console.log(`parity: ${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
