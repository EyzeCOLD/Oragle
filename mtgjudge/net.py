"""Small urllib wrappers. The only I/O layer in the package."""

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request

quote = urllib.parse.quote
USER_AGENT = f"mtg-rules-judge/0.1 (+https://github.com/local; CLI rules assistant)"
DEFAULT_TIMEOUT = 30
LLM_TIMEOUT = 240  # thinking models on hard questions can churn for minutes;
                  # override via MTG_LLM_TIMEOUT


class NetError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def llm_timeout():
    """LLM request timeout in seconds. MTG_LLM_TIMEOUT env overrides."""
    try:
        return int(os.environ.get("MTG_LLM_TIMEOUT", "0")) or LLM_TIMEOUT
    except ValueError:
        return LLM_TIMEOUT


def _open(url, data=None, headers=None, timeout=DEFAULT_TIMEOUT, accept="application/json"):
    req = urllib.request.Request(url, data=data, method="GET" if data is None else "POST")
    req.add_header("User-Agent", USER_AGENT)
    # Scryfall's API rejects requests without an explicit Accept header.
    if accept:
        req.add_header("Accept", accept)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        raise NetError(f"HTTP {e.code} for {url}: {body}", status=e.code) from e
    except (TimeoutError, socket.timeout) as e:
        raise NetError(f"Request to {url} timed out after {timeout}s"
                       + (" (LLM calls: MTG_LLM_TIMEOUT to change)" if timeout >= LLM_TIMEOUT else "")) from e
    except urllib.error.URLError as e:
        raise NetError(f"Request failed for {url}: {e.reason}") from e


def get_text(url, timeout=DEFAULT_TIMEOUT):
    return _open(url, timeout=timeout, accept=None).decode("utf-8", "replace")


def get_json(url, timeout=DEFAULT_TIMEOUT, headers=None):
    return json.loads(_open(url, timeout=timeout, headers=headers))


def post_json(url, payload, timeout=DEFAULT_TIMEOUT, headers=None):
    body = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    return json.loads(_open(url, data=body, headers=h, timeout=timeout))


def post_sse_lines(url, payload, timeout=DEFAULT_TIMEOUT, headers=None):
    """POST expecting a Server-Sent-Events stream (OpenAI-compatible
    stream=true). Yields decoded lines while the stream stays open. The
    socket timeout applies per read, so a stalled stream raises NetError
    mid-iteration instead of hanging silently."""
    body = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    req.add_header("User-Agent", USER_AGENT)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", "replace")[:500]
        raise NetError(f"HTTP {e.code} for {url}: {body_text}", status=e.code) from e
    except (TimeoutError, socket.timeout) as e:
        raise NetError(f"Request to {url} timed out after {timeout}s"
                       + (" (LLM calls: MTG_LLM_TIMEOUT to change)" if timeout >= LLM_TIMEOUT else "")) from e
    except urllib.error.URLError as e:
        raise NetError(f"Request failed for {url}: {e.reason}") from e
    with resp:
        try:
            for raw_line in resp:
                yield raw_line.decode("utf-8", "replace")
        except (TimeoutError, socket.timeout) as e:
            raise NetError(f"Stream from {url} stalled after {timeout}s"
                           + (" (LLM calls: MTG_LLM_TIMEOUT to change)" if timeout >= LLM_TIMEOUT else "")) from e
