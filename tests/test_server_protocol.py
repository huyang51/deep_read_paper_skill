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


def drive(messages):
    """Run the real main() loop over `messages`; return the replies it wrote."""
    stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
    stdout = _CaptureStdout()

    original = (server.get_store, server.watch_vault, sys.stdin, sys.stdout)
    server.get_store = _StubStore
    server.watch_vault = _no_watcher
    sys.stdin, sys.stdout = stdin, stdout
    try:
        asyncio.run(server.main())
    finally:
        server.get_store, server.watch_vault, sys.stdin, sys.stdout = original

    return stdout.messages()


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
