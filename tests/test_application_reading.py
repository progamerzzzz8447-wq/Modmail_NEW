import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from core.application_reading import (
    application_reading_reply, expand_application_reading,
    message_schedule_text, schedule_timestamp,
)


class ReadingTests(unittest.IsolatedAsyncioTestCase):
    def bot(self, *messages):
        async def history(**kwargs):
            for content in messages:
                yield SimpleNamespace(content=content, embeds=[])
        return SimpleNamespace(get_channel=lambda _: SimpleNamespace(history=history))

    def test_uk_date_and_discord_timestamp(self):
        self.assertEqual(schedule_timestamp('Next Scheduled Reading: 10/09/2026, 16:30'), (True, 1789054200))
        self.assertEqual(schedule_timestamp('**Next Scheduled Reading:** <t:1789054200:s>'), (True, 1789054200))

    def test_timestamp_on_following_line(self):
        self.assertEqual(schedule_timestamp('Next Scheduled Reading:\n<t:1789054200:F>'), (True, 1789054200))

    def test_invalid_and_unrelated_dates(self):
        self.assertEqual(schedule_timestamp('Next Scheduled Reading: 31/02/2026, 16:30'), (True, None))
        self.assertEqual(schedule_timestamp('Next flight: <t:1789054200:f>'), (False, None))

    def test_clock_change_is_not_guessed(self):
        for date in ['29/03/2026, 01:30', '25/10/2026, 01:30']:
            self.assertEqual(schedule_timestamp('Next Scheduled Reading: '+date), (True, None))

    def test_embed_field(self):
        message=SimpleNamespace(content='', embeds=[SimpleNamespace(title='', description='', fields=[SimpleNamespace(name='Next Scheduled Reading', value='10/09/2026, 16:30')])])
        self.assertEqual(schedule_timestamp(message_schedule_text(message)), (True, 1789054200))

    async def test_future_schedule_and_disclaimer(self):
        reply=await application_reading_reply(self.bot('Next Scheduled Reading: 10/09/2026, 16:30'), now=datetime(2026,9,6,tzinfo=timezone.utc))
        self.assertIn('<t:1789054200:f>', reply)
        self.assertIn('not a guaranteed result time', reply)

    async def test_latest_cancelled_notice_does_not_resurrect_old_schedule(self):
        reply=await application_reading_reply(self.bot('Next Scheduled Reading: TBA', 'Next Scheduled Reading: 10/09/2026, 16:30'), now=datetime(2026,9,6,tzinfo=timezone.utc))
        self.assertNotIn('<t:', reply)

    async def test_past_schedule_is_not_promised(self):
        reply=await application_reading_reply(self.bot('Next Scheduled Reading: 10/09/2026, 16:30'), now=datetime(2026,9,11,tzinfo=timezone.utc))
        self.assertNotIn('<t:', reply)

    async def test_channel_access_failure_is_safe(self):
        bot=SimpleNamespace(get_channel=lambda _:None, fetch_channel=AsyncMock(side_effect=PermissionError))
        with self.assertLogs('core.application_reading',level='WARNING'):
            reply=await application_reading_reply(bot)
        self.assertIn("couldn't confirm",reply)

    async def test_ordinary_reply_does_not_access_channel(self):
        self.assertEqual(await expand_application_reading(None,'Hello'), 'Hello')
