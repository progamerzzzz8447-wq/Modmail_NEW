import unittest

from core.alias_parser import (
    normalize_alias,
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

    def test_reply_alias_rejects_non_reply_commands(self):
        self.assertIsNone(parse_reply_alias('"freply Thanks" && "close"'))
        self.assertIsNone(parse_reply_alias('"freply"'))

    def test_normalize_alias_appends_invocation_text(self):
        self.assertEqual(normalize_alias('"freply Hello"', "world"), ["freply Hello world"])

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


if __name__ == "__main__":
    unittest.main()
