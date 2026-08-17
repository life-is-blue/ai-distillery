"""Tests for interaction-pattern extraction — the adjacency-preserving
signal path that feeds harness / loop engineering.

Covers the new extract_interaction_turns() excerpt function plus the
5-section threading (Interaction as a first-class SOUL section).
"""

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from ai_report import (
    extract_interaction_turns,
    _parse_soul_sections,
    _merge_soul_entry,
    _rebuild_soul,
    priority_gate,
)


def _write_jsonl(d, name, lines):
    path = Path(d) / name
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    return path


import json


class TestExtractInteractionTurns(unittest.TestCase):
    def test_preserves_adjacency_order(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                {"role": "user", "content": "why does X fail?"},
                {"role": "assistant", "content": "because of Y"},
                {"role": "user", "content": "then what about Z?"},
            ])
            out = extract_interaction_turns(p)
        lines = out.splitlines()
        self.assertEqual(lines[0], "[user] why does X fail?")
        self.assertEqual(lines[1], "[assistant] because of Y")
        self.assertEqual(lines[2], "[user] then what about Z?")

    def test_budget_inverted_user_larger(self):
        with tempfile.TemporaryDirectory() as d:
            long_user = "Q" * 900  # > 800, will be truncated
            long_assist = "A" * 900  # > 120, truncated much harder
            p = _write_jsonl(d, "s.jsonl", [
                {"role": "user", "content": long_user},
                {"role": "assistant", "content": long_assist},
            ])
            out = extract_interaction_turns(p)
        user_line = out.splitlines()[0]
        assist_line = out.splitlines()[1]
        # user truncated at 800, assistant at 120
        self.assertEqual(len(user_line), len("[user] ") + 800)
        self.assertEqual(len(assist_line), len("[assistant] ") + 120)

    def test_drops_tool_and_system_roles_but_keeps_adjacency(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                {"role": "assistant", "content": [{"type": "tool_use", "name": "Read", "input": {}}]},
                {"role": "user", "content": "did you read it?"},
                {"role": "tool", "content": "result"},
            ])
            out = extract_interaction_turns(p)
        # tool_use has no text → dropped; tool role dropped entirely; user kept
        self.assertEqual(out.strip(), "[user] did you read it?")

    def test_content_list_joins_text_blocks_only(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                {"role": "user", "content": [
                    {"type": "text", "text": "hello"},
                    {"type": "tool_use", "name": "X", "input": {}},
                    {"type": "text", "text": "world"},
                ]},
            ])
            out = extract_interaction_turns(p)
        self.assertIn("hello world", out)
        self.assertNotIn("tool_use", out)

    def test_target_date_filter(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [
                {"role": "user", "content": "on date A", "meta": {"timestamp": "2026-08-01T10:00:00"}},
                {"role": "user", "content": "on date B", "meta": {"timestamp": "2026-08-02T10:00:00"}},
            ])
            out = extract_interaction_turns(p, target_date=date(2026, 8, 1))
        self.assertIn("on date A", out)
        self.assertNotIn("on date B", out)

    def test_empty_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write_jsonl(d, "s.jsonl", [])
            out = extract_interaction_turns(p)
        self.assertEqual(out, "")


class TestInteractionSectionThreading(unittest.TestCase):
    def test_parse_returns_interaction_key(self):
        content = (
            "## Identity\n\n## Preferences\n\n## Patterns\n\n## Context\n\n"
            "## Interaction\n\n- **反问**: 用问题纠偏 | Evidence: '你觉得呢' <!-- pk: socratic-probe --> <!-- priority: 80 -->\n"
        )
        sections = _parse_soul_sections(content)
        self.assertEqual(len(sections["Interaction"]), 1)
        self.assertIn("socratic-probe", sections["Interaction"][0])

    def test_merge_appends_interaction_with_new_tag(self):
        soul = (
            "# SOUL.md\n\n"
            "## Identity\n\n## Preferences\n\n## Patterns\n\n## Context\n\n## Interaction\n\n"
        )
        new_obs = "## Interaction\n\n- **提问**: 分步骤编号提问 | Evidence: '1) 2) 3)' <!-- pk: numbered-probe --> <!-- priority: 60 -->\n"
        result = _merge_soul_entry(soul, new_obs, "2026-08-16")
        self.assertIn("numbered-probe", result)
        self.assertIn("<!-- new: 2026-08-16 -->", result)

    def test_rebuild_emits_interaction_header(self):
        soul = "# SOUL.md\n\n## Identity\n\n## Preferences\n\n## Patterns\n\n## Context\n\n## Interaction\n\n"
        sections = _parse_soul_sections(soul)
        rebuilt = _rebuild_soul(soul, sections)
        self.assertIn("## Interaction", rebuilt)

    def test_priority_gate_filters_interaction(self):
        obs = (
            "## Interaction\n"
            "- **反问**: high signal <!-- pk: a --> <!-- priority: 85 -->\n"
            "- **提问**: low signal <!-- pk: b --> <!-- priority: 30 -->\n"
        )
        result = priority_gate(obs)
        self.assertIn("high signal", result)
        self.assertNotIn("low signal", result)


if __name__ == "__main__":
    unittest.main()
