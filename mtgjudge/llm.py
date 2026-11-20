"""One abstraction over local and hosted LLMs, OpenAI-compatible chat shape.

Backends (all speak POST {base}/chat/completions):
- openai:   any OpenAI-compatible endpoint; select with MTG_API_BASE
            (+ MTG_API_KEY, MTG_MODEL). Covers OpenAI, OpenRouter, Z.ai,
            Groq, Together, Mistral, ... anything speaking the protocol.
- mistral:  https://api.mistral.ai/v1, needs MISTRAL_API_KEY
- lmstudio: http://localhost:1234/v1 (LM Studio local server)
- ollama:   http://localhost:11434/v1

MTG_BACKEND forces one explicitly; otherwise auto-detection prefers
MTG_API_BASE, then MISTRAL_API_KEY, then local servers.
"""

import json
import os
import time

from . import net

BASE_URLS = {
    "mistral": "https://api.mistral.ai/v1",
    "lmstudio": "http://localhost:1234/v1",
    "ollama": "http://localhost:11434/v1",
}
DEFAULT_MODELS = {"mistral": "zai-glm-5-3-flash"}


class LLMError(RuntimeError):
    pass


MAX_HISTORY_MESSAGES = 6  # last 3 exchanges keep follow-ups affordable


def _base_url(backend):
    """Endpoint root for a backend; the openai backend is user-supplied."""
    if backend == "openai":
        return (os.environ.get("MTG_API_BASE") or "").rstrip("/")
    return BASE_URLS.get(backend, "")


def _probe(base):
    try:
        net.get_json(f"{base}/models", timeout=2)
        return True
    except net.NetError:
        return False


def resolve_backend(explicit=None):
    if explicit:
        return explicit
    env_backend = os.environ.get("MTG_BACKEND", "").strip()
    if env_backend:
        return env_backend
    if os.environ.get("MTG_API_BASE"):
        return "openai"
    if os.environ.get("MISTRAL_API_KEY"):
        return "mistral"
    for backend in ("lmstudio", "ollama"):
        if _probe(BASE_URLS[backend]):
            return backend
    raise LLMError(
        "No LLM backend available. Set MTG_API_BASE (any OpenAI-compatible "
        "endpoint) with MTG_API_KEY, or MISTRAL_API_KEY, or start the LM "
        "Studio server (localhost:1234) or Ollama. MTG_BACKEND forces one; "
        "--backend does the same per run."
    )


def default_model(backend):
    env_model = os.environ.get("MTG_MODEL")
    if env_model:
        return env_model
    if backend in DEFAULT_MODELS:
        return DEFAULT_MODELS[backend]
    try:
        models = net.get_json(f"{_base_url(backend)}/models", timeout=2)
        data = models.get("data", [])
        if data:
            return data[0]["id"]
    except (net.NetError, KeyError):
        pass
    raise LLMError(
        f"No model available for backend '{backend}'. Load a model in LM "
        "Studio, or set MTG_MODEL (required for the openai backend)."
    )


def _content_to_text(content):
    """Normalize chat content: some models (e.g. zai-glm) return a list of
    blocks (thinking/text) instead of a plain string. Keep text blocks only."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b["text"] for b in content
                 if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
        if parts:
            return "\n".join(parts).strip()
    raise LLMError(f"No text content in LLM message: {str(content)[:200]}")


def _fmt_args(args):
    return ", ".join(f"{k}={str(v)[:60]!r}" for k, v in (args or {}).items())


def _assemble_stream_chunk(chunk, content_parts, tool_call_slots, on_text):
    """Fold one SSE chunk into the assembling assistant message; shape
    quirks are skipped, never raised."""
    choices = chunk.get("choices") or []
    if not choices:
        return
    delta = choices[0].get("delta") or {}
    c = delta.get("content")
    texts = []
    if isinstance(c, str):
        texts.append(c)
    elif isinstance(c, list):  # zai-glm style block lists: text blocks only
        texts += [b.get("text") for b in c
                  if isinstance(b, dict) and b.get("type") == "text"]
    had_answer_text = False
    for t in texts:
        if t:
            content_parts.append(t)
            on_text(t)
            had_answer_text = True
    for tc in delta.get("tool_calls") or []:
        idx = tc.get("index", 0)
        slot = tool_call_slots.setdefault(idx, {
            "id": "", "type": "function",
            "function": {"name": "", "arguments": ""}})
        if tc.get("id"):
            slot["id"] = tc["id"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] = fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments"] += fn["arguments"]
    return had_answer_text


def chat(backend, model, messages, temperature=0.1, tools=None, progress=None,
         stream=None, elapsed=None, info=None, first_answer_deadline=None):
    """One completion call. Returns the raw assistant message dict (content
    may be a string, a block list, or None alongside tool_calls).

    stream: optional callable receiving each text delta as it arrives, so
    callers can show the answer while it is generated. Tool-call deltas are
    reassembled transparently, so a streaming round behaves like a buffered
    one. Model seconds accumulate into `elapsed` (a list) when given."""
    headers = {}
    if backend == "mistral":
        key = os.environ.get("MISTRAL_API_KEY")
        if not key:
            raise LLMError("MISTRAL_API_KEY is not set.")
        headers["Authorization"] = f"Bearer {key}"
    elif backend == "openai":
        key = os.environ.get("MTG_API_KEY")
        if not key:
            raise LLMError("MTG_API_KEY is not set (used with MTG_API_BASE).")
        headers["Authorization"] = f"Bearer {key}"
    payload = {"model": model, "messages": messages, "temperature": temperature}
    if backend == "mistral":
        # Billing router, mistral-specific: bare API calls bill to the
        # pay-as-you-go API bucket; a non-empty metadata.call_type marks
        # a request as plan/Vibe-allowance traffic the way Mistral's own
        # clients do. Off by default — set MTG_MISTRAL_CALL_TYPE only if
        # you know your account is billed that way.
        call_type = os.environ.get("MTG_MISTRAL_CALL_TYPE", "").strip()
        if call_type:
            payload["metadata"] = {"call_type": call_type}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if stream is not None:
        payload["stream"] = True
    url = f"{_base_url(backend)}/chat/completions"

    def _run_buffered():
        resp = net.post_json(url, payload, headers=headers,
                             timeout=net.llm_timeout())
        return resp["choices"][0]["message"]

    def _run_streamed():
        """Returns (message, completed). completed=False means the stream
        was cut (no [DONE], no finish_reason) — providers do this under
        load, so callers may fall back to the buffered path. While the
        model is 'thinking' (chunks arrive, no answer text yet), a
        first_answer_deadline can abandon the attempt as too slow; info
        gets 'abandoned' set, nothing was printed, so the caller can
        safely retry with a different model."""
        content_parts, slots = [], {}
        completed = False
        text_started = False
        deadline_at = (time.monotonic() + first_answer_deadline
                       if first_answer_deadline else None)
        # A request can sit unacknowledged for a long time (queue), so a
        # per-read timeout bound to the deadline lets the watchdog fire
        # even when no chunk ever arrives.
        read_timeout = net.llm_timeout()
        if deadline_at is not None:
            read_timeout = min(read_timeout,
                               max(first_answer_deadline, 15))
        try:
            for line in net.post_sse_lines(url, payload, headers=headers,
                                           timeout=read_timeout):
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    completed = True
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                had_text = _assemble_stream_chunk(chunk, content_parts,
                                                  slots, stream)
                if had_text:
                    text_started = True
                if (deadline_at is not None and not text_started
                        and time.monotonic() > deadline_at):
                    if isinstance(info, dict):
                        info["abandoned"] = "slow-thinking"
                    return ({"role": "assistant", "content": None}, False)
                try:
                    if (chunk.get("choices") or [{}])[0].get("finish_reason"):
                        completed = True
                except (IndexError, TypeError):
                    pass
        except net.NetError:
            # read stall: abandon if the deadline already elapsed with
            # no answer text; otherwise it's the usual retryable error
            if (deadline_at is not None and not text_started
                    and time.monotonic() > deadline_at):
                if isinstance(info, dict):
                    info["abandoned"] = "slow-thinking"
                return ({"role": "assistant", "content": None}, False)
            raise
        message = {"role": "assistant",
                   "content": "".join(content_parts) or None}
        if slots:
            message["tool_calls"] = [slots[i] for i in sorted(slots)]
        return message, completed

    def _report(key):
        if isinstance(info, dict):
            info[key] = True

    def _run():
        if stream is None:
            return _run_buffered(), True
        message, completed = _run_streamed()
        if isinstance(info, dict) and info.get("abandoned"):
            return message, completed  # caller decides: switch models
        if not completed:
            if not message.get("content") and not message.get("tool_calls"):
                # The provider accepted the stream, then closed it without
                # delivering anything usable (no [DONE], no finish_reason) -
                # observed from Mistral's GLM backend under load. Nothing
                # was received, so a buffered attempt is the first real
                # try at this round, not duplicated work.
                if progress:
                    progress("provider cut the stream; "
                             "asking again without live output")
                _report("fell_back")
                return _run_buffered(), True
            if progress:
                progress("provider cut the stream early; "
                         "keeping the partial answer")
            _report("partial_stream")
        return message, completed

    t0 = time.time()
    try:
        message, _completed = _run()
    except net.NetError as e:
        # One retry on timeout/capacity: an interrupted LLM call returns
        # nothing to us, and 429 "backend_out_of_capacity" is transient.
        retryable = ("timed out" in str(e) or "stalled" in str(e)
                     or getattr(e, "status", None) == 429)
        if not retryable:
            raise LLMError(f"LLM call failed ({backend}): {e}") from e
        if progress:
            reason = "backends out of capacity" if e.status == 429 \
                else "model call timed out"
            progress(f"{reason}; retrying once")
        time.sleep(3 if getattr(e, "status", None) == 429 else 0)
        try:
            message, _completed = _run()
        except net.NetError as e2:
            raise LLMError(f"LLM call failed ({backend}): {e2}") from e2
        except (KeyError, IndexError, TypeError) as e2:
            raise LLMError(f"Unexpected LLM response shape: {e2}") from e2
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"Unexpected LLM response shape: {e}") from e
    if isinstance(elapsed, list):
        elapsed.append(time.time() - t0)
    return message


def _assistant_echo(message):
    """Message to put back into history after a tool-calling turn: content
    normalized to plain text (some backends reject block lists on echo)."""
    try:
        content = _content_to_text(message.get("content"))
    except LLMError:
        content = ""
    echo = {"role": "assistant", "content": content}
    if message.get("tool_calls"):
        echo["tool_calls"] = message["tool_calls"]
    return echo


def _run_tool_calls(message, runner, progress=None):
    """Execute the model's tool calls, returning tool-role messages."""
    out = []
    for call in message.get("tool_calls") or []:
        try:
            name = call["function"]["name"]
            args = json.loads(call["function"].get("arguments") or "{}")
        except (KeyError, TypeError, json.JSONDecodeError) as e:
            out.append({"role": "tool", "content": f"tool call unreadable: {e}"})
            continue
        if progress:
            progress(f"→ {name}({_fmt_args(args)})")
        try:
            result = runner(name, args)
        except Exception as e:  # noqa: BLE001 - tool errors go back to the model
            result = f"tool error: {e}"
        out.append({
            "role": "tool",
            "tool_call_id": call.get("id", ""),
            "name": name,
            "content": str(result),
        })
    return out


def ask_llm(system_prompt, context_blocks, question, backend=None, model=None,
            tools=None, tool_runner=None, max_tool_rounds=2, progress=None,
            stream=None, stats=None, fallback_model=None,
            first_answer_deadline=None, history=None):
    """The single LLM entry point per the plan. With tools+runner, runs a
    tool-calling loop until the model answers without calling a tool.
    stream is forwarded to each round (text deltas live); stats, a dict,
    is filled with rounds/model_seconds/tool_calls for latency forensics.

    fallback_model: if a streamed round exceeds first_answer_deadline
    seconds without producing answer text (a slow 'thinking' model, or a
    provider having a bad hour), the round is abandoned safely (nothing
    streamed yet) and retried with this model. Hard call failures (e.g.
    429 after retries) get the same treatment once per answer."""
    backend = resolve_backend(backend)
    model = model or default_model(backend)
    if stats is not None:
        stats.update({"rounds": 1, "model_seconds": 0.0, "tool_calls": 0,
                      "fallback_used": False})
    model_seconds = []
    fallback_used = False
    user_content = (
        "CONTEXT BLOCKS (each is introduced by its rule number or card name):\n\n"
        + "\n\n".join(context_blocks)
        + f"\n\nQUESTION: {question}"
    )
    # Conversation turns (REPL follow-ups): the assistant's prior answers
    # were themselves grounded in retrieved context, so replaying them is
    # honest memory; the system prompt still forbids ungrounded claims.
    history = list(history or [])[-MAX_HISTORY_MESSAGES:]
    messages = ([{"role": "system", "content": system_prompt}]
                + history + [{"role": "user", "content": user_content}])
    answer = None
    for _round in range(max_tool_rounds + 1):
        round_info = {}
        try:
            message = chat(backend, model, messages,
                           tools=tools if tool_runner else None,
                           progress=progress, stream=stream,
                           elapsed=model_seconds, info=round_info,
                           first_answer_deadline=(first_answer_deadline
                                                  if stream else None))
        except LLMError as e:
            if fallback_model and not fallback_used and fallback_model != model:
                if progress:
                    progress(f"{model} failed ({e}); trying {fallback_model}")
                model = fallback_model
                fallback_used = True
                if stats is not None:
                    stats["fallback_used"] = True
                round_info = {}
                message = chat(backend, model, messages,
                               tools=tools if tool_runner else None,
                               progress=progress, stream=None,
                               elapsed=model_seconds, info=round_info)
            else:
                raise
        if round_info.get("abandoned") == "slow-thinking":
            if fallback_model and not fallback_used and fallback_model != model:
                if progress:
                    progress(f"no answer text from {model} in "
                             f"{first_answer_deadline:.0f}s; "
                             f"switching to {fallback_model}")
                model = fallback_model
                fallback_used = True
                if stats is not None:
                    stats["fallback_used"] = True
                round_info = {}
                message = chat(backend, model, messages,
                               tools=tools if tool_runner else None,
                               progress=progress, stream=stream,
                               elapsed=model_seconds, info=round_info,
                               first_answer_deadline=first_answer_deadline)
            else:
                if progress:
                    progress("model slow but no fallback configured; "
                             "letting it finish")
        if round_info.get("fell_back"):
            # the provider keeps cutting streams; stop paying that tax
            stream = None
        wants_tools = tool_runner and message.get("tool_calls")
        if wants_tools and _round < max_tool_rounds:
            messages.append(_assistant_echo(message))
            messages.extend(_run_tool_calls(message, tool_runner, progress=progress))
            if progress:
                progress(f"waiting for {model} (round {_round + 2})")
            if stats is not None:
                stats["rounds"] = _round + 2
                stats["tool_calls"] += len(message["tool_calls"])
            continue
        if wants_tools:
            # Out of rounds: answer with what we have; run the last calls anyway.
            messages.append(_assistant_echo(message))
            messages.extend(_run_tool_calls(message, tool_runner, progress=progress))
            if progress:
                progress(f"final answer (budget used; asking {model} once more)")
            if stream is not None:
                progress("  (streaming restarts for the final answer)")
            message = chat(backend, model, messages, tools=None,
                           stream=stream, elapsed=model_seconds)
            if stats is not None:
                stats["rounds"] += 1
        answer = _content_to_text(message.get("content"))
        break
    if stats is not None:
        stats["model_seconds"] = sum(model_seconds)
    return backend, model, answer
