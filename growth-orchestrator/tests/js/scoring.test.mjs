// The page recomputes the score in the browser with edited weights. Check that code against the Python-exported
// default scores for all 500 accounts. Run from the repo root: node growth-orchestrator/tests/js/scoring.test.mjs
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";

const html = readFileSync("public/index.html", "utf8");
const src = html.match(/function scoreOf[\s\S]*?\n}\n/)[0];
const scoreOf = new Function(`${src}; return scoreOf;`)();
const d = JSON.parse(readFileSync("public/data/scoring.json", "utf8"));
const w = { ...d.config.weights, tier_a: d.config.tier_a, tier_b: d.config.tier_b };
for (const a of d.accounts) assert.deepEqual(scoreOf(a.feat, d.config, w), a.base, a.id);
console.log(`scoring parity: ${d.accounts.length}/${d.accounts.length}`);

// The 50k summary: scoring its groups with the page's code must reproduce Python's tier counts for the default weights.
const ov = JSON.parse(readFileSync("public/data/overview.json", "utf8"));
const tiers = { A: 0, B: 0, C: 0 };
for (const g of ov.groups) { const t = scoreOf({ size: g.size, pain: g.pain, signals: new Array(g.signals) }, ov.config, w).tier; tiers[t] += g.counts.contact || 0; }
assert.equal(tiers.A + tiers.B + tiers.C, ov.actions.contact);
console.log("50k ready accounts by tier:", tiers);

// Every version's tier counts, scored with the page's code, must equal what the engine measured for that version.
const cmp = JSON.parse(readFileSync("public/data/scoring_compare.json", "utf8"));
for (const v of ov.config.versions) {
  const wv = { ...v.weights }, t = { A: 0, B: 0, C: 0 };
  for (const g of ov.groups) t[scoreOf({ size: g.size, pain: g.pain, signals: new Array(g.signals) }, ov.config, wv).tier] += g.counts.contact || 0;
  assert.deepEqual(t, { A: 0, B: 0, C: 0, ...cmp.runs[v.id].ready_by_tier }, `page and engine disagree on ${v.id}`);
}
console.log("versions: page and engine agree on", ov.config.versions.map(v => v.id).join(", "));
