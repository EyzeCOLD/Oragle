import contextlib
import io
import unittest
from unittest import mock

from mtgjudge import cli


class ReplDispatchTest(unittest.TestCase):
    def setUp(self):
        self.state = {"offline": False, "verbose": False,
                      "backend": None, "model": None}

    def _dispatch(self, line):
        captured = []
        with mock.patch.object(cli, "cmd_rule",
                               lambda args: captured.append(("rule", args))), \
                mock.patch.object(cli, "cmd_card",
                                  lambda args: captured.append(("card", args))), \
                mock.patch.object(cli, "cmd_search",
                                  lambda args: captured.append(("search", args))), \
                mock.patch.object(cli, "cmd_stats",
                                  lambda args: captured.append(("stats", args))):
            cmd = cli._repl_dispatch(line, self.state)
        return cmd, captured

    def test_quit(self):
        self.assertEqual(self._dispatch(":quit")[0], "quit")
        self.assertEqual(cli._repl_dispatch(":exit", self.state), "quit")

    def test_rule_passes_number(self):
        _cmd, calls = self._dispatch(":rule   704.5j ")
        self.assertEqual(calls[0][1].number, "704.5j")

    def test_card_passes_words_and_offline_state(self):
        self.state["offline"] = True
        _cmd, calls = self._dispatch(":card Lightning Helix")
        self.assertEqual(calls[0][1].name, ["Lightning", "Helix"])
        self.assertTrue(calls[0][1].offline)

    def test_search_passes_query(self):
        _cmd, calls = self._dispatch(":search deathtouch trample")
        self.assertEqual(calls[0][1].query, ["deathtouch", "trample"])

    def test_unknown_command_keeps_session(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = cli._repl_dispatch(":bogus", self.state)
        self.assertIsNone(result)
        self.assertIn(":help", out.getvalue())

    def test_offline_and_verbose_toggle(self):
        for line in (":offline", ":offline"):
            with contextlib.redirect_stdout(io.StringIO()):
                cli._repl_dispatch(line, self.state)
        self.assertFalse(self.state["offline"])
        with contextlib.redirect_stdout(io.StringIO()):
            cli._repl_dispatch(":verbose", self.state)
        self.assertTrue(self.state["verbose"])


class UnbracketedHintTest(unittest.TestCase):
    """Exact-name search_cards lookups mean the user skipped [[ ]] —
    the runner prints a polite hint once, TTY stderr only, never for
    bracketed cards."""

    HELIX = {"name": "Lightning Helix", "mana_cost": "{R}{W}",
             "type_line": "Instant", "oracle_text": "3 damage."}

    def _run(self, runner, query, tty=True):
        err = io.StringIO()
        if tty:
            err.isatty = lambda: True
        with mock.patch.object(cli.cards, "search_cards",
                               return_value=([self.HELIX], 1)), \
                mock.patch("sys.stderr", err):
            result = runner("search_cards", {"query": query})
        return result, err.getvalue()

    def test_exact_lookup_hints_once(self):
        runner = cli.make_tool_runner([])
        _r, err1 = self._run(runner, '!"Lightning Helix"')
        self.assertIn("please surround card names", err1)
        self.assertIn("[[Lightning Helix]]", err1)
        _r, err2 = self._run(runner, '!"Grizzly Bears"')
        self.assertEqual(err2, "")  # once per answer

    def test_broad_query_no_hint(self):
        runner = cli.make_tool_runner([])
        _r, err = self._run(runner, "c<=2 colors:blue o:exile")
        self.assertEqual(err, "")

    def test_bare_exact_query_hints(self):
        runner = cli.make_tool_runner([])
        _r, err = self._run(runner, "!Squee")
        self.assertIn("[[Squee]]", err)

    def test_referenced_card_not_hinted(self):
        runner = cli.make_tool_runner([], referenced=("Lightning Helix",))
        _r, err = self._run(runner, '!"Lightning Helix"')
        self.assertEqual(err, "")

    def test_piped_stderr_no_hint(self):
        runner = cli.make_tool_runner([])
        _r, err = self._run(runner, '!"Lightning Helix"', tty=False)
        self.assertEqual(err, "")

    def test_hint_text_mentions_brackets(self):
        self.assertEqual(
            cli.UNBRACKETED_HINT.format(name="Lightning Helix"),
            "please surround card names such as [[Lightning Helix]] with "
            "brackets for an exact lookup")


class ConfigCommandTest(unittest.TestCase):
    def test_config_dispatch_prints_rows(self):
        import io, contextlib
        from mtgjudge import cli
        state = {"offline": False, "verbose": False,
                 "backend": "mistral", "model": None}
        rows = [("backend", "mistral", None),
                ("model", "zai-glm-5-3-flash", "/repo/.env")]
        with mock.patch.object(cli, "settings_report", return_value=rows):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                result = cli._repl_dispatch(":config", state)
        self.assertIsNone(result)
        self.assertIn("backend: mistral", out.getvalue())
        self.assertIn("model: zai-glm-5-3-flash", out.getvalue())
        self.assertIn("/repo/.env", out.getvalue())

    def test_config_in_completion_list(self):
        self.assertEqual(cli.complete_repl_line(":co", ":co"), [":config"])
        self.assertIn(":new", cli.complete_repl_line(":", ":"))


class ReplConversationTest(unittest.TestCase):
    def _res(self, blocks=None):
        return {"answer": "grounded answer", "backend": "mistral",
                "model": "m", "meta": {"effective_date": "x"},
                "blocks": blocks or ["RULE 101.1 [Game Rules]\ntext"],
                "context_ids": ["101.1"], "elapsed": 0.1,
                "stats": {}, "streamed": False}

    def _answer(self, state, question):
        with mock.patch.object(cli, "answer_question") as aq, \
                mock.patch.object(cli, "_print_answer"):
            aq.return_value = self._res()
            cli._repl_answer(question, state, interactive=False)
        return aq

    def test_answer_accumulates_conversation(self):
        state = {"offline": False, "verbose": False, "backend": "mistral",
                 "model": None, "history": [], "prior_blocks": []}
        aq = self._answer(state, "who goes first?")
        self.assertEqual(aq.call_args.kwargs["history"], [])
        self.assertEqual(aq.call_args.kwargs["prior_blocks"], [])
        self.assertEqual(state["history"], [
            {"role": "user", "content": "who goes first?"},
            {"role": "assistant", "content": "grounded answer"}])
        self.assertEqual(state["prior_blocks"], self._res()["blocks"])

        # second turn receives the accumulated conversation
        aq = self._answer(state, "in 2HG?")
        self.assertEqual(aq.call_args.kwargs["history"],
                         state["history"][:2])
        self.assertEqual(aq.call_args.kwargs["prior_blocks"],
                         self._res()["blocks"])

    def test_prior_blocks_deduped_on_repeat(self):
        state = {"offline": False, "verbose": False, "backend": "mistral",
                 "model": None, "history": [], "prior_blocks": []}
        self._answer(state, "q1")
        other = self._res(["RULE 808.4 [Variant]\nteam question"])
        with mock.patch.object(cli, "answer_question") as aq, \
                mock.patch.object(cli, "_print_answer"):
            aq.return_value = other
            cli._repl_answer("q2", state, interactive=False)
        heads = [b.splitlines()[0] for b in state["prior_blocks"]]
        self.assertEqual(len(heads), len(set(heads)))
        self.assertEqual(len(state["prior_blocks"]), 2)

    def test_history_and_blocks_capped(self):
        state = {"offline": False, "verbose": False, "backend": "mistral",
                 "model": None, "history": [], "prior_blocks": []}
        for i in range(10):
            self._answer(state, f"q{i}")
        self.assertLessEqual(len(state["history"]), 12)
        self.assertLessEqual(len(state["prior_blocks"]),
                             cli.MAX_SESSION_BLOCKS)
        self.assertTrue(state["prior_blocks"])  # capped, not emptied

    def test_thread_hint_printed_interactively(self):
        state = {"offline": False, "verbose": False, "backend": "mistral",
                 "model": None, "history": [], "prior_blocks": []}
        with mock.patch.object(cli, "answer_question",
                               return_value=self._res()), \
                mock.patch.object(cli, "_print_answer"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli._repl_answer("q1", state, interactive=True)
        # the shared-context default must stay visible after an answer
        self.assertIn(":new", out.getvalue())
        self.assertIn("fresh thread", out.getvalue())

    def test_thread_hint_absent_when_piped(self):
        state = {"offline": False, "verbose": False, "backend": "mistral",
                 "model": None, "history": [], "prior_blocks": []}
        with mock.patch.object(cli, "answer_question",
                               return_value=self._res()), \
                mock.patch.object(cli, "_print_answer"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli._repl_answer("q1", state, interactive=False)
        self.assertEqual(out.getvalue(), "")

    def test_new_resets_conversation(self):
        state = {"offline": False, "verbose": False, "backend": "mistral",
                 "model": None, "history": [{"role": "user", "content": "q"}],
                 "prior_blocks": ["RULE 101.1 [Game Rules]\ntext"]}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = cli._repl_dispatch(":new", state)
        self.assertIsNone(result)
        self.assertEqual(state["history"], [])
        self.assertEqual(state["prior_blocks"], [])
        self.assertIn("conversation reset", out.getvalue())


class AnswerQuestionCarryTest(unittest.TestCase):
    def test_prior_blocks_carried_deduped_after_new_retrieval(self):
        chunk = {"number": "101.1", "chapter": "Game Rules", "section": "",
                 "text": "text"}
        chunk2 = {"number": "101.2", "chapter": "Game Rules", "section": "",
                  "text": "can't beats can"}
        chunks = [dict(chunk), dict(chunk2)]
        carried = "RULE 700.1 [Old]\nold topic rule"
        rules_calls = []

        def search_rules(_chunks, query, top_k=8):
            rules_calls.append(query)
            return [(dict(chunk), 1.0)]

        with mock.patch.object(cli.store, "load_rules",
                               return_value=({"effective_date": "x"}, chunks)), \
                mock.patch.object(cli, "_card_blocks",
                                  return_value=("priority", [])), \
                mock.patch.object(cli.search, "search_rules",
                                  side_effect=search_rules), \
                mock.patch.object(cli.search, "question_anchor_numbers",
                                  return_value=[]), \
                mock.patch.object(cli.llm, "ask_llm",
                                  return_value=("mistral", "m", "ans")):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                res = cli.answer_question("priority", backend="mistral",
                                          model="m",
                                          history=[{"role": "user",
                                                    "content": "who goes first?"}],
                                          prior_blocks=[carried])
        heads = [b.splitlines()[0] for b in res["blocks"]]
        self.assertIn("RULE 101.1 [Game Rules]", heads)
        self.assertIn("RULE 700.1 [Old]", heads)
        self.assertIn("carried", err.getvalue())
        self.assertIn("who goes first?", rules_calls)


if __name__ == "__main__":
    unittest.main()
