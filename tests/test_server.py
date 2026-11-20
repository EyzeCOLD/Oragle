import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from mtgjudge import server


def fake_pipeline(answer="**No.** Rule 101.2 applies."):
    """A stand-in for cli.answer_question recording what the hub passed,
    delivering lines through line_sink and notes through emit so the
    job/ poll shape is exercised without any LLM."""
    calls = []

    def pipeline(question, **kwargs):
        calls.append({"question": question, "kwargs": kwargs})
        sink = kwargs.get("line_sink")
        emit = kwargs.get("emit")
        emit("-> 2 rules in context")
        if sink:
            sink("**No.**")
            sink("Rule 101.2 applies.")
        return {
            "answer": answer,
            "backend": "mistral", "model": "test-model",
            "meta": {"effective_date": "Nov 6, 2026"},
            "blocks": ["RULE 101.2 [General]\ncan't beats can",
                       "CARD Lightning Helix\nLightning Helix"],
            "context_ids": ["101.2", "Lightning Helix"],
            "elapsed": 0.1, "stats": {"rounds": 1}, "streamed": False,
        }

    pipeline.calls = calls
    return pipeline


class HubTest(unittest.TestCase):
    def _hub(self, pipeline=None):
        return server.Hub(pipeline=pipeline or fake_pipeline(),
                          offline=True)

    def test_submit_runs_job_and_records_turn(self):
        hub = self._hub()
        thread_id = hub.new_thread()
        job_id, tid, _created = hub.submit("what is trample?", thread_id,
                                           sync=True)
        snap = hub.job_snapshot(job_id)
        self.assertEqual(snap["status"], "done")
        self.assertIn("No.", snap["answer"])
        self.assertEqual(snap["lines"], ["**No.**", "Rule 101.2 applies."])
        self.assertEqual(snap["events"], ["-> 2 rules in context"])
        self.assertEqual(snap["cards"], ["Lightning Helix"])
        self.assertEqual(snap["effective_date"], "Nov 6, 2026")
        # conversation memory: turn recorded in the thread
        t = hub.threads[thread_id]
        self.assertEqual(len(t["history"]), 2)
        self.assertEqual(t["history"][0]["content"], "what is trample?")
        # second submit sees the accumulated history
        hub.submit("and in 2hg?", thread_id, sync=True)
        kwargs = hub.pipeline.calls[-1]["kwargs"]
        self.assertEqual(kwargs["history"][0]["content"],
                         "what is trample?")
        self.assertTrue(kwargs["prior_blocks"])

    def test_pipeline_error_becomes_job_error(self):
        def boom(question, **kwargs):
            raise RuntimeError("llm exploded")
        hub = server.Hub(pipeline=boom, offline=True)
        thread_id = hub.new_thread()
        job_id, _tid, _c = hub.submit("q", thread_id, sync=True)
        snap = hub.job_snapshot(job_id)
        self.assertEqual(snap["status"], "error")
        self.assertIn("llm exploded", snap["error"])

    def test_unknown_job_snapshot_is_none(self):
        hub = self._hub()
        self.assertIsNone(hub.job_snapshot("nope"))

    def test_unknown_thread_creates_new(self):
        hub = self._hub()
        tid, created = hub.thread_or_new("bogus")
        self.assertTrue(created)
        self.assertIn(tid, hub.threads)

    def test_idle_thread_evicted(self):
        # conversation memory must not live forever: an expired thread id
        # from a stale client starts a fresh thread, not the old context
        import time as time_mod
        hub = self._hub()
        tid = hub.new_thread()
        hub.submit("q", tid, sync=True)
        self.assertIn(tid, hub.threads)
        hub.threads[tid]["last_used"] = (
            time_mod.time() - server.THREAD_TTL_SECONDS - 60)
        hub.submit("unrelated", None, sync=True)  # runs the prune
        self.assertNotIn(tid, hub.threads)
        tid2, created = hub.thread_or_new(tid)
        self.assertTrue(created)
        self.assertNotEqual(tid2, tid)
        self.assertEqual(hub.threads[tid2]["history"], [])


class ApiTest(unittest.TestCase):
    """End-to-end over a real socket: auth, ask -> poll, health."""

    def setUp(self):
        self.httpd = server.run(token="secret-token",
                                 pipeline=fake_pipeline(), offline=True)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.token = "secret-token" 
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)

    def _req(self, method, path, body=None, auth=True):
        req = urllib.request.Request(
            self.base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}"} if auth else {})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())

    def test_ask_requires_auth(self):
        req = urllib.request.Request(
            self.base + "/ask", method="POST",
            data=json.dumps({"question": "q"}).encode(), headers={})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 401)

    def test_health_open_and_ask_poll_flow(self):
        status, body = self._req("GET", "/health", auth=True)
        self.assertEqual(status, 200)

        status, body = self._req("POST", "/ask",
                                 {"question": "who goes first?"})
        self.assertEqual(status, 200)
        job_id, thread_id = body["job"], body["thread"]
        self.assertTrue(thread_id)
        # the fake pipeline runs synchronously in the worker thread; poll
        # until done (should be immediate)
        for _ in range(20):
            code, snap = self._req("GET", f"/job/{job_id}")
            if snap["status"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(snap["status"], "done")
        self.assertIn("No.", snap["answer"])
        self.assertEqual(snap["context_ids"], ["101.2", "Lightning Helix"])

        # follow-up goes to the same thread with accumulated context
        code, body2 = self._req("POST", "/ask",
                                 {"question": "in 2hg?",
                                  "thread": thread_id})
        self.assertEqual(body2["thread"], thread_id)

    def test_unknown_job_404(self):
        req = urllib.request.Request(
            self.base + "/job/nope",
            headers={"Authorization": f"Bearer {self.token}"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 404)

    def test_bad_body_400(self):
        req = urllib.request.Request(
            self.base + "/ask", method="POST",
            data=b"{not json",
            headers={"Authorization": f"Bearer {self.token}"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_index_html_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as resp:
            html = resp.read().decode()
        self.assertIn("oragle", html)
        self.assertIn("/health", html)

    def test_rule_lookup_with_cached_rules(self):
        # inject a fixture rules index into the hub cache
        self.httpd.RequestHandlerClass.hub._rules = (
            {"effective_date": "Nov 6, 2026"},
            [{"number": "704.1", "chapter": "SBA", "section": "",
               "text": "state-based actions are checked"},
             {"number": "704.1a", "chapter": "SBA", "section": "",
               "text": "a subrule"}],
        )
        status, body = self._req("GET", "/rule?number=704.1")
        self.assertEqual(status, 200)
        self.assertEqual(body["rules"][0]["number"], "704.1")
        self.assertEqual(len(body["rules"]), 2)  # subrule included

    def test_symbol_route(self):
        # img tags cannot send Authorization: symbol serving is open like
        # /health. Cache-first; offline + uncached 404s (which the
        # frontend's onerror turns back into literal {T} text).
        import tempfile
        from pathlib import Path
        from unittest import mock as _mock
        from mtgjudge import symbols as sym_mod
        with tempfile.TemporaryDirectory() as tmp:
            sym_dir = Path(tmp) / "symbols"
            with _mock.patch.object(sym_mod, "SYMBOL_DIR", sym_dir):
                # unbaked + uncached + offline = 404 (baked icons like T
                # serve offline forever), no auth needed to see it
                req = urllib.request.Request(self.base + "/symbol/ZZ.svg")
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(ctx.exception.code, 404)

                # bad codes never touch the filesystem
                for bad in ("/symbol/..%2Fetc.svg",
                            "/symbol/nope-nope"):
                    req = urllib.request.Request(self.base + bad)
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(req, timeout=5)
                    self.assertEqual(ctx.exception.code, 404)

                # a cached icon serves with the SVG type and cache header
                sym_dir.mkdir(parents=True, exist_ok=True)
                (sym_dir / "ZZ.svg").write_bytes(b"<svg>icon</svg>")
                with urllib.request.urlopen(
                        self.base + "/symbol/ZZ.svg", timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertEqual(resp.headers["Content-Type"],
                                     "image/svg+xml")
                    self.assertIn("max-age", resp.headers["Cache-Control"])
                    self.assertEqual(resp.read(), b"<svg>icon</svg>")


if __name__ == "__main__":
    unittest.main()
