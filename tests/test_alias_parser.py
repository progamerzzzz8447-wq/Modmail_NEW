import unittest

from core.alias_parser import (
    DeferredDeleteMessage,
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
            parse_reply_alias('"move applications" && "fareply Thanks" && "close"'),
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

    def test_rule_requires_name_triggers_and_alias(self):
        with self.assertRaises(ValueError):
            parse_autoreply_rule_spec("Apply", "[apply] apply")
        with self.assertRaises(ValueError):
            parse_autoreply_rule_spec(
                "NAME: Apply",
                '["MUST MENTION TO CHECK": ] apply',
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
