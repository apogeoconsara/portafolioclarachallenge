"""Alignment of the data with the challenge: policy, templates, AI contracts + recorded outputs, routing,
send windows, experiment design, demo script, traceability, and a guard that no real company ever returns.

Run:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from collections import Counter
from datetime import timedelta
from pathlib import Path

from generator import ai_ref, golden, impact, llm_fixtures, oracle, policy_data
from generator.build import build_world
from generator.config import AS_OF
from generator.demo import ASSUMPTION_DRILLS, DEMO_FLOWS
from generator.replies import resolve
from generator.reply_seeds import CORE_IDS, LABEL_ACTION, SEEDS
from generator.util import parse_iso, read_jsonl

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent
SEED = ROOT / "data" / "seed"
CASES = golden.eval_cases()
RECS = llm_fixtures.build_recordings(CASES)


class Recordings(unittest.TestCase):
    def test_recordings_match_the_reference_validator(self):
        by = {c["case_id"]: c for c in CASES}
        bad = []
        for r in RECS:
            c, e = by[r["case_id"]], r["expected"]
            if r["kind"] == "reply_interpretation":
                v = ai_ref.validate_reply(r["model_output_raw"], c["input"]["reply_text"], parse_iso(c["input"]["received_at"]))
                got, want = (v["verdict"], v["violation_codes"], v["final_action"], v["warnings"]), \
                            (e["verdict"], e["violation_codes"], e["final_action"], e["warnings"])
            else:
                v = ai_ref.validate_draft(r["model_output_raw"], c["input"]["facts"], set(c["expected"]["usable_fact_ids"]),
                                          c["input"]["contact"]["language"])
                got, want = (v["verdict"], v["violation_codes"], v["template_mode"]), \
                            (e["verdict"], e["violation_codes"], e["template_mode"])
            if got != want:
                bad.append((r["recording_id"], got, want))
        self.assertEqual(bad, [])

    def test_recording_files_are_in_sync_and_ids_unique(self):
        ids = [r["recording_id"] for r in RECS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(read_jsonl(SEED / "llm_recordings.jsonl"), json.loads(json.dumps(RECS)))
        self.assertEqual(json.loads((SEED / "ai_schemas.json").read_text()), json.loads(json.dumps(llm_fixtures.schemas_doc())))

    def test_every_case_has_a_correct_output_and_defects(self):
        for c in CASES:
            rows = [r for r in RECS if r["case_id"] == c["case_id"]]
            self.assertTrue(any(r["variant"] == "good" for r in rows), c["case_id"])
            self.assertGreaterEqual(len(rows), 8, c["case_id"])

    def test_every_verdict_and_defect_family_is_represented(self):
        verdicts = {r["expected"]["verdict"] for r in RECS}
        self.assertEqual(verdicts, {"accept", "accept_with_warning", "reject_retry", "reject_escalate",
                                    "escalate_low_confidence", "override_rule", "fallback_generic"})
        variants = {r["variant"].split(":")[0] for r in RECS}
        for needed in ("truncated_json", "prose_not_json", "empty_output", "missing_field", "bad_enum",
                       "confidence_out_of_range", "evidence_hallucinated", "hallucinated_referral_email", "date_in_past",
                       "date_invented", "qualification_hallucinated", "obeys_injection", "overconfident_wrong_label",
                       "forbidden_claim", "missing_footer", "fact_id_hallucinated", "unsupported_detail",
                       "uses_unusable_fact", "invented_fact_when_none"):
            self.assertIn(needed, variants)

    def test_only_plausible_but_wrong_outputs_can_be_unsafe(self):
        """A validator can stop everything except plausible, schema-valid, wrong answers; those are flagged honestly."""
        unsafe = [r["variant"] for r in RECS if r["expected"].get("final_action_unsafe")]
        self.assertTrue(unsafe)
        self.assertTrue(all(v.startswith("wrong_but_valid") for v in unsafe), unsafe)
        for r in RECS:
            if r["variant"].startswith("wrong_but_valid"):
                self.assertFalse(r["expected"]["model_output_correct"])
                self.assertIn("note", r["expected"])

    def test_opt_out_guard_beats_a_confident_model(self):
        r = next(r for r in RECS if r["variant"] == "overconfident_wrong_label")
        out = json.loads(r["model_output_raw"])
        self.assertGreater(out["confidence"], 0.9)
        self.assertEqual(r["expected"]["final_action"], "suppress")
        self.assertFalse(r["expected"]["final_action_unsafe"])

    def test_model_suggested_action_is_never_trusted(self):
        r = next(r for r in RECS if r["variant"] == "action_label_mismatch")
        self.assertEqual(json.loads(r["model_output_raw"])["suggested_action"], "contact")
        self.assertEqual(r["expected"]["final_action"], "suppress")

    def test_every_seed_annotation_is_supported_by_its_text(self):
        """Run the support checks over all 117 seeds: evidence, referral, dates and qualification must be in the text."""
        received = AS_OF + timedelta(days=9)
        ref = ("Mariana Beltran", "mariana.beltran@group-example.mx.example")
        for s in SEEDS:
            text, dates, hn, he = resolve(s, received, ref)
            good = {"label": s["label"], "confidence": 0.95, "interest_level": "high" if s["label"] == "interested" else "none",
                    "follow_up_date": dates[-1].date().isoformat() if dates else None,
                    "referred_contact": {"name": ref[0] if hn else None, "email": ref[1] if he else None} if (hn or he) else None,
                    "qualification": s["qualification"], "suggested_action": LABEL_ACTION[s["label"]],
                    "needs_human_review": False, "evidence": llm_fixtures._evidence(text)}
            v = ai_ref.validate_reply(json.dumps(good, ensure_ascii=False), text, received)
            self.assertNotIn(v["verdict"], ("reject_retry", "reject_escalate"), (s["seed_id"], v))

    def test_qualification_trap_is_present_and_null(self):
        trap = next(s for s in SEEDS if s["seed_id"] == "R-INT-18")
        self.assertIn("12 companies", trap["text"])
        self.assertIsNone(trap["qualification"]["team_size"])

    def test_core_suite_size(self):
        core = [c for c in CASES if c["kind"] == "reply_classification" and c["suite"] == "core"]
        self.assertTrue(6 <= len(core) <= 10)
        self.assertEqual({c["case_id"] for c in core}, {f"EV-{i}" for i in CORE_IDS})


class Policy(unittest.TestCase):
    def test_policy_file_in_sync(self):
        self.assertEqual(json.loads((SEED / "send_policy.json").read_text()), json.loads(json.dumps(policy_data.SEND_POLICY)))

    def test_forbidden_claim_regexes_catch_examples_and_spare_approved_copy(self):
        examples = {"guaranteed_approval": "guaranteed approval in 24 hours", "zero_fee": "0% commission",
                    "superlative": "we are the best in the market", "savings_pct": "save up to 30%",
                    "competitor_comparison": "better than Otherbank", "regulatory_claim": "regulated by the central bank"}
        rules = {r["id"]: r["regex"] for r in policy_data.SEND_POLICY["content_rules"]["forbidden_claims"]}
        self.assertEqual(set(examples), set(rules))
        for k, text in examples.items():
            self.assertRegex(text, re.compile(rules[k], re.I), k)
        for t in policy_data.templates():
            for rx in rules.values():
                self.assertNotRegex(t["subject"] + t["body"], re.compile(rx, re.I), t["template_id"])

    def test_templates_cover_every_step_and_language(self):
        tpls = policy_data.templates()
        self.assertEqual({(t["language"], t["step"]) for t in tpls}, {("en", s) for s in (1, 2, 3, 4)})
        self.assertEqual(read_jsonl(SEED / "outreach_templates.jsonl"), json.loads(json.dumps(tpls)))
        for t in tpls:
            subject, body = policy_data.render_template(t["language"], t["step"], "Lucia", "Dorada", "Valeria Montes", "")
            self.assertNotRegex(subject + body, r"[{}]")
            self.assertIn(policy_data.UNSUBSCRIBE_FOOTER[t["language"]], body)
            self.assertLessEqual(len(body.split()), policy_data.SEND_POLICY["content_rules"]["max_body_words"])
            self.assertLessEqual(len(subject), policy_data.SEND_POLICY["content_rules"]["max_subject_chars"])

    def test_generic_template_passes_the_draft_validator(self):
        subject, body = policy_data.render_template("en", 1, "Lucia", "Dorada", "Valeria Montes", "")
        raw = json.dumps({"language": "en", "subject": subject, "body": body, "claims": [], "personalized": False})
        self.assertEqual(ai_ref.validate_draft(raw, [], set(), "en")["verdict"], "accept")

    def test_send_windows_match_the_reference_and_golden(self):
        for gid, country in (("G090", "MX"), ("G091", "MX"), ("G092", "BR")):
            g = next(x for x in golden.GOLDEN if x["id"] == gid)
            got = policy_data.next_send_time(parse_iso(g["events"][0]["received_at"]), country).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.assertEqual(got, g["expected"][0]["send_after"], gid)
        g = next(x for x in golden.GOLDEN if x["id"] == "G093")
        rs = g["state"]["runtime_state"]
        self.assertGreaterEqual(rs["sent_today"], rs["daily_send_cap_total"])
        # cap reached today -> the next day's window start (local midnight tomorrow, then the first window instant)
        off = timedelta(hours=policy_data.UTC_OFFSET["MX"])
        local = parse_iso(g["events"][0]["received_at"]) + off
        tomorrow = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0) - off
        self.assertEqual(policy_data.next_send_time(tomorrow, "MX").strftime("%Y-%m-%dT%H:%M:%SZ"),
                         g["expected"][0]["send_after"])

    def test_ai_autonomy_has_the_gates(self):
        a = policy_data.SEND_POLICY["ai_autonomy"]
        self.assertEqual(a["confidence_threshold_auto_act"], ai_ref.CONFIDENCE_MIN)
        self.assertEqual(a["human_sampling"]["initial_review_rate"], 1.0)
        self.assertTrue(any("opt-out" in x for x in a["forbidden_decisions"]))
        self.assertTrue(any("instructions found inside" in x for x in a["forbidden_decisions"]))


class Routing(unittest.TestCase):
    def test_golden_routing_matches_the_oracle(self):
        n = 0
        for g in golden.GOLDEN:
            if "aes" not in g["state"]:
                continue
            st, e = g["state"], g["expected"][0]
            lang = st["contacts"][0]["language"]
            self.assertEqual(oracle.route_ae(st["accounts"][0], lang, st["aes"]), (e["route_to_ae_id"], e["route_reason"]), g["id"])
            n += 1
        self.assertGreaterEqual(n, 5)

    def test_oracle_with_aes_matches_goldens(self):
        for g in golden.GOLDEN:
            if g["oracle"] and "aes" in g["state"]:
                st, e = g["state"], g["expected"][0]
                idx = oracle.Index(st["accounts"], st["contacts"], st["opportunities"], st["outreach_history"], st["suppression"])
                d = oracle.decide(st["accounts"][0], idx, aes=st["aes"])
                self.assertEqual((d["action"], d["reason_codes"]), (e["action"], e["reason_codes"]), g["id"])

    def test_world_has_leave_inactive_and_full_aes(self):
        w = build_world(42, 6000)
        self.assertTrue(any(not a["active"] for a in w.aes))
        self.assertTrue(any(a["out_of_office_until"] for a in w.aes))
        self.assertTrue(any(a["open_accounts"] >= a["max_open_accounts"] for a in w.aes if a["active"]))
        self.assertTrue(all(set(a["languages"]) == {"en"} for a in w.aes))
        reasons = {t["route_reason"] for t in w.truth_accounts if t["route_reason"]}
        self.assertIn("OWNER_BACKUP", reasons)


class Experiment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.w = build_world(42, 4000)

    def test_domain_clusters_share_an_arm(self):
        arm = {r["account_id"]: r["arm"] for r in self.w.arms}
        by = {}
        for a in self.w.accounts:
            by.setdefault(a["domain"], set()).add(arm[a["account_id"]])
        dup = [d for d, v in by.items() if sum(1 for a in self.w.accounts if a["domain"] == d) > 1]
        self.assertTrue(dup)
        self.assertTrue(all(len(by[d]) == 1 for d in dup))

    def test_stratified_balance(self):
        by = {}
        for r in self.w.arms:
            by.setdefault(r["stratum"], [0, 0])[r["arm"] == "treatment"] += 1
        self.assertLessEqual(max(abs(a - b) for a, b in by.values()), 12)

    def test_treatment_never_violates_policy_and_control_sometimes_does(self):
        self.assertEqual(sum(r["violation"] for r in self.w.sim if r["arm"] == "treatment"), 0)
        self.assertGreater(sum(r["violation"] for r in self.w.sim if r["arm"] == "control"), 0)
        self.assertTrue(all(r["simulated"] for r in self.w.sim))

    def test_ineligible_accounts_are_only_contacted_by_mistake(self):
        for r in self.w.sim:
            if r["contacted"] and not r["eligible"]:
                self.assertTrue(r["violation"])

    def test_exact_test_behaves(self):
        self.assertAlmostEqual(impact.exact_rate_p_value(15, 25000, 15, 25000), 1.0)
        self.assertLess(impact.exact_rate_p_value(2, 25000, 30, 25000), 0.001)
        self.assertEqual(impact.exact_rate_p_value(0, 100, 0, 100), 1.0)
        self.assertGreater(impact.sample_size_two_proportions(0.001, 0.0011), impact.sample_size_two_proportions(0.001, 0.002))

    def test_aa_has_no_systematic_effect(self):
        acc, tr, arms = self.w.accounts, self.w.truth_accounts, self.w.arms
        diffs = []
        for k in range(6):
            sim = impact.simulate_outcomes(100 + k, acc, tr, arms, effect={})
            t = [r for r in sim if r["arm"] == "treatment"]
            c = [r for r in sim if r["arm"] == "control"]
            diffs.append(impact.per_1000(t, "contacted") - impact.per_1000(c, "contacted"))
        self.assertLess(abs(sum(diffs) / len(diffs)), 25)   # per 1,000 accounts; noise only


class DemoAndTraceability(unittest.TestCase):
    def test_demo_flows_reference_real_goldens_and_recordings(self):
        gids = {g["id"] for g in golden.GOLDEN}
        rids = {r["recording_id"] for r in RECS}
        for f in DEMO_FLOWS:
            self.assertTrue(set(f["golden"]) <= gids, f["id"])
            self.assertTrue(set(f["recordings"]) <= rids, (f["id"], set(f["recordings"]) - rids))
        needed = {"at least one successful flow", "one duplicate event", "one realistic failure scenario", "one ambiguous or unsafe AI case"}
        self.assertTrue(needed <= {f["requirement"] for f in DEMO_FLOWS})
        self.assertGreaterEqual(len(ASSUMPTION_DRILLS), 4)
        self.assertEqual(json.loads((SEED / "demo_flows.json").read_text()),
                         json.loads(json.dumps({"flows": DEMO_FLOWS, "assumption_drills": ASSUMPTION_DRILLS})))

    def test_traceability_references_exist(self):
        text = (ROOT / "data" / "TRACEABILITY.md").read_text(encoding="utf-8")
        gids = {g["id"] for g in golden.GOLDEN}
        rids = {r["recording_id"] for r in RECS}
        sample_tables = {"events.jsonl", "aes.jsonl", "ae_calendar.jsonl"}
        checked = 0
        for tok in re.findall(r"`([^`]+)`", text):
            if re.fullmatch(r"G\d{3}", tok):
                self.assertIn(tok, gids, tok)
            elif re.fullmatch(r"EV-[\w-]+:[\w:-]+", tok):
                self.assertIn(tok, rids, tok)
            elif re.match(r"(data|generator|tests)/", tok):
                path = tok.split(" ")[0].split("::")[0]
                if path.startswith("data/generated/"):
                    continue          # regenerated, not in git
                self.assertTrue((ROOT / path).exists(), tok)
            elif re.fullmatch(r"G\d{3}`…`G\d{3}", tok):
                pass
            checked += 1
        self.assertGreater(checked, 60)
        # ranges like `G001`…`G034` : both ends must exist
        for a, b in re.findall(r"`(G\d{3})`…`(G\d{3})`", text):
            self.assertIn(a, gids)
            self.assertIn(b, gids)


class CsvExport(unittest.TestCase):
    def test_csv_row_counts_match_jsonl_and_open_in_excel_encoding(self):
        import csv
        from generator.export import export_csv, write_world
        with tempfile.TemporaryDirectory() as t:
            src = Path(t) / "w"
            write_world(build_world(42, 300), src, 42, 300, sqlite=False)
            counts = export_csv(src, src / "csv")
            self.assertIn("accounts.csv", counts)
            self.assertIn("truth/truth_accounts.csv", counts)
            raw = (src / "csv" / "accounts.csv").read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))   # BOM for Excel
            with (src / "csv" / "accounts.csv").open(encoding="utf-8-sig", newline="") as fh:
                self.assertEqual(sum(1 for _ in csv.DictReader(fh)), 300)


class NoRealCompanies(unittest.TestCase):
    """The project works only with synthetic data. The names of companies and third parties that an earlier demo used must
    never appear anywhere in the repository. They are stored as SHA-256 hashes of 1 to 3 word phrases (lowercase, letters and
    digits only), so this test does not list them, and it compares every phrase of every text file against the hashes."""
    FORBIDDEN_HASHES = {
        "0fb16c002893d8354e38c962e6602dc33c04f08bfd2144d6d37231d11fbff41a",
        "334c70cf9e86865a32f3150de1c0dab547a2a8fd1dd946727defa64bbde3dcf5",
        "34293a14de994d3c8146201f6385c0c1c9f2a7c0e8c6c5e039d89d667ecf21fc",
        "3ad648648a56dd77020f8dc94e96f06c7aaf6353805ef51dea0e854c4dfeb75f",
        "44cbb5e6d84f9c3d466de1e4a53757242f724b57af9fe0dc42384c038c05fd9e",
        "48fd4b29455edc5b5fefb6ca8adccfd19c3c1f04a719e37e0d718d5c28980059",
        "4a401d7df8e46e9ca204146d3ee9dc87fcd82276404148f2c71eb6abebf8538e",
        "7f870b0e69c4235084b4c352b183d978e17b9fde1924cbe021ecb1814a0171c3",
        "8e9565ed27da96dc3839981eeb54f815df4e4dbef618862500306a8dc3b52c32",
        "a1b1ef8bf0523f328c61edd7562d8a5814f7aefe7bd082d27468b6273c0adb17",
        "abd5e40465991219f939ea8f695dd80883e36c30c3815819e02e44627b4bbe03",
        "ae90842a1d9baa748f2bbea7d575b435a3616597a062feb7d5d0a33b51dafec7",
        "bca615497484c9608be069887be0aa1415b36f8ed48cc848b78b63a1a8e21b46",
        "be8017ba563c9746b90e537af1c27e2283615f71205e7cdb5e2af6167a515f32",
        "d8e7c58a50de44532c2184bc78035380117b7b3b0b9610c2731a50a6626238fb",
        "db4b593cedf2b826cdea6a7505750078dd2bdc44fd85bfa029904496c9ee4ad9",
        "dcfa08fcfbede7dc7b3957356f379991c8ce9304efd1855137da58c80d7427c0",
        "ee4dec224bb6ac1de14c6bc2ae32e7b5dacaae46a1f8ede60304be2f44bca829",
        "ee6b5c9fad3da1ccac169f94d4537e73afbd35b3aaf3a4aab5a0bea4bd4e561a",
        "f440bf68ed104dac4515b8b3cbd70b9003a27dbe535745879a30ae69d67df7fd",
    }

    @staticmethod
    def _phrases(text):
        import hashlib
        words = re.findall(r"[a-z0-9]+", text.lower())
        for n in (1, 2, 3):
            for i in range(len(words) - n + 1):
                yield hashlib.sha256(" ".join(words[i:i + n]).encode()).hexdigest()

    def test_the_phrase_hashing_works(self):
        import hashlib
        got = set(self._phrases("Dorada  Demo, is synthetic!"))
        for phrase in ("dorada", "demo is", "dorada demo is"):         # 1, 2 and 3 word phrases, punctuation and spacing ignored
            self.assertIn(hashlib.sha256(phrase.encode()).hexdigest(), got)

    def test_no_real_company_names_anywhere(self):
        skip = {".git", "node_modules", "generated", "__pycache__"}
        hits = []
        for p in REPO.rglob("*"):
            if not p.is_file() or any(part in skip for part in p.parts) \
                    or p.suffix not in {".md", ".mjs", ".js", ".html", ".csv", ".json", ".jsonl", ".py", ".toml", ".txt", ".yml"}:
                continue
            if set(self._phrases(p.read_text(encoding="utf-8", errors="ignore"))) & self.FORBIDDEN_HASHES:
                hits.append(str(p.relative_to(REPO)))
        self.assertEqual(hits, [])

    def test_all_company_domains_are_reserved(self):
        w = build_world(42, 1500)
        self.assertTrue(all(a["domain"].endswith(".example") for a in w.accounts))
        self.assertTrue(all(g["state"]["accounts"][0]["domain"].endswith(".example") for g in golden.GOLDEN))


if __name__ == "__main__":
    unittest.main()
