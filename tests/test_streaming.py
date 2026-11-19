import io
import time
import unittest
from contextlib import redirect_stdout
import os
from unittest import mock

from mtgjudge import llm


def _chunk(delta, index=0):
    return {"choices": [{"index": index, "delta": delta,
                         "finish_reason": None}]}


class StreamChunkAssemblyTest(unittest.TestCase):
    def test_text_deltas_forwarded(self):
        parts, slots, seen = [], [], []
        llm._assemble_stream_chunk(_chunk({"content": "Hel"}), parts, slots,
                                   seen.append)
        llm._assemble_stream_chunk(_chunk({"content": "lo"}), parts, slots,
                                   seen.append)
        self.assertEqual("".join(parts), "Hello")
        self.assertEqual("".join(seen), "Hello")

    def test_block_list_content_handled(self):
        # zai-glm style: content as [{"type": "thinking"...}, {"type": "text"}]
        parts, slots, seen = [], [], []
        chunk = _chunk({"content": [
            {"type": "thinking", "thinking": "hm"},
            {"type": "text", "text": "answer"},
        ]})
        llm._assemble_stream_chunk(chunk, parts, slots, seen.append)
        self.assertEqual(parts, ["answer"])
        self.assertEqual(seen, ["answer"])

    def test_tool_call_deltas_reassembled(self):
        parts, slots, seen = [], {}, []
        llm._assemble_stream_chunk(
            _chunk({"tool_calls": [{"index": 0, "id": "call_1",
                                   "function": {"name": "search_rules"}}]}),
            parts, slots, seen.append)
        llm._assemble_stream_chunk(
            _chunk({"tool_calls": [{"index": 0,
                                    "function": {"arguments": '{"que'}}]}),
            parts, slots, seen.append)
        llm._assemble_stream_chunk(
            _chunk({"tool_calls": [{"index": 0,
                                    "function": {"arguments": 'ry": "x"}'}}]}),
            parts, slots, seen.append)
        self.assertEqual(len(slots), 1)
        call = slots[0]
        self.assertEqual(call["id"], "call_1")
        self.assertEqual(call["function"]["name"], "search_rules")
        self.assertEqual(call["function"]["arguments"], '{"query": "x"}')

    def test_empty_and_malformed_chunks_skipped(self):
        parts, slots, seen = [], {}, []
        llm._assemble_stream_chunk({}, parts, slots, seen.append)
        llm._assemble_stream_chunk({"choices": []}, parts, slots, seen.append)
        llm._assemble_stream_chunk(_chunk({}), parts, slots, seen.append)
        self.assertEqual((parts, slots, seen), ([], {}, []))


class StreamPrinterTest(unittest.TestCase):
    def test_line_buffered_rendering(self):
        os.environ["FORCE_COLOR"] = "1"
        self.addCleanup(os.environ.pop, "FORCE_COLOR", None)
        from mtgjudge import cli
        spin = mock.Mock()
        printed = io.StringIO()
        hook, finish, _mark = cli._make_stream_printer(["Lightning Helix"], spin)
        with redirect_stdout(printed):
            hook("**bold** star")
            hook("t: rule 702.19\nnext ")
            hook("line\n")
            live = finish()
        self.assertTrue(live)  # text streamed -> body is on screen
        out = printed.getvalue()
        # first two chunks joined into one complete line, styled
        self.assertIn("bold", out)
        self.assertIn("702.19", out)
        self.assertNotIn("**", out)
        # newline split across chunks still separates lines
        lines = out.split("\n")
        self.assertTrue(any("next line" in ln for ln in lines))
        finish()  # idempotent
        spin.stop.assert_called()

    def test_no_deltas_stops_spinner(self):
        from mtgjudge import cli
        spin = mock.Mock()
        printed = io.StringIO()
        hook, finish, _mark = cli._make_stream_printer([], spin)
        with redirect_stdout(printed):
            live = finish()
        # nothing streamed live -> caller must print the body itself
        self.assertFalse(live)
        self.assertEqual(printed.getvalue(), "")
        spin.stop.assert_called_once()

    def test_fallback_round_does_not_flag_streamed(self):
        """The Helix bug: round 1 stream empty -> streaming disabled for
        later rounds; nothing arrives live, so finish() must report False
        and the answer must be printed at the end, not suppressed."""
        from mtgjudge import cli
        spin = mock.Mock()
        hook, finish, _mark = cli._make_stream_printer([], spin)
        self.assertFalse(finish())


class RuleRefRegexTest(unittest.TestCase):
    def test_finds_citation_references(self):
        from mtgjudge.cli import RULE_REF_RE
        text = ('This is called the "legend rule" (see rule 704.5j). '
                "See rules 603.3 and rule 702.2. Also rule 704.5aa.")
        self.assertEqual(RULE_REF_RE.findall(text),
                         ["704.5j", "603.3", "702.2", "704.5aa"])


if __name__ == "__main__":
    unittest.main()


class SlowModelWatchdogTest(unittest.TestCase):
    def _thinking_chunks(self, n, delay):
        for i in range(n):
            yield ('data: {"choices":[{"index":0,"delta":{"content":'
                   '[{"type":"thinking","thinking":[{"type":"text","text":"hm"}]'
                   '}]},"finish_reason":null}]}\n')
            time.sleep(delay)
        yield "data: [DONE]\n"

    def test_slow_thinking_abandoned(self):
        from mtgjudge import llm
        seen = []
        info = {}
        with mock.patch.object(llm.net, "post_sse_lines",
                               return_value=self._thinking_chunks(6, 0.03)):
            message, completed = None, None

            def run():
                # chat returns just the message; wrap to capture pair
                return llm.chat("mistral", "m", [{"role": "user", "content": "q"}],
                                stream=seen.append, info=info,
                                first_answer_deadline=0.1)

            msg = run()
        # deadline passed with only thinking chunks -> round abandoned
        self.assertEqual(info.get("abandoned"), "slow-thinking")
        self.assertIsNone(msg.get("content"))
        self.assertEqual(seen, [])  # nothing was streamed to the user

    def test_normal_answer_not_abandoned(self):
        from mtgjudge import llm
        lines = [
            'data: {"choices":[{"index":0,"delta":{"content":"Hi"}}]}\n',
            "data: [DONE]\n",
        ]
        seen = []
        info = {}
        with mock.patch.object(llm.net, "post_sse_lines",
                               return_value=iter(lines)):
            msg = llm.chat("mistral", "m", [{"role": "user", "content": "q"}],
                           stream=seen.append, info=info,
                           first_answer_deadline=5)
        self.assertEqual(msg["content"], "Hi")
        self.assertNotIn("abandoned", info)
        self.assertEqual("".join(seen), "Hi")


class FallbackModelTest(unittest.TestCase):
    def test_abandoned_round_switches_to_fallback(self):
        from mtgjudge import llm
        calls = []

        def fake_chat(backend, model, messages, **kw):
            calls.append(model)
            info = kw.get("info")
            if model == "slow-thinker":
                if isinstance(info, dict):
                    info["abandoned"] = "slow-thinking"
                return {"role": "assistant", "content": None}
            return {"role": "assistant", "content": "fallback answer"}

        notes = []
        with mock.patch.object(llm, "chat", side_effect=fake_chat), \
                mock.patch.object(llm, "resolve_backend", return_value="mistral"), \
                mock.patch.object(llm, "default_model",
                                  return_value="slow-thinker"):
            backend, model, answer = llm.ask_llm(
                "sys", ["ctx"], "q",
                stream=lambda d: None, stats={},
                fallback_model="fast-model", first_answer_deadline=5,
                progress=notes.append)
        self.assertEqual(calls, ["slow-thinker", "fast-model"])
        self.assertEqual(model, "fast-model")
        self.assertEqual(answer, "fallback answer")
        self.assertTrue(any("switching" in n for n in notes), notes)

    def test_hard_failure_gets_one_fallback_attempt(self):
        from mtgjudge import llm
        calls = []

        def fake_chat(backend, model, messages, **kw):
            calls.append(model)
            if model == "flakey":
                raise llm.LLMError("HTTP 429: out of capacity")
            return {"role": "assistant", "content": "rescued"}

        with mock.patch.object(llm, "chat", side_effect=fake_chat), \
                mock.patch.object(llm, "resolve_backend", return_value="mistral"), \
                mock.patch.object(llm, "default_model", return_value="flakey"):
            backend, model, answer = llm.ask_llm(
                "sys", ["ctx"], "q", stats={},
                fallback_model="steady-model", progress=lambda n: None)
        self.assertEqual(calls, ["flakey", "steady-model"])
        self.assertEqual(answer, "rescued")


if __name__ == "__main__":
    unittest.main()
