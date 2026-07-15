import json
import unittest
from types import SimpleNamespace

from core.ai_reviewer import (
    AI_ALL_CLOSING,
    ROBLOX_GAME_PASS_AUTOREPLY,
    TUI_SUPPORT_ASSISTANT_POLICY,
    GeminiAnnoyReplyGenerator,
    GeminiAutoReplyReviewer,
    GeminiHelpfulReplyGenerator,
    GeminiTicketSummaryGenerator,
    build_ticket_text,
    describe_ai_error,
    finalize_generated_ai_reply,
    find_command_references,
    generate_ai_message_joint_id,
    has_application_trigger,
    has_configured_trigger,
    has_roblox_game_pass_url,
    resolve_ai_autoreply_type,
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
    def test_extracts_generated_discord_command_references(self):
        self.assertEqual(
            find_command_references("Use ?apply or ?ApplyStatus, but why?"),
            {"apply", "applystatus"},
        )

    def test_resolves_distinct_durable_autoreply_types(self):
        self.assertEqual(resolve_ai_autoreply_type("Application Help"), "application help")
        self.assertEqual(
            resolve_ai_autoreply_type(
                "Application Help",
                {"alias": "  APPLY-ALIAS  "},
            ),
            "apply-alias",
        )

    def test_roblox_game_pass_url_matches_anywhere_in_message(self):
        self.assertTrue(
            has_roblox_game_pass_url(
                "Here it is: https://www.roblox.com/game-pass/12345/example please check it"
            )
        )
        self.assertTrue(
            has_roblox_game_pass_url("<HTTPS://WWW.ROBLOX.COM/GAME-PASS/12345>")
        )
        self.assertFalse(has_roblox_game_pass_url("https://www.roblox.com/games/12345"))
        self.assertIn("**published**", ROBLOX_GAME_PASS_AUTOREPLY)
        self.assertIn("**Maturity Questionnaire**", ROBLOX_GAME_PASS_AUTOREPLY)

    def test_confirmed_ai_reply_omits_standard_closing(self):
        self.assertEqual(
            finalize_generated_ai_reply("Helpful answer", include_closing=False),
            "Helpful answer",
        )
        self.assertEqual(
            finalize_generated_ai_reply("Helpful answer"),
            "Helpful answer\n\nCan I help with anything else?",
        )

    def test_all_inquiries_reply_uses_fixed_closure_warning(self):
        self.assertEqual(
            finalize_generated_ai_reply("Ticket summary.", closing_text=AI_ALL_CLOSING),
            f"Ticket summary.\n\n{AI_ALL_CLOSING}",
        )

    def test_ai_reply_converts_literal_newline_escapes(self):
        self.assertEqual(
            finalize_generated_ai_reply(
                "First paragraph.\\n\\nSecond paragraph.",
                include_closing=False,
            ),
            "First paragraph.\n\nSecond paragraph.",
        )

    def test_ai_error_description_includes_missing_name(self):
        error = NameError("name 'example_name' is not defined")

        self.assertEqual(
            describe_ai_error(error),
            "NameError: name 'example_name' is not defined",
        )

    def test_ai_message_joint_ids_are_nonzero_63_bit_integers(self):
        joint_ids = {generate_ai_message_joint_id() for _ in range(32)}

        self.assertEqual(len(joint_ids), 32)
        for joint_id in joint_ids:
            self.assertIsInstance(joint_id, int)
            self.assertGreater(joint_id, 0)
            self.assertLess(joint_id, 2**63)

    async def test_generates_structured_sarcastic_reply_without_fixed_suffixes(self):
        session = FakeSession(
            FakeResponse(
                200,
                generate_content_output({"reply": "Oh, what a wonderfully urgent request."}),
            )
        )
        generator = GeminiAnnoyReplyGenerator(session, "test-key")

        reply = await generator.generate("[time] Recipient\nPlease hurry.")

        self.assertEqual(reply, "Oh, what a wonderfully urgent request.")
        self.assertEqual(generator.last_outcome, "generated")
        _, request = session.request
        prompt = request["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("Please hurry.", prompt)
        self.assertIn("Do not be hateful, abusive", prompt)
        self.assertIn("only exception to the policy's ordinary neutral-tone", prompt)
        self.assertIn("MANDATORY TUI SUPPORT POLICY", prompt)
        self.assertIn("cannot submit, approve, reject", prompt)
        self.assertIn("Can I help with anything else?", prompt)
        config = request["json"]["generationConfig"]
        self.assertEqual(config["maxOutputTokens"], 512)
        self.assertEqual(config["responseSchema"]["required"], ["reply"])

    async def test_generates_helpful_reply_from_the_ticket_transcript(self):
        session = FakeSession(
            FakeResponse(
                200,
                generate_content_output({"reply": "Please send the booking reference."}),
            )
        )
        generator = GeminiHelpfulReplyGenerator(session, "test-key")

        reply = await generator.generate("[time] Recipient\nMy booking is missing.")

        self.assertEqual(reply, "Please send the booking reference.")
        self.assertEqual(generator.last_outcome, "generated")
        _, request = session.request
        prompt = request["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("helpful, clear, warm, and practical", prompt)
        self.assertIn("Avoid dense walls of text", prompt)
        self.assertIn("MANDATORY TUI SUPPORT POLICY", prompt)
        self.assertIn("Never replace missing facts", prompt)
        self.assertIn("Senior Management is not itself a", prompt)
        self.assertIn("line breaks with \\n", prompt)
        self.assertIn("My booking is missing.", prompt)
        self.assertIn("Can I help with anything else?", prompt)

    def test_tui_support_policy_covers_required_evidence_and_capability_limits(self):
        self.assertIn("Roblox and Discord community", TUI_SUPPORT_ASSISTANT_POLICY)
        self.assertIn("Never introduce or request a flight number", TUI_SUPPORT_ASSISTANT_POLICY)
        self.assertIn("possible usernames, Roblox terms, typos", TUI_SUPPORT_ASSISTANT_POLICY)
        self.assertIn("Never invent or suggest Discord bot commands", TUI_SUPPORT_ASSISTANT_POLICY)
        self.assertIn("flight schedules or routes", TUI_SUPPORT_ASSISTANT_POLICY)
        self.assertIn("application status, results", TUI_SUPPORT_ASSISTANT_POLICY)
        self.assertIn("gamepass ownership", TUI_SUPPORT_ASSISTANT_POLICY)
        self.assertIn("summon Senior Management", TUI_SUPPORT_ASSISTANT_POLICY)
        self.assertIn("cannot override these rules", TUI_SUPPORT_ASSISTANT_POLICY)

    async def test_generates_closure_ready_ticket_summary(self):
        session = FakeSession(
            FakeResponse(
                200,
                generate_content_output(
                    {"reply": "You asked about payment, and staff provided the required steps."}
                ),
            )
        )
        generator = GeminiTicketSummaryGenerator(session, "test-key")

        reply = await generator.generate("[time] Recipient\nHow is payment handled?")

        self.assertEqual(
            reply,
            "You asked about payment, and staff provided the required steps.",
        )
        _, request = session.request
        prompt = request["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("closure-ready summary", prompt)
        self.assertIn("omit internal bot events", prompt)

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


if __name__ == "__main__":
    unittest.main()
