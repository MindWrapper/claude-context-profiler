"""Tests for the tree builder of the profiler. Run: python -m unittest discover -s tests"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from claude_context_profiler import profiler, reader  # noqa: E402


def request(messages, system=None, tools=None, usage=None, index=1):
    """A recorded exchange with a plain JSON answer, as the proxy would write it."""
    body = {"model": "m", "max_tokens": 10, "messages": messages}
    if system is not None:
        body["system"] = system
    if tools is not None:
        body["tools"] = tools
    return {
        "index": index,
        "method": "POST",
        "path": "/v1/messages",
        "request_json": body,
        "response_json": {"content": [{"type": "text", "text": "ok"}], "usage": usage or {}, "stop_reason": "end_turn"},
    }


def total(item) -> int:
    return sum(child["chars"] for child in item["children"])


class Sections(unittest.TestCase):
    def test_top_level_headings_only(self):
        text = "intro\n# One\na\n## Sub\nb\n# Two\nc\n"
        self.assertEqual([n for n, _ in profiler.split_sections(text)], ["(preamble)", "One", "Two"])

    def test_sections_are_the_whole_text(self):
        text = "intro\n# One\na\n# Two\nc\n"
        self.assertEqual("".join(body for _, body in profiler.split_sections(text)), text)

    def test_tag_block_is_one_section(self):
        text = "# A\nx\n<system-reminder>\n# Not a cut\ny\n</system-reminder>\n# B\nz\n"
        self.assertEqual([n for n, _ in profiler.split_sections(text)], ["A", "system-reminder: Not a cut", "B"])

    def test_headings_inside_fences_do_not_cut(self):
        text = "# A\n```\n# code\n```\n# B\n"
        self.assertEqual([n for n, _ in profiler.split_sections(text)], ["A", "B"])

    def test_nothing_to_cut(self):
        self.assertEqual(profiler.split_sections("just a line\nand another\n"), [])


class Labels(unittest.TestCase):
    def test_known_pieces(self):
        self.assertEqual(profiler.label_text("<system-reminder>\nContents of /a/b/CLAUDE.md (project):\nx\n</system-reminder>"), "system-reminder: CLAUDE.md")
        self.assertEqual(profiler.label_text("The following skills are available for use"), "skills listing")
        self.assertEqual(profiler.label_text("As you answer, you can use the following context:\n# userEmail\nx\n# gitStatus\ny\n"), "context: userEmail, gitStatus")

    def test_fallback_is_the_first_line(self):
        self.assertEqual(profiler.label_text("\n\n# Heading here\nbody"), "Heading here")
        self.assertEqual(profiler.label_text("x" * 100)[-1], "…")


class Tree(unittest.TestCase):
    def build(self, records):
        return profiler.build_profile(reader.build_exchanges(records))["requests"]

    def test_parts_add_up_to_the_request(self):
        tools = [{"name": "Bash", "input_schema": {}}, {"name": "Read", "input_schema": {}, "cache_control": {"type": "ephemeral"}}]
        system = [{"type": "text", "text": "# A\nx\n# B\ny\n", "cache_control": {"type": "ephemeral"}}]
        messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
        root = self.build([request(messages, system, tools, {"input_tokens": 100})])[0]
        body = root["children"][:-1]  # the answer is not part of the prompt
        self.assertEqual(root["chars"], sum(part["chars"] for part in body))
        self.assertEqual([p["label"][:8] for p in body], ["system", "tools", "messages", "envelope"])

    def test_sections_fit_in_their_block(self):
        system = [{"type": "text", "text": "# A\nx\n# B\ny\n"}]
        root = self.build([request([{"role": "user", "content": "hi"}], system, usage={"input_tokens": 10})])[0]
        block = root["children"][0]["children"][0]
        self.assertEqual([c["label"] for c in block["children"]], ["A", "B"])
        self.assertLessEqual(total(block), block["chars"])

    def test_tokens_add_up_to_the_real_count(self):
        root = self.build([request([{"role": "user", "content": "hello there"}], usage={"input_tokens": 40, "cache_read_input_tokens": 60})])[0]
        ratio = root["info"]["ratio"]
        self.assertEqual(profiler.tokens(root, ratio), 100)
        self.assertEqual(root["info"]["ratio_source"], "measured")
        self.assertAlmostEqual(sum(profiler.tokens(p, ratio) for p in root["children"][:-1]), 100, delta=len(root["children"]))

    def test_ratio_falls_back_to_the_chain_then_to_the_constant(self):
        first = request([{"role": "user", "content": "hello"}], usage={"input_tokens": 50}, index=1)
        aborted = request([{"role": "user", "content": "hello"}, {"role": "assistant", "content": "ok"}, {"role": "user", "content": "more"}], index=2)
        aborted["response_json"] = {"error": {"type": "overloaded"}}
        lonely = request([{"role": "user", "content": "other chain"}], index=3)
        lonely["response_json"] = {"error": {"type": "overloaded"}}
        roots = self.build([first, aborted, lonely])
        self.assertEqual(roots[1]["info"]["ratio_source"], "from request 1")
        self.assertEqual(roots[1]["info"]["ratio"], roots[0]["info"]["ratio"])
        self.assertEqual(roots[2]["info"]["ratio_source"], "fallback")
        self.assertEqual(roots[2]["info"]["ratio"], profiler.FALLBACK_CHARS_PER_TOKEN)

    def test_tool_results_are_named_after_their_tool(self):
        messages = [
            {"role": "user", "content": "run"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "a\nb", "is_error": True}]},
        ]
        root = self.build([request(messages, usage={"input_tokens": 10})])[0]
        labels = [c["label"] for m in root["children"][2]["children"] for c in m["children"]]
        self.assertEqual(labels, ["tool_use Bash", "tool_result Bash (error)"])

    def test_cache_segments_follow_tools_system_messages(self):
        tools = [{"name": "Bash", "input_schema": {}, "cache_control": {"type": "ephemeral"}}]
        system = [{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}]
        messages = [{"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}]}]
        root = self.build([request(messages, system, tools, {"input_tokens": 10})])[0]
        seg, _ = profiler.segments(root)
        self.assertEqual((seg["tools"], seg["system"], seg["messages"]), (0, 1, 2))
        self.assertEqual(root["info"]["segments"], 4)

    def test_hidden_thinking_gets_the_unexplained_output(self):
        record = request([{"role": "user", "content": "hi"}], usage={"input_tokens": 10, "output_tokens": 500})
        record["response_json"]["content"] = [{"type": "thinking", "thinking": "", "signature": "x"}, {"type": "text", "text": "ok"}]
        root = self.build([record])[0]
        answer = root["children"][-1]
        self.assertEqual(answer["tok"], 500)
        self.assertEqual(sum(c["tok"] for c in answer["children"]), 500)
        self.assertGreater(answer["children"][0]["hidden_tok"], 400)

    def test_hidden_thinking_without_a_thinking_block(self):
        # The API counted more output than the visible answer explains and the
        # answer carries no thinking block: the node for it is created here.
        record = request([{"role": "user", "content": "hi"}], usage={"input_tokens": 10, "output_tokens": 500})
        record["response_json"]["content"] = [{"type": "text", "text": "ok"}]
        root = self.build([record])[0]
        answer = root["children"][-1]
        thinking = answer["children"][0]
        self.assertEqual(thinking["label"], "thinking (not in the recording)")
        self.assertEqual(thinking["tok"], thinking["hidden_tok"])
        self.assertEqual(sum(c["tok"] for c in answer["children"]), 500)

    def test_content_is_stored_once(self):
        text = "same text in every request " * 20
        first = request([{"role": "user", "content": text}], usage={"input_tokens": 10}, index=1)
        second = request([{"role": "user", "content": text}, {"role": "assistant", "content": "ok"}, {"role": "user", "content": "next"}], usage={"input_tokens": 12}, index=2)
        page = profiler.build_profile(reader.build_exchanges([first, second]))
        self.assertEqual(sum(1 for value in page["content"].values() if value == text), 1)
        self.assertEqual(len(page["requests"]), 2)
        json.dumps(page)  # the page embeds it as JSON


if __name__ == "__main__":
    unittest.main()
