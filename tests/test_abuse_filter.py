import unittest

from core.abuse_filter import ABUSE_AUTO_CLOSE_MESSAGE, contains_abusive_language


class AbuseFilterTests(unittest.TestCase):
    def test_detects_configured_term_n_word_forms_and_severe_abuse(self):
        abusive_messages = (
            "You are a goon.",
            "What a g00n",
            "the n-word",
            "the n words",
            "n.i.g.g.e.r",
            "nigggger",
            "f@gg0t",
            "retards",
            "go fuck yourself",
            "K.Y.S",
        )

        for message in abusive_messages:
            with self.subTest(message=message):
                self.assertTrue(contains_abusive_language(message))

    def test_uses_whole_words_to_avoid_embedded_fragment_matches(self):
        acceptable_messages = (
            "The lagoon looks nice.",
            "Can I go on the game?",
            "Please add more spacing.",
            "This process is retarding progress.",
            "I have five working days.",
        )

        for message in acceptable_messages:
            with self.subTest(message=message):
                self.assertFalse(contains_abusive_language(message))

    def test_warning_is_the_approved_auto_close_message(self):
        self.assertEqual(
            ABUSE_AUTO_CLOSE_MESSAGE,
            "Unfortunately, we do not tolerate abuse directed towards our staff team. This ticket "
            "has now been automatically closed.\n\n"
            "You may open a new ticket if you still require assistance. However, any further "
            "abusive behaviour will result in you being blocked from contacting support.",
        )


if __name__ == "__main__":
    unittest.main()
