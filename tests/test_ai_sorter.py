import json
import unittest

from core.ai_sorter import (
    GEMINI_GENERATE_CONTENT_URL,
    GeminiTicketBatchReviewer,
    build_sorting_transcript,
    canonicalize_sorted_ticket_name,
    latest_conversation_has_closing,
    latest_recipient_message,
    normalize_sorted_ticket_name,
    ticket_is_rename_eligible,
)


ALL_INQUIRIES_CLOSING = (
    "We have now answered all of your inquiries. Can we help with anything else? "
    "Otherwise, this ticket will be closed."
)


class FakeResponse:
    def __init__(self, status, data):
        self.status = status
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        return self.data


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.response


def gemini_response(value):
    return {
        "candidates": [{"content": {"parts": [{"text": json.dumps(value)}]}}]
    }


class GeminiTicketBatchReviewerTests(unittest.IsolatedAsyncioTestCase):
    def test_builds_complete_transcript_with_roles(self):
        transcript = build_sorting_transcript(
            [
                {"author": {"id": "1", "mod": False}, "content": "Need help"},
                {"author": {"id": "2", "mod": True}, "content": "What happened?"},
                {"author": {"id": "9", "mod": True}, "content": "Automated reply"},
            ],
            bot_user_id=9,
        )
        self.assertIn("[RECIPIENT MESSAGE]\nNeed help", transcript)
        self.assertIn("[STAFF MESSAGE]\nWhat happened?", transcript)
        self.assertIn("[BOT OR AI MESSAGE]\nAutomated reply", transcript)

    def test_only_unnamed_tickets_are_rename_eligible(self):
        self.assertTrue(ticket_is_rename_eligible("unnamed"))
        self.assertTrue(ticket_is_rename_eligible("ticket-unnamed"))
        self.assertFalse(ticket_is_rename_eligible("manually-named"))
        self.assertFalse(
            ticket_is_rename_eligible(
                "manually-named", category_id=2, general_category_id=2,
                category_name="General Support",
            )
        )

    def test_helpers_preserve_consistent_names_and_resolution(self):
        self.assertEqual(normalize_sorted_ticket_name("Gamepass Payment"), "gamepass-payment")
        self.assertEqual(
            canonicalize_sorted_ticket_name("Ramp Agent application"), "app-inquiry"
        )
        messages = [
            {"author": {"id": "1", "mod": False}, "content": "Can I apply?"},
            {"author": {"id": "9", "mod": True}, "content": ALL_INQUIRIES_CLOSING},
        ]
        self.assertEqual(latest_recipient_message(messages), "Can I apply?")
        self.assertTrue(latest_conversation_has_closing(messages, ALL_INQUIRIES_CLOSING))

    async def test_reviews_all_tickets_in_exactly_one_gemini_request(self):
        session = FakeSession(
            FakeResponse(
                200,
                gemini_response({
                    "tickets": [
                        {
                            "id": "100",
                            "status": "awaiting_staff",
                            "summary": "Recipient needs payment help.",
                            "suggested_reply": "Please send the game link.",
                            "ticket_name": "Gamepass payment",
                        },
                        {
                            "id": "200",
                            "status": "resolved",
                            "summary": "",
                            "suggested_reply": "",
                            "ticket_name": "Ramp Agent application",
                        },
                    ]
                }),
            )
        )
        reviewer = GeminiTicketBatchReviewer(session, "secret")
        decisions = await reviewer.review([
            {"id": "100", "channel_name": "unnamed", "transcript": "Ticket one"},
            {"id": "200", "channel_name": "named", "transcript": "Ticket two"},
        ])

        self.assertEqual(len(session.requests), 1)
        self.assertEqual(decisions["100"]["ticket_name"], "gamepass-payment")
        self.assertEqual(decisions["200"]["ticket_name"], "app-inquiry")
        url, request = session.requests[0]
        self.assertEqual(
            url,
            GEMINI_GENERATE_CONTENT_URL.format(model="gemini-2.5-flash-lite"),
        )
        self.assertEqual(request["headers"], {"x-goog-api-key": "secret"})
        prompt = request["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn('"id": "100"', prompt)
        self.assertIn('"id": "200"', prompt)

    async def test_http_error_does_not_retry(self):
        session = FakeSession(FakeResponse(500, {}))
        reviewer = GeminiTicketBatchReviewer(session, "secret")
        result = await reviewer.review([{"id": "100", "transcript": "Help"}])
        self.assertIsNone(result)
        self.assertEqual(len(session.requests), 1)
        self.assertEqual(reviewer.last_detail, "Gemini returned HTTP 500.")


if __name__ == "__main__":
    unittest.main()
