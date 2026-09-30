"""Offline tests for the JSON-RPC framing rules in server.py's main loop.

The failure this guards: the dispatcher matched notifications on one exact
spelling, "notifications/initialised" — with an `s`. Every client sends the
spec's "notifications/initialized", with a `z`, so the handshake notification
fell through to the Method-not-found branch and the server replied to it. A
reply to a notification is a protocol violation, and this one carried
"id": null because notifications have no id.

These tests drive the real `main()` loop rather than calling the dispatcher
directly, because the rule has two halves: the dispatcher must not treat a
notification as unknown, and the loop must not write a reply to a message that
carried no id. Testing only the dispatcher would leave the second half — the
half that actually keeps the violation off the wire — uncovered.

The store and the vault watcher are stubbed out, so no index is opened, no
model is loaded, and nothing is downloaded.

Run from repo root:  python -m unittest discover -s tests -v
"""
import asyncio
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_server import server  # noqa: E402


class _CaptureStdout:
    """Stands in for sys.stdout: write_response only ever touches .buffer."""

    def __init__(self):
        self.buffer = io.BytesIO()

    def messages(self):
        raw = self.buffer.getvalue().decode("utf-8")
        return [json.loads(line) for line in raw.splitlines() if line.strip()]


class _StubStore:
    """Enough of ChromaStore for main()'s startup lines, and nothing more."""

    class collection:
        @staticmethod
        def count():
            return 0

    def init_collection(self):
        pass

    def index_all_papers(self):
        pass


async def _no_watcher():
    """Replaces watch_vault, which would otherwise watch a real directory."""
    await asyncio.Event().wait()


def drive_raw(text, store=_StubStore):
    """Run the real main() loop over raw stdin `text`; return the replies."""
    stdin = io.StringIO(text)
    stdout = _CaptureStdout()

    original = (server.get_store, server.watch_vault, sys.stdin, sys.stdout,
                server.STARTUP_ERROR)
    server.get_store = store
    server.watch_vault = _no_watcher
    sys.stdin, sys.stdout = stdin, stdout
    try:
        asyncio.run(server.main())
    finally:
        (server.get_store, server.watch_vault, sys.stdin, sys.stdout,
         server.STARTUP_ERROR) = original

    return stdout.messages()


def drive(messages):
    """Run the real main() loop over `messages`; return the replies it wrote."""
    return drive_raw("".join(json.dumps(m) + "\n" for m in messages))


def conversation(*messages):
    """A realistic session start, with the caller's messages spliced in."""
    return [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        *messages,
    ]


class NotificationTest(unittest.TestCase):
    def test_initialize_notification_gets_no_reply(self):
        """The exact sequence every client sends, and the one that regressed."""
        replies = drive(conversation(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ))

        self.assertEqual([r.get("id") for r in replies], [1])
        self.assertNotIn("error", replies[0])

    def test_no_reply_is_written_without_an_id(self):
        """A notification must stay unanswered even if it reaches a handler.

        "initialized" is the one clients send, but the rule is about the id, not
        the method — an unknown notification is still not a request.
        """
        replies = drive(conversation(
            {"jsonrpc": "2.0", "method": "notifications/something/new"},
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}},
        ))

        self.assertEqual([r.get("id") for r in replies], [1])

    def test_unknown_request_still_reports_method_not_found(self):
        """The guard must not swallow the errors it sits in front of."""
        replies = drive(conversation(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/nonexistent"},
        ))

        self.assertEqual([r.get("id") for r in replies], [1, 2])
        self.assertEqual(replies[1]["error"]["code"], -32601)

    def test_tools_list_answers_after_the_handshake(self):
        replies = drive(conversation(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ))

        self.assertEqual([r.get("id") for r in replies], [1, 2])
        self.assertEqual(len(replies[1]["result"]["tools"]), len(server.TOOLS))


    def test_handshake_survives_a_malformed_line(self):
        """Junk on stdin costs one log line, not the session.

        The dispatcher's try/except never saw these: json.loads returns a list
        for a batch, a str for `"hi"`, an int for `42` — and `msg.get()` raised
        AttributeError in the loop itself, outside every handler, ending the
        process without a reply and dropping the client's connection.
        """
        replies = drive_raw(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}) + "\n"
            + "[{\"jsonrpc\":\"2.0\"}]\n"          # a batch, per the spec
            + '"hi"\n'
            + "42\n"
            + "{\n"                                 # truncated JSON
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
        )

        self.assertEqual([r.get("id") for r in replies], [1, 2])
        self.assertIn("tools", replies[1]["result"])

    def test_non_object_params_do_not_kill_the_loop(self):
        replies = drive(conversation(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": [1, 2]},
        ))

        self.assertEqual([r.get("id") for r in replies], [1, 2])
        self.assertEqual(replies[1]["error"]["code"], -32601)  # empty name


class _BrokenStore:
    """A store that cannot be opened — the disk-full / bad-path case."""

    def init_collection(self):
        raise RuntimeError("unable to open database file")


class StartupFailureTest(unittest.TestCase):
    """A broken install must reach the user through the tools.

    The index and the vault directories are prepared before `initialize` is
    answered. Anything that raised there ended the process before it said a
    word: the client reports "Failed to connect", zero tools exist, and the
    reason sits in a stderr log nobody reads.
    """

    CALL = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "paper_search", "arguments": {"query": "x"}}}

    def patch_config_error(self, message):
        from mcp_server import config
        original = config.CONFIG_ERROR
        config.CONFIG_ERROR = message
        self.addCleanup(setattr, config, "CONFIG_ERROR", original)

    def test_broken_settings_json_reaches_the_caller(self):
        """main() seeds STARTUP_ERROR from config.CONFIG_ERROR, so a settings
        file that could not be parsed is reported by the tools instead of
        silently pointing the vault somewhere else."""
        self.patch_config_error("settings.json 读取失败: ValueError: boom")

        replies = drive(conversation(self.CALL))

        self.assertEqual([r.get("id") for r in replies], [1, 2])
        # The handshake is unaffected — that is how the message gets out at all.
        self.assertNotIn("error", replies[0])
        self.assertEqual(replies[1]["error"]["code"], -32000)
        self.assertIn("boom", replies[1]["error"]["message"])

    def test_unopenable_index_reaches_the_caller(self):
        replies = drive_raw(
            "".join(json.dumps(m) + "\n"
                    for m in conversation(self.CALL)),
            store=_BrokenStore,
        )

        self.assertEqual(replies[1]["error"]["code"], -32000)
        self.assertIn("向量索引初始化失败", replies[1]["error"]["message"])
        self.assertIn("unable to open database file",
                      replies[1]["error"]["message"])


class DispatcherTest(unittest.TestCase):
    def test_notification_prefix_is_matched_by_spelling_variants(self):
        """Prefix, not one exact string: that is what the typo taught us."""
        for method in ("notifications/initialized", "notifications/initialised",
                       "notifications/progress", "notifications/roots/list_changed"):
            with self.subTest(method=method):
                self.assertIsNone(
                    asyncio.run(server.handle_request(method, None, {}))
                )

    def test_unknown_method_is_not_silently_ignored(self):
        response = asyncio.run(server.handle_request("tools/bogus", 7, {}))
        self.assertEqual(response["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
