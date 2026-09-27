import re
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

try:
    from cogs.modmail import HR_CASE_ROLE_ID, Modmail
except ModuleNotFoundError as exc:
    if exc.name != "discord":
        raise
    Modmail = None


class FakeConfig(dict):
    async def update(self):
        return None


@asynccontextmanager
async def no_typing(_ctx):
    yield


@unittest.skipIf(Modmail is None, "discord.py is not installed in the unit-test runtime")
class CaseOpenedTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_hr_role_passes_the_command_check(self):
        command = Modmail.caseopened
        ctx = SimpleNamespace(author=SimpleNamespace(roles=[]), thread=object())
        self.assertFalse(all([await check(ctx) for check in command.checks]))
        ctx.author.roles = [SimpleNamespace(id=HR_CASE_ROLE_ID)]
        self.assertTrue(all([await check(ctx) for check in command.checks]))

    async def test_reply_uses_one_persistent_case_number(self):
        config = FakeConfig(hr_case_numbers={})
        cog = Modmail.__new__(Modmail)
        cog.bot = SimpleNamespace(config=config)
        message = SimpleNamespace(content="?caseopened")
        thread = SimpleNamespace(reply=AsyncMock())
        ctx = SimpleNamespace(channel=SimpleNamespace(id=123), message=message, thread=thread)

        with patch("cogs.modmail.safe_typing", no_typing):
            await Modmail.caseopened.callback(cog, ctx)
            first_message = message.content
            await Modmail.caseopened.callback(cog, ctx)

        self.assertRegex(first_message, r"^\*\*Case Opened: [A-Z0-9]{6}\*\*\n\n")
        self.assertIn("**as much detail**", first_message)
        self.assertEqual(message.content, first_message)
        self.assertEqual(config["hr_case_numbers"]["123"], re.search(r"[A-Z0-9]{6}", first_message).group())
        self.assertEqual(thread.reply.await_count, 2)
