import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

try:
    from cogs.modmail import Modmail
except ModuleNotFoundError as exc:
    if exc.name != "discord":
        raise
    Modmail = None


class FakeConfig(dict):
    async def update(self):
        return None


@unittest.skipIf(Modmail is None, "discord.py is not installed in the unit-test runtime")
class ModmailAutoreplyFlowTests(unittest.IsolatedAsyncioTestCase):
    async def _dispatch_followup(self, *, subscribers=None, sticky_subscription=False):
        config = FakeConfig(
            subscriptions={"123": list(subscribers or [])},
            reply_reminders={},
            recipient_reply_reminder_delay=43_200,
        )
        cog = Modmail.__new__(Modmail)
        cog.bot = SimpleNamespace(config=config)
        cog._ai_test_threads = set()

        message = SimpleNamespace(id=456)
        thread = SimpleNamespace(
            id=123,
            _initial_message_id=1,
            _opening_intake_pending=False,
            _opening_alias_subscribed=sticky_subscription,
            _intake_collecting=True,
            _intake_handed_to_agent=False,
            _awaiting_initial_inquiry=False,
            begin_followup_autoreply_workflow=AsyncMock(),
        )

        await cog.on_thread_reply(thread, False, message, False, False)
        return thread, message

    async def test_subscribed_ticket_still_reviews_later_messages_for_autoreplies(self):
        thread, message = await self._dispatch_followup(subscribers=["<@42>"])

        thread.begin_followup_autoreply_workflow.assert_awaited_once_with(message)
        self.assertFalse(thread._intake_collecting)
        self.assertTrue(thread._intake_handed_to_agent)

    async def test_sticky_alias_subscription_flag_does_not_permanently_stop_autoreplies(self):
        thread, message = await self._dispatch_followup(sticky_subscription=True)

        thread.begin_followup_autoreply_workflow.assert_awaited_once_with(message)
        self.assertFalse(thread._intake_collecting)
        self.assertTrue(thread._intake_handed_to_agent)
