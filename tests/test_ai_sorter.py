import json
import unittest

from core.ai_sorter import (
    GROQ_CHAT_COMPLETIONS_URL,
    GroqTicketSorter,
    build_sorting_transcript,
    normalize_sorted_ticket_name,
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
        self.calls = 0
        self.request = None

    def post(self, url, **kwargs):
        self.request = (url, kwargs)
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


def completion(decision):
    return {
        "choices": [
            {
                "message": {
                    "content": json.dumps(decision),
                }
            }
        ]
    }


class GroqTicketSorterTests(unittest.IsolatedAsyncioTestCase):
    def test_builds_the_entire_stored_transcript_with_roles(self):
        transcript = build_sorting_transcript(
            [
                {
                    "timestamp": "one",
                    "author": {"id": "10", "mod": False},
                    "content": "I need a gamepass payment.",
                    "type": "thread_message",
                },
                {
                    "timestamp": "two",
                    "author": {"id": "20", "mod": True},
                    "content": "Please send the game link.",
                    "type": "thread_message",
                },
                {
                    "timestamp": "three",
                    "author": {"id": "999", "mod": True},
                    "content": "Automated response.",
                    "type": "thread_message",
                },
                {
                    "timestamp": "four",
                    "author": {"id": "20", "mod": True},
                    "content": "Payment team should review this.",
                    "type": "note",
                    "attachments": [{"filename": "proof.txt"}],
                },
            ],
            bot_user_id=999,
        )

        self.assertIn("RECIPIENT MESSAGE\nI need a gamepass payment.", transcript)
        self.assertIn("STAFF MESSAGE\nPlease send the game link.", transcript)
        self.assertIn("BOT OR AI MESSAGE\nAutomated response.", transcript)
        self.assertIn("INTERNAL STAFF NOTE\nPayment team should review this.", transcript)
        self.assertIn("Attachments: proof.txt", transcript)

    def test_normalizes_names_to_two_or_three_safe_words(self):
        self.assertEqual(normalize_sorted_ticket_name("Gamepass Payment Pending!"), "gamepass-payment-pending")
        self.assertEqual(normalize_sorted_ticket_name("Application"), "application-support")
        self.assertEqual(normalize_sorted_ticket_name(""), "general-support")

    async def test_calls_groq_json_mode_and_validates_the_category(self):
        session = FakeSession(
            FakeResponse(
                200,
                completion(
                    {
                        "ticket_name": "Gamepass payment",
                        "category_id": "222",
                        "reason": "The recipient is awaiting a gamepass payment.",
                    }
                ),
            )
        )
        sorter = GroqTicketSorter(session, "secret", model="llama-3.1-8b-instant")

        decision = await sorter.sort(
            "[RECIPIENT MESSAGE]\nWhere is my payment?",
            [
                {"id": "111", "name": "General Support"},
                {"id": "222", "name": "Payments"},
            ],
            current_category_id=111,
        )

        self.assertEqual(decision["ticket_name"], "gamepass-payment")
        self.assertEqual(decision["category_id"], "222")
        url, request = session.request
        self.assertEqual(url, GROQ_CHAT_COMPLETIONS_URL)
        self.assertEqual(request["headers"], {"Authorization": "Bearer secret"})
        self.assertEqual(request["json"]["response_format"], {"type": "json_object"})
        self.assertIn("AVAILABLE DISCORD CATEGORIES", request["json"]["messages"][0]["content"])

    async def test_rejects_an_unknown_category(self):
        sorter = GroqTicketSorter(
            FakeSession(
                FakeResponse(
                    200,
                    completion(
                        {
                            "ticket_name": "Mystery issue",
                            "category_id": "999",
                            "reason": "Unknown category.",
                        }
                    ),
                )
            ),
            "secret",
        )

        decision = await sorter.sort(
            "[RECIPIENT MESSAGE]\nHelp",
            [{"id": "111", "name": "General Support"}],
        )

        self.assertIsNone(decision)
        self.assertEqual(sorter.last_outcome, "invalid_response")


if __name__ == "__main__":
    unittest.main()
