// Switching tabs or reloading must not lose what a person did on the page. Needs a browser: skipped when playwright-core
// (or Chromium) is not available, unless REQUIRE_BROWSER=1 (set in CI), where a missing browser is a failure.
// Run from the repo root: node growth-orchestrator/tests/js/ui_persistence.test.mjs
import http from "node:http";
import { readFileSync, existsSync } from "node:fs";
import { extname, join } from "node:path";
import assert from "node:assert/strict";

const skip = why => { console.log("skipped: " + why); process.exit(process.env.REQUIRE_BROWSER === "1" ? 1 : 0); };
let chromium;
try { ({ chromium } = await import("playwright-core")); } catch { skip("playwright-core not installed"); }
const exe = process.env.CHROMIUM_PATH || ["/opt/pw-browsers/chromium", "/usr/bin/chromium", "/usr/bin/chromium-browser"].find(existsSync);
if (!exe) skip("no Chromium found");

const types = { ".html": "text/html", ".json": "application/json", ".js": "text/javascript" };
const server = http.createServer((req, res) => {
  const f = join("public", req.url.split("?")[0] === "/" ? "index.html" : req.url.split("?")[0]);
  if (!existsSync(f)) { res.writeHead(404); return res.end(); }
  res.writeHead(200, { "content-type": types[extname(f)] || "text/plain" }); res.end(readFileSync(f));
}).listen(0);
const base = `http://localhost:${server.address().port}/`;

const browser = await chromium.launch({ executablePath: exe, args: ["--no-sandbox"] });
const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
const errors = []; page.on("pageerror", e => errors.push(e.message));
const go = async v => { await page.click(`#nav button[data-v="${v}"]`); await page.waitForTimeout(250); };
const goSec = async (group, sec) => { await go(group); await page.click(`[data-sec="${sec}"]`); await page.waitForTimeout(250); };
const text = sel => page.$eval(sel, e => e.innerText);

await page.goto(base + "#run"); await page.waitForSelector("#leadList tr.row");

// 1. Live Demo opens on the twelve leads, four per group; an open lead page survives a tab change and a reload
assert.equal((await page.$$("#leadList tr.row")).length, 12);
for (const t of ["A", "B", "C"]) assert.equal((await page.$$(`#leadList .tchip${t}`)).length, 4, `four leads in group ${t}`);
await page.click('#leadList tr.row[data-i="2"]');
assert.match(await text("#leadPage"), /Score \(ICP\)[\s\S]*Routing decision[\s\S]*CRM state/i);
await go("overview"); await go("run");
assert.match(await text("#leadPage"), /Routing decision/i, "the open lead was lost after switching tabs");
await page.reload(); await page.waitForSelector("#leadPage .back");
assert.match(await text("#leadPage"), /Routing decision/i, "the open lead was lost after a reload");
await page.click("#leadBack");

// 2. Scenarios: every one draws its company, its score and its journey; the animation runs on its own and is instant the second time
await page.click('#runTabs [data-rt="scn"]');
assert.equal((await page.$$("#scnPick .pill")).length, 16);
await page.click('#scnPick .pill[data-id="F4"]');
await page.waitForFunction(() => document.querySelectorAll("#scnJr .st.on").length === document.querySelectorAll("#scnJr .st").length, null, { timeout: 12000 });
assert.match(await text("#scnJr"), /503 temporary error[\s\S]*200 ok/, "the retry with the same key should be visible");
assert.match(await text("#scnCo"), /Facts known/);
await go("operations"); await go("run");
assert.equal(await page.$$eval("#scnJr .st:not(.on)", e => e.length), 0, "a scenario already played should show at once");

// 3. each scenario has a live Claude panel: a reply for the reply scenarios, an opening line for the others
const liveReply = { task: "reply", model: "m", crm_state: "prospect", received_date: "2026-10-01", label: "not_now", confidence: 0.9, verdict: "accept", violation_codes: [], action: "wait",
  reason_codes: [], email_sent: false, attempts: [], extracted: { interest_level: "low", follow_up_date: null, referred_contact: null, qualification: {} } };
const liveDraft = { task: "draft", mode: "personalized", verdict: "accept", codes: [], usable_fact_ids: ["fct_edit_1"], attempts: [], claims: [{ fact_id: "fct_edit_1", text: "x" }], subject: "s", body: "Hi,\n\nI saw that x\n\nSTOP" };
const seenBodies = [];
await page.route("**/.netlify/functions/**", r => { const d = JSON.parse(r.request().postData() || "{}"); seenBodies.push(d);
  r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(d.task === "reply" ? liveReply : d.task === "draft" ? liveDraft : { configured: true, model: "m" }) }); });
await page.click('#scnPick .pill[data-id="F5"]'); await page.waitForSelector("#lvText");
await page.click("#lvGo"); await page.waitForSelector("#lvOut .rhero");
assert.equal(seenBodies.at(-1).task, "reply"); assert.match(seenBodies.at(-1).reply_text, /Let's see/);
await page.click('#scnPick .pill[data-id="F10"]'); await page.waitForSelector("#lvGo");
await page.click("#lvGo"); await page.waitForSelector("#lvOut .aipart");
const sentDraft = seenBodies.at(-1); assert.equal(sentDraft.task, "draft"); assert.ok(sentDraft.company && sentDraft.facts.length > 1, "the live draft carries the company on screen and its facts");
await page.reload(); await page.waitForSelector("#scnPick .pill");
assert.match(await text("#scnCo"), /Facts known/, "the open scenario tab was lost after a reload");
await page.unroute("**/.netlify/functions/**");

// 4. Architecture keeps the open scenario list
await go("flows"); await page.click("#moreCases > summary"); await page.click("tr.click");
await go("overview"); await go("flows");
assert.ok(await page.$eval("#detail", e => e.innerText.length > 50), "the opened scenario was closed by switching tabs");

// 5. weights, account choice and typed reply survive a reload
await goSec("decisions", "priority"); await page.fill("#w_size", "41"); await page.reload(); await page.waitForSelector("#w_size");
assert.equal(await page.inputValue("#w_size"), "41", "edited weight lost on reload");
await goSec("decisions", "account"); const id = await page.$eval("#audSel option:nth-child(7)", o => o.value); await page.selectOption("#audSel", id);
await go("overview"); await goSec("decisions", "account");
assert.equal(await page.inputValue("#audSel"), id, "chosen account lost after switching tabs");
await goSec("aisafety", "live"); await page.fill("#reply", "Please call me next week"); await go("overview"); await go("aisafety");
assert.equal(await page.inputValue("#reply"), "Please call me next week", "typed reply lost after switching tabs");

// 6. a trace section you opened stays open
await go("flows");
if (!(await page.$eval("#moreCases", d => d.open))) await page.click("#moreCases > summary");   // "More cases" (it is already open: that is the point)
await page.click("tr.click");                                           // open a scenario
await page.waitForSelector("#detail details summary");
const audit = (await page.$$("#detail details")).find(Boolean);
const summaryText = await audit.$eval("summary", e => e.textContent);
await audit.$eval("summary", e => e.click());
const openBefore = await audit.evaluate(d => d.open);
await go("overview"); await go("flows");
const again = (await page.$$("#detail details"))[0];
assert.equal(await again.evaluate(d => d.open), openBefore, `"${summaryText}" changed state after switching tabs`);

// 6b. approval decisions survive tab changes and a reload, and are reflected on the Command Center
await go("approvals"); await page.waitForSelector("#apApprove");
await page.click("#apApprove"); await page.selectOption("#apReason", "Tone or wording"); await page.click("#apReject");
await go("overview"); assert.match(await text("#main"), /Approval queue: 1 approved, 1 rejected/);
await page.reload(); await go("approvals"); assert.match(await text("#apKpi"), /1 \/ 1 \/ 198/, "approval decisions lost");

// 6c. a group reopens on the section you were in
await goSec("aisafety", "evals"); await go("overview"); await go("aisafety");
assert.ok(await page.$eval('[data-sec="evals"]', b => b.classList.contains("active")), "the group did not reopen on its last section");

// 7. Reset demo clears what you changed
await goSec("decisions", "priority"); await page.fill("#w_size", "44");
page.once("dialog", d => d.accept()); await page.click("#resetDemo"); await page.waitForSelector("#main h1");
await goSec("decisions", "priority");
assert.equal(await page.inputValue("#w_size"), "30", "Reset demo did not restore the default weight");
await go("approvals"); await page.waitForSelector("#apApprove"); assert.match(await text("#apKpi"), /0 \/ 0 \/ 200/, "Reset demo did not clear the approvals");

// 8. with storage blocked the page still keeps your changes while the tab is open, and says so
// 9. Measuring impact: the replay starts on its own, ends on the simulation's numbers, and survives switching tabs
await go("run"); await go("overview"); await page.waitForSelector("#miPlay");
await page.waitForTimeout(4200);                                   // the dots take about 2.4 s to split before the clock moves
assert.notEqual(await text("#miClock"), "Day 0", "the replay did not start by itself");
await go("run"); await go("overview"); await page.click("#miSkip");
assert.ok(await page.$eval("#miResult", e => e.classList.contains("show")), "the comparison is not shown at the end of the replay");
const ms = JSON.parse(readFileSync(join("public", "data", "measurement.json"), "utf8"));
assert.equal(await text("#mi_a_p"), "$" + ms.pipeline.control.toLocaleString("en-US"));
assert.equal(await text("#mi_b_p"), "$" + ms.pipeline.treatment.toLocaleString("en-US"));
assert.match(await text("#miPlay"), /about USD 20,000 per 1,000 companies/);

const blocked = await browser.newPage({ viewport: { width: 1280, height: 900 } });
await blocked.addInitScript(() => { Object.defineProperty(window, "localStorage", { get() { throw new Error("blocked"); } }); });
const berrors = []; blocked.on("pageerror", e => berrors.push(e.message));
await blocked.goto(base + "#priority"); await blocked.waitForSelector("#w_size");
assert.match(await blocked.$eval("#main", e => e.innerText), /blocking site storage/);
await blocked.fill("#w_size", "37");
await blocked.click('#nav button[data-v="overview"]'); await blocked.waitForTimeout(250); await blocked.click('#nav button[data-v="decisions"]'); await blocked.waitForSelector("#w_size");
assert.equal(await blocked.inputValue("#w_size"), "37", "with storage blocked, an edit was lost on a tab change");
assert.deepEqual(berrors, []);

assert.deepEqual(errors, []);
console.log("ui persistence: ok");
// 10. saved weights equal to the engine's are not a change: B still opens on the official proposal, and the change log is visible
await page.evaluate(() => { const v = JSON.parse(localStorage.getItem("orch.weights") || "{}"); localStorage.setItem("orch.weights", JSON.stringify({ ...v, size: 30 })); });
await page.goto(base + "#decisions"); await page.reload(); await goSec("decisions", "priority");
assert.match(await text("#priVer"), /change group/);
assert.doesNotMatch(await text("#priVer"), /\b0 of [\d,]+ ready companies/, "B opened on equal edits instead of the proposal");
assert.match(await text("#priVer"), /Change log/);

// 11. Live demo: a "wait" result shows the next check date (the stated date, or 7 days after the reply) with the technical detail folded away
const reply = extra => ({ task: "reply", model: "m", crm_state: "prospect", received_date: "2026-10-01", label: "not_now", confidence: 0.95, verdict: "accept", violation_codes: [], action: "wait",
  reason_codes: [], email_sent: false, attempts: [], extracted: { interest_level: "medium", follow_up_date: "2026-11-15", referred_contact: null, qualification: {}, ...extra } });
let replyBody = reply({});
await page.route("**/.netlify/functions/**", r => r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(r.request().postData()?.includes('"reply"') ? replyBody : { configured: true, model: "m" }) }));
await goSec("aisafety", "live"); await page.click("#runReply"); await page.waitForSelector(".rnext");
assert.match(await text(".rnext"), /15 November 2026/);
replyBody = reply({ follow_up_date: null }); await page.click("#runReply"); await page.waitForTimeout(400);
assert.match(await text(".rnext"), /8 October 2026[\s\S]*default of 7 days/);
assert.equal(await page.$eval("#replyOut details", e => e.open), false, "technical details should start folded");

// 12. Evaluation: all 18 live cases are shown as cards, each with the result of the last saved run
await goSec("aisafety", "evals"); await page.waitForSelector(".ccase");
assert.equal((await page.$$(".ccase")).length, 18, "the 18 evaluation cases should all be listed");
assert.ok((await page.$$(".ccase.pass")).length > 0, "cards should carry the saved run's result");

// 13. Personalized opening line: each fact says whether the rules allowed it, and the AI's one line is shown apart from the template
const draftReply = { task: "draft", mode: "personalized", verdict: "accept", codes: [], usable_fact_ids: ["fct_edit_2", "fct_edit_3"], attempts: [],
  claims: [{ fact_id: "fct_edit_2", text: "Dorada 80 posted 12 openings." }], subject: "Spend management", body: "Hi,\n\nI saw that Dorada 80 posted 12 openings.\n\nreply STOP" };
await page.unroute("**/.netlify/functions/**");
await page.route("**/.netlify/functions/**", r => r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(r.request().postData()?.includes('"draft"') ? draftReply : { configured: true, model: "m" }) }));
await goSec("aisafety", "live"); await page.uncheck('[data-fv="0"]'); await page.click("#runDraft"); await page.waitForSelector(".aipart");
assert.match(await text(".aipart"), /only part the AI wrote/);
const stats = await page.$$eval(".fstat", e => e.map(x => x.innerText));
assert.match(stats[0], /not verified/); assert.match(stats[1], /Allowed/);

// 14. Command Center: the month runs on its own, ends on totals that add up to every company, and "Run again" starts it again
await go("overview");
await page.waitForFunction(() => { const b = document.querySelector("#rmGo"); return b && !b.disabled; }, null, { timeout: 40000 });
assert.equal(await text("#rmGo"), "↻ Run again");
const num = id => text(id).then(t => Number(t.replace(/,/g, "")));
assert.equal(await num("#rnA") + await num("#rnP") + await num("#rnB"), 50000, "the three groups should add up to every company");
assert.equal(await num("#evN"), 55959); assert.ok(await num("#evD") > 0 && await num("#evR") > 0 && await num("#evX") > 0);
await page.click("#rmGo"); assert.equal(await text("#rmGo"), "Running…");
assert.equal(await page.$eval("#rmGo", b => b.disabled), true);

await browser.close(); server.close();
