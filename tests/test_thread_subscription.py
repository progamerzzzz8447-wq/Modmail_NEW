import unittest
from types import SimpleNamespace

from core.subscriptions import author_has_thread_subscription


class ThreadSubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.author = SimpleNamespace(
            id=123,
            roles=[SimpleNamespace(id=456, mention="<@&456>")],
        )

    def test_direct_user_subscription_allows_reply(self):
        self.assertTrue(author_has_thread_subscription(["<@123>"], self.author))
        self.assertTrue(author_has_thread_subscription(["<@!123>"], self.author))

    def test_subscribed_role_allows_reply(self):
        self.assertTrue(author_has_thread_subscription(["<@&456>"], self.author))

    def test_guild_wide_subscription_allows_reply(self):
        self.assertTrue(author_has_thread_subscription(["@here"], self.author))
        self.assertTrue(author_has_thread_subscription(["@everyone"], self.author))

    def test_unsubscribed_author_cannot_reply(self):
        self.assertFalse(author_has_thread_subscription([], self.author))
        self.assertFalse(author_has_thread_subscription(["<@999>", "<@&888>"], self.author))


if __name__ == "__main__":
    unittest.main()
