import unittest

from core.message_autofill import AUTOFILL_CLOSING, expand_staff_reply_markers


class StaffMessageAutofillTests(unittest.TestCase):
    def test_expands_both_markers_and_preserves_middle(self):
        self.assertEqual(
            expand_staff_reply_markers("hi message here bla bla bye", "Alex"),
            "Hello Alex,\n\nThank you for writing in.\n\n"
            "message here bla bla\n\n"
            + AUTOFILL_CLOSING,
        )

    def test_expands_markers_independently(self):
        self.assertEqual(
            expand_staff_reply_markers("hi Please provide evidence.", "Alex"),
            "Hello Alex,\n\nThank you for writing in.\n\nPlease provide evidence.",
        )
        self.assertEqual(
            expand_staff_reply_markers("Thank you. bye", "Alex"),
            "Thank you.\n\n" + AUTOFILL_CLOSING,
        )

    def test_does_not_replace_partial_or_internal_words(self):
        text = "hii say bye to them please"
        self.assertEqual(expand_staff_reply_markers(text, "Alex"), text)

