"""A small progress indicator for long LLM calls: an animated spinner with
elapsed time while the model churns, and event lines for things the model
does (tool calls, retries). Everything goes to stderr; the animation only
runs when stderr is a TTY, so piped output stays clean.
"""

import itertools
import sys
import threading
import time

LINE_WIDTH = 120


class Spinner:
    FRAMES = "|/-\\"

    def __init__(self, stream=None, enabled=None):
        self.stream = stream or sys.stderr
        if enabled is None:
            enabled = hasattr(self.stream, "isatty") and self.stream.isatty()
        self.enabled = enabled
        self._thread = None
        self._stop = threading.Event()
        self._text = ""
        self._t0 = 0.0

    def start(self, text="working"):
        if not self.enabled:
            self._text = text
            return
        self.stop()
        self._text = text
        self._t0 = time.time()
        self._frames = itertools.cycle(self.FRAMES)
        self._stop.clear()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self):
        while not self._stop.wait(0.15):
            elapsed = time.time() - self._t0
            line = f"\r  {next(self._frames)} {self._text} ({elapsed:.0f}s)"
            self.stream.write(line[:LINE_WIDTH])
            self.stream.flush()

    def note(self, msg):
        """Print an event line; pauses the animation while it resumes."""
        text = self._text
        self.stop()
        self.stream.write(f"  {msg}\n")
        self.stream.flush()
        if self._thread is None and self.enabled and text:
            self.start(text)

    def stop(self):
        if self._thread:
            self._stop.set()
            self._thread.join(timeout=1)
            self._thread = None
            self.stream.write("\r" + " " * LINE_WIDTH + "\r")
            self.stream.flush()
