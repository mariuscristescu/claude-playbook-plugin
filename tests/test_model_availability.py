#!/usr/bin/env python3
"""Point tests for model-availability discovery + failure classification (task 012).

Covers: the new returncode-first format_judge_output semantics; the
classify_failure corpus (built from LIVE captured provider outputs, run
through format_judge_output's exact shape — not raw stderr); judge_failed /
budget_exceeded block-start anchoring; codex cache + agy output parsers;
check_pins verdict cross-check with probes monkeypatched (no network); and
models select create/preserve behavior.

Pure stdlib unittest (no hypothesis — honors the stdlib-only runtime invariant).
Run: python3 tests/test_model_availability.py   (or: python3 -m unittest ...)
"""
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

# The runtime tree is plugins/playbook/ (dispatcher sets PYTHONPATH there).
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))

from provider import sandbox  # noqa: E402
from tasks import models_check as mc  # noqa: E402

_CP = subprocess.CompletedProcess

# Live-captured provider outputs (task 012 References corpus).
_CODEX_GONE_STDERR = (
    'warning: Model metadata for `gpt-5.3-codex` not found.\n'
    'ERROR: {"type":"error","status":400,"error":{"type":"invalid_request_error",'
    '"message":"The \'gpt-5.3-codex\' model is not supported when using Codex '
    'with a ChatGPT account."}}'
)
_CODEX_CLI_OLD_STDERR = (
    'warning: Model metadata for `gpt-5.6-luna` not found.\n'
    'ERROR: {"type":"error","status":400,"error":{"type":"invalid_request_error",'
    '"message":"The \'gpt-5.6-luna\' model requires a newer version of Codex. '
    'Please upgrade to the latest app or CLI and try again."}}'
)
_CLAUDE_GONE_STDOUT = (
    "There's an issue with the selected model (claude-bogus-model-99). It may "
    "not exist or you may not have access to it. Run --model to pick a different model."
)
_BUDGET_STDOUT = "Error: Exceeded USD budget (2)"
_EFFORT_ERROR = ("(error: unknown reasoning effort 'nope' in codex model spec "
                 "'gpt-5.5:nope'. Use one of: high, low, max, medium, minimal, ultra, xhigh.)")

_CACHE_FIXTURE = json.dumps({
    "fetched_at": "2026-07-13T10:38:44.254295Z",
    "client_version": "0.144.3",
    "models": [
        {"slug": "gpt-5.6-sol", "visibility": "list",
         "supported_reasoning_levels": [{"effort": e} for e in
                                        ("low", "medium", "high", "xhigh", "max", "ultra")]},
        {"slug": "gpt-5.5", "visibility": "list",
         "supported_reasoning_levels": [{"effort": e} for e in
                                        ("low", "medium", "high", "xhigh")]},
        {"slug": "codex-auto-review", "visibility": "hide",
         "supported_reasoning_levels": [{"effort": "medium"}]},
    ],
})


class FormatJudgeOutputTest(unittest.TestCase):
    """B0: returncode wins over stdout; both streams survive failures."""

    def test_nonzero_exit_with_stdout_progress_keeps_both_streams(self):
        out = sandbox.format_judge_output(
            _CP([], 1, stdout="model: gpt-5.3-codex\nthinking...", stderr=_CODEX_GONE_STDERR))
        self.assertTrue(out.startswith("(FAILED — exit 1)"))
        self.assertIn("thinking...", out)
        self.assertIn("model is not supported", out)

    def test_success_returns_stdout_verbatim(self):
        review = 'Good review quoting "invalid_request_error" and model is not supported.'
        self.assertEqual(sandbox.format_judge_output(_CP([], 0, stdout=review, stderr="")), review)

    def test_success_empty_is_no_output(self):
        self.assertEqual(sandbox.format_judge_output(_CP([], 0, stdout="", stderr="")), "(no output)")

    def test_failure_with_no_streams_says_so(self):
        self.assertIn("(no output captured)", sandbox.format_judge_output(_CP([], 3, stdout="", stderr="")))

    def test_stdout_borne_error_with_nonzero_exit_is_failure_marked(self):
        out = sandbox.format_judge_output(_CP([], 1, stdout=_CLAUDE_GONE_STDOUT, stderr=""))
        self.assertTrue(out.startswith("(FAILED — exit 1)"))
        self.assertIn("There's an issue with the selected model", out)

    def test_long_stderr_keeps_the_error_tail(self):
        # Signature at the END survives the 2000-char tail even with a long preamble.
        noisy = ("x" * 5000) + "\n" + _CODEX_GONE_STDERR
        out = sandbox.format_judge_output(_CP([], 1, stdout="", stderr=noisy))
        self.assertIn("model is not supported", out)

    def test_signature_before_truncation_point_survives(self):
        # D3: signature followed by 5000 chars of trailing noise would be cut
        # by the 2000-char tail — the [signature] line must rescue it, and the
        # classifier must still fire.
        noisy = _CODEX_GONE_STDERR + "\n" + ("y" * 5000)
        out = sandbox.format_judge_output(_CP([], 1, stdout="", stderr=noisy))
        self.assertIn("[signature]", out)
        self.assertIn("model is not supported", out)
        self.assertEqual(mc.classify_failure(out), mc.MODEL_UNAVAILABLE)


class ClassifyFailureTest(unittest.TestCase):
    """B1: corpus classification over post-format strings, R2 guards."""

    def _formatted(self, rc, stdout="", stderr=""):
        return sandbox.format_judge_output(_CP([], rc, stdout=stdout, stderr=stderr))

    def test_codex_model_gone(self):
        s = self._formatted(1, stdout="progress...", stderr=_CODEX_GONE_STDERR)
        self.assertEqual(mc.classify_failure(s), mc.MODEL_UNAVAILABLE)

    def test_codex_cli_too_old(self):
        s = self._formatted(1, stderr=_CODEX_CLI_OLD_STDERR)
        self.assertEqual(mc.classify_failure(s), mc.CLI_UPGRADE_REQUIRED)

    def test_codex_patterns_mutually_exclusive(self):
        s = self._formatted(1, stderr=_CODEX_GONE_STDERR + "\n" + _CODEX_CLI_OLD_STDERR)
        self.assertEqual(mc.classify_failure(s), mc.CLI_UPGRADE_REQUIRED)

    def test_claude_model_gone_via_stdout(self):
        s = self._formatted(1, stdout=_CLAUDE_GONE_STDOUT)
        self.assertEqual(mc.classify_failure(s), mc.MODEL_UNAVAILABLE)

    def test_timeout_and_effort_error_are_other(self):
        self.assertEqual(mc.classify_failure("(timed out after 300s)"), mc.OTHER)
        self.assertEqual(mc.classify_failure(_EFFORT_ERROR), mc.OTHER)

    def test_budget_is_other_but_failed(self):
        self.assertEqual(mc.classify_failure(_BUDGET_STDOUT), mc.OTHER)
        self.assertTrue(mc.judge_failed(_BUDGET_STDOUT))
        self.assertTrue(mc.budget_exceeded(_BUDGET_STDOUT))

    def test_successful_review_quoting_patterns_never_classifies(self):
        review = ("Review: the classifier matches invalid_request_error + "
                  "model is not supported and There's an issue with the selected model.")
        s = self._formatted(0, stdout=review)
        self.assertEqual(mc.classify_failure(s), mc.OTHER)
        self.assertFalse(mc.judge_failed(s))

    def test_budget_quoted_mid_review_not_flagged(self):
        review = f"Fine review. A judge once printed {_BUDGET_STDOUT} mid-run."
        self.assertFalse(mc.judge_failed(review))
        self.assertFalse(mc.budget_exceeded(review))


class ParserTest(unittest.TestCase):
    def test_codex_cache_parser(self):
        parsed = mc.parse_codex_cache(_CACHE_FIXTURE)
        self.assertEqual(parsed["client_version"], "0.144.3")
        self.assertIn("gpt-5.6-sol", parsed["models"])
        self.assertIn("ultra", parsed["models"]["gpt-5.6-sol"])
        self.assertIn("codex-auto-review", parsed["models"])  # hidden entries kept

    def test_cache_age(self):
        self.assertGreater(mc.cache_age_days("2026-07-01T00:00:00Z"), 1)
        self.assertIsNone(mc.cache_age_days("not-a-date"))
        self.assertIsNone(mc.cache_age_days(None))

    def test_agy_models_parser(self):
        out = "Gemini 3.5 Flash (High)\n\nGemini 3.1 Pro (Low)\n"
        self.assertEqual(mc.parse_agy_models(out),
                         ["Gemini 3.5 Flash (High)", "Gemini 3.1 Pro (Low)"])


class CheckPinsTest(unittest.TestCase):
    """Verdict cross-check with all provider I/O monkeypatched (no network)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        (self.project / ".agent").mkdir()
        cache = mc.parse_codex_cache(_CACHE_FIXTURE)
        avail = mock.MagicMock()
        avail.is_available.return_value = True
        self._patches = [
            mock.patch.object(mc, "load_codex_cache", return_value=cache),
            mock.patch.object(mc, "installed_cli_version", return_value="0.144.3"),
            mock.patch.object(mc, "list_agy_models", return_value=["Gemini 3.5 Flash (High)"]),
            mock.patch.object(mc, "_adapter_classes", return_value={
                "claude": avail, "codex": avail, "agy": avail, "pi": avail}),
            mock.patch.object(mc, "probe_claude_model",
                              side_effect=lambda m, timeout=0: (mc.OK, "responds")
                              if m == "claude-fable-5" else (mc.GONE, "claude rejects this model id")),
            mock.patch.object(mc, "probe_codex_model",
                              side_effect=lambda m, effort=None, timeout=0: (mc.OK, "responds")
                              if m in ("gpt-5.6-sol", "gpt-5.5") else (mc.GONE, "rejected")),
            mock.patch("provider.sandbox.load_judge_config", return_value={
                "default_judge": "claude",
                "panel": ["claude:claude-fable-5", "claude:claude-dead-1",
                          "codex:gpt-5.6-sol:high", "codex:gpt-5.5:nope",
                          "codex:gpt-5.3-codex", "agy", "codex:"]}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _verdicts(self, report):
        return {e["spec"]: e["verdict"] for e in report["entries"]}

    def test_probed_verdicts(self):
        report = mc.check_pins(self.project, probe=True)
        v = self._verdicts(report)
        self.assertEqual(v["claude:claude-fable-5"], mc.OK)
        self.assertEqual(v["claude:claude-dead-1"], mc.GONE)
        self.assertEqual(v["codex:gpt-5.6-sol:high"], mc.OK)
        self.assertEqual(v["codex:gpt-5.5:nope"], mc.BAD_EFFORT)  # validator, no probe
        self.assertEqual(v["codex:gpt-5.3-codex"], mc.GONE)
        self.assertEqual(v["agy"], mc.UNVERIFIABLE)
        self.assertEqual(v["claude"], mc.OK)  # default_judge, bare provider
        self.assertTrue(any("empty variant" in w for w in report["warnings"]))  # R13

    def test_no_probe_degrades_to_cache_and_unprobed(self):
        report = mc.check_pins(self.project, probe=False)
        v = self._verdicts(report)
        self.assertEqual(v["claude:claude-dead-1"], mc.UNPROBED)
        self.assertEqual(v["codex:gpt-5.6-sol:high"], mc.LISTED)
        self.assertEqual(v["codex:gpt-5.3-codex"], mc.GONE)  # not even in cache
        self.assertEqual(v["codex:gpt-5.5:nope"], mc.BAD_EFFORT)

    def test_extra_specs_included(self):
        report = mc.check_pins(self.project, probe=False, extra_specs=["codex:gpt-5.4"])
        self.assertIn("codex:gpt-5.4", self._verdicts(report))

    def test_bad_pins_selects_actionable_verdicts(self):
        report = mc.check_pins(self.project, probe=True)
        specs = {e["spec"] for e in mc.bad_pins(report)}
        self.assertEqual(specs, {"claude:claude-dead-1", "codex:gpt-5.5:nope",
                                 "codex:gpt-5.3-codex"})

    def test_cli_older_than_cache_writer_warns(self):
        with mock.patch.object(mc, "installed_cli_version", return_value="0.142.5"):
            report = mc.check_pins(self.project, probe=False)
        self.assertTrue(any("older than the cache writer" in w for w in report["warnings"]))

    def test_claude_candidates_get_probed_verdicts(self):
        # D4/I7: user-supplied candidates become real probed entries.
        report = mc.check_pins(self.project, probe=True,
                               claude_candidates=["claude-fable-5", "claude-dead-1"])
        v = self._verdicts(report)
        self.assertEqual(v.get("claude:claude-dead-1"), mc.GONE)

    def test_provider_missing_is_a_bad_pin(self):
        # D6/I10: a pin whose provider CLI is absent can't run here — check
        # must exit nonzero, so it counts as bad.
        unavail = mock.MagicMock()
        unavail.is_available.return_value = False
        avail = mock.MagicMock()
        avail.is_available.return_value = True
        with mock.patch.object(mc, "_adapter_classes", return_value={
                "claude": avail, "codex": avail, "agy": avail, "pi": unavail}), \
             mock.patch("provider.sandbox.load_judge_config",
                        return_value={"default_judge": None, "panel": ["pi"]}):
            report = mc.check_pins(self.project, probe=False)
        self.assertEqual(self._verdicts(report)["pi"], mc.PROVIDER_MISSING)
        self.assertEqual([e["spec"] for e in mc.bad_pins(report)], ["pi"])

    def test_unknown_alias_provider_is_bad_pin_not_crash(self):
        # D6/I9: a hand-authored alias with an unknown provider must yield an
        # actionable entry, not a KeyError.
        with mock.patch("provider.sandbox.resolve_judge_spec",
                        return_value=("mystery", "m-1")), \
             mock.patch("provider.sandbox.load_judge_config",
                        return_value={"default_judge": None, "panel": ["mystery:m-1"]}):
            report = mc.check_pins(self.project, probe=False)
        e = report["entries"][0]
        self.assertEqual(e["verdict"], mc.GONE)
        self.assertIn("unknown provider", e["detail"])

    def test_malformed_cache_entries_tolerated(self):
        # D6/I11: null entries / wrong shapes must not crash the parser.
        parsed = mc.parse_codex_cache(json.dumps(
            {"models": [None, {"slug": "ok-model", "supported_reasoning_levels": None},
                        {"no_slug": True}, {"slug": "x", "supported_reasoning_levels": [None]}]}))
        self.assertEqual(parsed["models"], {"ok-model": [], "x": []})
        with self.assertRaises(ValueError):
            mc.parse_codex_cache("[1,2,3]")


class ProbeArgvTest(unittest.TestCase):
    def test_codex_probe_argv_carries_effort(self):
        # D4/I16: the probe must send the same model_reasoning_effort the
        # judge path sends, so an unsupported effort fails at check time.
        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            return _CP(argv, 0, stdout="ok", stderr="")

        with mock.patch.object(mc.subprocess, "run", side_effect=fake_run):
            verdict, _ = mc.probe_codex_model("gpt-5.5", effort="xhigh")
        self.assertEqual(verdict, mc.OK)
        self.assertIn("-c", captured["argv"])
        self.assertIn("model_reasoning_effort=xhigh", captured["argv"])

    def test_codex_probe_argv_no_effort_flag_when_absent(self):
        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            return _CP(argv, 0, stdout="ok", stderr="")

        with mock.patch.object(mc.subprocess, "run", side_effect=fake_run):
            mc.probe_codex_model("gpt-5.5")
        self.assertNotIn("-c", captured["argv"])


class ConfirmDeadSpecsTest(unittest.TestCase):
    """D2: the hard-stop gate — classification is a hint, the probe decides."""

    def setUp(self):
        self.codex_gone = sandbox.format_judge_output(
            _CP([], 1, stdout="", stderr=_CODEX_GONE_STDERR))
        self.codex_old = sandbox.format_judge_output(
            _CP([], 1, stdout="", stderr=_CODEX_CLI_OLD_STDERR))
        self.claude_gone = sandbox.format_judge_output(
            _CP([], 1, stdout=_CLAUDE_GONE_STDOUT, stderr=""))
        self.probe_calls = []

    def _probe(self, verdict, detail="probed"):
        def fn(m, effort=None, timeout=0):
            self.probe_calls.append(m)
            return verdict, detail
        return fn

    def test_model_gone_probe_confirms(self):
        confirmed = mc.confirm_dead_specs(
            {"codex:gpt-5.3-codex": self.codex_gone},
            {"codex:gpt-5.3-codex": ("codex", "gpt-5.3-codex")},
            probe_codex=self._probe(mc.GONE))
        self.assertEqual(confirmed["codex:gpt-5.3-codex"][0], mc.GONE)
        self.assertEqual(self.probe_calls, ["gpt-5.3-codex"])

    def test_classified_but_probe_ok_not_confirmed(self):
        confirmed = mc.confirm_dead_specs(
            {"codex:gpt-5.5": self.codex_gone},
            {"codex:gpt-5.5": ("codex", "gpt-5.5")},
            probe_codex=self._probe(mc.OK, "responds"))
        self.assertEqual(confirmed, {})

    def test_probe_unknown_not_confirmed(self):
        confirmed = mc.confirm_dead_specs(
            {"claude:claude-x": self.claude_gone},
            {"claude:claude-x": ("claude", "claude-x")},
            probe_claude=self._probe(mc.UNKNOWN, "probe timed out"))
        self.assertEqual(confirmed, {})

    def test_timeout_and_budget_never_probe(self):
        confirmed = mc.confirm_dead_specs(
            {"claude:claude-fable-5": "(timed out after 300s)",
             "claude:claude-sonnet-5": "Error: Exceeded USD budget (5)"},
            {"claude:claude-fable-5": ("claude", "claude-fable-5"),
             "claude:claude-sonnet-5": ("claude", "claude-sonnet-5")},
            probe_claude=self._probe(mc.GONE))
        self.assertEqual(confirmed, {})
        self.assertEqual(self.probe_calls, [])

    def test_agy_and_variantless_skipped(self):
        confirmed = mc.confirm_dead_specs(
            {"agy": self.claude_gone, "codex": self.codex_gone},
            {"agy": ("agy", None), "codex": ("codex", None)},
            probe_claude=self._probe(mc.GONE), probe_codex=self._probe(mc.GONE))
        self.assertEqual(confirmed, {})
        self.assertEqual(self.probe_calls, [])

    def test_bad_effort_spec_skipped(self):
        confirmed = mc.confirm_dead_specs(
            {"codex:gpt-5.5:nope": self.codex_gone},
            {"codex:gpt-5.5:nope": ("codex", "gpt-5.5:nope")},
            probe_codex=self._probe(mc.GONE))
        self.assertEqual(confirmed, {})

    def test_cli_upgrade_confirmed_with_its_verdict(self):
        confirmed = mc.confirm_dead_specs(
            {"codex:gpt-5.6-luna:medium": self.codex_old},
            {"codex:gpt-5.6-luna:medium": ("codex", "gpt-5.6-luna:medium")},
            probe_codex=self._probe(mc.NEEDS_CLI_UPGRADE, "needs newer CLI"))
        self.assertEqual(confirmed["codex:gpt-5.6-luna:medium"][0], mc.NEEDS_CLI_UPGRADE)
        self.assertEqual(self.probe_calls, ["gpt-5.6-luna"])  # effort stripped

    def test_apply_confirmed_overrides_report(self):
        report = {"entries": [
            {"spec": "codex:gpt-5.3-codex", "verdict": mc.LISTED, "detail": "in cache"},
            {"spec": "agy", "verdict": mc.UNVERIFIABLE, "detail": "ui"}]}
        mc.apply_confirmed(report, {"codex:gpt-5.3-codex": (mc.GONE, "rejected")})
        self.assertEqual(report["entries"][0]["verdict"], mc.GONE)
        self.assertEqual(report["entries"][1]["verdict"], mc.UNVERIFIABLE)


class SelectTest(unittest.TestCase):
    """A3: fresh-install create + custom-key preservation (R12)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        (self.project / ".agent").mkdir()
        self._patches = [
            mock.patch.object(mc, "check_pins", return_value={
                "entries": [], "codex": None, "codex_cli_version": None,
                "agy_models": None, "claude_candidates": [], "warnings": []}),
            mock.patch("provider.sandbox.load_judge_config",
                       return_value={"default_judge": "codex", "panel": ["agy"]}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _run(self, answers):
        with mock.patch("builtins.input", side_effect=answers), \
                redirect_stdout(io.StringIO()):
            return mc.run_select(self.project, probe=False)

    def test_fresh_install_creates_file(self):
        rc = self._run(["codex:gpt-5.5:medium, agy", "claude"])
        self.assertEqual(rc, 0)
        data = json.loads((self.project / ".agent" / "models.json").read_text())
        self.assertEqual(data["panel"], ["codex:gpt-5.5:medium", "agy"])
        self.assertEqual(data["default_judge"], "claude")
        self.assertIn("_doc", data)

    def test_rerun_preserves_custom_keys_and_empty_input(self):
        self._run(["codex:gpt-5.5:medium, agy", "claude"])
        path = self.project / ".agent" / "models.json"
        data = json.loads(path.read_text())
        data["aliases"] = {"mine": ["claude", "claude-fable-5", []]}
        path.write_text(json.dumps(data))
        rc = self._run(["", ""])
        self.assertEqual(rc, 0)
        data2 = json.loads(path.read_text())
        self.assertEqual(data2["panel"], ["codex:gpt-5.5:medium", "agy"])
        self.assertEqual(data2["default_judge"], "claude")
        self.assertEqual(data2["aliases"], {"mine": ["claude", "claude-fable-5", []]})

    def test_rejects_empty_variant_and_unknown_spec(self):
        self.assertEqual(self._run(["codex:, agy", ""]), 1)
        self.assertEqual(self._run(["not-a-provider:x", ""]), 1)

    def test_rejects_bad_codex_effort_and_empty_variant_default_judge(self):
        # D5/I8: resolve_judge_spec alone accepts both of these.
        self.assertEqual(self._run(["codex:gpt-5.5:bogus", ""]), 1)
        self.assertEqual(self._run(["agy", "codex:"]), 1)

    def test_dead_proposed_pin_requires_confirmation(self):
        # D5: audit of the PROPOSED panel — 'n' aborts without writing.
        gone_entry = {"spec": "codex:gpt-5.3-codex", "provider": "codex",
                      "variant": "gpt-5.3-codex", "verdict": mc.GONE, "detail": "dead"}
        self._patches[0].stop()
        self._patches[0] = mock.patch.object(mc, "check_pins", return_value={
            "entries": [gone_entry], "codex": None, "codex_cli_version": None,
            "agy_models": None, "claude_candidates": [], "warnings": []})
        self._patches[0].start()
        rc = self._run(["codex:gpt-5.3-codex", "", "n"])
        self.assertEqual(rc, 1)
        self.assertFalse((self.project / ".agent" / "models.json").exists())
        rc = self._run(["codex:gpt-5.3-codex", "", "y"])
        self.assertEqual(rc, 0)
        self.assertTrue((self.project / ".agent" / "models.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
