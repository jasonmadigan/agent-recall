#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import claude_recall as cr


def user_msg(text, **kwargs):
    entry = {
        "type": "user",
        "timestamp": kwargs.get("timestamp", "2026-08-17T10:00:00.000Z"),
        "cwd": kwargs.get("cwd", "/Users/jmadigan/Work/demo"),
        "gitBranch": kwargs.get("branch", "main"),
        "isSidechain": False,
        "message": {"role": "user", "content": text},
    }
    entry.update({k: v for k, v in kwargs.items() if k not in {"timestamp", "cwd", "branch"}})
    return entry


class ExtractHumanTextTests(unittest.TestCase):
    def test_plain_string(self):
        self.assertEqual(cr.extract_human_text(user_msg("hello world")), "hello world")

    def test_skips_tool_result(self):
        entry = user_msg(
            [{"type": "tool_result", "content": "ls output", "tool_use_id": "1"}]
        )
        self.assertIsNone(cr.extract_human_text(entry))

    def test_skips_meta(self):
        self.assertIsNone(cr.extract_human_text(user_msg("hi", isMeta=True)))

    def test_slash_command_args(self):
        text = (
            "<command-name>/goal</command-name>\n"
            "<command-message>goal</command-message>\n"
            "<command-args>Let's build this PoC mcp inspector</command-args>"
        )
        self.assertEqual(
            cr.extract_human_text(user_msg(text)),
            "/goal Let's build this PoC mcp inspector",
        )

    def test_skips_local_command_stdout(self):
        self.assertIsNone(
            cr.extract_human_text(user_msg("<local-command-stdout>Goal set: x</local-command-stdout>"))
        )


class QueryTests(unittest.TestCase):
    def test_tokenize_drops_filler(self):
        terms, phrases = cr.tokenize_query(
            "the session where I was looking at an mcp inspector PoC"
        )
        self.assertIn("mcp", terms)
        self.assertIn("inspector", terms)
        self.assertIn("poc", terms)
        self.assertNotIn("session", terms)
        self.assertIn("mcp inspector", phrases)

    def test_encode_project_path(self):
        self.assertEqual(
            cr.encode_project_path("/Users/jmadigan/Work/kuadrant-console-plugin"),
            "-Users-jmadigan-Work-kuadrant-console-plugin",
        )


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "projects"
        self.cache = Path(self.tmp.name) / "cache.json"
        self.here = "/Users/jmadigan/Work/demo"
        enc = cr.encode_project_path(self.here)
        self.proj = self.root / enc
        self.proj.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, sid: str, entries: list[dict]) -> None:
        path = self.proj / f"{sid}.jsonl"
        path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")

    def test_builder_beats_mention_and_finder(self):
        self.write(
            "aaaaaaaa-1111-1111-1111-111111111111",
            [
                user_msg(
                    "<command-name>/goal</command-name><command-args>"
                    "Let's build this out. We'll be building this PoC mcp inspector UI"
                    "</command-args>",
                    branch="poc/mcp-inspector-direct",
                    timestamp="2026-08-17T10:00:00.000Z",
                ),
                user_msg(
                    "keep going on the inspector PoC",
                    branch="poc/mcp-inspector-direct",
                    timestamp="2026-08-17T12:00:00.000Z",
                ),
            ]
            + [
                user_msg(
                    f"follow-up on mcp inspector step {i}",
                    branch="poc/mcp-inspector-direct",
                    timestamp=f"2026-08-17T13:{i:02d}:00.000Z",
                )
                for i in range(10)
            ],
        )
        self.write(
            "bbbbbbbb-2222-2222-2222-222222222222",
            [
                user_msg(
                    "how does MCP inspector typically talk to a gateway?",
                    branch="main",
                    timestamp="2026-08-11T10:00:00.000Z",
                )
            ],
        )
        self.write(
            "cccccccc-3333-3333-3333-333333333333",
            [
                user_msg(
                    "can you find me the session in this folder/repo for claude "
                    "that looked to build out an mcp inspector PoC?",
                    branch="main",
                    timestamp="2026-08-18T08:46:46.000Z",
                )
            ],
        )
        sessions = cr.build_index(self.root, self.cache, rebuild=True)
        hits, _ = cr.search(
            sessions,
            "the session that looked to build out an mcp inspector PoC",
            here=self.here,
            all_projects=False,
            limit=8,
        )
        self.assertGreaterEqual(len(hits), 1)
        self.assertTrue(hits[0].session.session_id.startswith("aaaaaaaa"))
        ids = [h.session.session_id for h in hits]
        self.assertIn("cccccccc-3333-3333-3333-333333333333", ids)
        finder = next(h for h in hits if h.session.session_id.startswith("cccccccc"))
        self.assertLess(finder.score, hits[0].score)
        self.assertTrue(any("downranked" in e for e in finder.evidence))

    def test_json_cli(self):
        self.write(
            "dddddddd-4444-4444-4444-444444444444",
            [user_msg("rewrite the auth middleware", timestamp="2026-08-01T00:00:00.000Z")],
        )
        env = {
            "CLAUDE_PROJECTS_DIR": str(self.root),
            "CLAUDE_RECALL_CACHE": str(self.cache),
        }
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), mock.patch("sys.stdout", buf):
            rc = cr.main(["--fast", "--json", "--here", self.here, "auth middleware"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(
            payload["results"][0]["session_id"],
            "dddddddd-4444-4444-4444-444444444444",
        )


class ScopeTests(unittest.TestCase):
    def test_in_scope_cwd(self):
        s = cr.Session(
            path="x",
            mtime_ns=0,
            size=0,
            session_id="abc",
            cwd="/Users/a/Work/demo",
            project_dir="-Users-a-Work-demo",
        )
        self.assertTrue(cr.in_scope(s, "/Users/a/Work/demo", False))
        self.assertFalse(cr.in_scope(s, "/Users/a/Work/other", False))
        self.assertTrue(cr.in_scope(s, "/Users/a/Work/other", True))

    def test_last_branch_wins(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "projects"
        cache = Path(tmp.name) / "cache.json"
        here = "/Users/jmadigan/Work/demo"
        proj = root / cr.encode_project_path(here)
        proj.mkdir(parents=True)
        sid = "eeeeeeee-5555-5555-5555-555555555555"
        entries = [
            user_msg("start on main", branch="main", timestamp="2026-08-17T10:00:00.000Z"),
            user_msg("now on the poc branch", branch="poc/mcp-inspector-direct", timestamp="2026-08-17T11:00:00.000Z"),
        ]
        (proj / f"{sid}.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8"
        )
        sessions = cr.build_index(root, cache, rebuild=True)
        self.assertEqual(sessions[0].branch, "poc/mcp-inspector-direct")


class ClaudeParseTests(unittest.TestCase):
    def test_parse_wrapped_cli_json(self):
        raw = json.dumps(
            {
                "type": "result",
                "result": json.dumps(
                    {
                        "results": [
                            {
                                "id": "aaaaaaaa-1111-1111-1111-111111111111",
                                "reason": "Built the PoC; matching branch.",
                            }
                        ]
                    }
                ),
            }
        )
        picked = cr.parse_claude_results(raw)
        self.assertEqual(picked[0]["id"], "aaaaaaaa-1111-1111-1111-111111111111")
        self.assertIn("PoC", picked[0]["reason"])

    def test_parse_fenced_object(self):
        text = (
            "```json\n"
            '{"results": [{"id": "bbbbbbbb-2222-2222-2222-222222222222", "reason": "mention"}]}\n'
            "```"
        )
        picked = cr.parse_claude_results(text)
        self.assertEqual(len(picked), 1)
        self.assertTrue(picked[0]["id"].startswith("bbbbbbbb"))

    def test_ignores_cli_stream_envelope(self):
        raw = json.dumps(
            [
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": "09f8cef7-db77-4013-b390-738edfd760b4",
                },
                {
                    "type": "result",
                    "result": '{"results": [{"id": "aaaaaaaa-1111-1111-1111-111111111111", "reason": "Built the PoC"}]}',
                },
            ]
        )
        picked = cr.parse_claude_results(raw)
        self.assertEqual(len(picked), 1)
        self.assertTrue(picked[0]["id"].startswith("aaaaaaaa"))
        self.assertNotIn("09f8cef7", picked[0]["id"])

    def test_parse_id_array(self):
        picked = cr.parse_claude_results('["cccccccc-3333-3333-3333-333333333333"]')
        self.assertEqual(picked[0]["id"], "cccccccc-3333-3333-3333-333333333333")

    def test_picker_prompt_fills_placeholders(self):
        text = cr.build_picker_prompt(
            "mcp inspector PoC",
            [{"id": "aaaaaaaa-1111-1111-1111-111111111111", "title": "Build"}],
            3,
        )
        self.assertIn("mcp inspector PoC", text)
        self.assertIn("aaaaaaaa-1111-1111-1111-111111111111", text)
        self.assertIn("3", text)
        self.assertNotIn("{{QUERY}}", text)
        self.assertNotIn("{{SESSIONS_JSON}}", text)
        self.assertNotIn("{{LIMIT}}", text)


if __name__ == "__main__":
    unittest.main()
