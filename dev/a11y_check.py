#!/usr/bin/env python3
"""Accessibility audit for the web frontend, both themes.

Spins a throwaway server with a canned pipeline (no model tokens),
drives headless Chromium via CDP, injects axe-core and prints WCAG
violations. Requires chromium and (once) network to fetch axe-core.

    uv run python dev/a11y_check.py [--theme wood|steel|both]

Cache for axe-core: /tmp/axe.min.js
"""
import json
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

AXE_CACHE = Path("/tmp/axe.min.js")
AXE_URL = ("https://cdnjs.cloudflare.com/ajax/libs/axe-core/"
           "4.10.2/axe.min.js")
DEBUG_PORT = 9345
USER_DIR = "/tmp/chrome-a11y"

ANSWER = (
    "Here is the answer. Cite rule 704.3 when a player would receive "
    "priority. *Emphasized* and **bold** passes render. "
    "Cards: [[Lightning Helix]] and Trench Gnome.\n\n"
    "Second point with rule 117.1a for good measure."
)


def make_pipeline():
    import time as time_mod
    t0 = time_mod.time()

    def pipeline(question, top=8, offline=False, backend=None, model=None,
                 spinner=None, interactive=False, stream_output=False,
                 history=None, prior_blocks=None, line_sink=None, emit=None):
        return {
            "answer": ANSWER,
            "backend": "fake-backend", "model": "fake-model",
            "meta": {"effective_date": "November 6, 2026"},
            "blocks": [], "context_ids": [],
            "elapsed": time_mod.time() - t0, "stats": {"rounds": 1},
            "streamed": False,
        }
    return pipeline


def fetch_axe():
    if not AXE_CACHE.exists():
        print("fetching axe-core (one-time)...")
        with urllib.request.urlopen(AXE_URL, timeout=60) as r:
            AXE_CACHE.write_bytes(r.read())
    return AXE_CACHE.read_text()


class CDP:
    def __init__(self):
        subprocess.run(["pkill", "-f", f"--user-data-dir={USER_DIR}"],
                       capture_output=True)
        subprocess.Popen(
            ["/snap/bin/chromium", "--headless=new",
             f"--remote-debugging-port={DEBUG_PORT}",
             f"--user-data-dir={USER_DIR}",
             "--no-first-run", "--no-default-browser-check",
             "--disable-gpu", "--window-size=480,800", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{DEBUG_PORT}/json/version",
                        timeout=1) as r:
                    json.loads(r.read())
                break
            except Exception:
                time.sleep(0.2)
        req = urllib.request.Request(
            f"http://127.0.0.1:{DEBUG_PORT}/json/new", method="PUT")
        with urllib.request.urlopen(req, timeout=5) as r:
            url = json.loads(r.read())["webSocketDebuggerUrl"]
        from websocket import create_connection
        self.ws = create_connection(url, timeout=15)
        self.mid = 0

    def call(self, method, **params):
        self.mid += 1
        self.ws.send(json.dumps({"id": self.mid, "method": method,
                                 "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def eval(self, expr, await_promise=False):
        r = self.call("Runtime.evaluate", expression=expr,
                      awaitPromise=await_promise, returnByValue=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(r["exceptionDetails"]["text"])
        return r["result"].get("value")


def main():
    from mtgjudge import server

    theme_arg = "both"
    if len(sys.argv) > 2 and sys.argv[1] == "--theme":
        theme_arg = sys.argv[2]
    themes = ["wood", "steel"] if theme_arg == "both" else [theme_arg]

    httpd = server.run("localtest", pipeline=make_pipeline())
    port = httpd.server_port
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"audit server on 127.0.0.1:{port}")

    axe_src = fetch_axe()
    cdp = CDP()
    cdp.call("Page.enable")
    cdp.call("Emulation.setFocusEmulationEnabled", enabled=True)

    bad = 0
    for theme in themes:
        cdp.call("Page.navigate", url=f"http://127.0.0.1:{port}/")
        for _ in range(100):
            if cdp.eval("document.readyState") == "complete":
                break
            time.sleep(0.2)
        cdp.eval(f"localStorage.setItem('oragle_token','localtest');"
                 f"localStorage.setItem('oragle_theme','{theme}');")
        cdp.call("Page.reload")
        for _ in range(100):
            if cdp.eval("document.readyState") == "complete":
                break
            time.sleep(0.2)
        # a full conversation: user bubble, answer with links, footer
        cdp.eval("document.getElementById('q').value = 'who goes first?';"
                 "document.getElementById('composer').requestSubmit()")
        for _ in range(60):
            time.sleep(0.5)
            if not cdp.eval("running"):
                break
        # expand a rule citation, open the deck panel: more surface
        cdp.eval("(document.querySelector('a.rule')||{click:()=>{}}).click()")
        cdp.eval("document.getElementById('deckbtn').click()")

        cdp.eval(axe_src)  # defines window.axe
        run_js = """
        axe.run(document, {resultTypes: ['violations', 'incomplete']})
          .then(r => {
            const out = r.violations.map(v => ({
              id: v.id, impact: v.impact, help: v.help,
              nodes: v.nodes.map(n => n.html.slice(0, 90))
            }));
            for (const i of r.incomplete) {
              if (i.id === 'color-contrast') out.push({
                id: i.id + '?', impact: i.impact,
                help: 'contrast needs review (gradient/textured bg)',
                nodes: i.nodes.map(n => n.html.slice(0, 90))
              });
            }
            return JSON.stringify(out);
          })
        """
        raw = cdp.eval(run_js, await_promise=True)
        findings = json.loads(raw)
        print(f"\n=== theme: {theme} — {len(findings)} finding(s) ===")
        for f in findings:
            print(f"[{f['impact']}] {f['id']}: {f['help']}")
            for n in f["nodes"][:4]:
                print(f"    {n}")
            bad += 1
    print(f"\ntotal findings: {bad}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
