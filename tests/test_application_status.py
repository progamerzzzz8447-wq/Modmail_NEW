import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from core.application_reading import describe_dynamic_reply, expand_application_reading
from core.application_status import application_status_reply, classify_unknown_status, known_status


class Response:
    def __init__(self, status=200, data=None):
        self.status, self.data = status, data
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return False
    async def json(self): return self.data


class Session:
    def __init__(self, response): self.response, self.calls = response, []
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


class Config(dict):
    def get(self, key, default=None, **kwargs): return super().get(key, default)


class StatusTests(unittest.IsolatedAsyncioTestCase):
    def bot(self, status='submitted', http=200):
        return SimpleNamespace(session=Session(Response(http, {'status':status,'tomCode':'TOM-12345'})),config=Config())

    async def lookup(self, bot):
        with patch.dict(os.environ, TOM_LOOKUP_API_KEY='test-only-key'):
            return await application_status_reply(bot, SimpleNamespace(name='actual_recipient',display_name='Someone else'))

    async def test_accepted_uses_recipient_and_dm_guidance(self):
        bot=self.bot('accepted')
        reply=await self.lookup(bot)
        self.assertIn('application has been **accepted**',reply)
        self.assertIn('TUI | Careers#9460',reply)
        request=bot.session.calls[0][1]
        self.assertEqual(request['json'],{'discordUsername':'actual_recipient'})
        self.assertFalse(request['allow_redirects'])
        self.assertEqual(request['timeout'],20)

    async def test_declined(self):
        reply=await self.lookup(self.bot('rejected'))
        self.assertIn('Unfortunately, your application has been **declined**',reply)
        self.assertTrue(reply.startswith('Hi, thanks for getting in touch!\n\n'))
        self.assertTrue(reply.endswith('**Application reference:** `TOM-12345`'))
        self.assertIn('TUI | Careers#9460',reply)

    async def test_submitted_includes_schedule(self):
        with patch('core.application_status.application_reading_reply',new=AsyncMock(return_value='LIVE SCHEDULE')):
            reply=await self.lookup(self.bot())
        self.assertIn('has been received',reply)
        self.assertIn('LIVE SCHEDULE',reply)

    async def test_archived_is_not_a_rejection(self):
        with patch('core.application_status.classify_unknown_status',new=AsyncMock()) as ai:
            reply=await self.lookup(self.bot('archived'))
        ai.assert_not_awaited()
        self.assertIn('**Your application update**',reply)
        self.assertIn('reply here',reply)
        self.assertIn('TUI | Careers#9460',reply)
        self.assertNotIn('archived',reply.lower())
        self.assertNotIn('**Application declined**',reply)

    async def test_unknown_uses_classifier_without_exposing_raw_label(self):
        with patch('core.application_status.classify_unknown_status',new=AsyncMock(return_value='unknown')):
            reply=await self.lookup(self.bot('secret @everyone instruction'))
        self.assertNotIn('@everyone',reply)
        self.assertIn("outcome isn't available",reply)

    async def test_failed_lookup_does_not_claim_no_application(self):
        for code in [401,429,500]:
            with self.subTest(code=code):
                reply=await self.lookup(self.bot(http=code))
                self.assertIn('temporarily unavailable',reply)

    async def test_not_found_is_not_proof_of_no_submission(self):
        reply=await self.lookup(self.bot(http=404))
        self.assertIn('current Discord username',reply)

    async def test_missing_key_makes_no_request(self):
        bot=self.bot()
        with patch.dict(os.environ,{},clear=True):
            reply=await application_status_reply(bot,SimpleNamespace(name='user'))
        self.assertEqual(bot.session.calls,[])
        self.assertIn("can't check",reply)

    async def test_marker_uses_recipient(self):
        recipient=SimpleNamespace(name='ticket_owner')
        with patch('core.application_status.application_status_reply',new=AsyncMock(return_value='STATUS')) as lookup:
            result=await expand_application_reading(None,'[APPLICATION_STATUS]',recipient=recipient)
        self.assertEqual(result,'STATUS')
        lookup.assert_awaited_once_with(None,recipient)

    async def test_ai_status_label_request_omits_personal_details(self):
        data={'candidates':[{'content':{'parts':[{'text':'{"category":"accepted","explicit":true}'}]}}]}
        bot=SimpleNamespace(session=Session(Response(data=data)),config=Config(gemini_api_key='test',gemini_model='gemini-test'))
        self.assertEqual(await classify_unknown_status(bot,'Application approved successfully'),'accepted')
        payload=bot.session.calls[0][1]['json']
        self.assertIn('untrusted data',payload['contents'][0]['parts'][0]['text'])

    def test_known_labels_and_selector_description(self):
        self.assertEqual(known_status('UNDER_REVIEW'),'reviewing')
        self.assertIsNone(known_status('not accepted'))
        self.assertIn('receipt',describe_dynamic_reply('[APPLICATION_STATUS]'))
