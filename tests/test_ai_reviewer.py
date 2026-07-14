import json
import unittest
from types import SimpleNamespace

from core.ai_reviewer import GeminiAutoReplyReviewer, build_ticket_text


class FakeResponse:
    def __init__(self, status, data):
        self.status = status
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self):
        return self.data


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.request = None

    def post(self, url, **kwargs):
        self.request = (url, kwargs)
        return self.response


def interaction_output(value):
    return {
        "steps": [
            {"type": "thought", "signature": "not inspected"},
            {
                "type": "model_output",
                "content": [{"type": "text", "text": json.dumps(value)}],
            },
        ]
    }


class GeminiAutoReplyReviewerTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_only_a_configured_match(self):
        session = FakeSession(
            FakeResponse(200, interaction_output({"autoreply_key": "apply"}))
        )
        reviewer = GeminiAutoReplyReviewer(session, "test-key")

        selected = await reviewer.classify(
            "How do I apply?",
            {"apply": "Use the application form.", "refund": "Request a refund here."},
        )

        self.assertEqual(selected, "apply")
        _, request = session.request
        self.assertFalse(request["json"]["store"])
        self.assertEqual(request["headers"]["x-goog-api-key"], "test-key")

    async def test_no_match_returns_none(self):
        session = FakeSession(
            FakeResponse(200, interaction_output({"autoreply_key": "__NO_MATCH__"}))
        )
        reviewer = GeminiAutoReplyReviewer(session, "test-key")

        selected = await reviewer.classify("Unrelated question", {"apply": "Apply here."})

        self.assertIsNone(selected)

    async def test_unknown_or_invalid_output_fails_closed(self):
        unknown_session = FakeSession(
            FakeResponse(200, interaction_output({"autoreply_key": "invented"}))
        )
        invalid_session = FakeSession(
            FakeResponse(
                200,
                {"steps": [{"type": "model_output", "content": [{"type": "text", "text": "?"}]}]},
            )
        )

        self.assertIsNone(
            await GeminiAutoReplyReviewer(unknown_session, "key").classify(
                "Question", {"apply": "Apply here."}
            )
        )
        self.assertIsNone(
            await GeminiAutoReplyReviewer(invalid_session, "key").classify(
                "Question", {"apply": "Apply here."}
            )
        )

    async def test_http_failure_falls_back(self):
        reviewer = GeminiAutoReplyReviewer(FakeSession(FakeResponse(429, {})), "key")

        self.assertIsNone(await reviewer.classify("How do I apply?", {"apply": "Apply here."}))

    def test_build_ticket_text_includes_attachment_names_and_truncates(self):
        message = SimpleNamespace(
            content="Please review this",
            attachments=[SimpleNamespace(filename="application.pdf")],
        )

        self.assertEqual(
            build_ticket_text(message),
            "Please review this\n\nAttachments: application.pdf",
        )
        self.assertEqual(build_ticket_text(message, max_chars=6), "Please")


if __name__ == "__main__":
    unittest.main()
