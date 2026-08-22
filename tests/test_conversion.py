import json
import subprocess
import tempfile
import unittest
from pathlib import Path

# Get project root directory
PROJECT_ROOT = Path(__file__).parent.parent

class TestAiLogConverter(unittest.TestCase):
    DATA_DIR = PROJECT_ROOT / "tests" / "data"
    SCRIPT = PROJECT_ROOT / "ai_log_converter.py"
    
    def run_convert(self, input_file, args=None):
        cmd = ["python3", str(self.SCRIPT)]
        if args:
            cmd.extend(args)
        cmd.extend([str(input_file), "-"])
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return result.stdout

    def test_claude_conversion(self):
        input_file = self.DATA_DIR / "claude_masked.jsonl"
        output = self.run_convert(input_file)
        self.assertIn("Assistant (", output)
        self.assertIn("Tool Call:", output)

    def test_gemini_conversion(self):
        input_file = self.DATA_DIR / "gemini_masked.json"
        output = self.run_convert(input_file)
        self.assertIn("### User", output)
        self.assertIn("### Assistant", output)

    def test_codebuddy_conversion(self):
        input_file = self.DATA_DIR / "codebuddy_masked.jsonl"
        output = self.run_convert(input_file)
        self.assertIn("### Assistant", output)
        self.assertIn("Tool Call: `Read`", output)

    def test_role_filtering(self):
        input_file = self.DATA_DIR / "gemini_masked.json"
        output = self.run_convert(input_file, args=["-t", "txt", "--role", "user"])
        self.assertIn("[USER]", output)
        self.assertNotIn("[ASSISTANT]", output)

    def test_codex_conversion(self):
        input_file = self.DATA_DIR / "codex_masked.jsonl"
        output = self.run_convert(input_file)
        self.assertIn("### User", output)
        self.assertIn("### Assistant", output)

    def test_cursor_conversion(self):
        input_file = self.DATA_DIR / "cursor_masked.jsonl"
        output = self.run_convert(input_file)
        self.assertIn("### User", output)
        self.assertIn("### Assistant", output)
        self.assertIn("Tool Call: `Shell`", output)
        self.assertNotIn("<user_query>", output)
        self.assertNotIn("<timestamp>", output)

    def test_agy_conversion(self):
        input_file = self.DATA_DIR / "agy_masked.jsonl"
        output = self.run_convert(input_file)
        self.assertIn("### User", output)
        self.assertIn("Please inspect the repository", output)
        self.assertIn("### Assistant", output)
        self.assertIn("Tool Call: `list_dir`", output)
        self.assertIn("Tool Result", output)
        self.assertNotIn("ADDITIONAL_METADATA", output)
        self.assertNotIn("internal metadata", output)
        self.assertNotIn("checkpoint payload", output)

    def test_agy_normalized_jsonl_preserves_structure(self):
        input_file = self.DATA_DIR / "agy_masked.jsonl"
        output = self.run_convert(input_file, args=["-f", "agy", "-t", "jsonl"])
        messages = [json.loads(line) for line in output.splitlines()]

        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "tool", "assistant"])
        self.assertEqual(messages[0]["meta"]["timestamp"], "2026-08-22T07:20:01Z")
        self.assertEqual(messages[1]["content"][0]["type"], "thought")
        self.assertEqual(messages[1]["content"][1]["name"], "list_dir")
        self.assertEqual(messages[2]["content"][0]["type"], "tool_result")

    def test_detect_format_skips_initial_metadata_lines(self):
        with tempfile.NamedTemporaryFile("w+", suffix=".jsonl", delete=False) as tf:
            tf.write('{"type":"info","msg":"meta"}\n')
            tf.write('{"type":"model","parts":[{"text":"hello"}]}\n')
            temp_path = tf.name

        try:
            result = subprocess.run(
                ["python3", str(self.SCRIPT), temp_path, "-"],
                capture_output=True,
                text=True,
                check=True,
            )
        finally:
            Path(temp_path).unlink(missing_ok=True)

        self.assertIn("### Assistant", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_success_is_silent_on_stderr(self):
        input_file = self.DATA_DIR / "gemini_masked.json"
        with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as tf:
            output_file = tf.name

        try:
            result = subprocess.run(
                ["python3", str(self.SCRIPT), str(input_file), output_file],
                capture_output=True,
                text=True,
                check=True,
            )
        finally:
            Path(output_file).unlink(missing_ok=True)

        self.assertEqual(result.stderr, "")

if __name__ == "__main__":
    unittest.main()
