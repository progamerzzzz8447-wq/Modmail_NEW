import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    from core.thread import Thread
except ModuleNotFoundError:
    Thread = None


@unittest.skipIf(Thread is None, "Modmail runtime dependencies are not installed")
class FormResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_fenced_ai_form_waits_for_recipient_reply_before_aiall(self):
        staff_message = SimpleNamespace(id=10, created_at=datetime.now(timezone.utc))
        channel = SimpleNamespace(
            id=20,
            send=AsyncMock(return_value=staff_message),
            edit=AsyncMock(),
            guild=SimpleNamespace(get_channel=lambda _: None),
        )
        bot = SimpleNamespace(
            config={"message_embeds_v2": True, "subscriptions": {}},
            user=SimpleNamespace(id=30, display_avatar=SimpleNamespace(url="https://example.com/avatar")),
            api=SimpleNamespace(append_log=AsyncMock()),
        )
        thread = SimpleNamespace(
            id=40,
            channel=channel,
            bot=bot,
            recipient=SimpleNamespace(send=AsyncMock()),
            _followup_revision=0,
            _intake_collecting=False,
            _intake_handed_to_agent=True,
            cancel_informative_autoreply_rescan=lambda: None,
            close=AsyncMock(),
            _send_ai_autoreply=AsyncMock(),
        )
        with patch("core.application_reading.expand_application_reading", new=AsyncMock(side_effect=lambda _, text, **__: text)):
            await Thread._send_ai_autoreply(thread, "Form", "Please fill in:\n```\nNAME:\n```")

        self.assertEqual(thread._ai_form_sent_revision, 0)
        await Thread._run_automatic_aiall(thread)
        thread._send_ai_autoreply.assert_not_awaited()
        channel.edit.assert_not_awaited()
        thread.close.assert_not_awaited()
        self.assertTrue(thread._intake_collecting)
        self.assertFalse(thread._intake_handed_to_agent)

        thread._followup_revision += 1
        await Thread._run_automatic_aiall(thread)
        thread._send_ai_autoreply.assert_awaited_once()
        channel.edit.assert_awaited_once()
        thread.close.assert_awaited_once()
