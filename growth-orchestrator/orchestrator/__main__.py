"""Command line.

  python -m orchestrator demo [--flow D1] [--live]   run the demo flows through the engine and print each step
  python -m orchestrator stream                      replay the 561-delivery sample stream and summarise
  python -m orchestrator eval --recorded             validators vs 220 recorded model outputs (no model call)
  python -m orchestrator eval --live                 the 18 live eval cases against the real model (needs ANTHROPIC_API_KEY)
  python -m orchestrator serve [--port 8080]         local webhook receiver (POST /webhook), mock systems only, plus the
                                                     approval queue: GET /approvals, POST /approvals/<key>/approve|reject
  python -m orchestrator export-web                  regenerate the data and prompts used by the Netlify page
  python -m orchestrator export-overview             summarise all 50k accounts for the page (needs data/generated)
  python -m orchestrator compare-scoring [V1 V2]      what changing the scoring does, measured by the real engine on the 50k world

Nothing here can send a real email: outreach goes to the mock ledger only, and only after a person approves it.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import db, evals, scenario
from .ai.fixture import FixtureLLM
from .ai.llm import AnthropicLLM, LLMUnavailable
from .engine import Orchestrator
from .policy import SEED_DIR
from .timeutil import parse


def _unquote(part: str) -> str:
    """%XX decoding for the approval keys in a URL path (no urllib here: only the LLM client may import it)."""
    return re.sub(r"%([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), part)


def _live_llm():
    try:
        return AnthropicLLM()
    except LLMUnavailable as e:
        sys.exit(f"--live needs a model: {e}. Set ANTHROPIC_API_KEY in your shell (never in a file).")


def _print_result(e, r):
    print(f"  [{e.get('type')}] delivery={e.get('delivery_id')}  handling={r.handling}")
    if r.action:
        line = f"    decision: {r.action} {r.reason_codes}"
        if r.final_action and (r.final_action, r.final_reason_codes) != (r.action, r.reason_codes):
            line += f"  ->  after execution: {r.final_action} {r.final_reason_codes}"
        print(line)
    if r.ai:
        print(f"    ai: {json.dumps({k: v for k, v in r.ai.items() if v not in (None, [], '')})}")
    if r.route_to_ae_id:
        print(f"    route: {r.route_to_ae_id} ({r.route_reason})")
    if r.email:
        print(f"    email ({r.email['mode']}, {r.email.get('status')}, send_after={r.send_after}): {r.email['subject']}")
    for fx in r.effects:
        print(f"    effect: {fx}")


def cmd_demo(args):
    flows = json.loads((SEED_DIR / "demo_flows.json").read_text(encoding="utf-8"))["flows"]
    golden = {g["id"]: g for g in scenario.load_golden()}
    for f in flows:
        if args.flow and f["id"] != args.flow:
            continue
        print(f"\n=== {f['id']}: {f['title']} ===")
        for gid in f["golden"]:
            g = golden[gid]
            llm = _live_llm() if args.live else None
            print(f"\n-- {gid}: {g['title']}  (model: {'live ' + llm.model if llm else 'offline fixture'})")
            orch, results = scenario.run(g, llm=llm)
            for e, r in zip(g["events"], results):
                _print_result(e, r)
            held = orch.pending_approvals()
            for p in held:
                print(f"  PENDING APPROVAL {p['key']}: \"{p['subject']}\" ({p['mode']}); nothing can send until a person approves")
            for p in held:                                       # the demo's reviewer is a scripted stand-in for a person
                out = orch.approve(p["key"], "demo reviewer (scripted stand-in for a person)", now=parse(g["events"][-1]["received_at"]))
                print(f"  APPROVED by {out.get('reviewer')}: {out['status']}" + (f" (send_after {out['send_after']})" if out.get("send_after") else ""))
            led = orch.mocks.ledger
            print(f"  mock ledgers: emails={len(led['send'])} crm_writes={len(led['crm'])} meetings={len(led['calendar'])}"
                  f"  review_queue={orch.conn.execute('SELECT COUNT(*) FROM review_queue').fetchone()[0]}")


def cmd_stream(args):
    from collections import Counter
    conn = db.connect()
    db.load_world_dir(conn, SEED_DIR / "sample")
    events = [json.loads(l) for l in (SEED_DIR / "sample" / "events.jsonl").read_text(encoding="utf-8").splitlines() if l]
    orch = Orchestrator(conn, llm=_live_llm() if args.live else FixtureLLM(), as_of=parse("2026-10-01T16:00:00Z"))
    res = [orch.process(e) for e in events]
    print("handling:", dict(Counter(r.handling for r in res)))
    print("actions: ", dict(Counter(r.action for r in res)))
    print("emails held for approval:", len(orch.pending_approvals()), " mock emails sent:", len(orch.mocks.ledger["send"]), " review queue:",
          conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0],
          " dead letters:", conn.execute("SELECT COUNT(*) FROM dead_letters").fetchone()[0])


def cmd_eval(args):
    if args.live:
        result = evals.run_live(_live_llm())
    else:
        result = evals.run_recorded()
    p = evals.save(result)
    print(json.dumps(result["summary"], indent=2))
    print(f"saved: {p.relative_to(evals.ROOT)}")


def cmd_serve(args):
    conn = db.connect(args.db)
    if not conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():
        db.load_world_dir(conn, SEED_DIR / "sample")
    llm = _live_llm() if args.live else FixtureLLM()
    orch = Orchestrator(conn, llm=llm)
    secret = os.environ.get("ORCH_WEBHOOK_SECRET", "").encode()

    class H(BaseHTTPRequestHandler):
        def _send(self, code, body):
            data = json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("content-length", 0))
            if n > 64_000:
                self._send(413, {"error": "too_large"})
                return None
            raw = self.rfile.read(n)
            if secret:                                  # HMAC-signed requests when a secret is configured
                sig = hmac.new(secret, raw, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(self.headers.get("x-signature", ""), "sha256=" + sig):
                    self._send(401, {"error": "bad_signature"})
                    return None
            return raw

        def do_POST(self):
            parts = [_unquote(p) for p in self.path.strip("/").split("/")]
            is_review = len(parts) == 3 and parts[0] == "approvals" and parts[2] in ("approve", "reject")
            if self.path != "/webhook" and not is_review:
                return self._send(404, {"error": "not_found"})
            raw = self._body()
            if raw is None:
                return
            if is_review:
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    return self._send(400, {"error": "invalid_json"})
                reviewer = body.get("reviewer") if isinstance(body, dict) else None
                if not isinstance(reviewer, str) or not reviewer.strip():
                    return self._send(400, {"error": "reviewer_required"})
                if parts[2] == "approve":
                    return self._send(200, orch.approve(parts[1], reviewer, note=str(body.get("note", ""))))
                return self._send(200, orch.reject(parts[1], reviewer, str(body.get("reason", ""))))
            try:
                env = json.loads(raw)
            except ValueError:
                env = {"_unparseable": raw[:2000].decode(errors="replace")}
            self._send(200, orch.process(env).to_dict())

        def do_GET(self):
            parts = self.path.strip("/").split("/")
            if parts == ["approvals"]:
                return self._send(200, {"pending": orch.pending_approvals()})
            if len(parts) == 2 and parts[0] == "accounts":
                return self._send(200, {"account": db.one(conn, "SELECT * FROM accounts WHERE account_id=?", (parts[1],)),
                                        "decisions": db.rows(conn, "SELECT * FROM decisions WHERE account_id=?", (parts[1],)),
                                        "audit": orch.audit.trail(parts[1])})
            self._send(404, {"error": "not_found"})

    print(f"listening on http://127.0.0.1:{args.port}/webhook  (mock systems only; model: {getattr(llm, 'mode', '?')})")
    # One request at a time, on purpose: the engine handles events synchronously (one transaction each) over a single SQLite
    # connection, which cannot be shared across threads.
    HTTPServer(("127.0.0.1", args.port), H).serve_forever()


def cmd_export_web(args):
    from . import webexport
    for p in webexport.export_all():
        print("wrote", p)


def cmd_export_overview(args):
    from . import webexport
    for p in webexport.export_overview():
        print("wrote", p)


def cmd_compare_scoring(args):
    from . import showcase
    world = Path(args.world) if args.world else showcase.GENERATED
    p = showcase.scoring_compare_payload(world, [args.a, args.b] if args.a and args.b else None)
    print(showcase.scoring_compare_markdown(p))
    if args.write_web:
        from . import webexport
        out = webexport.WEB / "scoring_compare.json"
        out.write_text(json.dumps(p, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print("wrote", out)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m orchestrator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo"); d.add_argument("--flow"); d.add_argument("--live", action="store_true")
    s = sub.add_parser("stream"); s.add_argument("--live", action="store_true")
    e = sub.add_parser("eval"); g = e.add_mutually_exclusive_group(required=True)
    g.add_argument("--live", action="store_true"); g.add_argument("--recorded", action="store_true")
    v = sub.add_parser("serve"); v.add_argument("--port", type=int, default=8080); v.add_argument("--db", default=":memory:")
    v.add_argument("--live", action="store_true")
    sub.add_parser("export-web")
    sub.add_parser("export-overview")
    c = sub.add_parser("compare-scoring"); c.add_argument("a", nargs="?"); c.add_argument("b", nargs="?"); c.add_argument("--world"); c.add_argument("--write-web", action="store_true")
    a = ap.parse_args(argv)
    {"demo": cmd_demo, "stream": cmd_stream, "eval": cmd_eval, "serve": cmd_serve, "export-web": cmd_export_web,
     "export-overview": cmd_export_overview, "compare-scoring": cmd_compare_scoring}[a.cmd](a)


if __name__ == "__main__":
    main()
