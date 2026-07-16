import json
import unittest

from core.alias_parser import (
    DeferredDeleteMessage,
    format_autoreply_rule_spec,
    normalize_alias,
    normalize_compact_fakeautoreply_invocation,
    parse_alias,
    parse_autoreply_rule_spec,
    parse_reply_alias,
)


class AliasParserTests(unittest.TestCase):
    def test_multiline_quoted_alias_preserves_markdown_and_embedded_separator(self):
        raw = '"freply\nHello **there** && welcome" && "reply Second message"'

        self.assertEqual(
            parse_alias(raw),
            ["freply\nHello **there** && welcome", "reply Second message"],
        )
        self.assertEqual(
            parse_reply_alias(raw),
            [
                ("freply", "Hello **there** && welcome"),
                ("reply", "Second message"),
            ],
        )

    def test_reply_alias_ignores_non_reply_commands(self):
        self.assertEqual(
            parse_reply_alias(
                '"move applications" && "fareply Thanks" && '
                '"notify <@&1391515982417100951>" && '
                '"context Application specialists were notified." && "close"'
            ),
            [("fareply", "Thanks")],
        )
        self.assertIsNone(parse_reply_alias('"move applications" && "close"'))
        self.assertIsNone(parse_reply_alias('"freply"'))

    def test_normalize_alias_appends_invocation_text(self):
        self.assertEqual(normalize_alias('"freply Hello"', "world"), ["freply Hello world"])

    def test_normalizes_fakeautoreply_without_space(self):
        self.assertEqual(
            normalize_compact_fakeautoreply_invocation(
                "?fakeautoreplyPlease check the gamepass.", "?"
            ),
            "?fakeautoreply Please check the gamepass.",
        )

    def test_does_not_change_normal_or_unrelated_commands(self):
        self.assertIsNone(
            normalize_compact_fakeautoreply_invocation("?fakeautoreply Already spaced", "?")
        )
        self.assertIsNone(
            normalize_compact_fakeautoreply_invocation("?replyHello", "?")
        )

    def test_parses_autoreply_rule_syntax(self):
        self.assertEqual(
            parse_autoreply_rule_spec(
                "NAME: How can I apply",
                '["MUST MENTION TO CHECK": apply, application, become, staff] apply',
            ),
            {
                "name": "How can I apply",
                "triggers": ["apply", "application", "become", "staff"],
                "alias": "apply",
            },
        )

    def test_parses_named_autoreply_alternatives(self):
        self.assertEqual(
            parse_autoreply_rule_spec(
                "NAME: Application help",
                '["MUST MENTION TO CHECK": apply, application] apply '
                '["ALTERNATIVES": {"Application status": app-status}, '
                '{"Application requirements": "app requirements"}]',
            ),
            {
                "name": "Application help",
                "triggers": ["apply", "application"],
                "alias": "apply",
                "alternatives": [
                    {"name": "Application status", "alias": "app-status"},
                    {
                        "name": "Application requirements",
                        "alias": "app requirements",
                    },
                ],
            },
        )

    def test_accepts_descriptive_alternative_names_up_to_200_characters(self):
        long_name = (
            "Can my application be reviewed early? "
            "(Must mention read now / skip waiting / instead of waiting etc)"
        )
        parsed = parse_autoreply_rule_spec(
            "NAME: Application help",
            '["MUST MENTION TO CHECK": apply] apply '
            f'["ALTERNATIVES": {{"{long_name}": early-review}}]',
        )

        self.assertEqual(len(long_name), 101)
        self.assertEqual(parsed["alternatives"][0]["name"], long_name)

        with self.assertRaisesRegex(ValueError, "between 1 and 200 characters"):
            parse_autoreply_rule_spec(
                "NAME: Application help",
                '["MUST MENTION TO CHECK": apply] apply '
                f'["ALTERNATIVES": {{"{"x" * 201}": too-long}}]',
            )

    def test_parses_and_round_trips_additional_selection_info(self):
        guidance = (
            "Use the context to distinguish staff applications from questions about playing "
            "the game on mobile. This guidance must never be sent to the recipient."
        )
        parsed = parse_autoreply_rule_spec(
            "NAME: Application help",
            '["MUST MENTION TO CHECK": apply, staff role] apply '
            '["ALTERNATIVES": {"Device requirements": app-device}] '
            f'["ADDITIONAL INFO": "{guidance}"]',
        )

        self.assertEqual(parsed["additional_info"], guidance)
        formatted = format_autoreply_rule_spec("apply", parsed)
        self.assertTrue(
            formatted.endswith(f'["ADDITIONAL INFO": {json.dumps(guidance)}]')
        )
        self.assertNotIn("additional_info", formatted)

    def test_additional_info_supports_multiline_text_and_rejects_empty_values(self):
        parsed = parse_autoreply_rule_spec(
            "NAME: Application help",
            '["MUST MENTION TO CHECK": apply] apply '
            '["ADDITIONAL INFO": "First line.\nSecond line."]',
        )
        self.assertEqual(parsed["additional_info"], "First line.\nSecond line.")

        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            parse_autoreply_rule_spec(
                "NAME: Application help",
                '["MUST MENTION TO CHECK": apply] apply ["ADDITIONAL INFO": ]',
            )

    def test_alternatives_block_can_precede_primary_alias(self):
        parsed = parse_autoreply_rule_spec(
            "NAME: Application help",
            '["MUST MENTION TO CHECK": apply] '
            '["ALTERNATIVES": {"Application status": status}] apply',
        )

        self.assertEqual(parsed["alias"], "apply")
        self.assertEqual(
            parsed["alternatives"],
            [{"name": "Application status", "alias": "status"}],
        )

    def test_formats_autoreply_rule_as_raw_edit_arguments(self):
        formatted = format_autoreply_rule_spec(
            "apply",
            {
                "name": "Application help",
                "triggers": ["apply", "application status"],
                "alias": "apply",
                "alternatives": [
                    {"name": "Application status", "alias": "app-status"}
                ],
            },
        )

        self.assertEqual(
            formatted,
            '"NAME: Application help" '
            '["MUST MENTION TO CHECK": "apply", "application status"] "apply" '
            '["ALTERNATIVES": {"Application status": "app-status"}]',
        )

    def test_rule_requires_name_triggers_and_alias(self):
        with self.assertRaises(ValueError):
            parse_autoreply_rule_spec("Apply", "[apply] apply")
        with self.assertRaises(ValueError):
            parse_autoreply_rule_spec(
                "NAME: Apply",
                '["MUST MENTION TO CHECK": ] apply',
            )
        with self.assertRaises(ValueError):
            parse_autoreply_rule_spec(
                "NAME: Apply",
                '["MUST MENTION TO CHECK": apply] apply '
                '["ALTERNATIVES": {"Status": status}',
            )


class DeferredDeleteMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_defers_delete_and_delegates_other_message_operations(self):
        class Message:
            marker = "real-message"

            def __init__(self):
                self.deleted_with = "not-deleted"
                self.reactions = []

            async def delete(self, *, delay=None):
                self.deleted_with = delay

            async def add_reaction(self, emoji):
                self.reactions.append(emoji)

        message = Message()
        deferred = DeferredDeleteMessage(message)

        self.assertEqual(deferred.marker, "real-message")
        await deferred.add_reaction("check")
        await deferred.delete(delay=5)
        self.assertEqual(message.deleted_with, "not-deleted")
        await deferred.finalize_delete()

        self.assertEqual(message.reactions, ["check"])
        self.assertEqual(message.deleted_with, 5)

    async def test_immediate_delete_takes_precedence_over_delayed_delete(self):
        class Message:
            def __init__(self):
                self.deleted_with = "not-deleted"

            async def delete(self, *, delay=None):
                self.deleted_with = delay

        message = Message()
        deferred = DeferredDeleteMessage(message)
        await deferred.delete()
        await deferred.delete(delay=10)
        await deferred.finalize_delete()

        self.assertIsNone(message.deleted_with)


if __name__ == "__main__":
    unittest.main()
