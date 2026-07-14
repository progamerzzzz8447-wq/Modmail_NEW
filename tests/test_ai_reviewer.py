import json
import unittest
from types import SimpleNamespace

from core.ai_reviewer import (
    AI_REVIEW_MESSAGE_LIMIT,
    ApplicationReviewWindow,
    GeminiAutoReplyReviewer,
    build_ticket_text,
    has_application_trigger,
    has_configured_trigger,
)


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
        self.responses = response if isinstance(response, list) else [response]
        self.request = None
        self.calls = 0

    def post(self, url, **kwargs):
        self.request = (url, kwargs)
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


def generate_content_output(value):
    return {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": json.dumps(value)}]}}
        ]
    }


class GeminiAutoReplyReviewerTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_only_a_configured_match(self):
        session = FakeSession(
            FakeResponse(200, generate_content_output({"autoreply_key": "apply"}))
        )
        reviewer = GeminiAutoReplyReviewer(session, "test-key")

        selected = await reviewer.classify(
            "How do I apply?",
            {"apply": "Use the application form.", "refund": "Request a refund here."},
        )

        self.assertEqual(selected, "apply")
        self.assertEqual(reviewer.last_outcome, "matched")
        self.assertEqual(reviewer.last_detail, "Selected autoreply: apply.")
        url, request = session.request
        self.assertEqual(
            url,
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-3.1-flash-lite:generateContent",
        )
        generation_config = request["json"]["generationConfig"]
        self.assertEqual(generation_config["thinkingConfig"]["thinkingLevel"], "minimal")
        self.assertEqual(generation_config["maxOutputTokens"], 256)
        self.assertEqual(generation_config["responseMimeType"], "application/json")
        self.assertEqual(
            generation_config["responseSchema"]["properties"]["autoreply_key"]["enum"],
            ["__NO_MATCH__", "apply", "refund"],
        )
        self.assertEqual(request["headers"]["x-goog-api-key"], "test-key")

    async def test_2_5_flash_lite_omits_unsupported_thinking_level(self):
        session = FakeSession(
            FakeResponse(200, generate_content_output({"autoreply_key": "__NO_MATCH__"}))
        )
        reviewer = GeminiAutoReplyReviewer(
            session, "test-key", model="models/gemini-2.5-flash-lite"
        )

        await reviewer.classify("A question", {"apply": "Apply here."})

        url, request = session.request
        self.assertEqual(
            url,
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-flash-lite:generateContent",
        )
        self.assertNotIn("thinkingConfig", request["json"]["generationConfig"])

    async def test_no_match_returns_none(self):
        session = FakeSession(
            FakeResponse(200, generate_content_output({"autoreply_key": "__NO_MATCH__"}))
        )
        reviewer = GeminiAutoReplyReviewer(session, "test-key")

        selected = await reviewer.classify("Unrelated question", {"apply": "Apply here."})

        self.assertIsNone(selected)
        self.assertEqual(reviewer.last_outcome, "no_match")

    async def test_unknown_or_invalid_output_fails_closed(self):
        unknown_session = FakeSession(
            FakeResponse(200, generate_content_output({"autoreply_key": "invented"}))
        )
        invalid_session = FakeSession(
            FakeResponse(
                200,
                {"candidates": [{"content": {"parts": [{"text": "?"}]}}]},
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
        self.assertEqual(reviewer.last_outcome, "http_error")
        self.assertEqual(reviewer.last_detail, "Gemini returned HTTP 429.")

    async def test_transient_server_error_retries_once(self):
        session = FakeSession(
            [
                FakeResponse(500, {}),
                FakeResponse(200, generate_content_output({"autoreply_key": "apply"})),
            ]
        )
        reviewer = GeminiAutoReplyReviewer(session, "key")

        selected = await reviewer.classify("How do I apply?", {"apply": "Apply here."})

        self.assertEqual(selected, "apply")
        self.assertEqual(session.calls, 2)
        self.assertEqual(reviewer.last_outcome, "matched")

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

    def test_application_trigger_covers_common_wording_and_typos(self):
        examples = (
            "Hello! How can I apply?",
            "Where is the application form?",
            "I am apllying for cabin crew",
            "Are you recruiting pilots?",
            "Do you have any vacancies?",
            "I want to join the TUI team",
            "Could I work for the airline?",
            "I would love to be cabin crew",
            "Are there any careers or jobs available?",
            "Where should I send my CV?",
            "How do I register for a staff position?",
        )

        for text in examples:
            with self.subTest(text=text):
                self.assertTrue(has_application_trigger(text))

        self.assertFalse(has_application_trigger("My apple juice is missing."))
        self.assertFalse(has_application_trigger("When does flight 123 depart?"))
        self.assertTrue(
            has_configured_trigger(
                "I would like to become cabin crew.",
                ["apply", "become", "staff"],
            )
        )
        self.assertFalse(has_configured_trigger("The staffing level is fine.", ["staff"]))
        self.assertEqual(AI_REVIEW_MESSAGE_LIMIT, 4)

    def test_application_review_window_checks_once_within_first_four_messages(self):
        window = ApplicationReviewWindow()

        self.assertFalse(window.consider("Hello"))
        self.assertFalse(window.consider("I need some help"))
        self.assertFalse(window.consider("Is anybody there?"))
        self.assertTrue(window.consider("How can I apply?"))
        self.assertTrue(window.closed)
        self.assertEqual(window.messages_seen, 4)
        self.assertFalse(window.consider("Another application question"))

        expired = ApplicationReviewWindow()
        for text in ("one", "two", "three", "four"):
            self.assertFalse(expired.consider(text))
        self.assertTrue(expired.closed)
        self.assertFalse(expired.consider("I want a job"))


if __name__ == "__main__":
    unittest.main()
