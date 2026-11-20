import json
import os
import unittest
from unittest import mock

from mtgjudge import cards, carddb, cli, llm


def tool_call_response(call_id, name, args):
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }],
            }
        }]
    }


def text_response(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


class ToolLoopTest(unittest.TestCase):
    def setUp(self):
        self.env = {"MISTRAL_API_KEY": "k"}
        for k, v in self.env.items():
            os.environ.setdefault(k, v)
        self.calls = []

    def _script(self, responses):
        def fake_post(url, payload, headers=None, timeout=0):
            self.calls.append(payload)
            return responses[len(self.calls) - 1]
        return fake_post

    def test_tool_round_trip(self):
        responses = [
            tool_call_response("c1", "search_cards", {"query": "c<=2 colors:blue"}),
            text_response("Blue instants found."),
        ]
        runner_calls = []

        def runner(name, args):
            runner_calls.append((name, args))
            return "2 card(s) match. Showing 1:\n- Unsummon {U} | Instant | return"

        with mock.patch.object(llm.net, "post_json", side_effect=self._script(responses)), \
             mock.patch.dict(os.environ, self.env):
            backend, model, answer = llm.ask_llm(
                "sys", [], "any cheap blue stuff?",
                tools=[{"type": "function", "function": {"name": "search_cards",
                                                        "parameters": {"type": "object"}}}],
                tool_runner=runner,
            )
        self.assertEqual(answer, "Blue instants found.")
        self.assertEqual(runner_calls, [("search_cards", {"query": "c<=2 colors:blue"})])
        # round 2 payload must contain the assistant echo + tool result
        sent = self.calls[1]["messages"]
        self.assertEqual(sent[2]["role"], "assistant")
        self.assertEqual(sent[2]["tool_calls"][0]["id"], "c1")
        self.assertEqual(sent[3]["role"], "tool")
        self.assertIn("Unsummon", sent[3]["content"])
        # round 1 payload offered the tools
        self.assertIn("tools", self.calls[0])

    def test_runs_last_tools_when_out_of_rounds(self):
        responses = [
            tool_call_response("c1", "search_cards", {"query": "t:goblin"}),
            text_response("answered with search results."),
        ]
        runner_calls = []

        def runner(name, args):
            runner_calls.append(args)
            return "0 cards matched."

        with mock.patch.object(llm.net, "post_json", side_effect=self._script(responses)), \
             mock.patch.dict(os.environ, self.env):
            _b, _m, answer = llm.ask_llm(
                "sys", [], "q", tools=[], tool_runner=runner, max_tool_rounds=0,
            )
        self.assertEqual(answer, "answered with search results.")
        self.assertEqual(len(runner_calls), 1)
        self.assertEqual(self.calls[1]["messages"][3]["content"], "0 cards matched.")

    def test_no_tools_single_shot(self):
        with mock.patch.object(llm.net, "post_json",
                               side_effect=self._script([text_response("plain answer")])) as post, \
             mock.patch.dict(os.environ, self.env):
            _b, _m, answer = llm.ask_llm("sys", [], "hi")
        self.assertEqual(answer, "plain answer")
        self.assertNotIn("tools", self.calls[0])
        self.assertEqual(post.call_count, 1)

    def test_history_inserted_between_system_and_question(self):
        history = [{"role": "user", "content": "who goes first?"},
                   {"role": "assistant", "content": "random (808.4)."}]
        with mock.patch.object(llm.net, "post_json",
                               side_effect=self._script([text_response("ok")])) as post, \
             mock.patch.dict(os.environ, self.env):
            _b, _m, answer = llm.ask_llm("sys", [], "in 2HG?",
                                         history=history)
        self.assertEqual(answer, "ok")
        msgs = self.calls[0]["messages"]
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[1]["role"], "user")
        self.assertEqual(msgs[1]["content"], "who goes first?")
        self.assertEqual(msgs[2]["role"], "assistant")
        self.assertIn("QUESTION: in 2HG?", msgs[3]["content"])
        self.assertEqual(post.call_count, 1)

    def test_history_capped_to_last_messages(self):
        history = [{"role": "user", "content": f"q{i}"} for i in range(10)]
        with mock.patch.object(llm.net, "post_json",
                               side_effect=self._script([text_response("ok")])), \
             mock.patch.dict(os.environ, self.env):
            llm.ask_llm("sys", [], "now", history=history)
        msgs = self.calls[0]["messages"]
        sent = [m["content"] for m in msgs[1:-1]]
        self.assertEqual(sent, [f"q{i}" for i in range(4, 10)])

    def test_progress_events_report_tools_and_rounds(self):
        responses = [
            tool_call_response("c1", "search_rules", {"query": "legend rule"}),
            tool_call_response("c2", "search_cards", {"query": "t:legendary"}),
            text_response("done"),
        ]

        def runner(name, args):
            return "results"

        events = []
        with mock.patch.object(llm.net, "post_json", side_effect=self._script(responses)), \
             mock.patch.dict(os.environ, self.env):
            llm.ask_llm("sys", [], "q", tools=[], tool_runner=runner,
                        max_tool_rounds=2, progress=events.append)
        self.assertTrue(any("search_rules(query='legend rule')" in e for e in events))
        self.assertTrue(any("search_cards(query='t:legendary')" in e for e in events))
        self.assertTrue(any("round 2" in e for e in events))
        self.assertTrue(any("round 3" in e for e in events))


class ContentEchoTest(unittest.TestCase):
    def test_block_list_echoed_as_text(self):
        msg = {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": [{"type": "text", "text": "h"}]},
                        {"type": "text", "text": "searching"}],
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "t", "arguments": "{}"}}],
        }
        echo = llm._assistant_echo(msg)
        self.assertEqual(echo["content"], "searching")
        self.assertIn("tool_calls", echo)


class SearchRulesRunnerTest(unittest.TestCase):
    def test_returns_rule_blocks(self):
        from mtgjudge import cli
        from mtgjudge.cr import Rule

        chunks = [Rule(number="700.4", text="The term dies means put into a graveyard.",
                       chapter="Additional Rules", section="General")]
        runner = cli.make_tool_runner(chunks)
        result = runner("search_rules", {"query": "dies graveyard", "top": 3})
        self.assertIn("RULE 700.4", result)
        self.assertIn("dies means", result)

    def test_bad_vocabulary_gets_hint(self):
        from mtgjudge import cli
        from mtgjudge.cr import Rule

        chunks = [Rule(number="700.4", text="The term dies means put into a graveyard.",
                       chapter="", section="")]
        runner = cli.make_tool_runner(chunks)
        result = runner("search_rules", {"query": "zzz qqq"})
        self.assertIn("No rules matched", result)

    def test_unknown_tool(self):
        from mtgjudge import cli
        runner = cli.make_tool_runner([])
        self.assertIn("no such tool", runner("does_not_exist", {}))


class SearchTest(unittest.TestCase):
    def test_search_404_empty(self):
        from mtgjudge.net import NetError

        def raise_404(url, timeout=0, headers=None):
            raise NetError("HTTP 404", status=404)

        with mock.patch.object(cards.net, "get_json", side_effect=raise_404):
            found, total = cards.search_cards("bogus:nothing")
        self.assertEqual((found, total), ([], 0))

    def test_brief_format_truncates(self):
        found = [
            {"name": "Into the Roil", "mana_cost": "{1}{U}", "type_line": "Instant",
             "oracle_text": "Return target nonland permanent. " + "word " * 140},
            {"name": "Faceless", "type_line": "", "oracle_text": ""},
        ]
        brief = cards.format_search_brief(found, 137)
        self.assertIn("137 card(s) match. Showing 2:", brief)
        self.assertIn("Into the Roil {1}{U} | Instant |", brief)
        # few results (a card-resolution lookup) still truncate, at 600
        self.assertIn("...", brief.splitlines()[1])
        self.assertIn("(no rules text)", brief)

    def test_brief_small_result_set_gets_bigger_budget(self):
        found = [
            {"name": "Card", "mana_cost": "{1}{U}", "type_line": "Instant",
             "oracle_text": "word " * 50},  # 250 chars: >220, <600
        ]
        brief = cards.format_search_brief(found, 1)
        self.assertNotIn("...", brief.splitlines()[1])


class MistralCallTypeTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def _fake_post(self):
        def fake_post(url, payload, headers=None, timeout=0):
            self.calls.append((url, payload, headers))
            return text_response("ok")
        return fake_post

    def test_call_type_marker_off_by_default(self):
        # clear=True: other tests may load the developer's real .env
        # (which can set MTG_MISTRAL_CALL_TYPE); the default must still
        # be "no marker" in a clean environment.
        with mock.patch.object(llm.net, "post_json",
                               side_effect=self._fake_post()), \
             mock.patch.dict(os.environ, {"MISTRAL_API_KEY": "k"},
                             clear=True):
            llm.chat("mistral", "m", [{"role": "user", "content": "q"}])
        self.assertNotIn("metadata", self.calls[0][1])

    def test_call_type_marker_enabled_via_env(self):
        with mock.patch.object(llm.net, "post_json",
                               side_effect=self._fake_post()), \
             mock.patch.dict(os.environ,
                             {"MISTRAL_API_KEY": "k",
                              "MTG_MISTRAL_CALL_TYPE": "main_call"}):
            llm.chat("mistral", "m", [{"role": "user", "content": "q"}])
        self.assertEqual(self.calls[0][1]["metadata"],
                         {"call_type": "main_call"})

    def test_other_backends_never_send_marker(self):
        with mock.patch.object(llm.net, "post_json",
                               side_effect=self._fake_post()), \
             mock.patch.dict(os.environ, {"MTG_MISTRAL_CALL_TYPE": "x",
                                          "MTG_API_KEY": "sk"}):
            llm.chat("lmstudio", "m", [{"role": "user", "content": "q"}])
            llm.chat("openai", "m", [{"role": "user", "content": "q"}])
        for _url, payload, _headers in self.calls:
            self.assertNotIn("metadata", payload)


class OpenAIBackendTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def test_resolves_when_api_base_set(self):
        with mock.patch.dict(os.environ, {"MTG_API_BASE": "https://x/v1"}):
            self.assertEqual(llm.resolve_backend(), "openai")

    def test_backend_env_wins(self):
        with mock.patch.dict(os.environ, {"MTG_API_BASE": "https://x/v1",
                                          "MTG_BACKEND": "ollama"}):
            self.assertEqual(llm.resolve_backend(), "ollama")

    def test_chat_uses_base_and_key(self):
        def fake_post(url, payload, headers=None, timeout=0):
            self.calls.append((url, headers))
            return text_response("ok")
        with mock.patch.object(llm.net, "post_json", side_effect=fake_post), \
             mock.patch.dict(os.environ,
                             {"MTG_API_BASE": "https://api.example.com/v1",
                              "MTG_API_KEY": "sk-x"}):
            llm.chat("openai", "m", [{"role": "user", "content": "q"}])
        self.assertEqual(self.calls[0][0],
                         "https://api.example.com/v1/chat/completions")
        self.assertEqual(self.calls[0][1]["Authorization"], "Bearer sk-x")

    def test_chat_requires_key(self):
        with mock.patch.dict(os.environ, {"MTG_API_BASE": "https://x/v1",
                                          "MTG_API_KEY": ""}):
            with self.assertRaises(llm.LLMError):
                llm.chat("openai", "m", [{"role": "user", "content": "q"}])


class LocalSqlToolTest(unittest.TestCase):
    """query_local_cards passes model SQL straight to carddb.run_sql;
    the unbracketed hint fires on the taught exact-lookup idiom only."""

    def test_sql_passed_through_with_hint_idiom(self):
        events = []

        def fake_run_sql(sql):
            return '{"name": "Lightning Helix"}'

        with mock.patch.object(cli.carddb, "run_sql", side_effect=fake_run_sql):
            runner = cli.make_tool_runner([], emit=events.append)
            result = runner("query_local_cards",
                            {"sql": "SELECT name FROM cards "
                                    "WHERE lookup_all LIKE '%Lightning Helix %'"})
        self.assertIn("Lightning Helix", result)
        self.assertEqual(len(events), 1)  # the unbracketed hint
        self.assertIn("[[Lightning Helix]]", events[0])

    def test_empty_sql_rejected(self):
        runner = cli.make_tool_runner([])
        self.assertEqual(runner("query_local_cards", {"sql": "  "}),
                         "error: empty query")

    def test_missing_tool_reported(self):
        runner = cli.make_tool_runner([])
        self.assertIn("no such tool", runner("search_cards", {"query": "x"}))


class ScryfallDownTest(unittest.TestCase):
    """Cut off from Scryfall: the live tool degrades to a tool result
    (never an exception), and the model keeps going to its answer."""

    def test_live_search_failure_is_a_tool_result(self):
        with mock.patch.object(cli.cards, "search_cards",
                               side_effect=RuntimeError("connection refused")):
            runner = cli.make_tool_runner([])
            result = runner("search_scryfall", {"query": "o:exile"})
        self.assertIn("search failed: connection refused", result)

    def test_pipeline_continues_after_tool_failure(self):
        # scripted model: round 1 calls scryfall (fails), round 2 answers
        responses = [
            tool_call_response("c1", "search_scryfall", {"query": "o:exile"}),
            text_response("Scryfall is unreachable, but per rule 101.1 ..."),
        ]
        calls = []

        def fake_post(url, payload, headers=None, timeout=0):
            calls.append(payload)
            return responses[len(calls) - 1]

        runner = cli.make_tool_runner([])

        with mock.patch.object(llm.net, "post_json", side_effect=fake_post), \
             mock.patch.dict(os.environ, {"MISTRAL_API_KEY": "k"}, clear=True), \
             mock.patch.object(cli.cards, "search_cards",
                               side_effect=RuntimeError("connection refused")):
            backend, model, answer = llm.ask_llm(
                "sys", [], "which cards exile?",
                tools=[{"type": "function",
                        "function": {"name": "search_scryfall",
                                     "parameters": {"type": "object"}}},
                       {"type": "function",
                        "function": {"name": "query_local_cards",
                                     "parameters": {"type": "object"}}}],
                tool_runner=runner)
        self.assertIn("unreachable", answer)



if __name__ == "__main__":
    unittest.main()
