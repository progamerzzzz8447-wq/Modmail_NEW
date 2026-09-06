import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.application_reading import (
    application_reading_reply, expand_application_reading,
    message_schedule_text, schedule_timestamp,
    is_general_reading_question, handle_reading_question,
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
        self.assertIn('This time is an estimate', reply)

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

    def test_reported_questions_and_personal_status_are_distinct(self):
        for text in ['when are apps next being read', 'hi when is next application reading',
                     'when will applications be read next?', 'what time is the next app reading pls']:
            self.assertTrue(is_general_reading_question(text),text)
        for text in ['what is the status of my application', 'when will my app result arrive',
                     'when is next app reading and why was I banned', 'when is next flight',
                     'when is next application reading? I also need a refund']:
            self.assertFalse(is_general_reading_question(text),text)

    def thread(self, subscribed=False):
        return SimpleNamespace(
            id=123, channel=SimpleNamespace(id=456,send=AsyncMock()),
            bot=SimpleNamespace(config={'gemini_ai_enabled':True,'subscriptions':{'123':['staff'] if subscribed else []}},
                api=SimpleNamespace(claim_ai_autoreply=AsyncMock(return_value=True))),
            _send_ai_autoreply=AsyncMock(),_run_automatic_aiall=AsyncMock(),
            _followup_revision=0,_pending_followup_message=None,
        )

    async def test_confirmed_standalone_schedule_runs_aiall(self):
        thread=self.thread()
        message=SimpleNamespace(id=999,content='when are apps next being read',attachments=[])
        with patch('core.application_reading.application_reading_result',new=AsyncMock(return_value=(True,'SCHEDULE'))):
            self.assertTrue(await handle_reading_question(thread,message,allow_resolution=True))
        thread._run_automatic_aiall.assert_awaited_once()
        self.assertFalse(thread._intake_collecting)
        thread.bot.api.claim_ai_autoreply.assert_awaited_once_with(456,'system:application-reading:999','Next application reading')

    async def test_subscribed_or_unresolved_ticket_still_gets_answer_without_closure(self):
        for subscribed,allow in [(True,True),(False,False)]:
            thread=self.thread(subscribed)
            with patch('core.application_reading.application_reading_result',new=AsyncMock(return_value=(True,'SCHEDULE'))):
                await handle_reading_question(thread,SimpleNamespace(id=999,content='when is next app reading'),allow_resolution=allow)
            thread._send_ai_autoreply.assert_awaited_once()
            thread._run_automatic_aiall.assert_not_awaited()

    async def test_missing_schedule_does_not_resolve(self):
        thread=self.thread()
        with patch('core.application_reading.application_reading_result',new=AsyncMock(return_value=(False,'UNAVAILABLE'))):
            await handle_reading_question(thread,SimpleNamespace(id=999,content='when is next app reading'),allow_resolution=True)
        thread._run_automatic_aiall.assert_not_awaited()

    async def test_duplicate_callback_does_not_reply_again(self):
        thread=self.thread()
        thread.bot.api.claim_ai_autoreply.return_value=False
        self.assertTrue(await handle_reading_question(thread,SimpleNamespace(id=999,content='when is next app reading'),allow_resolution=True))
        thread._send_ai_autoreply.assert_not_awaited()
        thread._run_automatic_aiall.assert_not_awaited()

    async def test_new_message_during_lookup_prevents_closure(self):
        thread=self.thread()
        async def lookup(bot):
            thread._followup_revision+=1
            return True,'SCHEDULE'
        with patch('core.application_reading.application_reading_result',side_effect=lookup):
            await handle_reading_question(thread,SimpleNamespace(id=999,content='when is next app reading'),allow_resolution=True)
        thread._run_automatic_aiall.assert_not_awaited()
