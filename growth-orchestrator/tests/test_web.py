"""The web layer stays faithful to the engine: generated modules are in sync with the Python source, and the JS
validators / live function pass their node tests (skipped only if node is not installed).

Run:  python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from orchestrator import webexport

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent


class GeneratedFilesInSync(unittest.TestCase):
    def test_prompts_module(self):
        self.assertEqual((REPO / "netlify/functions/_orchestrator_prompts.mjs").read_text(encoding="utf-8"),
                         webexport.prompts_module(), "run: python -m orchestrator export-web")

    def test_cases_module(self):
        self.assertEqual((REPO / "netlify/functions/_orchestrator_cases.mjs").read_text(encoding="utf-8"),
                         webexport.cases_module(), "run: python -m orchestrator export-web")

    def test_recorded_runs_match_the_engine(self):
        published = json.loads((REPO / "public/data/runs.json").read_text(encoding="utf-8"))
        fresh = json.loads(json.dumps(webexport.runs(), default=str))
        self.assertEqual([s["results"] for s in published["scenarios"]], [s["results"] for s in fresh["scenarios"]],
                         "the page shows stale runs: python -m orchestrator export-web")

    def test_scoring_data_matches_the_engine(self):
        published = json.loads((REPO / "public/data/scoring.json").read_text(encoding="utf-8"))
        fresh = json.loads(json.dumps(webexport.scoring_payload(), default=str))
        self.assertEqual(published, fresh, "the page shows stale scoring: python -m orchestrator export-web")

    @unittest.skipUnless((ROOT / "data" / "generated" / "manifest.json").exists(), "50k world not generated (make data)")
    def test_overview_matches_the_engine(self):
        published = json.loads((REPO / "public/data/overview.json").read_text(encoding="utf-8"))
        fresh = json.loads(json.dumps(webexport.overview_payload(), default=str))
        self.assertEqual(published, fresh, "the page shows a stale 50k summary: python -m orchestrator export-overview")

    def test_overview_is_self_consistent(self):
        o = json.loads((REPO / "public/data/overview.json").read_text(encoding="utf-8"))
        self.assertEqual(sum(o["actions"].values()), o["n_accounts"])
        self.assertEqual(sum(sum(g["counts"].values()) for g in o["groups"]), o["n_accounts"])
        for a, n in o["actions"].items():
            self.assertEqual(sum(g["counts"].get(a, 0) for g in o["groups"]), n, a)
            self.assertEqual(sum(o["reasons"][a].values()), n, a)
        self.assertIn("contact", o["actions"])
        for codes in o["reasons"].values():
            for c in codes:
                self.assertIn(c, o["plain"]["codes"], f"no everyday-language text for {c}")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class NodeTests(unittest.TestCase):
    def _run(self, name):
        p = subprocess.run(["node", str(ROOT / "tests" / "js" / name)], capture_output=True, text=True, cwd=REPO)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_validator_parity(self):
        self._run("parity.test.mjs")

    def test_the_page_keeps_what_you_did_when_switching_tabs(self):
        self._run("ui_persistence.test.mjs")           # skips itself when no browser is available

    def test_scoring_parity(self):
        self._run("scoring.test.mjs")

    def test_live_function_with_stubbed_api(self):
        self._run("function.test.mjs")


if __name__ == "__main__":
    unittest.main()
