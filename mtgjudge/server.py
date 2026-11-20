"""Web adapter: one HTTP server around the same engine the CLI uses.

Design (see PLAN.md, "Web remote access"): interchangeable frontends over
one core — every route here calls the same functions (`cli.answer_question`,
`cli.record_turn`, `cmd`-equivalent lookups) the terminal frontends use.

Render free tier constraints made the shape: every HTTP request must be
short (proxy timeout ~15-60 s), so `POST /ask` only starts a background
thread and returns a job id; the browser polls `GET /job/{id}`. Answers
stream via the line_sink callback (raw lines) while progress notes arrive
via the emit callback as job events. Conversations live in memory only
(no persistent history — decided) and die with the process.
"""

import json
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import carddb, cards, cli, cr, llm, search, store

WEB_DIR = Path(__file__).parent / "web"
MAX_JOBS = 500  # oldest finished jobs pruned; single-user app


class EventSpinner:
    """Spinner stand-in for the server: progress notes become job events
    instead of terminal control characters."""

    def __init__(self, emit):
        self._emit = emit

    def start(self, text="working"):
        self._emit(f"asking: {text}")

    def note(self, msg):
        self._emit(msg)

    def stop(self):
        pass


class Hub:
    """All server state: conversation threads and answer jobs.

    A thread is the web equivalent of a REPL session state dict
    (history + prior_blocks); `:new` maps to creating a fresh thread.
    """

    def __init__(self, pipeline=None, offline=False, backend=None):
        self.lock = threading.Lock()
        self.threads = {}
        self.jobs = {}
        self.pipeline = pipeline or cli.answer_question
        self.offline = offline
        self.backend = backend
        self._rules = None  # (meta, chunks) cache; data only changes on deploy

    # -- data ------------------------------------------------------------

    def rules(self):
        with self.lock:
            if self._rules is None:
                self._rules = store.load_rules()
            return self._rules

    # -- threads -----------------------------------------------------------

    def new_thread(self):
        thread_id = uuid.uuid4().hex[:12]
        with self.lock:
            self.threads[thread_id] = {"history": [], "prior_blocks": []}
        return thread_id

    def thread_or_new(self, thread_id):
        with self.lock:
            if thread_id and thread_id in self.threads:
                return thread_id, False
        return self.new_thread(), True

    # -- jobs -----------------------------------------------------------

    def submit(self, question, thread_id, sync=False):
        thread_id, created = self.thread_or_new(thread_id)
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "thread": thread_id,
            "status": "running",
            "events": [],
            "lines": [],
            "question": question,
            "created": time.time(),
        }
        with self.lock:
            self.jobs[job_id] = job
            self._prune_locked()
        worker = threading.Thread(target=self._run, args=(job_id,),
                                   daemon=True, name=f"job-{job_id}")
        if sync:  # tests: run inline on the caller's thread
            self._run(job_id)
        else:
            worker.start()
        return job_id, thread_id, created

    def _run(self, job_id):
        with self.lock:
            job = self.jobs[job_id]
            thread = self.threads[job["thread"]]
        question = job["question"]

        def line_sink(line):
            with self.lock:
                job["lines"].append(line)

        def emit(msg):
            # job event for the frontend + server log line
            with self.lock:
                job["events"].append(msg)
            print(msg, file=sys.stderr, flush=True)

        try:
            res = self.pipeline(
                question, offline=self.offline, backend=self.backend,
                interactive=False, stream_output=False,
                history=list(thread.get("history") or []),
                prior_blocks=list(thread.get("prior_blocks") or []),
                line_sink=line_sink, emit=emit,
                spinner=EventSpinner(emit),
            )
        except Exception as e:  # noqa: BLE001 - report to the poller
            with self.lock:
                job["status"] = "error"
                job["error"] = str(e)
            print(f"job {job_id} failed: {e}", file=sys.stderr, flush=True)
            return
        if "error" in res:  # pipeline-level error (nothing retrieved)
            with self.lock:
                job["status"] = "error"
                job["error"] = res["error"]
            return
        cli.record_turn(thread, question, res)
        card_names = [b.splitlines()[0][len("CARD "):]
                      for b in res.get("blocks", []) if b.startswith("CARD ")]
        with self.lock:
            job.update({
                "status": "done",
                "answer": res["answer"],
                "cards": card_names,
                "context_ids": res.get("context_ids", []),
                "model": res.get("model"),
                "backend": res.get("backend"),
                "effective_date": res["meta"]["effective_date"],
                "elapsed": res.get("elapsed"),
            })

    def job_snapshot(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            return {k: (list(v) if isinstance(v, list) else v)
                    for k, v in job.items()
                    if k != "created"}

    def _prune_locked(self):
        done = sorted(
            (j["created"], jid) for jid, j in self.jobs.items()
            if j["status"] in ("done", "error"))
        for _t, jid in done[:-MAX_JOBS]:
            del self.jobs[jid]


class Handler(BaseHTTPRequestHandler):
    hub = None       # set by serve()
    token = None
    index_cache = None
    index_mtime = None

    # -- helpers --------------------------------------------------------

    def _reply(self, code, payload=None, content_type="application/json"):
        body = b"" if payload is None else (
            payload if isinstance(payload, bytes)
            else json.dumps(payload).encode("utf-8"))
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _authorized(self):
        header = self.headers.get("Authorization", "")
        return header == f"Bearer {self.token}"

    def _body_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return None

    def log_message(self, fmt, *args):
        pass  # request logs are noise; job events are already logged

    # -- routes --------------------------------------------------------

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index.html"):
            return self._serve_index()
        if self.path == "/health":
            return self._health()
        if not self._authorized():
            return self._reply(401, {"error": "unauthorized"})
        path = self.path.split("?", 1)
        route, query = path[0], dict(
            part.split("=", 1) for part in
            (path[1].split("&") if len(path) > 1 else []) if "=" in part)
        from urllib.parse import unquote_plus
        query = {k: unquote_plus(v) for k, v in query.items()}
        if route.startswith("/job/"):
            return self._job(route[len("/job/"):])
        if route == "/rule":
            return self._rule(query.get("number", ""))
        if route == "/search":
            return self._search(query.get("q", ""))
        if route == "/card":
            return self._card(query.get("name", ""))
        return self._reply(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return self._reply(401, {"error": "unauthorized"})
        if self.path != "/ask":
            return self._reply(404, {"error": "not found"})
        data = self._body_json()
        if not isinstance(data, dict):
            return self._reply(400, {"error": "bad json body"})
        question = (data.get("question") or "").strip()
        if not question:
            return self._reply(400, {"error": "empty question"})
        job_id, thread_id, created = self.hub.submit(
            question, data.get("thread"))
        return self._reply(200, {
            "job": job_id,
            "thread": thread_id,
            "new_thread": created,
        })

    # -- route bodies --------------------------------------------------

    def _serve_index(self):
        path = WEB_DIR / "index.html"
        # mtime check: a long-lived server must serve the current page,
        # not the bytes it happened to read first (bit us once during
        # local iteration: an old, broken index.html kept being served)
        mtime = path.stat().st_mtime
        if (Handler.index_cache is None
                or Handler.index_mtime != mtime):
            Handler.index_cache = path.read_bytes()
            Handler.index_mtime = mtime
        self._reply(200, Handler.index_cache, "text/html; charset=utf-8")

    def _health(self):
        # no auth: the frontend uses this to detect the Render free-tier
        # wake-up before anything else
        try:
            meta, _chunks = self.hub.rules()
            effective = meta["effective_date"]
        except store.NoRulesError:
            effective = "(no rules loaded — run refresh)"
        self._reply(200, {"ok": True, "effective_date": effective})

    def _job(self, job_id):
        snap = self.hub.job_snapshot(job_id)
        if snap is None:
            return self._reply(404, {"error": "unknown job"})
        return self._reply(200, snap)

    def _rule(self, number):
        _meta, chunks = self.hub.rules()
        try:
            rules = cr.match_rule(chunks, number)
        except KeyError:
            return self._reply(404, {"error": f"no rule {number}"})
        self._reply(200, {
            "rules": [{"number": r["number"], "chapter": r["chapter"],
                       "section": r["section"], "text": r["text"]}
                      for r in rules],
        })

    def _search(self, query):
        if not query.strip():
            return self._reply(400, {"error": "empty query"})
        _meta, chunks = self.hub.rules()
        results = search.search_rules(chunks, query, top_k=5)
        self._reply(200, {
            "results": [{"number": r["number"], "text": r["text"]}
                        for r, _score in results],
        })

    def _card(self, name):
        if not name.strip():
            return self._reply(400, {"error": "empty name"})
        try:
            card = carddb.lookup_card(name, offline=self.hub.offline)
        except cards.CardNotFound as e:
            return self._reply(404, {"error": str(e)})
        rulings = carddb.get_rulings(card, offline=self.hub.offline)
        self._reply(200, {
            "card": cards.format_card(card),
            "rulings": [r.get("comment", "") for r in rulings],
        })


def main(args):
    hub = Hub(offline=args.offline, backend=getattr(args, "backend", None))
    port = args.port or int(_env_port() or 10000)
    token = args.auth_token or _env_token()
    if not token:
        print("error: serve needs a bearer token (--auth-token or the "
              "WEB_TOKEN env var). The endpoint fronts your Mistral key.",
              file=sys.stderr)
        return 2
    Handler.hub = hub
    Handler.token = token
    httpd = ThreadingHTTPServer((args.host, port), Handler)
    print(f"oragle web listening on http://{args.host}:{port} "
          f"(token auth, data: {store.DATA_DIR})", file=sys.stderr,
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


def _env_token():
    import os
    return os.environ.get("WEB_TOKEN")


def _env_port():
    import os
    return os.environ.get("PORT")


def run(token, pipeline=None, offline=False):
    """Start the server on an ephemeral port; returns the httpd object
    (with .server_port / .RequestHandlerClass). Used by tests."""
    hub = Hub(pipeline=pipeline, offline=offline)
    Handler.hub = hub
    Handler.token = token
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    return httpd
