"""Secure Discord -> E-Crew Human Resources case bridge."""

import asyncio
import re
import secrets
import string
from typing import Optional

import discord
from discord.ext import commands, tasks

from core.models import getLogger

logger = getLogger(__name__)
CASE_NAME_RE = re.compile(r"^([a-z0-9]{6})-unclaimed$")
LEGACY_CASE_NAME_RE = re.compile(r"^case-([a-z0-9]{6})$")
CASE_ALPHABET = string.ascii_uppercase + string.digits


class HumanResourcesBridge(commands.Cog):
    """Mirror HR ticket channels to the restricted E-Crew case workspace."""

    def __init__(self, bot):
        self.bot = bot
        self._case_channels = set()
        self._sync_locks = {}
        self._last_error = None

    async def cog_load(self):
        self.hr_category_reconciliation.start()

    def cog_unload(self):
        self.hr_category_reconciliation.cancel()

    @property
    def enabled(self):
        return bool(self._portal_url and self._secret and self._category_id)

    def _configuration_problem(self):
        missing = []
        if not self._portal_url:
            missing.append("HR_PORTAL_URL")
        if not self._secret:
            missing.append("HR_BOT_SECRET")
        if not self._category_id:
            missing.append("HR_DISCORD_CATEGORY_ID (or a category named Human Resources)")
        return ", ".join(missing)

    @property
    def _portal_url(self):
        return str(self.bot.config.get("hr_portal_url", convert=False) or "").rstrip("/")

    @property
    def _secret(self):
        return str(self.bot.config.get("hr_bot_secret", convert=False) or "")

    @property
    def _category_id(self):
        value = self.bot.config.get("hr_discord_category_id", convert=False)
        try:
            if value:
                return int(value)
        except (TypeError, ValueError):
            pass
        guild = getattr(self.bot, "modmail_guild", None)
        if guild:
            category = discord.utils.find(
                lambda item: item.name.strip().lower() in {"human resources", "hr"},
                guild.categories,
            )
            if category:
                return category.id
        return None

    def _is_hr_channel(self, channel):
        return (
            self.enabled
            and isinstance(channel, discord.TextChannel)
            and getattr(channel, "category_id", None) == self._category_id
        )

    async def _post(self, payload):
        if not self.enabled:
            return None
        url = f"{self._portal_url}/functions/v1/hr-cases"
        try:
            async with self.bot.session.post(
                url,
                json=payload,
                headers={"x-bot-secret": self._secret},
                timeout=20,
            ) as response:
                body = await response.text()
                if response.status >= 300:
                    self._last_error = f"HTTP {response.status}: {body[:500]}"
                    logger.error("HR bridge returned HTTP %s: %s", response.status, body[:1000])
                    return None
                self._last_error = None
                return await response.json()
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.error("HR bridge request failed.", exc_info=True)
            return None

    @staticmethod
    def _new_case_number():
        return "".join(secrets.choice(CASE_ALPHABET) for _ in range(6))

    async def _case_number_for_channel(self, channel):
        stored = self.bot.config["hr_case_numbers"].get(str(channel.id))
        if stored:
            return str(stored).upper(), False, False
        current = CASE_NAME_RE.match(channel.name)
        legacy = LEGACY_CASE_NAME_RE.match(channel.name)
        match = current or legacy
        case_number = match.group(1).upper() if match else self._new_case_number()
        self.bot.config["hr_case_numbers"][str(channel.id)] = case_number
        await self.bot.config.update()
        return case_number, match is None, legacy is not None

    async def _thread_for_channel(self, channel):
        try:
            return await self.bot.threads.find(channel=channel)
        except Exception:
            return None

    async def _ensure_case(self, channel, *, backfill=False):
        if not self._is_hr_channel(channel):
            return None
        lock = self._sync_locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            case_number, newly_assigned, legacy_name = await self._case_number_for_channel(channel)
            if newly_assigned or legacy_name:
                try:
                    channel = await channel.edit(
                        name=f"{case_number.lower()}-unclaimed",
                        reason="Human Resources case assigned",
                    )
                except discord.HTTPException:
                    logger.warning("Could not rename HR ticket channel %s.", channel.id, exc_info=True)

            thread = await self._thread_for_channel(channel)
            recipient = getattr(thread, "recipient", None)
            result = await self._post({
                "event": "case_upsert",
                "case": {
                    "case_number": case_number,
                    "discord_channel_id": str(channel.id),
                    "discord_guild_id": str(channel.guild.id),
                    "recipient_id": str(getattr(recipient, "id", "")) or None,
                    "recipient_name": str(recipient) if recipient else None,
                    "recipient_avatar_url": str(getattr(getattr(recipient, "display_avatar", None), "url", "")) or None,
                    "channel_name": channel.name,
                    "status": "open",
                },
            })
            if result:
                self._case_channels.add(channel.id)
                if backfill:
                    try:
                        async for message in channel.history(limit=None, oldest_first=True):
                            await self._sync_message(message)
                    except discord.HTTPException:
                        logger.warning("Could not backfill HR case %s.", case_number, exc_info=True)
            return result

    @staticmethod
    def _embed_dict(embed):
        data = embed.to_dict()
        # Keep the payload bounded and predictable for storage/UI rendering.
        return {key: data[key] for key in ("title", "description", "url", "color", "author", "footer", "image", "thumbnail", "fields", "timestamp") if key in data}

    def _serialize_message(self, message):
        embeds = [self._embed_dict(embed) for embed in message.embeds]
        content = message.content or ""
        attachments = [
            {
                "url": str(item.url), "proxy_url": str(item.proxy_url), "filename": item.filename,
                "content_type": item.content_type, "size": item.size,
            }
            for item in message.attachments
        ]

        direction = "system"
        author_name = getattr(message.author, "display_name", None) or str(message.author)
        author_avatar = str(getattr(getattr(message.author, "display_avatar", None), "url", "")) or None
        for embed in message.embeds:
            footer = getattr(getattr(embed, "footer", None), "text", None) or ""
            title = embed.title or ""
            if footer.startswith("Message ID:") or title.startswith("Message from "):
                direction = "recipient"
            elif "Staff Reply" in title or footer or getattr(embed, "author", None):
                direction = "staff"
            if embed.description:
                content = f"{content}\n{embed.description}".strip()
            embed_author = getattr(getattr(embed, "author", None), "name", None)
            embed_avatar = getattr(getattr(embed, "author", None), "icon_url", None)
            if embed_author:
                author_name = embed_author
            if embed_avatar:
                author_avatar = str(embed_avatar)
            image_url = getattr(getattr(embed, "image", None), "url", None)
            if image_url:
                attachments.append({"url": str(image_url), "filename": title or "image", "content_type": "image/*"})
            for field in getattr(embed, "fields", []):
                if field.name.startswith(("File upload", "Image")):
                    match = re.search(r"\[([^]]+)]\((https?://[^)]+)\)", field.value or "")
                    if match:
                        attachments.append({"url": match.group(2), "filename": match.group(1), "content_type": None})

        if message.author.id != getattr(self.bot.user, "id", None):
            # Raw command invocations are deleted by Modmail and are not transcript content.
            if content.lstrip().startswith(str(self.bot.prefix)):
                return None
            direction = "staff"

        reference_id = getattr(getattr(message, "reference", None), "message_id", None)
        return {
            "discord_message_id": str(message.id),
            "direction": direction,
            "author_id": str(message.author.id),
            "author_name": author_name,
            "author_avatar_url": author_avatar,
            "content": content,
            "attachments": attachments,
            "embeds": embeds,
            "reply_to_message_id": str(reference_id) if reference_id else None,
            "sent_at": message.created_at.isoformat(),
            "edited_at": message.edited_at.isoformat() if message.edited_at else None,
        }

    async def _sync_message(self, message):
        if not self._is_hr_channel(message.channel):
            return
        if message.channel.id not in self._case_channels:
            if not await self._ensure_case(message.channel):
                return
        payload = self._serialize_message(message)
        if payload:
            await self._post({
                "event": "message_upsert",
                "discord_channel_id": str(message.channel.id),
                "message": payload,
            })

    @tasks.loop(seconds=15)
    async def hr_category_reconciliation(self):
        """Recover category moves missed while Discord reconnects or caches update."""
        if not self.enabled:
            return
        category = self.bot.get_channel(self._category_id)
        if not isinstance(category, discord.CategoryChannel):
            return
        for channel in category.text_channels:
            if channel.id not in self._case_channels:
                await self._ensure_case(channel, backfill=True)
        renames = await self._post({"event": "pending_renames"})
        for request in (renames or {}).get("renames", []):
            try:
                channel = self.bot.get_channel(int(request["discord_channel_id"]))
            except (KeyError, TypeError, ValueError):
                channel = None
            if not isinstance(channel, discord.TextChannel):
                continue
            requested = re.sub(r"[^a-z0-9_-]+", "-", str(request.get("requested_channel_name") or "").strip().lower())
            requested = requested.strip("-")[:100]
            if not requested:
                continue
            if channel.name != requested:
                channel = await channel.edit(name=requested, reason="Human Resources portal rename")
            await self._post({
                "event": "rename_complete",
                "discord_channel_id": str(channel.id),
                "channel_name": channel.name,
            })

    @hr_category_reconciliation.before_loop
    async def before_hr_category_reconciliation(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.enabled:
            logger.warning("HR bridge disabled; missing %s.", self._configuration_problem())
            return
        category = self.bot.get_channel(self._category_id)
        if isinstance(category, discord.CategoryChannel):
            for channel in category.text_channels:
                self.bot.loop.create_task(self._ensure_case(channel, backfill=True))

    @commands.command(name="hrsync")
    @commands.is_owner()
    async def hr_sync(self, ctx):
        """Validate the HR bridge and backfill every ticket in its category."""
        if not self.enabled:
            await ctx.send(f"HR bridge is disabled. Missing: `{self._configuration_problem()}`.")
            return
        category = self.bot.get_channel(self._category_id)
        if not isinstance(category, discord.CategoryChannel):
            await ctx.send("The configured Human Resources category could not be found.")
            return
        status = await ctx.send(f"Synchronizing {len(category.text_channels)} HR ticket(s)…")
        succeeded = 0
        for channel in category.text_channels:
            if await self._ensure_case(channel, backfill=True):
                succeeded += 1
        result = f"HR synchronization complete: {succeeded}/{len(category.text_channels)} ticket(s) synchronized."
        if self._last_error:
            result += f"\nLast bridge error: `{self._last_error[:1500]}`"
        await status.edit(content=result)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after):
        if self._is_hr_channel(after) and getattr(before, "category_id", None) != self._category_id:
            await self._ensure_case(after, backfill=True)
        elif self._is_hr_channel(after) and before.name != after.name:
            await self._ensure_case(after)

    @commands.Cog.listener()
    async def on_thread_ready(self, thread, creator, category, initial_message):
        if self._is_hr_channel(getattr(thread, "channel", None)):
            await self._ensure_case(thread.channel, backfill=True)

    @commands.Cog.listener()
    async def on_message(self, message):
        if self._is_hr_channel(getattr(message, "channel", None)):
            await self._sync_message(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before, after):
        if self._is_hr_channel(getattr(after, "channel", None)):
            await self._sync_message(after)

    @commands.Cog.listener()
    async def on_message_delete(self, message):
        if self._is_hr_channel(getattr(message, "channel", None)):
            # Ignore command clean-up; those messages were intentionally never mirrored.
            if (message.content or "").lstrip().startswith(str(self.bot.prefix)):
                return
            await self._post({"event": "message_delete", "discord_message_id": str(message.id)})

    @commands.Cog.listener()
    async def on_thread_close(self, thread, closer, silent, delete_channel, message, scheduled):
        channel = getattr(thread, "channel", None)
        if channel and channel.id in self._case_channels:
            await self._post({"event": "case_close", "discord_channel_id": str(channel.id)})
            self._case_channels.discard(channel.id)


async def setup(bot):
    await bot.add_cog(HumanResourcesBridge(bot))
