import json
import unittest
from types import SimpleNamespace

from core.ai_reviewer import (
    AI_ALL_CLOSING,
    AI_HELLO_MESSAGES,
    AI_HELLO_FOOTER,
    AI_REPLY_FOOTER,
    ROBLOX_GAME_PASS_AUTOREPLY,
    TUI_SUPPORT_ASSISTANT_POLICY,
    GeminiAnnoyReplyGenerator,
    GeminiAutoReplyReviewer,
    GeminiHelpfulReplyGenerator,
    GeminiTicketSummaryGenerator,
    build_autoreply_context,
    build_relayed_reply_transcript,
    build_ticket_text,
    describe_ai_error,
    decode_ai_text_attachment,
    finalize_generated_ai_reply,
    find_command_references,
    generate_ai_message_joint_id,
    has_application_trigger,
    has_configured_trigger,
    has_department_transfer_intent,
    has_explicit_application_request,
    has_roblox_game_pass_url,
    is_ticket_routing_request,
    last_relayed_message_is_human_staff,
    parse_aireply_argument,
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
    def test_decodes_utf8_text_attachment_for_aireply(self):
        self.assertEqual(
            decode_ai_text_attachment("context.TXT", b"Useful context \xe2\x9c\x93"),
            "Useful context ✓",
        )
        self.assertEqual(
            decode_ai_text_attachment("knowledge.MD", b"# Knowledge\nUse route A."),
            "# Knowledge\nUse route A.",
        )
        self.assertEqual(
            decode_ai_text_attachment("knowledge.markdown", b"Canonical context"),
            "Canonical context",
        )
        with self.assertRaises(ValueError):
            decode_ai_text_attachment("context.pdf", b"not text")
        with self.assertRaises(ValueError):
            decode_ai_text_attachment("context.txt", b"\xff")

    def test_aihi_has_four_complete_premade_disclosures(self):
        self.assertEqual(AI_HELLO_FOOTER, AI_REPLY_FOOTER)
        self.assertEqual(len(AI_HELLO_MESSAGES), 4)
        self.assertEqual(len(set(AI_HELLO_MESSAGES)), 4)
        for message in AI_HELLO_MESSAGES:
            normalized = message.casefold()
            self.assertIn("full", normalized)
            self.assertIn("inquiry", normalized)
            self.assertIn("direct", normalized)
            self.assertIn("relevant team", normalized)
            self.assertIn("how can i help you today?", normalized)
            self.assertNotIn("human", normalized)
            self.assertNotIn("real agent", normalized)

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

    def test_department_transfer_requires_explicit_change_intent(self):
        self.assertFalse(has_department_transfer_intent("What department would be acceptable?"))
        self.assertFalse(has_department_transfer_intent("Which departments are available?"))
        self.assertTrue(has_department_transfer_intent("I want to change my department."))
        self.assertTrue(has_department_transfer_intent("Can I transfer departments?"))
        self.assertTrue(
            has_department_transfer_intent(
                "I want to change my dept from Ramp Agent to Ground Crew."
            )
        )
        self.assertFalse(
            has_department_transfer_intent(
                "Can you transfer this ticket to another support department?"
            )
        )
        self.assertTrue(
            is_ticket_routing_request(
                "Can you transfer this ticket to another support department?"
            )
        )

    async def test_ticket_routing_cannot_select_personal_department_transfer(self):
        selection_name = "I wish to CHANGE DEPARTMENT"
        session = FakeSession(
            FakeResponse(200, generate_content_output({"autoreply_key": selection_name}))
        )
        reviewer = GeminiAutoReplyReviewer(session, "test-key")

        selected = await reviewer.classify(
            "Can you transfer this ticket to another support department?",
            {selection_name: "Department Transfer | TUI Airways"},
            context_messages=[
                {
                    "speaker": "recipient",
                    "message": "I previously asked about transferring departments.",
                }
            ],
        )

        self.assertIsNone(selected)
        self.assertEqual(reviewer.last_outcome, "no_match")
        self.assertEqual(session.calls, 0)

    async def test_ambiguous_department_question_cannot_select_transfer_template(self):
        session = FakeSession(
            FakeResponse(
                200,
                generate_content_output(
                    {"autoreply_key": "I wish to CHANGE DEPARTMENT"}
                ),
            )
        )
        reviewer = GeminiAutoReplyReviewer(session, "test-key")

        selected = await reviewer.classify(
            "What department would be acceptable?",
            {
                "I wish to CHANGE DEPARTMENT": (
                    "Department Transfer | TUI Airways\n"
                    "Thank you for expressing an interest in changing your current department."
                )
            },
        )

        self.assertIsNone(selected)
        self.assertEqual(reviewer.last_outcome, "no_match")
        self.assertEqual(session.calls, 0)

    def test_builds_ten_message_human_context_without_current_or_bot_messages(self):
        log_messages = [
            {
                "message_id": str(index),
                "author": {"id": str(index), "mod": index % 2 == 0},
                "type": "thread_message",
                "content": f"Conversation message {index}",
            }
            for index in range(1, 13)
        ]
        log_messages.extend(
            [
                {
                    "message_id": "13",
                    "author": {"id": "999", "mod": True},
                    "type": "thread_message",
                    "content": "Bot or AI output",
                },
                {
                    "message_id": "14",
                    "author": {"id": "22", "mod": True},
                    "type": "internal",
                    "content": "Private staff note",
                },
                {
                    "message_id": "15",
                    "author": {"id": "10", "mod": False},
                    "type": "thread_message",
                    "content": "Current recipient message",
                },
            ]
        )

        context = build_autoreply_context(
            log_messages,
            current_message_id=15,
            bot_user_id=999,
            limit=10,
        )

        self.assertEqual(len(context), 10)
        self.assertEqual(context[0]["message"], "Conversation message 3")
        self.assertEqual(context[-1]["message"], "Conversation message 12")
        self.assertEqual(context[1]["speaker"], "human_staff")
        self.assertNotIn("Bot or AI output", str(context))
        self.assertNotIn("Private staff note", str(context))
        self.assertNotIn("Current recipient message", str(context))

    def test_manual_ai_transcript_uses_only_recipient_and_relayed_human_messages(self):
        transcript, count = build_relayed_reply_transcript(
            [
                {
                    "timestamp": "2026-07-17T10:00:00+00:00",
                    "author": {"id": "100", "mod": False},
                    "type": "thread_message",
                    "content": "Can I apply from mobile?",
                    "attachments": [],
                },
                {
                    "timestamp": "2026-07-17T10:01:00+00:00",
                    "author": {"id": "200", "mod": True},
                    "type": "thread_message",
                    "content": "Staff roles require a PC.",
                    "attachments": [{"filename": "requirements.txt"}],
                },
                {
                    "author": {"id": "999", "mod": True},
                    "type": "thread_message",
                    "content": "[AI autoreply: Helpful]\nPrevious AI-generated reply",
                },
                {
                    "author": {"id": "200", "mod": True},
                    "type": "note",
                    "content": "Private staff discussion",
                },
                {
                    "author": {"id": "200", "mod": True},
                    "type": "anonymous",
                    "content": "This was also relayed to the recipient.",
                },
            ],
            bot_user_id=999,
        )

        self.assertEqual(count, 4)
        self.assertIn("] RECIPIENT MESSAGE\nCan I apply from mobile?", transcript)
        self.assertIn("] STAFF-SENT MESSAGE\nStaff roles require a PC.", transcript)
        self.assertIn("Attachments: requirements.txt", transcript)
        self.assertIn("[STAFF-SENT MESSAGE]\nThis was also relayed to the recipient.", transcript)
        self.assertIn("[AI-SENT MESSAGE]\n[AI autoreply: Helpful]", transcript)
        self.assertIn("Previous AI-generated reply", transcript)
        self.assertNotIn("Private staff discussion", transcript)

    def test_latest_relayed_human_author_controls_aiall_skip(self):
        messages = [
            {
                "author": {"id": "100", "mod": False},
                "type": "thread_message",
                "content": "Recipient question",
            },
            {
                "author": {"id": "999", "mod": True},
                "type": "thread_message",
                "content": "Automated bot reply",
            },
            {
                "author": {"id": "200", "mod": True},
                "type": "note",
                "content": "Private note",
            },
            {
                "author": {"id": "200", "mod": True},
                "type": "anonymous",
                "content": "Human staff answer",
            },
        ]

        self.assertTrue(last_relayed_message_is_human_staff(messages, bot_user_id=999))
        messages.append(
            {
                "author": {"id": "100", "mod": False},
                "type": "thread_message",
                "content": "Recipient follow-up",
            }
        )
        self.assertFalse(last_relayed_message_is_human_staff(messages, bot_user_id=999))
        self.assertIsNone(last_relayed_message_is_human_staff([], bot_user_id=999))

    def test_aireply_argument_supports_optional_context_and_raw_context(self):
        self.assertEqual(parse_aireply_argument(""), (False, ""))
        self.assertEqual(
            parse_aireply_argument("they need to be on pc"),
            (False, "they need to be on pc"),
        )
        self.assertEqual(
            parse_aireply_argument("RAW they need to be on pc"),
            (True, "they need to be on pc"),
        )

    async def test_prior_recipient_and_staff_messages_are_context_only(self):
        selection_name = "I wish to CHANGE DEPARTMENT"
        session = FakeSession(
            FakeResponse(200, generate_content_output({"autoreply_key": selection_name}))
        )
        reviewer = GeminiAutoReplyReviewer(session, "test-key")

        selected = await reviewer.classify(
            "What department would be acceptable?",
            {selection_name: "Department Transfer | TUI Airways"},
            context_messages=[
                {
                    "speaker": "recipient",
                    "message": "I would like to transfer departments.",
                },
                {
                    "speaker": "human_staff",
                    "message": "Which department are you considering?",
                },
            ],
        )

        self.assertEqual(selected, selection_name)
        _, request = session.request
        prompt = request["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("prior_context_only", prompt)
        self.assertIn("CONTEXT ONLY", prompt)
        self.assertIn("A human staff message is not recipient intent", prompt)
        self.assertIn("Which department are you considering?", prompt)

    async def test_alias_name_and_reply_sanity_are_sent_to_gemini(self):
        session = FakeSession(
            FakeResponse(200, generate_content_output({"autoreply_key": "Application help"}))
        )
        reviewer = GeminiAutoReplyReviewer(session, "test-key")

        selected = await reviewer.classify(
            "How do I apply?",
            {"Application help": "Please use the application form."},
            alias_names={"Application help": "apply-help"},
        )

        self.assertEqual(selected, "Application help")
        _, request = session.request
        prompt = request["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn('"alias": "apply-help"', prompt)
        self.assertIn("what the recipient is actually asking", prompt)
        self.assertIn("confusing, nonsensical in context", prompt)
        self.assertIn("Useful extra context is allowed", prompt)

    async def test_department_change_cannot_select_sub_certification(self):
        department_reply = "Please complete this department transfer form."
        session = FakeSession(
            FakeResponse(200, generate_content_output({"autoreply_key": "Department change"}))
        )
        reviewer = GeminiAutoReplyReviewer(session, "test-key")

        selected = await reviewer.classify(
            "I want to change my dept from Ramp Agent to Ground Crew.",
            {
                "Department change": department_reply,
                "SUB CERTIFICATION REQUEST": (
                    "Please fill in your MAIN DEPARTMENT and DESIRED SUB DEPARTMENT."
                ),
            },
        )

        self.assertEqual(selected, "Department change")
        _, request = session.request
        prompt = request["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn(department_reply, prompt)
        self.assertNotIn("DESIRED SUB DEPARTMENT", prompt)

    async def test_conditional_ground_ops_offer_cannot_request_application_form(self):
        reviewer = GeminiAutoReplyReviewer(
            FakeSession(
                FakeResponse(
                    200,
                    generate_content_output(
                        {"autoreply_key": "Ground Operations DIRECT entry application ON REQUEST"}
                    ),
                )
            ),
            "test-key",
        )
        selected = await reviewer.classify(
            "If needs I am able to go to ground ops whichever works best for you people",
            {
                "Ground Operations DIRECT entry application ON REQUEST": (
                    "Ground Operations application form"
                )
            },
        )

        self.assertIsNone(selected)
        self.assertEqual(reviewer.last_outcome, "no_match")
        self.assertFalse(
            has_explicit_application_request(
                "If needs I am able to go to ground ops whichever works best for you people"
            )
        )
        self.assertTrue(has_explicit_application_request("Can I apply for Ground Operations?"))
        self.assertTrue(
            has_explicit_application_request("can I get the gops direct entry pls")
        )

    async def test_ground_ops_candidate_cannot_send_ramp_agent_form(self):
        session = FakeSession(
            FakeResponse(
                200,
                generate_content_output(
                    {"autoreply_key": "Ground Operations DIRECT entry application ON REQUEST"}
                ),
            )
        )
        reviewer = GeminiAutoReplyReviewer(session, "test-key")

        selected = await reviewer.classify(
            "Can I apply for Ground Operations?",
            {
                "Ground Operations DIRECT entry application ON REQUEST": (
                    "Ramp Agent Fast Track Application\nDiscord Username:"
                )
            },
        )

        self.assertIsNone(selected)
        self.assertEqual(session.calls, 0)

    async def test_staff_context_alone_cannot_supply_department_transfer_intent(self):
        session = FakeSession(
            FakeResponse(
                200,
                generate_content_output(
                    {"autoreply_key": "I wish to CHANGE DEPARTMENT"}
                ),
            )
        )
        reviewer = GeminiAutoReplyReviewer(session, "test-key")

        selected = await reviewer.classify(
            "What department would be acceptable?",
            {"I wish to CHANGE DEPARTMENT": "Department Transfer | TUI Airways"},
            context_messages=[
                {
                    "speaker": "human_staff",
                    "message": "You can transfer departments if needed.",
                }
            ],
        )

        self.assertIsNone(selected)
        self.assertEqual(session.calls, 0)

    def test_roblox_game_pass_url_matches_anywhere_in_message(self):
        self.assertTrue(
            has_roblox_game_pass_url(
                "Here it is: https://www.roblox.com/game-pass/12345/example please check it"
            )
        )
        self.assertTrue(
            has_roblox_game_pass_url("<HTTPS://WWW.ROBLOX.COM/GAME-PASS/12345>")
        )
        self.assertTrue(
            has_roblox_game_pass_url("https://roblox.com/game-pass/12345/example")
        )
        self.assertTrue(
            has_roblox_game_pass_url("http://roblox.com/game-pass/12345/example")
        )
        self.assertFalse(has_roblox_game_pass_url("https://www.roblox.com/games/12345"))
        self.assertIn("**published**", ROBLOX_GAME_PASS_AUTOREPLY)
        self.assertIn("**Maturity Questionnaire**", ROBLOX_GAME_PASS_AUTOREPLY)

    def test_generated_ai_reply_can_omit_standard_closing(self):
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

    def test_all_inquiries_reply_can_use_only_fixed_closure_warning(self):
        self.assertEqual(
            finalize_generated_ai_reply("", closing_text=AI_ALL_CLOSING),
            AI_ALL_CLOSING,
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
        self.assertEqual(config["maxOutputTokens"], 1024)
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
        self.assertIn("Continue the existing conversation naturally", prompt)
        self.assertIn("Do not begin with Hello, Hi, Hey, Welcome", prompt)
        self.assertIn("Avoid dense walls of text", prompt)
        self.assertIn("MANDATORY TUI SUPPORT POLICY", prompt)
        self.assertIn("Never replace missing facts", prompt)
        self.assertIn("Senior Management is not itself a", prompt)
        self.assertIn("line breaks with \\n", prompt)
        self.assertIn("My booking is missing.", prompt)
        self.assertIn("RECIPIENT MESSAGE, STAFF-SENT MESSAGE, or AI-SENT MESSAGE", prompt)
        self.assertIn("Can I help with anything else?", prompt)

    async def test_helpful_reply_retries_invalid_structured_output_once(self):
        invalid_output = {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": '{"reply":'}]}}
            ]
        }
        session = FakeSession(
            [
                FakeResponse(200, invalid_output),
                FakeResponse(200, generate_content_output({"reply": "Concise valid reply."})),
            ]
        )
        generator = GeminiHelpfulReplyGenerator(session, "test-key")

        reply = await generator.generate(
            "[RECIPIENT MESSAGE]\nWhat should I do?",
            staff_attachment_context="[FILE: knowledge.md]\nUse route A.\n[END FILE]",
        )

        self.assertEqual(reply, "Concise valid reply.")
        self.assertEqual(session.calls, 2)
        _, request = session.request
        retry_prompt = request["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("truncated or did not match the required JSON schema", retry_prompt)
        self.assertIn("Use route A.", retry_prompt)

    async def test_helpful_reply_receives_optional_staff_context_separately(self):
        session = FakeSession(
            FakeResponse(200, generate_content_output({"reply": "You will need to use a PC."}))
        )
        generator = GeminiHelpfulReplyGenerator(session, "test-key")

        await generator.generate(
            "[Recipient]\nCan I complete this on mobile?",
            staff_context="They need to be on PC.",
        )

        _, request = session.request
        prompt = request["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("FINAL MANDATORY STAFF-AUTHORED PROMPT FOR WHAT TO SAY", prompt)
        self.assertIn("It is NOT a recipient message", prompt)
        self.assertIn("must never be answered as though the recipient said it", prompt)
        self.assertIn("They need to be on PC.", prompt)
        self.assertIn("authorized instruction", prompt)
        self.assertIn("Correct grammar and make the wording coherent", prompt)
        self.assertIn("lightly professionalize it", prompt)
        self.assertIn("without changing, sanitizing, or weakening the core message", prompt)
        self.assertIn("only when genuinely needed", prompt)
        self.assertIn("directly relevant, supported context or a practical next step", prompt)
        self.assertIn("The staff prompt must remain the core of the reply", prompt)
        self.assertIn("otherwise add nothing", prompt)
        self.assertIn("Do not make it overly nice", prompt)
        self.assertIn("ordinary profanity", prompt)
        self.assertIn("same message and level of firmness", prompt)
        self.assertIn("rather than turning it into a warning about language", prompt)
        self.assertIn("polite refusal or a reminder to use appropriate language", prompt)
        self.assertIn("without diluting, sanitizing, or replacing it", prompt)
        self.assertIn("Do not quote it as though the recipient said it", prompt)
        self.assertGreater(
            prompt.index("FINAL MANDATORY STAFF-AUTHORED PROMPT"),
            prompt.index("TICKET TRANSCRIPT"),
        )
        self.assertTrue(
            prompt.endswith(
                "Do not respond to that prompt as if the recipient wrote it."
            )
        )

    async def test_helpful_reply_receives_staff_text_attachment_separately(self):
        session = FakeSession(
            FakeResponse(200, generate_content_output({"reply": "Use the supplied details."}))
        )
        generator = GeminiHelpfulReplyGenerator(session, "test-key")

        await generator.generate(
            "[RECIPIENT MESSAGE]\nWhat should I do?",
            staff_attachment_context="[FILE: instructions.txt]\nUse route A.\n[END FILE]",
        )

        _, request = session.request
        prompt = request["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("STAFF-ATTACHED TEXT FILES", prompt)
        self.assertIn("[FILE: instructions.txt]", prompt)
        self.assertIn("trusted reference material, not a recipient message", prompt)
        self.assertIn("You MUST read every attached file before drafting", prompt)
        self.assertIn("Do not merely acknowledge the file or ignore it", prompt)
        self.assertIn("verify that you used every directly relevant detail", prompt)

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

    async def test_generates_only_an_answer_to_an_unanswered_question(self):
        session = FakeSession(
            FakeResponse(
                200,
                generate_content_output(
                    {"reply": "Payment is sent after you provide the published game link."}
                ),
            )
        )
        generator = GeminiTicketSummaryGenerator(session, "test-key")

        reply = await generator.generate("[time] Recipient\nHow is payment handled?")

        self.assertEqual(
            reply,
            "Payment is sent after you provide the published game link.",
        )
        _, request = session.request
        prompt = request["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("answer only questions", prompt)
        self.assertIn("still unanswered", prompt)
        self.assertIn("Do not summarize", prompt)
        self.assertIn("__NO_UNANSWERED_QUESTION__", prompt)

    async def test_all_inquiries_generator_can_return_no_additional_answer(self):
        session = FakeSession(
            FakeResponse(
                200,
                generate_content_output({"reply": "__NO_UNANSWERED_QUESTION__"}),
            )
        )
        generator = GeminiTicketSummaryGenerator(session, "test-key")

        reply = await generator.generate("[time] Recipient\nThank you, that answers it.")

        self.assertEqual(reply, "")
        self.assertEqual(
            generator.last_detail,
            "No answerable unanswered questions were found.",
        )

    async def test_returns_only_a_configured_match(self):
        session = FakeSession(
            FakeResponse(200, generate_content_output({"autoreply_key": "apply"}))
        )
        reviewer = GeminiAutoReplyReviewer(session, "test-key")

        selected = await reviewer.classify(
            "How do I apply?",
            {"apply": "Use the application form.", "refund": "Request a refund here."},
            selection_guidance={
                "apply": "Select only for staff employment applications."
            },
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
        prompt = request["json"]["contents"][0]["parts"][0]["text"]
        self.assertIn("A shared topic word is never sufficient evidence", prompt)
        self.assertIn("What department would be acceptable?", prompt)
        self.assertIn("trusted `additional_info`", prompt)
        self.assertIn("Select only for staff employment applications.", prompt)
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
