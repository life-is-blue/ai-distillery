"""Tests for intervention mining — the mechanical autonomy-baseline path.

Every noise/false-positive case here was found by auditing the real corpus, not
imagined. The comments name what each guards against, because a silent
regression in this detector produces a plausible-looking but wrong baseline —
which is worse than no baseline at all.
"""

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from ai_report import (
    scan_session_interventions,
    _count_agent_actions,
    _classify_directive,
    _is_synthetic,
    _msg_parts,
    _intervention_stats,
    _render_intervention_report,
    find_sessions,
)

INTERRUPT = "[Request interrupted by user]"
REJECT = "The user doesn't want to proceed with this tool use"
APPROVE = "User has approved your plan. You can now start coding."
INLINE = "To tell you how to proceed, the user said:"


def _write_jsonl(d, name, lines):
    path = Path(d) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    return path


def _assistant_tool(name, **inp):
    return {"role": "assistant", "content": [{"type": "tool_call", "name": name, "input": inp}]}


def _tool_result(text, role="user"):
    return {"role": role, "content": [{"type": "tool_result", "content": text}]}


def _pad(n):
    """Filler messages so pending events reach their finalize window."""
    return [{"role": "assistant", "content": "..."} for _ in range(n)]


class TestNoiseRejection(unittest.TestCase):
    """The bare word 'rejected' has ~538 corpus hits, nearly all infrastructure."""

    def test_api_error_rejected_is_not_an_intervention(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                _assistant_tool("Bash", command="curl x"),
                {"role": "assistant", "content": 'API Error: Request rejected (429) {"code":6008}'},
                {"role": "user", "content": "retry it"},
            ])
            self.assertEqual(scan_session_interventions(p, "claude"), [])

    def test_git_remote_rejected_in_tool_result_is_not_an_intervention(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                _assistant_tool("Bash", command="git push"),
                _tool_result("! [remote rejected] main -> main (Rejected by committer-check)"),
            ])
            self.assertEqual(scan_session_interventions(p, "claude"), [])

    def test_word_rejected_in_user_prose_is_not_an_intervention(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                {"role": "user", "content": "the server rejected my request, why?"},
            ])
            self.assertEqual(scan_session_interventions(p, "claude"), [])

    def test_stack_trace_rejection_token_is_not_an_intervention(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                _tool_result("at processTicksAndRejections (node:internal/process/task_queues)"),
            ])
            self.assertEqual(scan_session_interventions(p, "claude"), [])


class TestBlockAndRoleCoverage(unittest.TestCase):
    """Guards the highest-impact miss: markers live in tool_result blocks, and
    codebuddy delivers them under role="tool" (18 real hits, 292 sessions)."""

    def test_codebuddy_rejection_under_role_tool_is_detected(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                _assistant_tool("Edit", file_path="/a"),
                _tool_result(REJECT, role="tool"),
                *_pad(30),
            ])
            recs = scan_session_interventions(p, "codebuddy")
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["action"], "Edit")

    def test_msg_parts_reads_text_and_tool_result_and_tool_calls(self):
        text, results, tools = _msg_parts({"content": [
            {"type": "text", "text": "hello"},
            {"type": "tool_result", "content": "output"},
            {"type": "tool_call", "name": "Bash", "input": {}},
        ]})
        self.assertEqual(text, "hello")
        self.assertEqual(results, "output")
        self.assertEqual(tools, ["Bash"])

    def test_content_as_bare_string_is_handled(self):
        text, results, tools = _msg_parts({"content": "plain"})
        self.assertEqual((text, results, tools), ("plain", "", []))


class TestPlanApprovalDisambiguation(unittest.TestCase):
    """Claude Code emits a rejection tool_result and THEN the approval re-prompt,
    so an ExitPlanMode 'interrupt' is usually an approval in disguise."""

    def test_approval_marker_after_rejection_is_not_an_intervention(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                _assistant_tool("ExitPlanMode"),
                _tool_result(REJECT),
                {"role": "assistant", "content": "ok"},
                _tool_result(APPROVE),
                *_pad(30),
            ])
            recs = scan_session_interventions(p, "claude")
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["klass"], "plan_approved")

    def test_implement_the_following_plan_directive_is_approval(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                _assistant_tool("ExitPlanMode"),
                _tool_result(REJECT),
                {"role": "user", "content": "Implement the following plan:\n# Plan\n..."},
                *_pad(30),
            ])
            recs = scan_session_interventions(p, "claude")
            self.assertEqual(recs[0]["klass"], "plan_approved")

    def test_impl_marker_counts_as_approval_for_non_exitplanmode_action(self):
        # Measured: the IMPL marker also follows Edit and no-tool actions.
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                _assistant_tool("Edit", file_path="/a"),
                _tool_result(REJECT),
                {"role": "user", "content": "Implement the following plan:\n# P"},
                *_pad(30),
            ])
            self.assertEqual(scan_session_interventions(p, "claude")[0]["klass"], "plan_approved")

    def test_inline_directive_makes_it_a_real_plan_rejection(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                _assistant_tool("ExitPlanMode"),
                _tool_result(f"{REJECT}. {INLINE}\n0依赖不是必须的约束, Linus 会如何考虑设计"),
                *_pad(30),
            ])
            recs = scan_session_interventions(p, "claude")
            self.assertEqual(recs[0]["klass"], "plan_rejected")
            self.assertIn("Linus", recs[0]["directive"])

    def test_approval_after_a_new_directive_does_not_excuse_the_event(self):
        # A later, unrelated plan approval must not retro-excuse this interrupt.
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                _assistant_tool("ExitPlanMode"),
                _tool_result(REJECT),
                {"role": "user", "content": "这个方案不对, 重新想"},
                *_pad(3),
                _tool_result(APPROVE),
                *_pad(30),
            ])
            recs = scan_session_interventions(p, "claude")
            self.assertEqual(recs[0]["klass"], "plan_rejected")


class TestAttribution(unittest.TestCase):
    def test_walks_back_past_text_only_assistant_message(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                _assistant_tool("Bash", command="ls"),
                {"role": "assistant", "content": "thinking out loud"},
                {"role": "user", "content": INTERRUPT},
                *_pad(30),
            ])
            self.assertEqual(scan_session_interventions(p, "claude")[0]["action"], "Bash")

    def test_no_tool_within_window_yields_none(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                {"role": "user", "content": INTERRUPT},
                *_pad(30),
            ])
            self.assertEqual(scan_session_interventions(p, "claude")[0]["action"], "(none)")

    def test_tool_far_outside_window_is_not_attributed(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                _assistant_tool("Bash", command="ls"),
                *_pad(12),
                {"role": "user", "content": INTERRUPT},
                *_pad(30),
            ])
            self.assertEqual(scan_session_interventions(p, "claude")[0]["action"], "(none)")

    def test_subagent_role_prefix_is_attributed(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                {"role": "assistant (ae377d0)",
                 "content": [{"type": "tool_call", "name": "Grep", "input": {}}]},
                {"role": "user", "content": INTERRUPT},
                *_pad(30),
            ])
            self.assertEqual(scan_session_interventions(p, "claude")[0]["action"], "Grep")

    def test_first_tool_call_wins_when_message_has_several(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                {"role": "assistant", "content": [
                    {"type": "tool_call", "name": "Read", "input": {}},
                    {"type": "tool_call", "name": "Write", "input": {}},
                ]},
                {"role": "user", "content": INTERRUPT},
                *_pad(30),
            ])
            self.assertEqual(scan_session_interventions(p, "claude")[0]["action"], "Read")


class TestMarkerTableIsolation(unittest.TestCase):
    def test_turn_aborted_counts_for_codex(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                _assistant_tool("exec_command", cmd="ls"),
                {"role": "user", "content": "<turn_aborted><reason>interrupted</reason>"},
                *_pad(30),
            ])
            self.assertEqual(len(scan_session_interventions(p, "codex")), 1)

    def test_turn_aborted_does_not_count_for_claude(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                {"role": "user", "content": "<turn_aborted><reason>interrupted</reason>"},
            ])
            self.assertEqual(scan_session_interventions(p, "claude"), [])

    def test_uncovered_tools_yield_nothing_even_with_claude_marker(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [{"role": "user", "content": INTERRUPT}])
            self.assertEqual(scan_session_interventions(p, "cursor"), [])
            self.assertEqual(scan_session_interventions(p, "gemini"), [])

    def test_codex_marker_message_does_not_become_its_own_directive(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                _assistant_tool("exec_command", cmd="ls"),
                {"role": "user", "content": "<turn_aborted><reason>interrupted</reason>"},
                {"role": "user", "content": "直接执行, 别问了"},
                *_pad(30),
            ])
            recs = scan_session_interventions(p, "codex")
            self.assertNotIn("turn_aborted", recs[0]["directive"])
            self.assertEqual(recs[0]["klass"], "over_verification")


class TestClassification(unittest.TestCase):
    def test_push_forward_is_over_verification(self):
        for text in ("不用核实,我已经配置了,执行先", "推送吧, 更新 tag", "请继续.", "直接执行"):
            self.assertEqual(_classify_directive(text), "over_verification", text)

    def test_negative_polarity_is_wrong_direction(self):
        for text in ("这个是坏品味", "方向不对", "全部回退到上一个版本"):
            self.assertEqual(_classify_directive(text), "wrong_direction", text)

    def test_polite_suggestion_is_not_wrong_direction(self):
        # 要不要/好不好 are suggestions — the opposite polarity of a correction.
        # 11 of 104 naive soft-tier hits were this sign error.
        self.assertNotEqual(
            _classify_directive("根目录的 AGENTS.md 要不要也优化一下"), "wrong_direction")

    def test_empty_directive_is_unresolved(self):
        self.assertEqual(_classify_directive(""), "unresolved")

    def test_question_is_counter_question(self):
        self.assertEqual(_classify_directive("为什么删除了这些内容"), "counter_question")


class TestSyntheticFilter(unittest.TestCase):
    def test_harness_plumbing_is_synthetic(self):
        for text in ("<command-name>/model</command-name>", "<system-reminder>x",
                     "Caveat: local commands", "Set model to opus",
                     "The user wants to clarify these questions.",
                     "Ran git push  └ To https://github.com/x/y", "API Error: 529"):
            self.assertTrue(_is_synthetic(text), text)

    def test_real_user_text_is_not_synthetic(self):
        self.assertFalse(_is_synthetic("先评估操作安全吗?"))


class TestMechanics(unittest.TestCase):
    def test_consecutive_markers_collapse_into_one_event(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                _assistant_tool("Bash", command="rm"),
                _tool_result(REJECT),
                {"role": "user", "content": "[Request interrupted by user for tool use]"},
                *_pad(30),
            ])
            self.assertEqual(len(scan_session_interventions(p, "claude")), 1)

    def test_empty_file_yields_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.jsonl"
            p.write_text("", encoding="utf-8")
            self.assertEqual(scan_session_interventions(p, "claude"), [])

    def test_malformed_line_is_skipped_and_rest_still_parsed(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.jsonl"
            p.write_text(
                json.dumps(_assistant_tool("Bash", command="ls")) + "\n"
                + "{not json\n"
                + json.dumps({"role": "user", "content": INTERRUPT}) + "\n"
                + "\n".join(json.dumps(m) for m in _pad(30)) + "\n",
                encoding="utf-8")
            recs = scan_session_interventions(p, "claude")
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["action"], "Bash")

    def test_missing_file_returns_empty(self):
        self.assertEqual(scan_session_interventions(Path("/nonexistent/x.jsonl"), "claude"), [])

    def test_event_at_eof_is_still_finalized(self):
        # No padding: the pending event must flush at EOF, not be dropped.
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                _assistant_tool("Bash", command="ls"),
                {"role": "user", "content": INTERRUPT},
            ])
            recs = scan_session_interventions(p, "claude")
            self.assertEqual(len(recs), 1)
            self.assertTrue(recs[0]["klass"])

    def test_count_agent_actions_counts_only_tool_bearing_assistant_msgs(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                _assistant_tool("Bash", command="ls"),
                {"role": "assistant", "content": "just text"},
                {"role": "user", "content": "hi"},
                {"role": "assistant (sub)", "content": [
                    {"type": "tool_call", "name": "Read", "input": {}}]},
            ])
            self.assertEqual(_count_agent_actions(p), 2)


class TestSessionDiscoveryAndDates(unittest.TestCase):
    def test_null_timestamp_session_is_retained_via_mtime_fallback(self):
        # All 430 cursor messages have timestamp=None; per-message date
        # filtering would drop the tool entirely.
        from ai_report import session_days
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "logs/cursor/proj/s.jsonl", [
                {"role": "user", "content": "hi", "meta": {"timestamp": None}},
            ])
            days = session_days(p)
            self.assertEqual(len(days), 1)

    def test_find_sessions_skips_reports_and_root_level_files(self):
        with tempfile.TemporaryDirectory() as d:
            logs = Path(d)
            _write_jsonl(d, "claude/proj/s.jsonl", [{"role": "user", "content": "x"}])
            _write_jsonl(d, ".skip-buffer.jsonl", [{"role": "user", "content": "x"}])
            _write_jsonl(d, "reports/2026/08/r.jsonl", [{"role": "user", "content": "x"}])
            found = [str(p.relative_to(logs)) for p in find_sessions(logs)]
            self.assertEqual(found, ["claude/proj/s.jsonl"])


class TestStatsAndRendering(unittest.TestCase):
    def _stats(self):
        records = [
            {"klass": "over_verification", "action": "Bash", "tool": "claude",
             "marker": "interrupted", "directive": "执行先", "session": "claude/p/s.jsonl",
             "msg": 3, "month": "2026-08"},
            {"klass": "plan_approved", "action": "ExitPlanMode", "tool": "claude",
             "marker": "tool_rejected", "directive": "Implement the following plan:",
             "session": "claude/p/s.jsonl", "msg": 9, "month": "2026-08"},
        ]
        meta = {
            "range": {"since": None, "until": None},
            "files_scanned": 2, "duplicates_dropped": 1,
            "tools": {
                "claude": {"sessions": 1, "sessions_hit": 1, "agent_actions": 100,
                           "markers_wired": 2},
                "cursor": {"sessions": 1, "sessions_hit": 0, "agent_actions": 5,
                           "markers_wired": 0},
            },
        }
        return _intervention_stats(records, meta, samples_n=8)

    def test_excluded_classes_do_not_count_as_hard_interventions(self):
        st = self._stats()
        self.assertEqual(st["scan"]["events"], 2)
        self.assertEqual(st["scan"]["hard_interventions"], 1)

    def test_uncovered_tool_is_flagged(self):
        st = self._stats()
        self.assertFalse(st["per_tool"]["cursor"]["covered"])
        self.assertTrue(st["per_tool"]["claude"]["covered"])

    def test_samples_carry_provenance(self):
        st = self._stats()
        self.assertEqual(st["samples"]["over_verification"][0]["provenance"],
                         "claude/p/s.jsonl:msg3")

    def test_render_includes_uncovered_warning_and_sections(self):
        md = _render_intervention_report(self._stats())
        self.assertIn("未覆盖", md)
        for heading in ("## 1.", "## 2.", "## 3.", "## 4.", "## 5.", "## 6.", "## 7."):
            self.assertIn(heading, md)

    def test_render_is_deterministic(self):
        self.assertEqual(_render_intervention_report(self._stats()),
                         _render_intervention_report(self._stats()))


if __name__ == "__main__":
    unittest.main()
