import asyncio
import re
from datetime import datetime, timezone, timedelta
from io import BytesIO
from itertools import zip_longest
from typing import Optional, Union, List, Tuple, Literal
import logging

import discord
from discord.ext import commands
from discord.ext import tasks
from discord.ext.commands.view import StringView
from discord.ext.commands.cooldowns import BucketType
from discord.role import Role
from discord.utils import escape_markdown

from dateutil import parser

from core import checks
from core.alias_parser import (
    AUTOREPLY_DISPLAY_NAME_LIMIT,
    format_autoreply_rule_spec,
    parse_autoreply_rule_spec,
    parse_reply_alias,
)
from core.abuse_filter import contains_abusive_language, normalize_custom_abuse_term
from core.ai_reviewer import (
    AI_ALL_CLOSING,
    AI_REPLY_CLOSING,
    AI_REPLY_FOOTER,
    GeminiAnnoyReplyGenerator,
    GeminiHelpfulReplyGenerator,
    GeminiTicketSummaryGenerator,
    NO_MATCH,
    find_command_references,
    finalize_generated_ai_reply,
)
from core.models import DMDisabled, PermissionLevel, SimilarCategoryConverter, getLogger
from core.paginator import EmbedPaginatorSession
from core.thread import Thread
from core.time import UserFriendlyTime, human_timedelta
from core.utils import *

logger = getLogger(__name__)

MANUAL_AI_ROLE_IDS = (1391515982417100951, 1516405254571298866)


class Modmail(commands.Cog):
    """Commands directly related to Modmail functionality."""

    def __init__(self, bot):
        self.bot = bot
        self._snoozed_cache = []
        self._auto_unsnooze_task = self.bot.loop.create_task(self.auto_unsnooze_task())

    @commands.Cog.listener()
    async def on_thread_reply(self, thread, from_mod, message, anonymous, plain):
        """Run eligible automatic replies and track unanswered recipient follow-ups."""
        key = str(thread.id)
        reminders = self.bot.config["reply_reminders"]

        if from_mod:
            if reminders.pop(key, None) is not None:
                await self.bot.config.update()
            return

        # The opening message is considered during thread setup. Every later recipient message is
        # considered here so different autoreply types can each run once in the same ticket.
        if getattr(thread, "_initial_message_id", None) != getattr(message, "id", None):
            try:
                await thread.consider_ai_autoreply(message)
            except Exception:
                logger.warning(
                    "AI ticket review failed for a recipient follow-up.",
                    exc_info=True,
                )

        # The opening ticket message has its own normal notification flow. The
        # 12-hour reminder begins only when the recipient subsequently replies.
        if getattr(thread, "_initial_message_id", None) == getattr(message, "id", None):
            return

        try:
            delay = int(self.bot.config.get("recipient_reply_reminder_delay") or 43_200)
        except (TypeError, ValueError):
            delay = 43_200
        delay = max(delay, 1)
        reminders[key] = {
            "due_at": (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(),
            "message_id": str(getattr(message, "id", "")),
        }
        await self.bot.config.update()

    @tasks.loop(minutes=1)
    async def unanswered_reply_reminders(self):
        """Ping a ticket's subscribers once when a recipient follow-up waits 12 hours."""
        now = datetime.now(timezone.utc)
        reminders = self.bot.config["reply_reminders"]
        changed = False

        for recipient_id, reminder in tuple(reminders.items()):
            try:
                due_at = datetime.fromisoformat(reminder["due_at"])
                if due_at.tzinfo is None:
                    due_at = due_at.replace(tzinfo=timezone.utc)
            except (KeyError, TypeError, ValueError):
                reminders.pop(recipient_id, None)
                changed = True
                continue

            if due_at > now:
                continue

            try:
                thread = await self.bot.threads.find(recipient_id=int(recipient_id))
            except Exception:
                logger.warning("Failed to resolve thread %s for its reply reminder.", recipient_id)
                continue

            # A newer recipient message or staff response may have updated the
            # reminder while thread lookup yielded control.
            if reminders.get(recipient_id) != reminder:
                continue

            if thread is None or thread.channel is None:
                reminders.pop(recipient_id, None)
                changed = True
                continue

            subscribers = self.bot.config["subscriptions"].get(recipient_id, [])
            mentions = " ".join(dict.fromkeys(subscribers))
            if not mentions:
                reminders.pop(recipient_id, None)
                changed = True
                continue

            reminder_text = self.bot.config.get("recipient_reply_reminder_text")
            try:
                await thread.channel.send(f"{mentions} {reminder_text}")
            except Exception:
                logger.warning("Failed to send reply reminder for thread %s.", recipient_id, exc_info=True)
                continue

            if reminders.get(recipient_id) == reminder:
                reminders.pop(recipient_id, None)
                changed = True

        if changed:
            await self.bot.config.update()

    @unanswered_reply_reminders.before_loop
    async def before_unanswered_reply_reminders(self):
        await self.bot.wait_until_ready()

    async def auto_unsnooze_task(self):
        await self.bot.wait_until_ready()
        last_db_query = 0
        while not self.bot.is_closed():
            now = datetime.now(timezone.utc)
            try:
                # Query DB every 2 minutes
                if (now.timestamp() - last_db_query) > 120:
                    snoozed_threads = await self.bot.api.logs.find(
                        {"snooze_until": {"$gte": now.isoformat()}}
                    ).to_list(None)
                    self._snoozed_cache = snoozed_threads or []
                    last_db_query = now.timestamp()
                # Check cache every 10 seconds
                to_unsnooze = []
                for thread_data in list(self._snoozed_cache):
                    snooze_until = thread_data.get("snooze_until")
                    recipient = thread_data.get("recipient")
                    if not recipient or not recipient.get("id"):
                        continue
                    thread_id = int(recipient.get("id"))
                    if snooze_until:
                        try:
                            dt = parser.isoparse(snooze_until)
                        except Exception:
                            continue
                        if now >= dt:
                            to_unsnooze.append(thread_data)
                for thread_data in to_unsnooze:
                    recipient = thread_data.get("recipient")
                    if not recipient or not recipient.get("id"):
                        continue
                    thread_id = int(recipient.get("id"))
                    thread = self.bot.threads.cache.get(thread_id) or await self.bot.threads.find(
                        id=thread_id
                    )
                    if thread and thread.snoozed:
                        await thread.restore_from_snooze()
                        logging.info(f"[AUTO-UNSNOOZE] Thread {thread_id} auto-unsnoozed.")
                        try:
                            channel = thread.channel
                            if channel:
                                await channel.send("⏰ This thread has been automatically unsnoozed.")
                        except Exception as e:
                            logger.info(
                                "Failed to notify channel after auto-unsnooze: %s",
                                e,
                            )
                        self._snoozed_cache.remove(thread_data)
            except Exception as e:
                logging.error(f"Error in auto_unsnooze_task: {e}")
            await asyncio.sleep(10)

    def _resolve_user(self, user_str):
        """Helper to resolve a user from mention, ID, or username."""
        import re

        if not user_str:
            return None
        if user_str.isdigit():
            return int(user_str)
        match = re.match(r"<@!?(\d+)>", user_str)
        if match:
            return int(match.group(1))
        return None

    def _resolve_user(self, user_str):
        """Helper to resolve a user from mention, ID, or username."""
        import re

        if not user_str:
            return None
        if user_str.isdigit():
            return int(user_str)
        match = re.match(r"<@!?(\d+)>", user_str)
        if match:
            return int(match.group(1))
        return None

    @commands.command()
    @trigger_typing
    @checks.has_permissions(PermissionLevel.OWNER)
    async def setup(self, ctx):
        """
        Sets up a server for Modmail.

        You only need to run this command
        once after configuring Modmail.
        """

        if ctx.guild != self.bot.modmail_guild:
            return await ctx.send(f"You can only setup in the Modmail guild: {self.bot.modmail_guild}.")

        if self.bot.main_category is not None:
            logger.debug("Can't re-setup server, main_category is found.")
            return await ctx.send(f"{self.bot.modmail_guild} is already set up.")

        if self.bot.modmail_guild is None:
            embed = discord.Embed(
                title="Error",
                description="Modmail functioning guild not found.",
                color=self.bot.error_color,
            )
            return await ctx.send(embed=embed)

        overwrites = {
            self.bot.modmail_guild.default_role: discord.PermissionOverwrite(read_messages=False),
            self.bot.modmail_guild.me: discord.PermissionOverwrite(read_messages=True),
        }

        for level in PermissionLevel:
            if level <= PermissionLevel.REGULAR:
                continue
            permissions = self.bot.config["level_permissions"].get(level.name, [])
            for perm in permissions:
                perm = int(perm)
                if perm == -1:
                    key = self.bot.modmail_guild.default_role
                else:
                    key = self.bot.modmail_guild.get_member(perm)
                    if key is None:
                        key = self.bot.modmail_guild.get_role(perm)
                if key is not None:
                    logger.info("Granting %s access to Modmail category.", key.name)
                    overwrites[key] = discord.PermissionOverwrite(read_messages=True)

        category = await self.bot.modmail_guild.create_category(name="Modmail", overwrites=overwrites)

        await category.edit(position=0)

        log_channel = await self.bot.modmail_guild.create_text_channel(name="bot-logs", category=category)

        embed = discord.Embed(
            title="Friendly Reminder",
            description=f"You may use the `{self.bot.prefix}config set log_channel_id "
            "<channel-id>` command to set up a custom log channel, then you can delete this default "
            f"{log_channel.mention} log channel.",
            color=self.bot.main_color,
        )

        embed.add_field(
            name="Thanks for using our bot!",
            value="If you like what you see, consider giving the "
            "[repo a star](https://github.com/modmail-dev/modmail) :star: and if you are "
            "feeling extra generous, buy us coffee on [Buy Me A Coffee](https://buymeacoffee.com/modmaildev) :heart:!",
        )

        embed.set_footer(text=f'Type "{self.bot.prefix}help" for a complete list of commands.')
        await log_channel.send(embed=embed)

        self.bot.config["main_category_id"] = category.id
        self.bot.config["log_channel_id"] = log_channel.id

        await self.bot.config.update()
        await ctx.send(
            "**Successfully set up server.**\n"
            "Consider setting permission levels to give access to roles "
            "or users the ability to use Modmail.\n\n"
            f"Type:\n- `{self.bot.prefix}permissions` and `{self.bot.prefix}permissions add` "
            "for more info on setting permissions.\n"
            f"- `{self.bot.prefix}config help` for a list of available customizations."
        )

        if not self.bot.config["command_permissions"] and not self.bot.config["level_permissions"]:
            await self.bot.update_perms(PermissionLevel.REGULAR, -1)
            for owner_id in self.bot.bot_owner_ids:
                await self.bot.update_perms(PermissionLevel.OWNER, owner_id)

    @commands.group(aliases=["snippets"], invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def snippet(self, ctx, *, name: str.lower = None):
        """
        Create pre-defined messages for use in threads.

        When `{prefix}snippet` is used by itself, this will retrieve
        a list of snippets that are currently set. `{prefix}snippet-name` will show what the
        snippet point to.

        To create a snippet:
        - `{prefix}snippet add snippet-name A pre-defined text.`

        You can use your snippet in a thread channel
        with `{prefix}snippet-name`, the message "A pre-defined text."
        will be sent to the recipient.

        Currently, there is not a built-in anonymous snippet command; however, a workaround
        is available using `{prefix}alias`. Here is how:
        - `{prefix}alias add snippet-name anonreply A pre-defined anonymous text.`

        See also `{prefix}alias`.
        """

        if name is not None:
            if name == "compact":
                embeds = []

                for i, names in enumerate(zip_longest(*(iter(sorted(self.bot.snippets)),) * 15)):
                    description = format_description(i, names)
                    embed = discord.Embed(color=self.bot.main_color, description=description)
                    embed.set_author(
                        name="Snippets", icon_url=self.bot.get_guild_icon(guild=ctx.guild, size=128)
                    )
                    embeds.append(embed)

                session = EmbedPaginatorSession(ctx, *embeds)
                await session.run()
                return

            snippet_name = self.bot._resolve_snippet(name)

            if snippet_name is None:
                embed = create_not_found_embed(name, self.bot.snippets.keys(), "Snippet")
            else:
                val = self.bot.snippets[snippet_name]
                embed = discord.Embed(
                    title=f'Snippet - "{snippet_name}":',
                    description=val,
                    color=self.bot.main_color,
                )
            return await ctx.send(embed=embed)

        if not self.bot.snippets:
            embed = discord.Embed(
                color=self.bot.error_color,
                description="You dont have any snippets at the moment.",
            )
            embed.set_footer(text=f'Check "{self.bot.prefix}help snippet add" to add a snippet.')
            embed.set_author(
                name="Snippets",
                icon_url=self.bot.get_guild_icon(guild=ctx.guild, size=128),
            )
            return await ctx.send(embed=embed)

        embeds = [discord.Embed(color=self.bot.main_color) for _ in range((len(self.bot.snippets) // 10) + 1)]
        for embed in embeds:
            embed.set_author(name="Snippets", icon_url=self.bot.get_guild_icon(guild=ctx.guild, size=128))

        for i, snippet in enumerate(sorted(self.bot.snippets.items())):
            embeds[i // 10].add_field(
                name=snippet[0], value=return_or_truncate(snippet[1], 350), inline=False
            )

        session = EmbedPaginatorSession(ctx, *embeds)
        await session.run()

    @snippet.command(name="raw")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def snippet_raw(self, ctx, *, name: str.lower):
        """
        View the raw content of a snippet.
        """
        snippet_name = self.bot._resolve_snippet(name)
        if snippet_name is None:
            embed = create_not_found_embed(name, self.bot.snippets.keys(), "Snippet")
        else:
            val = truncate(escape_code_block(self.bot.snippets[snippet_name]), 2048 - 7)
            embed = discord.Embed(
                title=f'Raw snippet - "{snippet_name}":',
                description=f"```\n{val}```",
                color=self.bot.main_color,
            )

        return await ctx.send(embed=embed)

    @snippet.command(name="add", aliases=["create", "make"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def snippet_add(self, ctx, name: str.lower, *, value: commands.clean_content):
        """
        Add a snippet.

        Simply to add a snippet, do: ```
        {prefix}snippet add hey hello there :)
        ```
        then when you type `{prefix}hey`, "hello there :)" will get sent to the recipient.

        To add a multi-word snippet name, use quotes: ```
        {prefix}snippet add "two word" this is a two word snippet.
        ```
        """
        if self.bot.get_command(name):
            embed = discord.Embed(
                title="Error",
                color=self.bot.error_color,
                description=f"A command with the same name already exists: `{name}`.",
            )
            return await ctx.send(embed=embed)
        elif name in self.bot.snippets:
            embed = discord.Embed(
                title="Error",
                color=self.bot.error_color,
                description=f"Snippet `{name}` already exists.",
            )
            return await ctx.send(embed=embed)

        if name in self.bot.aliases:
            embed = discord.Embed(
                title="Error",
                color=self.bot.error_color,
                description=f"An alias that shares the same name exists: `{name}`.",
            )
            return await ctx.send(embed=embed)

        if len(name) > 120:
            embed = discord.Embed(
                title="Error",
                color=self.bot.error_color,
                description="Snippet names cannot be longer than 120 characters.",
            )
            return await ctx.send(embed=embed)

        self.bot.snippets[name] = value
        await self.bot.config.update()

        embed = discord.Embed(
            title="Added snippet",
            color=self.bot.main_color,
            description="Successfully created snippet.",
        )
        return await ctx.send(embed=embed)

    def _fix_aliases(self, snippet_being_deleted: str) -> Tuple[List[str]]:
        """
        Remove references to the snippet being deleted from aliases.

        Direct aliases to snippets are deleted, and aliases having
        other steps are edited.

        A tuple of dictionaries are returned. The first dictionary
        contains a mapping of alias names which were deleted to their
        original value, and the second dictionary contains a mapping
        of alias names which were edited to their original value.
        """
        deleted = {}
        edited = {}

        # Using a copy since we might need to delete aliases
        for alias, val in self.bot.aliases.copy().items():
            values = parse_alias(val)

            save_aliases = []

            for val in values:
                view = StringView(val)
                linked_command = view.get_word().lower()
                message = view.read_rest()

                if linked_command == snippet_being_deleted:
                    continue

                is_valid_snippet = snippet_being_deleted in self.bot.snippets

                if not self.bot.get_command(linked_command) and not is_valid_snippet:
                    alias_command = self.bot.aliases[linked_command]
                    save_aliases.extend(normalize_alias(alias_command, message))
                else:
                    save_aliases.append(val)

            if not save_aliases:
                original_value = self.bot.aliases.pop(alias)
                deleted[alias] = original_value
            else:
                original_alias = self.bot.aliases[alias]
                new_alias = " && ".join(f'"{a}"' for a in save_aliases)

                if original_alias != new_alias:
                    self.bot.aliases[alias] = new_alias
                    edited[alias] = original_alias

        return deleted, edited

    @snippet.command(name="remove", aliases=["del", "delete"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def snippet_remove(self, ctx, *, name: str.lower):
        """Remove a snippet."""
        if name in self.bot.snippets:
            deleted_aliases, edited_aliases = self._fix_aliases(name)

            deleted_aliases_string = ",".join(f"`{alias}`" for alias in deleted_aliases)
            if len(deleted_aliases) == 1:
                deleted_aliases_output = f"The `{deleted_aliases_string}` direct alias has been removed."
            elif deleted_aliases:
                deleted_aliases_output = (
                    f"The following direct aliases have been removed: {deleted_aliases_string}."
                )
            else:
                deleted_aliases_output = None

            if len(edited_aliases) == 1:
                alias, val = edited_aliases.popitem()
                edited_aliases_output = (
                    f"Steps pointing to this snippet have been removed from the `{alias}` alias"
                    f" (previous value: `{val}`).`"
                )
            elif edited_aliases:
                alias_list = "\n".join(
                    [
                        f"- `{alias_name}` (previous value: `{val}`)"
                        for alias_name, val in edited_aliases.items()
                    ]
                )
                edited_aliases_output = (
                    f"Steps pointing to this snippet have been removed from the following aliases:"
                    f"\n\n{alias_list}"
                )
            else:
                edited_aliases_output = None

            description = f"Snippet `{name}` is now deleted."
            if deleted_aliases_output:
                description += f"\n\n{deleted_aliases_output}"
            if edited_aliases_output:
                description += f"\n\n{edited_aliases_output}"

            embed = discord.Embed(
                title="Removed snippet",
                color=self.bot.main_color,
                description=description,
            )
            self.bot.snippets.pop(name)
            await self.bot.config.update()
        else:
            embed = create_not_found_embed(name, self.bot.snippets.keys(), "Snippet")
        await ctx.send(embed=embed)

    @snippet.command(name="edit")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def snippet_edit(self, ctx, name: str.lower, *, value):
        """
        Edit a snippet.

        To edit a multi-word snippet name, use quotes: ```
        {prefix}snippet edit "two word" this is a new two word snippet.
        ```
        """
        if name in self.bot.snippets:
            self.bot.snippets[name] = value
            await self.bot.config.update()

            embed = discord.Embed(
                title="Edited snippet",
                color=self.bot.main_color,
                description=f'`{name}` will now send "{value}".',
            )
        else:
            embed = create_not_found_embed(name, self.bot.snippets.keys(), "Snippet")
        await ctx.send(embed=embed)

    @commands.group(aliases=["autoreplies"], invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @checks.has_any_role_id(*MANUAL_AI_ROLE_IDS)
    async def autoreply(self, ctx, *, name: str.lower = None):
        """
        Manage the set messages Gemini may select for a new ticket.

        Create an alias-backed rule with:
        - `{prefix}autoreply create "NAME: How can I apply"`
          `["MUST MENTION TO CHECK": apply, application, become, staff] apply`
        - Add choices with `["ALTERNATIVES": {{"Application status": status-alias}},`
          `{{"Application requirements": requirements-alias}}]`.

        The primary alias and every alternative must already exist. Once the trigger
        matches, Gemini selects the best reply and executes that alias in full.
        """
        autoreplies = self.bot.config["autoreplies"]
        raw_requested = False
        if name is not None:
            raw_match = re.fullmatch(r"(.+?)\s+raw\s*", name, re.IGNORECASE | re.DOTALL)
            if raw_match is not None:
                name = raw_match.group(1).strip()
                raw_requested = True
        if name is not None:
            key = self._find_autoreply_key(autoreplies, name)
            if key is None:
                embed = create_not_found_embed(name, autoreplies.keys(), "Autoreply")
            elif raw_requested:
                raw_command = (
                    f"{self.bot.prefix}autoreply edit "
                    f"{format_autoreply_rule_spec(key, autoreplies[key])}"
                )
                if len(raw_command) > 3_900:
                    return await ctx.send(
                        "The raw autoreply was too long for a code box, so it is attached below.",
                        file=discord.File(
                            BytesIO(raw_command.encode("utf-8")),
                            filename=f"autoreply-{key}.txt",
                        ),
                    )
                embed = discord.Embed(
                    title=f'Raw autoreply - "{key}"',
                    description=f"```\n{escape_code_block(raw_command)}\n```",
                    color=self.bot.main_color,
                )
            else:
                entry = autoreplies[key]
                embed = discord.Embed(
                    title=f'Autoreply - "{key}"',
                    description=self._format_autoreply_entry(key, entry),
                    color=self.bot.main_color,
                )
            return await ctx.send(embed=embed)

        if not autoreplies:
            return await ctx.send(
                embed=discord.Embed(
                    color=self.bot.error_color,
                    description=(
                        "No AI autoreplies are configured. Create one with "
                        f"`{self.bot.prefix}autoreply create \"NAME: ...\" "
                        "[\"MUST MENTION TO CHECK\": word, phrase] alias-name`."
                    ),
                )
            )

        embeds = [
            discord.Embed(title="AI autoreplies", color=self.bot.main_color)
            for _ in range((len(autoreplies) - 1) // 10 + 1)
        ]
        for index, (reply_name, value) in enumerate(sorted(autoreplies.items())):
            embeds[index // 10].add_field(
                name=reply_name,
                value=return_or_truncate(self._format_autoreply_entry(reply_name, value), 350),
                inline=False,
            )
        return await EmbedPaginatorSession(ctx, *embeds).run()

    @staticmethod
    def _format_autoreply_entry(key, entry):
        if not isinstance(entry, dict):
            return str(entry)
        triggers = ", ".join(f"`{term}`" for term in (entry.get("triggers") or []))
        formatted = (
            f"**Name:** {entry.get('name') or key}\n"
            f"**Must mention:** {triggers or '[none]'}\n"
            f"**Alias:** `{entry.get('alias') or '[missing]'}`"
        )
        alternatives = entry.get("alternatives") or []
        if alternatives:
            formatted += "\n**Alternatives:**\n" + "\n".join(
                f"- {alternative.get('name') or '[unnamed]'}: "
                f"`{alternative.get('alias') or '[missing]'}`"
                for alternative in alternatives
                if isinstance(alternative, dict)
            )
        return formatted

    @staticmethod
    def _autoreply_variants(key, entry):
        """Return the primary and named alternative alias choices for a rule."""
        if not isinstance(entry, dict):
            return [(str(key), None)]
        variants = [(str(entry.get("name") or key).strip(), str(entry.get("alias") or "").strip())]
        variants.extend(
            (
                str(alternative.get("name") or "").strip(),
                str(alternative.get("alias") or "").casefold().strip(),
            )
            for alternative in (entry.get("alternatives") or [])
            if isinstance(alternative, dict)
        )
        return variants

    @classmethod
    def _autoreply_choice_count(cls, key, entry):
        return len(cls._autoreply_variants(key, entry))

    @staticmethod
    def _find_autoreply_key(autoreplies, name):
        normalized = str(name).casefold().strip()
        if normalized in autoreplies:
            return normalized
        for key, entry in autoreplies.items():
            if isinstance(entry, dict) and str(entry.get("name", "")).casefold() == normalized:
                return key
            if isinstance(entry, dict) and any(
                isinstance(alternative, dict)
                and str(alternative.get("name", "")).casefold() == normalized
                for alternative in (entry.get("alternatives") or [])
            ):
                return key
        return None

    @autoreply.command(name="set", aliases=["add", "edit", "create"])
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @checks.has_any_role_id(*MANUAL_AI_ROLE_IDS)
    async def autoreply_set(self, ctx, name: str, *, value: commands.clean_content):
        """Create or update an AI-selectable set message."""
        autoreplies = self.bot.config["autoreplies"]
        if re.match(r"\s*name\s*:", name, re.IGNORECASE):
            try:
                entry = parse_autoreply_rule_spec(name, value)
            except ValueError as exc:
                raise commands.BadArgument(str(exc)) from exc

            alias_name = entry["alias"]
            variants = self._autoreply_variants(alias_name, entry)
            for display_name, variant_alias in variants:
                if display_name.upper() == NO_MATCH or variant_alias.upper() == NO_MATCH:
                    raise commands.BadArgument("That autoreply name is reserved.")
                raw_alias = self.bot.aliases.get(variant_alias)
                if raw_alias is None:
                    raise commands.BadArgument(
                        f"Alias `{variant_alias}` does not exist. Create it before this autoreply rule."
                    )
                reply_steps = parse_reply_alias(raw_alias)
                if reply_steps is None:
                    raise commands.BadArgument(
                        f"Alias `{variant_alias}` must include a reply-style step with message text."
                    )
                if len("\n\n".join(message for _, message in reply_steps)) > 4_000:
                    raise commands.BadArgument(
                        f"Alias `{variant_alias}` has more than 4,000 characters of reply text."
                    )

            new_display_names = {name.casefold() for name, _ in variants}
            for existing_key, existing_entry in autoreplies.items():
                if existing_key == alias_name:
                    continue
                existing_names = {
                    name.casefold()
                    for name, _ in self._autoreply_variants(existing_key, existing_entry)
                }
                if new_display_names & existing_names:
                    raise commands.BadArgument(
                        "Another primary or alternative autoreply already uses that display name."
                    )

            key = alias_name
            alternative_count = len(variants) - 1
            response_description = (
                f'`{entry["name"]}` and {alternative_count} alternative(s) will be compared when '
                "a recipient message contains one of the must-mention terms; Gemini will execute "
                "only the best matching alias."
            )
        else:
            key = name.casefold().strip()
            entry = str(value)
            if key.upper() == NO_MATCH:
                raise commands.BadArgument("That autoreply name is reserved.")
            if not key or len(key) > AUTOREPLY_DISPLAY_NAME_LIMIT:
                raise commands.BadArgument(
                    "Autoreply names cannot be longer than "
                    f"{AUTOREPLY_DISPLAY_NAME_LIMIT} characters."
                )
            if len(entry) > 4_000:
                raise commands.BadArgument(
                    "Autoreply messages cannot be longer than 4,000 characters."
                )
            response_description = f"`{key}` is ready for Gemini to select on new tickets."

        existing_choice_count = sum(
            self._autoreply_choice_count(existing_key, existing_entry)
            for existing_key, existing_entry in autoreplies.items()
            if existing_key != key
        )
        new_choice_count = self._autoreply_choice_count(key, entry)
        if existing_choice_count + new_choice_count > 25:
            raise commands.BadArgument(
                "You can configure up to 25 total primary and alternative AI autoreply choices."
            )

        existed = key in autoreplies
        autoreplies[key] = entry
        await self.bot.config.update()
        action = "Updated" if existed else "Created"
        await ctx.send(
            embed=discord.Embed(
                title=f"{action} AI autoreply",
                description=response_description,
                color=self.bot.main_color,
            )
        )

    @autoreply.command(name="remove", aliases=["delete", "del"])
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    @checks.has_any_role_id(*MANUAL_AI_ROLE_IDS)
    async def autoreply_remove(self, ctx, *, name: str.lower):
        """Delete an AI autoreply."""
        autoreplies = self.bot.config["autoreplies"]
        key = self._find_autoreply_key(autoreplies, name)
        if key is None:
            return await ctx.send(
                embed=create_not_found_embed(name, autoreplies.keys(), "Autoreply")
            )

        autoreplies.pop(key)
        await self.bot.config.update()
        await ctx.send(
            embed=discord.Embed(
                title="Removed AI autoreply",
                description=f"`{key}` will no longer be selected.",
                color=self.bot.main_color,
            )
        )

    def _custom_abuse_terms(self):
        raw_terms = self.bot.config.get("abuse_filter_extra_terms") or []
        if isinstance(raw_terms, str):
            raw_terms = [raw_terms]
        return sorted(
            dict.fromkeys(
                normalized
                for normalized in (
                    normalize_custom_abuse_term(term) for term in raw_terms
                )
                if normalized
            )
        )

    @commands.group(aliases=["abusewords"], invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def abuseword(self, ctx):
        """Manage additional words and phrases that automatically close tickets."""
        prefix = self.bot.prefix
        terms = self._custom_abuse_terms()
        await ctx.send(
            embed=discord.Embed(
                title="Automatic abuse-word filter",
                description=(
                    "The built-in severe-abuse list is always active. Additional entries persist "
                    "until removed. Enter their normal spelling; common evasions are detected "
                    "automatically.\n\n"
                    f"`{prefix}abuseword add WORD OR PHRASE`\n"
                    f"`{prefix}abuseword remove WORD OR PHRASE`\n"
                    f"`{prefix}abuseword list`\n\n"
                    f"**Custom entries:** {len(terms)}/100"
                ),
                color=self.bot.main_color,
            )
        )

    @abuseword.command(name="add")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def abuseword_add(self, ctx, *, term: str):
        """Add a persistent word or phrase to the automatic abuse filter."""
        normalized = normalize_custom_abuse_term(term)
        if len(normalized.replace(" ", "")) < 2:
            raise commands.BadArgument("Enter at least two letters or numbers.")
        if len(normalized) > 100:
            raise commands.BadArgument("Abuse-filter entries cannot exceed 100 characters.")
        if contains_abusive_language(normalized):
            return await ctx.send(
                embed=discord.Embed(
                    title="Already covered",
                    description=f"`{normalized}` is already covered by the built-in filter.",
                    color=self.bot.error_color,
                )
            )

        terms = self._custom_abuse_terms()
        if normalized in terms:
            return await ctx.send(
                embed=discord.Embed(
                    title="Already added",
                    description=f"`{normalized}` is already in the custom abuse-word list.",
                    color=self.bot.error_color,
                )
            )
        if len(terms) >= 100:
            raise commands.BadArgument("The custom abuse-word list is limited to 100 entries.")

        terms.append(normalized)
        self.bot.config["abuse_filter_extra_terms"] = sorted(terms)
        await self.bot.config.update()
        await ctx.send(
            embed=discord.Embed(
                title="Added abuse-filter entry",
                description=(
                    f"`{normalized}` will now automatically trigger the warning and close "
                    "the ticket."
                ),
                color=self.bot.main_color,
            )
        )

    @abuseword.command(name="remove", aliases=["delete", "del"])
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def abuseword_remove(self, ctx, *, term: str):
        """Remove a word or phrase from the custom automatic abuse filter."""
        normalized = normalize_custom_abuse_term(term)
        terms = self._custom_abuse_terms()
        if normalized not in terms:
            return await ctx.send(
                embed=create_not_found_embed(
                    normalized or term,
                    terms,
                    "Custom abuse-filter entry",
                )
            )

        terms.remove(normalized)
        self.bot.config["abuse_filter_extra_terms"] = terms
        await self.bot.config.update()
        await ctx.send(
            embed=discord.Embed(
                title="Removed abuse-filter entry",
                description=f"`{normalized}` is no longer in the custom abuse-word list.",
                color=self.bot.main_color,
            )
        )

    @abuseword.command(name="list")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def abuseword_list(self, ctx):
        """List all administrator-added abuse words and phrases."""
        terms = self._custom_abuse_terms()
        if not terms:
            return await ctx.send(
                embed=discord.Embed(
                    title="Custom abuse-filter entries",
                    description=(
                        "No custom entries are configured. The built-in list is still active."
                    ),
                    color=self.bot.main_color,
                )
            )

        embeds = []
        for offset in range(0, len(terms), 25):
            chunk = terms[offset : offset + 25]
            description = "\n".join(
                f"{index}. `{term}`"
                for index, term in enumerate(chunk, start=offset + 1)
            )
            embeds.append(
                discord.Embed(
                    title=f"Custom abuse-filter entries ({len(terms)}/100)",
                    description=description,
                    color=self.bot.main_color,
                )
            )
        return await EmbedPaginatorSession(ctx, *embeds).run()

    @commands.command(usage="<category> [options]")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    @checks.thread_only()
    async def move(self, ctx, *, arguments):
        """
        Move a thread to another category.

        `category` may be a category ID, mention, or name.
        `options` is a string which takes in arguments on how to perform the move. Ex: "silently"
        """
        split_args = arguments.strip('"').split(" ")
        category = None

        # manually parse arguments, consumes as much of args as possible for category
        for i in range(len(split_args)):
            try:
                if i == 0:
                    fmt = arguments
                else:
                    fmt = " ".join(split_args[:-i])

                category = await SimilarCategoryConverter().convert(ctx, fmt)
            except commands.BadArgument:
                if i == len(split_args) - 1:
                    # last one
                    raise
                pass
            else:
                break

        if not category:
            raise commands.ChannelNotFound(arguments)

        options = " ".join(arguments.split(" ")[-i:])

        thread = ctx.thread
        silent = False

        if options:
            silent_words = ["silent", "silently"]
            silent = any(word in silent_words for word in options.split())

        await thread.channel.move(
            category=category,
            end=True,
            sync_permissions=True,
            reason=f"{ctx.author} moved this thread.",
        )

        if self.bot.config["thread_move_notify"] and not silent:
            embed = discord.Embed(
                title=self.bot.config["thread_move_title"],
                description=self.bot.config["thread_move_response"],
                color=self.bot.main_color,
            )
            await thread.recipient.send(embed=embed)

        if self.bot.config["thread_move_notify_mods"]:
            mention = self.bot.config["mention"]
            if mention is not None:
                msg = f"{mention}, thread has been moved."
            else:
                msg = "Thread has been moved."
            await thread.channel.send(msg)

        sent_emoji, _ = await self.bot.retrieve_emoji()
        await self.bot.add_reaction(ctx.message, sent_emoji)

    async def send_scheduled_close_message(self, ctx, after, silent=False):
        """Send a scheduled close notice only to the staff thread channel.

        Uses Discord relative timestamp formatting for better UX.
        """
        ts = int((after.dt if after.dt.tzinfo else after.dt.replace(tzinfo=timezone.utc)).timestamp())
        embed = discord.Embed(
            title="Scheduled close",
            description=f"This thread will{' silently' if silent else ''} close <t:{ts}:R>.",
            color=self.bot.error_color,
        )
        if after.arg and not silent:
            embed.add_field(name="Message", value=after.arg)
        embed.set_footer(text="Closing will be cancelled if a thread message is sent.")
        embed.timestamp = after.dt

        thread = getattr(ctx, "thread", None)
        if thread and ctx.channel == thread.channel:
            await thread.channel.send(embed=embed)

    @commands.command(usage="[after] [close message]")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def close(
        self,
        ctx,
        option: Optional[Literal["silent", "silently", "cancel"]] = "",
        *,
        after: UserFriendlyTime = None,
    ):
        """
        Close the current thread.

        Close after a period of time:
        - `{prefix}close in 5 hours`
        - `{prefix}close 2m30s`

        Custom close messages:
        - `{prefix}close 2 hours The issue has been resolved.`
        - `{prefix}close We will contact you once we find out more.`

        Silently close a thread (no message)
        - `{prefix}close silently`
        - `{prefix}close silently in 10m`

        Stop a thread from closing:
        - `{prefix}close cancel`
        """

        thread = ctx.thread

        close_after = (after.dt - after.now).total_seconds() if after else 0
        silent = any(x == option for x in {"silent", "silently"})
        cancel = option == "cancel"

        if cancel:
            if thread.close_task is not None or thread.auto_close_task is not None:
                await thread.cancel_closure(all=True)
                embed = discord.Embed(
                    color=self.bot.error_color,
                    description="Scheduled close has been cancelled.",
                )
            else:
                embed = discord.Embed(
                    color=self.bot.error_color,
                    description="This thread has not already been scheduled to close.",
                )

            return await ctx.send(embed=embed)

        message = after.arg if after else None
        if self.bot.config["require_close_reason"] and message is None:
            raise commands.BadArgument("Provide a reason for closing the thread.")

        if after and after.dt > after.now:
            await self.send_scheduled_close_message(ctx, after, silent)

        await thread.close(closer=ctx.author, after=close_after, message=message, silent=silent)

    @staticmethod
    def parse_user_or_role(ctx, user_or_role):
        mention = None
        if user_or_role is None:
            mention = ctx.author.mention
        elif hasattr(user_or_role, "mention"):
            mention = user_or_role.mention
        elif user_or_role in {"here", "everyone", "@here", "@everyone"}:
            mention = "@" + user_or_role.lstrip("@")
        return mention

    @commands.command(usage="<MESSAGE>")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def context(self, ctx, *, message: str):
        """Post plain staff-only context in a ticket, including from an alias."""
        message = message.strip()
        if not message:
            raise commands.BadArgument("Provide the staff context message.")
        if len(message) > 2_000:
            raise commands.BadArgument("Staff context messages cannot exceed 2,000 characters.")

        staff_message = await ctx.send(
            message,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            await self.bot.api.append_log(staff_message, type_="internal")
        except Exception:
            logger.warning("Failed to append an alias context message to the ticket log.", exc_info=True)
        return staff_message

    @commands.command(aliases=["alert"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def notify(self, ctx, *, user_or_role: Union[discord.Role, User, str.lower, None] = None):
        """
        Notify a user or role when the next thread message received.

        Once a thread message is received, `user_or_role` will be pinged once.

        Leave `user_or_role` empty to notify yourself.
        `@here` and `@everyone` can be substituted with `here` and `everyone`.
        `user_or_role` may be a user ID, mention, name. role ID, mention, name, "everyone", or "here".
        """
        # In an automatic AI alias, ``"notify @role"`` means notify the role
        # immediately in the staff ticket. Direct staff use of ``?notify``
        # retains its original "notify on the next message" behavior.
        if getattr(ctx, "_ai_autoreply", False):
            if not isinstance(user_or_role, discord.Role):
                raise commands.BadArgument(
                    'The automatic alias step must use "notify @role" with a valid role.'
                )
            return await ctx.send(
                user_or_role.mention,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    users=False,
                    roles=[user_or_role],
                    replied_user=False,
                ),
            )

        mention = self.parse_user_or_role(ctx, user_or_role)
        if mention is None:
            raise commands.BadArgument(f"{user_or_role} is not a valid user or role.")

        thread = ctx.thread

        if str(thread.id) not in self.bot.config["notification_squad"]:
            self.bot.config["notification_squad"][str(thread.id)] = []

        mentions = self.bot.config["notification_squad"][str(thread.id)]

        if mention in mentions:
            embed = discord.Embed(
                color=self.bot.error_color,
                description=f"{mention} is already going to be mentioned.",
            )
        else:
            mentions.append(mention)
            await self.bot.config.update()
            embed = discord.Embed(
                color=self.bot.main_color,
                description=f"{mention} will be mentioned on the next message received.",
            )
        return await ctx.send(embed=embed)

    @commands.command(aliases=["unalert"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def unnotify(self, ctx, *, user_or_role: Union[discord.Role, User, str.lower, None] = None):
        """
        Un-notify a user, role, or yourself from a thread.

        Leave `user_or_role` empty to un-notify yourself.
        `@here` and `@everyone` can be substituted with `here` and `everyone`.
        `user_or_role` may be a user ID, mention, name, role ID, mention, name, "everyone", or "here".
        """
        mention = self.parse_user_or_role(ctx, user_or_role)
        if mention is None:
            mention = f"`{user_or_role}`"

        thread = ctx.thread

        if str(thread.id) not in self.bot.config["notification_squad"]:
            self.bot.config["notification_squad"][str(thread.id)] = []

        mentions = self.bot.config["notification_squad"][str(thread.id)]

        if mention not in mentions:
            embed = discord.Embed(
                color=self.bot.error_color,
                description=f"{mention} does not have a pending notification.",
            )
        else:
            mentions.remove(mention)
            await self.bot.config.update()
            embed = discord.Embed(
                color=self.bot.main_color,
                description=f"{mention} will no longer be notified.",
            )
        return await ctx.send(embed=embed)

    @commands.command(aliases=["sub"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def subscribe(self, ctx, *, user_or_role: Union[discord.Role, User, str.lower, None] = None):
        """
        Notify a user, role, or yourself for every thread message received.

        You will be pinged for every thread message received until you unsubscribe.

        Leave `user_or_role` empty to subscribe yourself.
        `@here` and `@everyone` can be substituted with `here` and `everyone`.
        `user_or_role` may be a user ID, mention, name, role ID, mention, name, "everyone", or "here".
        """
        embed = await self._subscribe_target(ctx, user_or_role)
        return await ctx.send(embed=embed)

    async def _subscribe_target(self, ctx, user_or_role):
        """Subscribe a target and return the normal staff-side result embed."""
        mention = self.parse_user_or_role(ctx, user_or_role)
        if mention is None:
            raise commands.BadArgument(f"{user_or_role} is not a valid user or role.")

        thread = ctx.thread

        if str(thread.id) not in self.bot.config["subscriptions"]:
            self.bot.config["subscriptions"][str(thread.id)] = []

        mentions = self.bot.config["subscriptions"][str(thread.id)]

        if mention in mentions:
            embed = discord.Embed(
                color=self.bot.error_color,
                description=f"{mention} is already subscribed to this thread.",
            )
        else:
            mentions.append(mention)
            await self.bot.config.update()
            embed = discord.Embed(
                color=self.bot.main_color,
                description=f"{mention} will now be notified of all messages received.",
            )
        return embed

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def transfer(self, ctx, *, user: User = None):
        """Transfer a ticket to a user and subscribe them to future replies."""
        if user is None:
            return await ctx.send(
                f"Please use {self.bot.prefix}transfer @USER",
                allowed_mentions=discord.AllowedMentions.none(),
            )

        user_mention = getattr(user, "mention", f"<@{user.id}>")
        transfer_message = (
            "**<:Connected:1384981326246969344> | Ticket Transferred**\n\n"
            f"This ticket has now been transferred to {user_mention} to best handle your inquiry."
        )

        # Match the requested ``freply ... && sub USER`` order: send the
        # recipient-facing transfer notice first, then subscribe the assignee.
        ctx.message.content = transfer_message
        async with safe_typing(ctx):
            await ctx.thread.reply(ctx.message, transfer_message)

        subscription_embed = await self._subscribe_target(ctx, user)
        await ctx.send(embed=subscription_embed)

    @commands.command(aliases=["unsub"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def unsubscribe(self, ctx, *, user_or_role: Union[discord.Role, User, str.lower, None] = None):
        """
        Unsubscribe a user, role, or yourself from a thread.

        Leave `user_or_role` empty to unsubscribe yourself.
        `@here` and `@everyone` can be substituted with `here` and `everyone`.
        `user_or_role` may be a user ID, mention, name, role ID, mention, name, "everyone", or "here".
        """
        mention = self.parse_user_or_role(ctx, user_or_role)
        if mention is None:
            mention = f"`{user_or_role}`"

        thread = ctx.thread

        if str(thread.id) not in self.bot.config["subscriptions"]:
            self.bot.config["subscriptions"][str(thread.id)] = []

        mentions = self.bot.config["subscriptions"][str(thread.id)]

        if mention not in mentions:
            embed = discord.Embed(
                color=self.bot.error_color,
                description=f"{mention} is not subscribed to this thread.",
            )
        else:
            mentions.remove(mention)
            await self.bot.config.update()
            embed = discord.Embed(
                color=self.bot.main_color,
                description=f"{mention} is now unsubscribed from this thread.",
            )
        return await ctx.send(embed=embed)

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def nsfw(self, ctx):
        """Flags a Modmail thread as NSFW (not safe for work)."""
        await ctx.channel.edit(nsfw=True)
        sent_emoji, _ = await self.bot.retrieve_emoji()
        await self.bot.add_reaction(ctx.message, sent_emoji)

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def sfw(self, ctx):
        """Flags a Modmail thread as SFW (safe for work)."""
        await ctx.channel.edit(nsfw=False)
        sent_emoji, _ = await self.bot.retrieve_emoji()
        await self.bot.add_reaction(ctx.message, sent_emoji)

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def msglink(self, ctx, message_id: int):
        """Retrieves the link to a message in the current thread."""
        found = False
        for recipient in ctx.thread.recipients:
            try:
                message = await recipient.fetch_message(message_id)
                found = True
                break
            except discord.NotFound:
                continue
        if not found:
            embed = discord.Embed(
                color=self.bot.error_color,
                description="Message not found or no longer exists.",
            )
        else:
            embed = discord.Embed(color=self.bot.main_color, description=message.jump_url)
        await ctx.send(embed=embed)

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def loglink(self, ctx):
        """Retrieves the link to the current thread's logs."""
        log_link = await self.bot.api.get_log_link(ctx.channel.id)
        await ctx.send(embed=discord.Embed(color=self.bot.main_color, description=log_link))

    def format_log_embeds(self, logs, avatar_url):
        embeds = []
        logs = tuple(logs)
        title = f"Total Results Found ({len(logs)})"

        for entry in logs:
            created_at = parser.parse(entry["created_at"]).astimezone(timezone.utc)

            prefix = self.bot.config["log_url_prefix"].strip("/")
            if prefix == "NONE":
                prefix = ""
            log_url = (
                f"{self.bot.config['log_url'].strip('/')}{'/' + prefix if prefix else ''}/{entry['key']}"
            )

            username = entry["recipient"]["name"]
            if entry["recipient"]["discriminator"] != "0":
                username += "#" + entry["recipient"]["discriminator"]

            embed = discord.Embed(color=self.bot.main_color, timestamp=created_at)
            embed.set_author(name=f"{title} - {username}", icon_url=avatar_url, url=log_url)
            embed.url = log_url
            embed.add_field(name="Created", value=human_timedelta(created_at))
            closer = entry.get("closer")
            if closer is None:
                closer_msg = "Unknown"
            else:
                closer_msg = f"<@{closer['id']}>"
            embed.add_field(name="Closed By", value=closer_msg)

            if entry["recipient"]["id"] != entry["creator"]["id"]:
                embed.add_field(name="Created by", value=f"<@{entry['creator']['id']}>")

            if entry.get("title"):
                embed.add_field(name="Title", value=entry["title"], inline=False)

            embed.add_field(name="Preview", value=format_preview(entry["messages"]), inline=False)

            if closer is not None:
                # BUG: Currently, logviewer can't display logs without a closer.
                embed.add_field(name="Link", value=log_url)
            else:
                logger.debug("Invalid log entry: no closer.")
                embed.add_field(name="Log Key", value=f"`{entry['key']}`")

            embed.set_footer(text="Recipient ID: " + str(entry["recipient"]["id"]))
            embeds.append(embed)
        return embeds

    @commands.command(cooldown_after_parsing=True)
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.cooldown(1, 600, BucketType.channel)
    async def title(self, ctx, *, name: str):
        """Sets title for a thread"""
        await ctx.thread.set_title(name)
        sent_emoji, _ = await self.bot.retrieve_emoji()
        await ctx.message.pin()
        await self.bot.add_reaction(ctx.message, sent_emoji)

    @commands.command(usage="<users_or_roles...> [options]", cooldown_after_parsing=True)
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.cooldown(1, 600, BucketType.channel)
    async def adduser(self, ctx, *users_arg: Union[discord.Member, discord.Role, str]):
        """Adds a user to a modmail thread

        `options` can be `silent` or `silently`.
        """
        silent = False
        users = []
        for u in users_arg:
            if isinstance(u, str):
                if "silent" in u or "silently" in u:
                    silent = True
            elif isinstance(u, discord.Role):
                users += u.members
            elif isinstance(u, discord.Member):
                users.append(u)

        for u in users:
            # u is a discord.Member
            curr_thread = await self.bot.threads.find(recipient=u)
            if curr_thread == ctx.thread:
                users.remove(u)
                continue

            if curr_thread:
                em = discord.Embed(
                    title="Error",
                    description=f"{u.mention} is already in a thread: {curr_thread.channel.mention}.",
                    color=self.bot.error_color,
                )
                await ctx.send(embed=em)
                ctx.command.reset_cooldown(ctx)
                return

        if not users:
            em = discord.Embed(
                title="Error",
                description="All users are already in the thread.",
                color=self.bot.error_color,
            )
            await ctx.send(embed=em)
            ctx.command.reset_cooldown(ctx)
            return

        if len(users + ctx.thread.recipients) > 5:
            em = discord.Embed(
                title="Error",
                description="Only 5 users are allowed in a group conversation",
                color=self.bot.error_color,
            )
            await ctx.send(embed=em)
            ctx.command.reset_cooldown(ctx)
            return

        to_exec = []
        if not silent:
            description = self.bot.formatter.format(
                self.bot.config["private_added_to_group_response"], moderator=ctx.author
            )
            em = discord.Embed(
                title=self.bot.config["private_added_to_group_title"],
                description=description,
                color=self.bot.main_color,
            )
            if self.bot.config["show_timestamp"]:
                em.timestamp = discord.utils.utcnow()
            em.set_footer(
                text=str(ctx.author),
                icon_url=ctx.author.display_avatar.url if ctx.author.display_avatar else None,
            )
            for u in users:
                to_exec.append(u.send(embed=em))

            description = self.bot.formatter.format(
                self.bot.config["public_added_to_group_response"],
                moderator=ctx.author,
                users=", ".join(u.name for u in users),
            )
            em = discord.Embed(
                title=self.bot.config["public_added_to_group_title"],
                description=description,
                color=self.bot.main_color,
            )
            if self.bot.config["show_timestamp"]:
                em.timestamp = discord.utils.utcnow()
            em.set_footer(
                text=f"{users[0]}", icon_url=users[0].display_avatar.url if users[0].display_avatar else None
            )

            for i in ctx.thread.recipients:
                if i not in users:
                    to_exec.append(i.send(embed=em))

        await ctx.thread.add_users(users)
        if to_exec:
            await asyncio.gather(*to_exec)

        sent_emoji, _ = await self.bot.retrieve_emoji()
        await self.bot.add_reaction(ctx.message, sent_emoji)

    @commands.command(usage="<users_or_roles...> [options]", cooldown_after_parsing=True)
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.cooldown(1, 600, BucketType.channel)
    async def removeuser(self, ctx, *users_arg: Union[discord.Member, discord.Role, str]):
        """Removes a user from a modmail thread

        `options` can be `silent` or `silently`.
        """
        silent = False
        users = []
        for u in users_arg:
            if isinstance(u, str):
                if "silent" in u or "silently" in u:
                    silent = True
            elif isinstance(u, discord.Role):
                users += u.members
            elif isinstance(u, discord.Member):
                users.append(u)

        for u in users:
            # u is a discord.Member
            curr_thread = await self.bot.threads.find(recipient=u)
            if ctx.thread != curr_thread:
                em = discord.Embed(
                    title="Error",
                    description=f"{u.mention} is not in this thread.",
                    color=self.bot.error_color,
                )
                await ctx.send(embed=em)
                ctx.command.reset_cooldown(ctx)
                return
            elif ctx.thread.recipient == u:
                em = discord.Embed(
                    title="Error",
                    description=f"{u.mention} is the main recipient of the thread and cannot be removed.",
                    color=self.bot.error_color,
                )
                await ctx.send(embed=em)
                ctx.command.reset_cooldown(ctx)
                return

        if not users:
            em = discord.Embed(
                title="Error",
                description="No valid users to remove.",
                color=self.bot.error_color,
            )
            await ctx.send(embed=em)
            ctx.command.reset_cooldown(ctx)
            return

        to_exec = []
        if not silent:
            description = self.bot.formatter.format(
                self.bot.config["private_removed_from_group_response"],
                moderator=ctx.author,
            )
            em = discord.Embed(
                title=self.bot.config["private_removed_from_group_title"],
                description=description,
                color=self.bot.main_color,
            )
            if self.bot.config["show_timestamp"]:
                em.timestamp = discord.utils.utcnow()
            em.set_footer(
                text=str(ctx.author),
                icon_url=ctx.author.display_avatar.url if ctx.author.display_avatar else None,
            )
            for u in users:
                to_exec.append(u.send(embed=em))

            description = self.bot.formatter.format(
                self.bot.config["public_removed_from_group_response"],
                moderator=ctx.author,
                users=", ".join(u.name for u in users),
            )
            em = discord.Embed(
                title=self.bot.config["public_removed_from_group_title"],
                description=description,
                color=self.bot.main_color,
            )
            if self.bot.config["show_timestamp"]:
                em.timestamp = discord.utils.utcnow()
            em.set_footer(
                text=f"{users[0]}", icon_url=users[0].display_avatar.url if users[0].display_avatar else None
            )

            for i in ctx.thread.recipients:
                if i not in users:
                    to_exec.append(i.send(embed=em))

        await ctx.thread.remove_users(users)
        if to_exec:
            await asyncio.gather(*to_exec)

        sent_emoji, _ = await self.bot.retrieve_emoji()
        await self.bot.add_reaction(ctx.message, sent_emoji)

    @commands.command(usage="<users_or_roles...> [options]", cooldown_after_parsing=True)
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.cooldown(1, 600, BucketType.channel)
    async def anonadduser(self, ctx, *users_arg: Union[discord.Member, discord.Role, str]):
        """Adds a user to a modmail thread anonymously

        `options` can be `silent` or `silently`.
        """
        silent = False
        users = []
        for u in users_arg:
            if isinstance(u, str):
                if "silent" in u or "silently" in u:
                    silent = True
            elif isinstance(u, discord.Role):
                users += u.members
            elif isinstance(u, discord.Member):
                users.append(u)

        for u in users:
            curr_thread = await self.bot.threads.find(recipient=u)
            if curr_thread == ctx.thread:
                users.remove(u)
                continue

            if curr_thread:
                em = discord.Embed(
                    title="Error",
                    description=f"{u.mention} is already in a thread: {curr_thread.channel.mention}.",
                    color=self.bot.error_color,
                )
                await ctx.send(embed=em)
                ctx.command.reset_cooldown(ctx)
                return

        if not users:
            em = discord.Embed(
                title="Error",
                description="All users are already in the thread.",
                color=self.bot.error_color,
            )
            await ctx.send(embed=em)
            ctx.command.reset_cooldown(ctx)
            return

        to_exec = []
        if not silent:
            em = discord.Embed(
                title=self.bot.config["private_added_to_group_title"],
                description=self.bot.config["private_added_to_group_description_anon"],
                color=self.bot.main_color,
            )
            if self.bot.config["show_timestamp"]:
                em.timestamp = discord.utils.utcnow()

            tag = self.bot.config["mod_tag"]
            if tag is None:
                tag = str(get_top_role(ctx.author, self.bot.config["use_hoisted_top_role"]))
            name = self.bot.config["anon_username"]
            if name is None:
                name = "Anonymous"
            avatar_url = self.bot.config["anon_avatar_url"]
            if avatar_url is None:
                avatar_url = self.bot.get_guild_icon(guild=ctx.guild, size=128)
            em.set_footer(text=name, icon_url=avatar_url if avatar_url else None)

            for u in users:
                to_exec.append(u.send(embed=em))

            description = self.bot.formatter.format(
                self.bot.config["public_added_to_group_description_anon"],
                users=", ".join(u.name for u in users),
            )
            em = discord.Embed(
                title=self.bot.config["public_added_to_group_title"],
                description=description,
                color=self.bot.main_color,
            )
            if self.bot.config["show_timestamp"]:
                em.timestamp = discord.utils.utcnow()
            em.set_footer(
                text=f"{users[0]}", icon_url=users[0].display_avatar.url if users[0].display_avatar else None
            )

            for i in ctx.thread.recipients:
                if i not in users:
                    to_exec.append(i.send(embed=em))

        await ctx.thread.add_users(users)
        if to_exec:
            await asyncio.gather(*to_exec)

        sent_emoji, _ = await self.bot.retrieve_emoji()
        await self.bot.add_reaction(ctx.message, sent_emoji)

    @commands.command(usage="<users_or_roles...> [options]", cooldown_after_parsing=True)
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    @commands.cooldown(1, 600, BucketType.channel)
    async def anonremoveuser(self, ctx, *users_arg: Union[discord.Member, discord.Role, str]):
        """Removes a user from a modmail thread anonymously

        `options` can be `silent` or `silently`.
        """
        silent = False
        users = []
        for u in users_arg:
            if isinstance(u, str):
                if "silent" in u or "silently" in u:
                    silent = True
            elif isinstance(u, discord.Role):
                users += u.members
            elif isinstance(u, discord.Member):
                users.append(u)

        for u in users:
            curr_thread = await self.bot.threads.find(recipient=u)
            if ctx.thread != curr_thread:
                em = discord.Embed(
                    title="Error",
                    description=f"{u.mention} is not in this thread.",
                    color=self.bot.error_color,
                )
                await ctx.send(embed=em)
                ctx.command.reset_cooldown(ctx)
                return
            elif ctx.thread.recipient == u:
                em = discord.Embed(
                    title="Error",
                    description=f"{u.mention} is the main recipient of the thread and cannot be removed.",
                    color=self.bot.error_color,
                )
                await ctx.send(embed=em)
                ctx.command.reset_cooldown(ctx)
                return

        to_exec = []
        if not silent:
            em = discord.Embed(
                title=self.bot.config["private_removed_from_group_title"],
                description=self.bot.config["private_removed_from_group_description_anon"],
                color=self.bot.main_color,
            )
            if self.bot.config["show_timestamp"]:
                em.timestamp = discord.utils.utcnow()

            tag = self.bot.config["mod_tag"]
            if tag is None:
                tag = str(get_top_role(ctx.author, self.bot.config["use_hoisted_top_role"]))
            name = self.bot.config["anon_username"]
            if name is None:
                name = "Anonymous"
            avatar_url = self.bot.config["anon_avatar_url"]
            if avatar_url is None:
                avatar_url = self.bot.get_guild_icon(guild=ctx.guild, size=128)
            em.set_footer(text=name, icon_url=avatar_url if avatar_url else None)

            for u in users:
                to_exec.append(u.send(embed=em))

            description = self.bot.formatter.format(
                self.bot.config["public_removed_from_group_description_anon"],
                users=", ".join(u.name for u in users),
            )
            em = discord.Embed(
                title=self.bot.config["public_removed_from_group_title"],
                description=description,
                color=self.bot.main_color,
            )
            if self.bot.config["show_timestamp"]:
                em.timestamp = discord.utils.utcnow()
            em.set_footer(
                text=f"{users[0]}", icon_url=users[0].display_avatar.url if users[0].display_avatar else None
            )

            for i in ctx.thread.recipients:
                if i not in users:
                    to_exec.append(i.send(embed=em))

        await ctx.thread.remove_users(users)
        if to_exec:
            await asyncio.gather(*to_exec)

        sent_emoji, _ = await self.bot.retrieve_emoji()
        await self.bot.add_reaction(ctx.message, sent_emoji)

    @commands.group(invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def logs(self, ctx, *, user: User = None):
        """
        Get previous Modmail thread logs of a member.

        Leave `user` blank when this command is used within a
        thread channel to show logs for the current recipient.
        `user` may be a user ID, mention, or name.
        """

        async with safe_typing(ctx):
            pass

        if not user:
            thread = ctx.thread
            if not thread:
                raise commands.MissingRequiredArgument(DummyParam("user"))
            user = thread.recipient or await self.bot.get_or_fetch_user(thread.id)

        default_avatar = "https://cdn.discordapp.com/embed/avatars/0.png"
        icon_url = getattr(user, "avatar_url", default_avatar)

        logs = await self.bot.api.get_user_logs(user.id)

        if not any(not log["open"] for log in logs):
            embed = discord.Embed(
                color=self.bot.error_color,
                description="This user does not have any previous logs.",
            )
            return await ctx.send(embed=embed)

        logs = reversed([log for log in logs if not log["open"]])

        embeds = self.format_log_embeds(logs, avatar_url=icon_url)

        session = EmbedPaginatorSession(ctx, *embeds)
        await session.run()

    @logs.command(name="closed-by", aliases=["closeby"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def logs_closed_by(self, ctx, *, user: User = None):
        """
        Get all logs closed by the specified user.

        If no `user` is provided, the user will be the person who sent this command.
        `user` may be a user ID, mention, or name.
        """
        user = user if user is not None else ctx.author

        entries = await self.bot.api.search_closed_by(user.id)
        embeds = self.format_log_embeds(entries, avatar_url=self.bot.get_guild_icon(guild=ctx.guild))

        if not embeds:
            embed = discord.Embed(
                color=self.bot.error_color,
                description="No log entries have been found for that query.",
            )
            return await ctx.send(embed=embed)

        session = EmbedPaginatorSession(ctx, *embeds)
        await session.run()

    @logs.command(name="key", aliases=["id"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def logs_key(self, ctx, key: str):
        """
        Get the log link for the specified log key.
        """
        icon_url = ctx.author.avatar.url

        logs = await self.bot.api.find_log_entry(key)

        if not logs:
            embed = discord.Embed(
                color=self.bot.error_color,
                description=f"Log entry `{key}` not found.",
            )
            return await ctx.send(embed=embed)

        embeds = self.format_log_embeds(logs, avatar_url=icon_url)

        session = EmbedPaginatorSession(ctx, *embeds)
        await session.run()

    @logs.command(name="delete", aliases=["wipe"])
    @checks.has_permissions(PermissionLevel.OWNER)
    async def logs_delete(self, ctx, key_or_link: str):
        """
        Wipe a log entry from the database.
        """
        key = key_or_link.split("/")[-1]

        success = await self.bot.api.delete_log_entry(key)

        if not success:
            embed = discord.Embed(
                title="Error",
                description=f"Log entry `{key}` not found.",
                color=self.bot.error_color,
            )
        else:
            embed = discord.Embed(
                title="Success",
                description=f"Log entry `{key}` successfully deleted.",
                color=self.bot.main_color,
            )

        await ctx.send(embed=embed)

    @logs.command(name="responded")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def logs_responded(self, ctx, *, user: User = None):
        """
        Get all logs where the specified user has responded at least once.

        If no `user` is provided, the user will be the person who sent this command.
        `user` may be a user ID, mention, or name.
        """
        user = user if user is not None else ctx.author

        entries = await self.bot.api.get_responded_logs(user.id)

        embeds = self.format_log_embeds(entries, avatar_url=self.bot.get_guild_icon(guild=ctx.guild))

        if not embeds:
            embed = discord.Embed(
                color=self.bot.error_color,
                description=f"{getattr(user, 'mention', user.id)} has not responded to any threads.",
            )
            return await ctx.send(embed=embed)

        session = EmbedPaginatorSession(ctx, *embeds)
        await session.run()

    @logs.command(name="search", aliases=["find"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def logs_search(self, ctx, limit: Optional[int] = None, *, query):
        """
        Retrieve all logs that contain messages with your query.

        Provide a `limit` to specify the maximum number of logs the bot should find.
        """

        async with safe_typing(ctx):
            pass

        entries = await self.bot.api.search_by_text(query, limit)

        embeds = self.format_log_embeds(entries, avatar_url=self.bot.get_guild_icon(guild=ctx.guild))

        if not embeds:
            embed = discord.Embed(
                color=self.bot.error_color,
                description="No log entries have been found for that query.",
            )
            return await ctx.send(embed=embed)

        session = EmbedPaginatorSession(ctx, *embeds)
        await session.run()

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def reply(self, ctx, *, msg: str = ""):
        """
        Reply to a Modmail thread.

        Supports attachments and images as well as
        automatically embedding image URLs.
        """

        # Ensure logs record only the reply text, not the command.
        ctx.message.content = msg
        async with safe_typing(ctx):
            await ctx.thread.reply(ctx.message, msg)

    @commands.command(usage="<MESSAGE>")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.has_any_role_id(*MANUAL_AI_ROLE_IDS)
    @checks.thread_only()
    async def fakeautoreply(self, ctx, *, message: str):
        """Send staff-provided text using the AI autoreply presentation."""
        if len(message) > 4_000:
            raise commands.BadArgument("Fake AI autoreplies cannot exceed 4,000 characters.")

        delivery_error = None
        async with safe_typing(ctx):
            try:
                await ctx.thread._send_ai_autoreply("Manual fake autoreply", message)
            except Exception as exc:
                delivery_error = exc

        await ctx.thread._log_ai_check(
            ctx.message,
            message,
            outcome="delivery_error" if delivery_error is not None else "manual_fake",
            detail=(
                f"Manual fake autoreply delivery failed ({type(delivery_error).__name__})."
                if delivery_error is not None
                else "A staff member sent a manual fake AI autoreply."
            ),
            selected_name="fakeautoreply",
            response_text=message,
            delivery_status=(
                "Manual fake AI autoreply delivery failed."
                if delivery_error is not None
                else "Manual fake AI autoreply delivered."
            ),
        )
        if delivery_error is not None:
            raise delivery_error

        # Treat the manually sent message as a staff response for reminder bookkeeping.
        self.bot.dispatch("thread_reply", ctx.thread, True, ctx.message, False, False)

    async def _send_generated_ai_reply(
        self,
        ctx,
        generator_cls,
        *,
        command_name: str,
        log_name: str,
        tone_label: str,
        staff_only: bool = False,
        include_closing: bool = True,
        closing_text: str = AI_REPLY_CLOSING,
    ):
        """Generate, deliver, and audit a manual AI reply from the full thread history."""
        api_key = self.bot.config.get("gemini_api_key", convert=False)
        if not api_key or self.bot.session is None:
            raise commands.CommandError("Gemini API credentials are not configured.")

        transcript_blocks = []
        message_count = 0
        async for message in ctx.channel.history(limit=None, oldest_first=True):
            if message.id == ctx.message.id:
                continue

            parts = []
            content = (getattr(message, "clean_content", None) or message.content or "").strip()
            if content:
                parts.append(content)
            for embed in message.embeds:
                embed_parts = []
                if embed.title:
                    embed_parts.append(f"Title: {embed.title}")
                if embed.description:
                    embed_parts.append(str(embed.description))
                for field in embed.fields:
                    embed_parts.append(f"{field.name}: {field.value}")
                if embed.footer and embed.footer.text:
                    embed_parts.append(f"Footer: {embed.footer.text}")
                if embed_parts:
                    embed_author = getattr(embed.author, "name", None) or "embedded message"
                    parts.append(f"{embed_author}: " + "\n".join(embed_parts))
            if message.attachments:
                parts.append(
                    "Attachments: "
                    + ", ".join(attachment.filename for attachment in message.attachments)
                )
            if not parts:
                continue

            timestamp = message.created_at.isoformat()
            author = getattr(message.author, "display_name", None) or str(message.author)
            transcript_blocks.append(f"[{timestamp}] {author}\n" + "\n".join(parts))
            message_count += 1

        transcript = "\n\n---\n\n".join(transcript_blocks)
        generator = generator_cls(
            self.bot.session,
            str(api_key),
            model=str(self.bot.config.get("gemini_model") or "gemini-3.1-flash-lite"),
            timeout_seconds=30,
        )

        response = None
        delivery_error = None
        async with safe_typing(ctx):
            response = await generator.generate(transcript)
            if response is not None:
                unsupported_commands = find_command_references(response)
                if unsupported_commands:
                    command_list = ", ".join(
                        f"{self.bot.prefix}{command}"
                        for command in sorted(unsupported_commands)
                    )
                    response = await generator.generate(
                        transcript,
                        correction=(
                            f"The previous draft invented or used unapproved command(s): "
                            f"{command_list}. Do not mention any Discord command. Answer using "
                            "verified information only; if the request is unclear, ask one concise "
                            "clarification question."
                        ),
                    )
                    if response is None or find_command_references(response):
                        response = (
                            "I do not have enough verified information to answer that request. "
                            "Could you clarify exactly what you need help with?"
                        )
                        generator.last_outcome = "safety_fallback"
                        generator.last_detail = (
                            "Blocked repeated unsupported Discord command references."
                        )
            if response is not None:
                # Leave extra room in raw mode for its disclosure and code-block wrapper.
                maximum_response_length = 3_850 if staff_only else 4_000
                response = finalize_generated_ai_reply(
                    response,
                    include_closing=include_closing,
                    closing_text=closing_text,
                    maximum_length=maximum_response_length,
                )
                try:
                    if staff_only:
                        raw_text = f"{response}\n\n{AI_REPLY_FOOTER}"
                        await ctx.send(
                            embed=discord.Embed(
                                description=f"```\n{escape_code_block(raw_text)}\n```",
                                color=self.bot.main_color,
                            )
                        )
                    else:
                        await ctx.thread._send_ai_autoreply(log_name, response)
                except Exception as exc:
                    delivery_error = exc

        if response is None:
            await ctx.thread._log_ai_check(
                ctx.message,
                transcript,
                outcome=generator.last_outcome,
                detail=generator.last_detail or "Gemini did not generate a reply.",
                selected_name=command_name,
                delivery_status="No AI reply was sent.",
            )
            return await ctx.send(
                embed=discord.Embed(
                    color=self.bot.error_color,
                    description=generator.last_detail or "Gemini could not generate a reply.",
                )
            )

        if delivery_error is not None:
            await ctx.thread._log_ai_check(
                ctx.message,
                transcript,
                outcome="delivery_error",
                detail=(
                    f"Gemini generated a reply, but Discord delivery failed "
                    f"({type(delivery_error).__name__})."
                ),
                selected_name=command_name,
                response_text=response,
                delivery_status=(
                    f"Staff-only {tone_label} AI draft delivery failed."
                    if staff_only
                    else f"Manual {tone_label} AI reply delivery failed."
                ),
            )
            raise delivery_error

        delivery_status = (
            f"Staff-only {tone_label} AI draft generated; nothing was sent to the recipient."
            if staff_only
            else f"Manual {tone_label} AI reply delivered."
        )
        await ctx.thread._log_ai_check(
            ctx.message,
            transcript,
            outcome=generator.last_outcome,
            detail=f"Generated from {message_count} thread messages.",
            selected_name=command_name,
            response_text=response,
            delivery_status=delivery_status,
        )
        if not staff_only:
            self.bot.dispatch("thread_reply", ctx.thread, True, ctx.message, False, False)

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.has_any_role_id(*MANUAL_AI_ROLE_IDS)
    async def aicommands(self, ctx):
        """Show the available manual AI commands and autoreply management syntax."""
        prefix = self.bot.prefix
        embed = discord.Embed(
            title="AI command guide",
            description=(
                "Commands for generating replies, preparing staff-only drafts, and managing "
                "automatic ticket responses."
            ),
            color=self.bot.main_color,
        )
        embed.add_field(
            name="Generate and send",
            value=(
                f"`{prefix}aireply` — Generate and send a helpful response.\n"
                f"`{prefix}aireply CONFIRM` — Send a helpful response without the "
                "“Can I help with anything else?” line.\n"
                f"`{prefix}aiall` — Summarize the resolved ticket and send the closure warning.\n"
                f"`{prefix}annoyautoreply` — Generate and send the sarcastic response.\n"
                f"`{prefix}fakeautoreply MESSAGE` — Send your own text using the AI presentation."
            ),
            inline=False,
        )
        embed.add_field(
            name="Staff-only raw drafts",
            value=(
                f"`{prefix}aireply raw` — Generate a helpful copyable draft without sending it.\n"
                f"`{prefix}aiall raw` — Generate a closure-ready copyable summary without sending it."
            ),
            inline=False,
        )
        embed.add_field(
            name="Automatic autoreplies — Administrator",
            value=(
                f"`{prefix}autoreply` — List all configured rules.\n"
                f"`{prefix}autoreply KEYWORD` — View one rule and its alternatives.\n"
                f"`{prefix}autoreply KEYWORD raw` — Get a copyable edit command.\n"
                f"`{prefix}autoreply create \"NAME: …\" "
                "[\"MUST MENTION TO CHECK\": …] ALIAS` — Create a rule.\n"
                f"`{prefix}autoreply edit …` — Update a rule, including `ALTERNATIVES`.\n"
                f"`{prefix}autoreply remove KEYWORD` — Delete a rule."
            ),
            inline=False,
        )
        embed.add_field(
            name="AI-message cleanup",
            value=(
                f"`{prefix}delete` — Delete the latest linked reply.\n"
                f"`{prefix}delete MESSAGE_ID` — Delete a specific linked AI/staff reply."
            ),
            inline=False,
        )
        embed.add_field(
            name="Automatic alias helpers",
            value=(
                '`"context MESSAGE"` — Post plain staff-only context in the ticket.\n'
                '`"notify @role"` — Immediately ping a role in the staff ticket.'
            ),
            inline=False,
        )
        embed.add_field(
            name="Automatic abuse filter — Administrator",
            value=(
                f"`{prefix}abuseword add WORD OR PHRASE` — Add a persistent custom entry.\n"
                f"`{prefix}abuseword remove WORD OR PHRASE` — Remove a custom entry.\n"
                f"`{prefix}abuseword list` — List custom entries."
            ),
            inline=False,
        )
        embed.set_footer(
            text="Fully automatic ticket checks and URL responses run without a command."
        )
        return await ctx.send(embed=embed)

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.has_any_role_id(*MANUAL_AI_ROLE_IDS)
    @checks.thread_only()
    async def aireply(self, ctx, mode: str = None):
        """Generate and send a helpful AI response using the complete thread history."""
        normalized_mode = mode.casefold() if mode is not None else None
        if normalized_mode not in {None, "raw", "confirm"}:
            raise commands.BadArgument(
                f"Use `{self.bot.prefix}aireply`, `{self.bot.prefix}aireply raw`, "
                f"or `{self.bot.prefix}aireply CONFIRM`."
            )
        staff_only = normalized_mode == "raw"
        confirmed_without_closing = normalized_mode == "confirm"
        await self._send_generated_ai_reply(
            ctx,
            GeminiHelpfulReplyGenerator,
            command_name=(
                "aireply raw"
                if staff_only
                else "aireply CONFIRM" if confirmed_without_closing else "aireply"
            ),
            log_name="Manual helpful AI reply",
            tone_label="helpful",
            staff_only=staff_only,
            include_closing=not confirmed_without_closing,
        )

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.has_any_role_id(*MANUAL_AI_ROLE_IDS)
    @checks.thread_only()
    async def aiall(self, ctx, mode: str = None):
        """Summarize the ticket and send a closure-ready all-inquiries response."""
        normalized_mode = mode.casefold() if mode is not None else None
        if normalized_mode not in {None, "raw"}:
            raise commands.BadArgument(
                f"Use `{self.bot.prefix}aiall` or `{self.bot.prefix}aiall raw`."
            )

        staff_only = normalized_mode == "raw"
        await self._send_generated_ai_reply(
            ctx,
            GeminiTicketSummaryGenerator,
            command_name="aiall raw" if staff_only else "aiall",
            log_name="Manual all-inquiries AI summary",
            tone_label="all-inquiries summary",
            staff_only=staff_only,
            include_closing=True,
            closing_text=AI_ALL_CLOSING,
        )

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.has_any_role_id(*MANUAL_AI_ROLE_IDS)
    @checks.thread_only()
    async def annoyautoreply(self, ctx):
        """Generate and send a sarcastic AI response using the complete thread history."""
        await self._send_generated_ai_reply(
            ctx,
            GeminiAnnoyReplyGenerator,
            command_name="annoyautoreply",
            log_name="Manual annoy autoreply",
            tone_label="sarcastic",
        )

    @commands.command(aliases=["formatreply"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def freply(self, ctx, *, msg: str = ""):
        """
        Reply to a Modmail thread with variables.

        Works just like `{prefix}reply`, however with the addition of three variables:
          - `{{channel}}` - the `discord.TextChannel` object
          - `{{recipient}}` - the `discord.User` object of the recipient
          - `{{author}}` - the `discord.User` object of the author

        Supports attachments and images as well as
        automatically embedding image URLs.
        """
        msg = self.bot.formatter.format(
            msg,
            channel=ctx.channel,
            recipient=ctx.thread.recipient,
            author=ctx.message.author,
        )
        # Ensure logs record only the reply text, not the command.
        ctx.message.content = msg
        async with safe_typing(ctx):
            await ctx.thread.reply(ctx.message, msg)

    @commands.command(aliases=["formatanonreply"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def fareply(self, ctx, *, msg: str = ""):
        """
        Anonymously reply to a Modmail thread with variables.

        Works just like `{prefix}areply`, however with the addition of three variables:
          - `{{channel}}` - the `discord.TextChannel` object
          - `{{recipient}}` - the `discord.User` object of the recipient
          - `{{author}}` - the `discord.User` object of the author

        Supports attachments and images as well as
        automatically embedding image URLs.
        """
        msg = self.bot.formatter.format(
            msg,
            channel=ctx.channel,
            recipient=ctx.thread.recipient,
            author=ctx.message.author,
        )
        # Ensure logs record only the reply text, not the command.
        ctx.message.content = msg
        async with safe_typing(ctx):
            await ctx.thread.reply(ctx.message, msg, anonymous=True)

    @commands.command(aliases=["formatplainreply"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def fpreply(self, ctx, *, msg: str = ""):
        """
        Reply to a Modmail thread with variables and a plain message.

        Works just like `{prefix}areply`, however with the addition of three variables:
          - `{{channel}}` - the `discord.TextChannel` object
          - `{{recipient}}` - the `discord.User` object of the recipient
          - `{{author}}` - the `discord.User` object of the author

        Supports attachments and images as well as
        automatically embedding image URLs.
        """
        msg = self.bot.formatter.format(
            msg,
            channel=ctx.channel,
            recipient=ctx.thread.recipient,
            author=ctx.message.author,
        )
        # Ensure logs record only the reply text, not the command.
        ctx.message.content = msg
        async with safe_typing(ctx):
            await ctx.thread.reply(ctx.message, msg, plain=True)

    @commands.command(aliases=["formatplainanonreply"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def fpareply(self, ctx, *, msg: str = ""):
        """
        Anonymously reply to a Modmail thread with variables and a plain message.

        Works just like `{prefix}areply`, however with the addition of three variables:
          - `{{channel}}` - the `discord.TextChannel` object
          - `{{recipient}}` - the `discord.User` object of the recipient
          - `{{author}}` - the `discord.User` object of the author

        Supports attachments and images as well as
        automatically embedding image URLs.
        """
        msg = self.bot.formatter.format(
            msg,
            channel=ctx.channel,
            recipient=ctx.thread.recipient,
            author=ctx.message.author,
        )
        # Ensure logs record only the reply text, not the command.
        ctx.message.content = msg
        async with safe_typing(ctx):
            await ctx.thread.reply(ctx.message, msg, anonymous=True, plain=True)

    @commands.command(aliases=["anonreply", "anonymousreply"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def areply(self, ctx, *, msg: str = ""):
        """
        Reply to a thread anonymously.

        You can edit the anonymous user's name,
        avatar and tag using the config command.

        Edit the `anon_username`, `anon_avatar_url`
        and `anon_tag` config variables to do so.
        """
        # Ensure logs record only the reply text, not the command.
        ctx.message.content = msg
        async with safe_typing(ctx):
            await ctx.thread.reply(ctx.message, msg, anonymous=True)

    @commands.command(aliases=["plainreply"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def preply(self, ctx, *, msg: str = ""):
        """
        Reply to a Modmail thread with a plain message.

        Supports attachments and images as well as
        automatically embedding image URLs.
        """
        # Ensure logs record only the reply text, not the command.
        ctx.message.content = msg
        async with safe_typing(ctx):
            await ctx.thread.reply(ctx.message, msg, plain=True)

    @commands.command(aliases=["plainanonreply", "plainanonymousreply"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def pareply(self, ctx, *, msg: str = ""):
        """
        Reply to a Modmail thread with a plain message and anonymously.

        Supports attachments and images as well as
        automatically embedding image URLs.
        """
        # Ensure logs record only the reply text, not the command.
        ctx.message.content = msg
        async with safe_typing(ctx):
            await ctx.thread.reply(ctx.message, msg, anonymous=True, plain=True)

    @commands.group(invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def note(self, ctx, *, msg: str = ""):
        """
        Take a note about the current thread.

        Useful for noting context.
        """
        ctx.message.content = msg
        async with safe_typing(ctx):
            msg = await ctx.thread.note(ctx.message)
            await msg.pin()
        # Acknowledge and clean up the invoking command message
        sent_emoji, _ = await self.bot.retrieve_emoji()
        await self.bot.add_reaction(ctx.message, sent_emoji)
        try:
            await ctx.message.delete(delay=3)
        except (discord.Forbidden, discord.NotFound):
            pass

    @note.command(name="persistent", aliases=["persist"])
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def note_persistent(self, ctx, *, msg: str = ""):
        """
        Take a persistent note about the current user.
        """
        ctx.message.content = msg
        async with safe_typing(ctx):
            msg = await ctx.thread.note(ctx.message, persistent=True)
            await msg.pin()
        await self.bot.api.create_note(recipient=ctx.thread.recipient, message=ctx.message, message_id=msg.id)
        # Acknowledge and clean up the invoking command message
        sent_emoji, _ = await self.bot.retrieve_emoji()
        await self.bot.add_reaction(ctx.message, sent_emoji)
        try:
            await ctx.message.delete(delay=3)
        except (discord.Forbidden, discord.NotFound) as e:
            logger.debug(f"Failed to delete note command message: {e}")

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def edit(self, ctx, message_id: Optional[int] = None, *, message: str):
        """
        Edit a message that was sent using the reply or anonreply command.

        If no `message_id` is provided,
        the last message sent by a staff will be edited.

        Note: attachments **cannot** be edited.
        """
        thread = ctx.thread

        try:
            await thread.edit_message(message_id, message)
        except ValueError:
            return await ctx.send(
                embed=discord.Embed(
                    title="Failed",
                    description="Cannot find a message to edit. Plain messages are not supported.",
                    color=self.bot.error_color,
                )
            )

        sent_emoji, _ = await self.bot.retrieve_emoji()
        await self.bot.add_reaction(ctx.message, sent_emoji)

    @commands.command()
    @checks.has_permissions(PermissionLevel.REGULAR)
    async def selfcontact(self, ctx):
        """Creates a thread with yourself"""
        # Check if user already has a thread
        existing_thread = await self.bot.threads.find(recipient=ctx.author)
        if existing_thread:
            if existing_thread.snoozed:
                # Unsnooze the thread
                msg = await ctx.send("ℹ️ You had a snoozed thread. Unsnoozing now...")
                await existing_thread.restore_from_snooze()
                self.bot.threads.cache[existing_thread.id] = existing_thread
                try:
                    await msg.delete(delay=10)
                except (discord.Forbidden, discord.NotFound):
                    pass
                return
            else:
                # Thread already exists and is active
                embed = discord.Embed(
                    title="Thread not created",
                    description=f"A thread for you already exists in {existing_thread.channel.mention}.",
                    color=self.bot.error_color,
                )
                await ctx.send(embed=embed, delete_after=10)
                return

        await ctx.invoke(self.contact, users=[ctx.author])

    @commands.command(usage="<user> [category] [options]")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def contact(
        self,
        ctx,
        users: commands.Greedy[
            Union[
                Literal["silent", "silently"],
                discord.Member,
                discord.User,
                discord.Role,
            ]
        ],
        *,
        category: SimilarCategoryConverter = None,
        manual_trigger=True,
    ):
        """
        Create a thread with a specified member.

        If `category` is specified, the thread
        will be created in that specified category.

        `category`, if specified, may be a category ID, mention, or name.
        `users` may be a user ID, mention, or name. If multiple users are specified, a group thread will start.
        A maximum of 5 users are allowed.
        `options` can be `silent` or `silently`.
        """
        silent = any(x in users for x in ("silent", "silently"))
        if silent:
            try:
                users.remove("silent")
            except ValueError:
                pass

            try:
                users.remove("silently")
            except ValueError:
                pass

        if isinstance(category, str):
            category = category.split()

            category = " ".join(category)
            if category:
                try:
                    category = await SimilarCategoryConverter().convert(
                        ctx, category
                    )  # attempt to find a category again
                except commands.BadArgument:
                    category = None

            if isinstance(category, str):
                category = None

        errors = []
        for u in list(users):
            if isinstance(u, discord.Role):
                users += u.members
                users.remove(u)

        snoozed_users = []
        for u in list(users):
            exists = await self.bot.threads.find(recipient=u)
            if exists:
                # Check if thread is snoozed
                if exists.snoozed:
                    snoozed_users.append(u)
                    continue
                errors.append(f"A thread for {u} already exists.")
                if exists.channel:
                    errors[-1] += f" in {exists.channel.mention}"
                errors[-1] += "."
                users.remove(u)
            elif u.bot:
                errors.append(f"{u} is a bot, cannot add to thread.")
                users.remove(u)
            elif await self.bot.is_blocked(u):
                ref = f"{u.mention} is" if ctx.author != u else "You are"
                errors.append(f"{ref} currently blocked from contacting {self.bot.user.name}.")
                users.remove(u)

        # Handle snoozed users - unsnooze them and return early
        if snoozed_users:
            for u in snoozed_users:
                thread = await self.bot.threads.find(recipient=u)
                if thread and thread.snoozed:
                    msg = await ctx.send(f"ℹ️ {u.mention} had a snoozed thread. Unsnoozing now...")
                    await thread.restore_from_snooze()
                    self.bot.threads.cache[thread.id] = thread
                    try:
                        await msg.delete(delay=10)
                    except (discord.Forbidden, discord.NotFound) as e:
                        logger.debug(
                            f"Failed to delete message (likely already deleted or lacking permissions): {e}"
                        )
            # Don't try to create a new thread - we just unsnoozed existing ones
            return

        if len(users) > 5:
            errors.append("Group conversations only support 5 users.")
            users = []

        if errors or not users:
            if not users:
                # no users left
                title = "Thread not created"
            else:
                title = None

            if manual_trigger:  # not react to contact
                embed = discord.Embed(
                    title=title,
                    color=self.bot.error_color,
                    description="\n".join(errors),
                )
                await ctx.send(embed=embed, delete_after=10)

            if not users:
                return

        creator = ctx.author if manual_trigger else users[0]

        thread = await self.bot.threads.create(
            recipient=users[0],
            creator=creator,
            category=category,
            manual_trigger=manual_trigger,
            # The minimum character check is enforced in ThreadManager.create
        )

        if thread.cancelled:
            return

        if self.bot.config["dm_disabled"] in (
            DMDisabled.NEW_THREADS,
            DMDisabled.ALL_THREADS,
        ):
            logger.info("Contacting user %s when Modmail DM is disabled.", users[0])

        if not silent and not self.bot.config.get("thread_contact_silently"):
            if creator.id == users[0].id:
                description = self.bot.config["thread_creation_self_contact_response"]
            else:
                description = self.bot.formatter.format(
                    self.bot.config["thread_creation_contact_response"], creator=creator
                )

            em = discord.Embed(
                title=self.bot.config["thread_creation_contact_title"],
                description=description,
                color=self.bot.main_color,
            )
            if self.bot.config["show_timestamp"]:
                em.timestamp = discord.utils.utcnow()
            em.set_footer(
                text=f"{creator}", icon_url=creator.display_avatar.url if creator.display_avatar else None
            )

            for u in users:
                await u.send(embed=em)

        embed = discord.Embed(
            title="Created Thread",
            description=f"Thread started by {creator.mention} for {', '.join(u.mention for u in users)}.",
            color=self.bot.main_color,
        )
        await thread.wait_until_ready()

        if users[1:]:
            await thread.add_users(users[1:])

        await thread.channel.send(embed=embed)

        if manual_trigger:
            sent_emoji, _ = await self.bot.retrieve_emoji()
            await self.bot.add_reaction(ctx.message, sent_emoji)
            try:
                await ctx.message.delete(delay=5)
            except (discord.Forbidden, discord.NotFound):
                pass

    @commands.group(invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.MODERATOR)
    @trigger_typing
    async def blocked(self, ctx):
        """Retrieve a list of blocked users."""

        roles = []
        users = []
        now = ctx.message.created_at

        blocked_users = list(self.bot.blocked_users.items())
        for id_, reason in blocked_users:
            # parse "reason" and check if block is expired
            try:
                end_time, after = extract_block_timestamp(reason, id_)
            except ValueError:
                continue

            if end_time is not None:
                if after <= 0:
                    # No longer blocked
                    self.bot.blocked_users.pop(str(id_))
                    logger.debug("No longer blocked, user %s.", id_)
                    continue
            users.append((f"<@{id_}>", reason))

        blocked_roles = list(self.bot.blocked_roles.items())
        for id_, reason in blocked_roles:
            # parse "reason" and check if block is expired
            # etc "blah blah blah... until 2019-10-14T21:12:45.559948."
            try:
                end_time, after = extract_block_timestamp(reason, id_)
            except ValueError:
                continue

            if end_time is not None:
                if after <= 0:
                    # No longer blocked
                    self.bot.blocked_roles.pop(str(id_))
                    logger.debug("No longer blocked, role %s.", id_)
                    continue

            role = self.bot.guild.get_role(int(id_))
            if role:
                roles.append((role.mention, reason))

        user_embeds = [discord.Embed(title="Blocked Users", color=self.bot.main_color, description="")]

        if users:
            embed = user_embeds[0]

            for mention, reason in users:
                line = mention + f" - {reason or 'No Reason Provided'}\n"
                if len(embed.description) + len(line) > 2048:
                    embed = discord.Embed(
                        title="Blocked Users",
                        color=self.bot.main_color,
                        description=line,
                    )
                    user_embeds.append(embed)
                else:
                    embed.description += line
        else:
            user_embeds[0].description = "Currently there are no blocked users."

        if len(user_embeds) > 1:
            for n, em in enumerate(user_embeds):
                em.title = f"{em.title} [{n + 1}]"

        role_embeds = [discord.Embed(title="Blocked Roles", color=self.bot.main_color, description="")]

        if roles:
            embed = role_embeds[-1]

            for mention, reason in roles:
                line = mention + f" - {reason or 'No Reason Provided'}\n"
                if len(embed.description) + len(line) > 2048:
                    role_embeds[-1].set_author()
                    embed = discord.Embed(
                        title="Blocked Roles",
                        color=self.bot.main_color,
                        description=line,
                    )
                    role_embeds.append(embed)
                else:
                    embed.description += line
        else:
            role_embeds[-1].description = "Currently there are no blocked roles."

        if len(role_embeds) > 1:
            for n, em in enumerate(role_embeds):
                em.title = f"{em.title} [{n + 1}]"

        session = EmbedPaginatorSession(ctx, *user_embeds, *role_embeds)

        await session.run()

    @blocked.command(name="whitelist")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    @trigger_typing
    async def blocked_whitelist(self, ctx, *, user: User = None):
        """
        Whitelist or un-whitelist a user from getting blocked.

        Useful for preventing users from getting blocked by account_age/guild_age restrictions.
        """
        if user is None:
            thread = ctx.thread
            if thread:
                user = thread.recipient
            else:
                return await ctx.send_help(ctx.command)

        mention = getattr(user, "mention", f"`{user.id}`")
        msg = ""

        if str(user.id) in self.bot.blocked_whitelisted_users:
            embed = discord.Embed(
                title="Success",
                description=f"{mention} is no longer whitelisted.",
                color=self.bot.main_color,
            )
            self.bot.blocked_whitelisted_users.remove(str(user.id))
            return await ctx.send(embed=embed)

        self.bot.blocked_whitelisted_users.append(str(user.id))

        if str(user.id) in self.bot.blocked_users:
            msg = self.bot.blocked_users.get(str(user.id)) or ""
            self.bot.blocked_users.pop(str(user.id))

        await self.bot.config.update()

        if msg.startswith("System Message: "):
            # If the user is blocked internally (for example: below minimum account age)
            # Show an extended message stating the original internal message
            reason = msg[16:].strip().rstrip(".")
            embed = discord.Embed(
                title="Success",
                description=f"{mention} was previously blocked internally for "
                f'"{reason}". {mention} is now whitelisted.',
                color=self.bot.main_color,
            )
        else:
            embed = discord.Embed(
                title="Success",
                color=self.bot.main_color,
                description=f"{mention} is now whitelisted.",
            )

        return await ctx.send(embed=embed)

    @commands.command(usage="[user] [duration] [reason]")
    @checks.has_permissions(PermissionLevel.MODERATOR)
    @trigger_typing
    async def block(
        self,
        ctx,
        user_or_role: Optional[Union[User, discord.Role]] = None,
        *,
        after: UserFriendlyTime = None,
    ):
        """
        Block a user or role from using Modmail.

        You may choose to set a time as to when the user will automatically be unblocked.

        Leave `user` blank when this command is used within a
        thread channel to block the current recipient.
        `user` may be a user ID, mention, or name.
        `duration` may be a simple "human-readable" time text. See `{prefix}help close` for examples.
        """

        if user_or_role is None:
            thread = ctx.thread
            if thread:
                user_or_role = thread.recipient
            elif after is None:
                raise commands.MissingRequiredArgument(DummyParam("user or role"))
            else:
                raise commands.BadArgument(f'User or role "{after.arg}" not found.')

        mention = getattr(user_or_role, "mention", f"`{user_or_role.id}`")

        if (
            not isinstance(user_or_role, discord.Role)
            and str(user_or_role.id) in self.bot.blocked_whitelisted_users
        ):
            embed = discord.Embed(
                title="Error",
                description=f"Cannot block {mention}, user is whitelisted.",
                color=self.bot.error_color,
            )
            return await ctx.send(embed=embed)

        reason = f"by {escape_markdown(str(ctx.author))}"

        if after is not None:
            if "%" in reason:
                raise commands.BadArgument('The reason contains illegal character "%".')

            if after.arg:
                fmt_dt = discord.utils.format_dt(after.dt, "R")
            if after.dt > after.now:
                fmt_dt = discord.utils.format_dt(after.dt, "f")

            reason += f" until {fmt_dt}"

        reason += "."

        if isinstance(user_or_role, discord.Role):
            msg = self.bot.blocked_roles.get(str(user_or_role.id))
        else:
            msg = self.bot.blocked_users.get(str(user_or_role.id))

        if msg is None:
            msg = ""

        if msg:
            old_reason = msg.strip().rstrip(".")
            embed = discord.Embed(
                title="Success",
                description=f"{mention} was previously blocked {old_reason}.\n"
                f"{mention} is now blocked {reason}",
                color=self.bot.main_color,
            )
        else:
            embed = discord.Embed(
                title="Success",
                color=self.bot.main_color,
                description=f"{mention} is now blocked {reason}",
            )

        if isinstance(user_or_role, discord.Role):
            self.bot.blocked_roles[str(user_or_role.id)] = reason
        else:
            self.bot.blocked_users[str(user_or_role.id)] = reason
        await self.bot.config.update()

        return await ctx.send(embed=embed)

    @commands.command()
    @checks.has_permissions(PermissionLevel.MODERATOR)
    @trigger_typing
    async def unblock(self, ctx, *, user_or_role: Union[User, Role] = None):
        """
        Unblock a user from using Modmail.

        Leave `user` blank when this command is used within a
        thread channel to unblock the current recipient.
        `user` may be a user ID, mention, or name.
        """

        if user_or_role is None:
            thread = ctx.thread
            if thread:
                user_or_role = thread.recipient
            else:
                raise commands.MissingRequiredArgument(DummyParam("user or role"))

        mention = getattr(user_or_role, "mention", f"`{user_or_role.id}`")
        name = getattr(user_or_role, "name", f"`{user_or_role.id}`")

        if not isinstance(user_or_role, discord.Role) and str(user_or_role.id) in self.bot.blocked_users:
            msg = self.bot.blocked_users.pop(str(user_or_role.id)) or ""
            await self.bot.config.update()

            if msg.startswith("System Message: "):
                # If the user is blocked internally (for example: below minimum account age)
                # Show an extended message stating the original internal message
                reason = msg[16:].strip().rstrip(".") or "no reason"
                embed = discord.Embed(
                    title="Success",
                    description=f"{mention} was previously blocked internally {reason}.\n"
                    f"{mention} is no longer blocked.",
                    color=self.bot.main_color,
                )
                embed.set_footer(
                    text="However, if the original system block reason still applies, "
                    f"{name} will be automatically blocked again. "
                    f'Use "{self.bot.prefix}blocked whitelist {user_or_role.id}" to whitelist the user.'
                )
            else:
                embed = discord.Embed(
                    title="Success",
                    color=self.bot.main_color,
                    description=f"{mention} is no longer blocked.",
                )
        elif isinstance(user_or_role, discord.Role) and str(user_or_role.id) in self.bot.blocked_roles:
            msg = self.bot.blocked_roles.pop(str(user_or_role.id)) or ""
            await self.bot.config.update()

            embed = discord.Embed(
                title="Success",
                color=self.bot.main_color,
                description=f"{mention} is no longer blocked.",
            )
        else:
            embed = discord.Embed(
                title="Error",
                description=f"{mention} is not blocked.",
                color=self.bot.error_color,
            )

        return await ctx.send(embed=embed)

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def delete(self, ctx, message_id: int = None):
        """
        Delete a message that was sent using the reply command or a note.

        Deletes the previous message, unless a message ID is provided,
        which in that case, deletes the message with that message ID.

        Notes can only be deleted when a note ID is provided.
        """
        thread = ctx.thread

        try:
            await thread.delete_message(message_id, note=True)
        except ValueError as e:
            logger.warning("Failed to delete message: %s.", e)
            return await ctx.send(
                embed=discord.Embed(
                    title="Failed",
                    description="Cannot find a message to delete. Plain messages are not supported.",
                    color=self.bot.error_color,
                )
            )

        sent_emoji, _ = await self.bot.retrieve_emoji()
        await self.bot.add_reaction(ctx.message, sent_emoji)

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def repair(self, ctx):
        """
        Repair a thread broken by Discord.
        """
        sent_emoji, blocked_emoji = await self.bot.retrieve_emoji()

        if ctx.thread:
            user_id = match_user_id(ctx.channel.topic)
            if user_id == -1:
                logger.info("Setting current channel's topic to User ID.")
                await ctx.channel.edit(topic=f"User ID: {ctx.thread.id}")
            return await self.bot.add_reaction(ctx.message, sent_emoji)

        logger.info("Attempting to fix a broken thread %s.", ctx.channel.name)

        # Search cache for channel
        user_id, thread = next(
            ((k, v) for k, v in self.bot.threads.cache.items() if v.channel == ctx.channel),
            (-1, None),
        )
        if thread is not None:
            logger.debug("Found thread with tempered ID.")
            await ctx.channel.edit(reason="Fix broken Modmail thread", topic=f"User ID: {user_id}")
            return await self.bot.add_reaction(ctx.message, sent_emoji)

        # find genesis message to retrieve User ID
        async for message in ctx.channel.history(limit=10, oldest_first=True):
            if (
                message.author == self.bot.user
                and message.embeds
                and message.embeds[0].color
                and message.embeds[0].color.value == self.bot.main_color
                and message.embeds[0].footer.text
            ):
                user_id = match_user_id(message.embeds[0].footer.text, any_string=True)
                other_recipients = match_other_recipients(ctx.channel.topic)
                for n, uid in enumerate(other_recipients):
                    other_recipients[n] = await self.bot.get_or_fetch_user(uid)

                if user_id != -1:
                    recipient = self.bot.get_user(user_id)
                    if recipient is None:
                        self.bot.threads.cache[user_id] = thread = Thread(
                            self.bot.threads, user_id, ctx.channel, other_recipients
                        )
                    else:
                        self.bot.threads.cache[user_id] = thread = Thread(
                            self.bot.threads, recipient, ctx.channel, other_recipients
                        )
                    thread.ready = True
                    logger.info("Setting current channel's topic to User ID and created new thread.")
                    await ctx.channel.edit(reason="Fix broken Modmail thread", topic=f"User ID: {user_id}")
                    return await self.bot.add_reaction(ctx.message, sent_emoji)

        else:
            logger.warning("No genesis message found.")

        # match username from channel name
        # username-1234, username-1234_1, username-1234_2
        m = re.match(r"^(.+?)(?:-(\d{4}))?(?:_\d+)?$", ctx.channel.name)
        if m is not None:
            users = set(
                filter(
                    lambda member: member.name == m.group(1)
                    and (member.discriminator == "0" or member.discriminator == m.group(2)),
                    ctx.guild.members,
                )
            )
            if len(users) == 1:
                user = users.pop()
                name = self.bot.format_channel_name(user, exclude_channel=ctx.channel)
                recipient = self.bot.get_user(user.id)
                if user.id in self.bot.threads.cache:
                    thread = self.bot.threads.cache[user.id]
                    if thread.channel:
                        embed = discord.Embed(
                            title="Delete Channel",
                            description="This thread channel is no longer in use. "
                            f"All messages will be directed to {ctx.channel.mention} instead.",
                            color=self.bot.error_color,
                        )
                        embed.set_footer(
                            text='Please manually delete this channel, do not use "{prefix}close".'
                        )
                        try:
                            await thread.channel.send(embed=embed)
                        except discord.HTTPException:
                            pass

                other_recipients = match_other_recipients(ctx.channel.topic)
                for n, uid in enumerate(other_recipients):
                    other_recipients[n] = await self.bot.get_or_fetch_user(uid)

                if recipient is None:
                    self.bot.threads.cache[user.id] = thread = Thread(
                        self.bot.threads, user_id, ctx.channel, other_recipients
                    )
                else:
                    self.bot.threads.cache[user.id] = thread = Thread(
                        self.bot.threads, recipient, ctx.channel, other_recipients
                    )
                thread.ready = True
                logger.info("Setting current channel's topic to User ID and created new thread.")
                await ctx.channel.edit(
                    reason="Fix broken Modmail thread",
                    name=name,
                    topic=f"User ID: {user.id}",
                )
                return await self.bot.add_reaction(ctx.message, sent_emoji)

            elif len(users) >= 2:
                logger.info("Multiple users with the same name and discriminator.")
        return await self.bot.add_reaction(ctx.message, blocked_emoji)

    @commands.command()
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def enable(self, ctx):
        """
        Re-enables DM functionalities of Modmail.

        Undo's the `{prefix}disable` command, all DM will be relayed after running this command.
        """
        embed = discord.Embed(
            title="Success",
            description="Modmail will now accept all DM messages.",
            color=self.bot.main_color,
        )

        if self.bot.config["dm_disabled"] != DMDisabled.NONE:
            self.bot.config["dm_disabled"] = DMDisabled.NONE
            await self.bot.config.update()

        return await ctx.send(embed=embed)

    @commands.group(invoke_without_command=True)
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def disable(self, ctx):
        """
        Disable partial or full Modmail thread functions.

        To stop all new threads from being created, do `{prefix}disable new`.
        To stop all existing threads from DMing Modmail, do `{prefix}disable all`.
        To check if the DM function for Modmail is enabled, do `{prefix}isenable`.
        """
        await ctx.send_help(ctx.command)

    @disable.command(name="new")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def disable_new(self, ctx):
        """
        Stop accepting new Modmail threads.

        No new threads can be created through DM.
        """
        embed = discord.Embed(
            title="Success",
            description="Modmail will not create any new threads.",
            color=self.bot.main_color,
        )
        if self.bot.config["dm_disabled"] != DMDisabled.NEW_THREADS:
            self.bot.config["dm_disabled"] = DMDisabled.NEW_THREADS
            await self.bot.config.update()

        return await ctx.send(embed=embed)

    @disable.command(name="all")
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def disable_all(self, ctx):
        """
        Disables all DM functionalities of Modmail.

        No new threads can be created through DM nor no further DM messages will be relayed.
        """
        embed = discord.Embed(
            title="Success",
            description="Modmail will not accept any DM messages.",
            color=self.bot.main_color,
        )

        if self.bot.config["dm_disabled"] != DMDisabled.ALL_THREADS:
            self.bot.config["dm_disabled"] = DMDisabled.ALL_THREADS
            await self.bot.config.update()

        return await ctx.send(embed=embed)

    @commands.command()
    @checks.has_permissions(PermissionLevel.ADMINISTRATOR)
    async def isenable(self, ctx):
        """
        Check if the DM functionalities of Modmail is enabled.
        """

        if self.bot.config["dm_disabled"] == DMDisabled.NEW_THREADS:
            embed = discord.Embed(
                title="New Threads Disabled",
                description="Modmail is not creating new threads.",
                color=self.bot.error_color,
            )
        elif self.bot.config["dm_disabled"] == DMDisabled.ALL_THREADS:
            embed = discord.Embed(
                title="All DM Disabled",
                description="Modmail is not accepting any DM messages for new and existing threads.",
                color=self.bot.error_color,
            )
        else:
            embed = discord.Embed(
                title="Enabled",
                description="Modmail now is accepting all DM messages.",
                color=self.bot.main_color,
            )

        return await ctx.send(embed=embed)

    @commands.command(usage="[duration]")
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    @checks.thread_only()
    async def snooze(self, ctx, *, duration: UserFriendlyTime = None):
        """
        Snooze this thread. Behavior depends on config:
        - delete (default): deletes the channel and restores it later
        - move: moves the channel to the configured snoozed category
            Optionally specify a duration, e.g. 'snooze 2d' for 2 days.
            Uses config: snooze_default_duration, snooze_title, snooze_text
        """
        thread = ctx.thread
        if thread.snoozed:
            await ctx.send("This thread is already snoozed.")
            logging.info(f"[SNOOZE] Thread for {getattr(thread.recipient, 'id', None)} already snoozed.")
            return
        # Default snooze duration with safe fallback
        try:
            default_snooze = int(self.bot.config.get("snooze_default_duration", 604800))
        except (ValueError, TypeError):
            default_snooze = 604800
        if duration:
            snooze_for = int((duration.dt - duration.now).total_seconds())
            snooze_for = min(snooze_for, default_snooze)
        else:
            snooze_for = default_snooze

        # Capacity pre-check: if behavior is move, ensure snoozed category has room (<49 channels)
        behavior = (self.bot.config.get("snooze_behavior") or "delete").lower()
        if behavior == "move":
            snoozed_cat_id = self.bot.config.get("snoozed_category_id")
            target_category = None
            if snoozed_cat_id:
                try:
                    target_category = self.bot.modmail_guild.get_channel(int(snoozed_cat_id))
                except Exception:
                    target_category = None
            # Auto-create snoozed category if missing
            if not isinstance(target_category, discord.CategoryChannel):
                try:
                    logging.info("Auto-creating snoozed category for move-based snoozing.")
                    # Hide category by default; only bot can view/manage
                    overwrites = {
                        self.bot.modmail_guild.default_role: discord.PermissionOverwrite(view_channel=False)
                    }
                    bot_member = self.bot.modmail_guild.me
                    if bot_member is not None:
                        overwrites[bot_member] = discord.PermissionOverwrite(
                            view_channel=True,
                            send_messages=True,
                            read_message_history=True,
                            manage_channels=True,
                            manage_messages=True,
                            attach_files=True,
                            embed_links=True,
                            add_reactions=True,
                        )
                    target_category = await self.bot.modmail_guild.create_category(
                        name="Snoozed Threads",
                        overwrites=overwrites,
                        reason="Auto-created snoozed category for move-based snoozing",
                    )
                    try:
                        await self.bot.config.set("snoozed_category_id", target_category.id)
                        await self.bot.config.update()
                    except Exception as e:
                        logging.warning("Failed to persist snoozed_category_id: %s", e)
                        try:
                            await ctx.send(
                                "⚠️ Created snoozed category but failed to save it to config. Please set `snoozed_category_id` manually."
                            )
                        except Exception as e:
                            logging.info(
                                "Failed to notify about snoozed category persistence issue: %s",
                                e,
                            )
                    await ctx.send(
                        embed=discord.Embed(
                            title="Snoozed category created",
                            description=(
                                f"Created category {target_category.mention if hasattr(target_category, 'mention') else target_category.name} "
                                "and set it as `snoozed_category_id`."
                            ),
                            color=self.bot.main_color,
                        )
                    )
                except Exception as e:
                    await ctx.send(
                        embed=discord.Embed(
                            title="Could not create snoozed category",
                            description=(
                                "I couldn't create a category automatically. Please ensure I have Manage Channels "
                                "permission, or set `snoozed_category_id` manually."
                            ),
                            color=self.bot.error_color,
                        )
                    )
                    logging.warning("Failed to auto-create snoozed category: %s", e)
            # Capacity check after ensuring category exists
            if isinstance(target_category, discord.CategoryChannel):
                try:
                    if len(target_category.channels) >= 49:
                        await ctx.send(
                            embed=discord.Embed(
                                title="Snooze unavailable",
                                description=(
                                    "The configured snoozed category is full (49 channels). "
                                    "Unsnooze or move some channels out before snoozing more."
                                ),
                                color=self.bot.error_color,
                            )
                        )
                        return
                except Exception as e:
                    logging.debug("Failed to check snoozed category channel count: %s", e)

        # Store snooze_until timestamp for reliable auto-unsnooze
        now = datetime.now(timezone.utc)
        snooze_until = now + timedelta(seconds=snooze_for)
        await self.bot.api.logs.update_one(
            {"recipient.id": str(thread.id)},
            {
                "$set": {
                    "snooze_start": now.isoformat(),
                    "snooze_for": snooze_for,
                    "snooze_until": snooze_until.isoformat(),
                }
            },
        )
        embed = discord.Embed(
            title=self.bot.config.get("snooze_title") or "Thread Snoozed",
            description=self.bot.config.get("snooze_text") or "This thread has been snoozed.",
            color=self.bot.error_color,
        )
        await ctx.send(embed=embed)
        ok = await thread.snooze(moderator=ctx.author, snooze_for=snooze_for)
        if ok:
            logging.info(
                f"[SNOOZE] Thread for {getattr(thread.recipient, 'id', None)} snoozed for {snooze_for}s."
            )
            self.bot.threads.cache[thread.id] = thread
        else:
            await ctx.send("Failed to snooze this thread.")
            logging.error(f"[SNOOZE] Failed to snooze thread for {getattr(thread.recipient, 'id', None)}.")

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def unsnooze(self, ctx, *, user: str = None):
        """
        Unsnooze a thread: restores the channel and replays messages.
        You can specify a user by mention or ID, or run in a thread channel to unsnooze that thread.
        Uses config: unsnooze_text
        """
        import discord

        thread = None
        user_obj = None
        if user is not None:
            user_id = self._resolve_user(user)
            if user_id:
                try:
                    user_obj = await self.bot.get_or_fetch_user(user_id)
                except Exception:
                    logger.debug(
                        "Failed fetching user during unsnooze; falling back to partial object (%s).",
                        user_id,
                        exc_info=True,
                    )
                    user_obj = discord.Object(user_id)
            if user_obj:
                thread = await self.bot.threads.find(recipient=user_obj)
            if not thread:
                await ctx.send(f"[DEBUG] No thread found for user {user} (obj: {user_obj}).")
                logging.warning(f"[UNSNOOZE] No thread found for user {user} (obj: {user_obj})")
                return
        elif hasattr(ctx, "thread"):
            thread = ctx.thread
        else:
            await ctx.send("This is not a Modmail thread.")
            logging.warning("[UNSNOOZE] Not a Modmail thread context.")
            return
        if not thread.snoozed:
            await ctx.send("This thread is not snoozed.")
            logging.info(f"[UNSNOOZE] Thread for {getattr(thread.recipient, 'id', None)} is not snoozed.")
            return

        # Manually fetch snooze_data if the thread object doesn't have it
        if not thread.snooze_data:
            log_entry = await self.bot.api.logs.find_one({"recipient.id": str(thread.id), "snoozed": True})
            if log_entry:
                thread.snooze_data = log_entry.get("snooze_data")

        ok = await thread.restore_from_snooze()
        if ok:
            self.bot.threads.cache[thread.id] = thread
            await ctx.send(
                self.bot.config.get("unsnooze_text") or "This thread has been unsnoozed and restored."
            )
            logging.info(f"[UNSNOOZE] Thread for {getattr(thread.recipient, 'id', None)} unsnoozed.")
        else:
            await ctx.send("Failed to unsnooze this thread.")
            logging.error(
                f"[UNSNOOZE] Failed to unsnooze thread for {getattr(thread.recipient, 'id', None)}."
            )

    @commands.command()
    @checks.has_permissions(PermissionLevel.SUPPORTER)
    async def snoozed(self, ctx):
        """
        List all currently snoozed threads/users.
        """
        snoozed_threads = [thread for thread in self.bot.threads.cache.values() if thread.snoozed]
        if not snoozed_threads:
            await ctx.send("No threads are currently snoozed.")
            return

        lines = []
        now = datetime.now(timezone.utc)
        for thread in snoozed_threads:
            user = thread.recipient.name if thread.recipient else "Unknown"
            user_id = thread.id

            since_str = "?"
            until_str = "?"

            if thread.snooze_data:
                since = thread.snooze_data.get("snooze_start")
                duration = thread.snooze_data.get("snooze_for")

                if since:
                    try:
                        since_dt = datetime.fromisoformat(since)
                        since_str = f"<t:{int(since_dt.timestamp())}:R>"  # Discord relative timestamp
                    except (ValueError, TypeError) as e:
                        logging.warning(f"[SNOOZED] Invalid snooze_start for {user_id}: {since} ({e})")
                else:
                    logging.warning(f"[SNOOZED] Missing snooze_start for {user_id}")

                if duration and since_str != "?":
                    try:
                        until_dt = datetime.fromisoformat(since) + timedelta(seconds=int(duration))
                        until_str = f"<t:{int(until_dt.timestamp())}:R>"
                    except (ValueError, TypeError) as e:
                        logging.warning(
                            f"[SNOOZED] Invalid until time for {user_id}: {since} + {duration} ({e})"
                        )

            lines.append(f"- {user} (`{user_id}`) since {since_str}, until {until_str}")

        await ctx.send("Snoozed threads:\n" + "\n".join(lines))

    async def cog_load(self):
        self.snooze_auto_unsnooze.start()
        self.unanswered_reply_reminders.start()

    def cog_unload(self):
        self.snooze_auto_unsnooze.cancel()
        self.unanswered_reply_reminders.cancel()
        self._auto_unsnooze_task.cancel()

    @tasks.loop(seconds=10)
    async def snooze_auto_unsnooze(self):
        now = datetime.now(timezone.utc)
        snoozed = await self.bot.api.logs.find({"snoozed": True}).to_list(None)
        for entry in snoozed:
            snooze_until = entry.get("snooze_until")
            if snooze_until:
                try:
                    until_dt = datetime.fromisoformat(snooze_until)
                    if now >= until_dt:
                        thread = await self.bot.threads.find(recipient_id=int(entry["recipient"]["id"]))
                        if thread and thread.snoozed:
                            await thread.restore_from_snooze()
                except (ValueError, TypeError) as e:
                    logger.debug(
                        "Failed parsing snooze_until timestamp for auto-unsnooze loop: %s",
                        e,
                    )

    @snooze_auto_unsnooze.before_loop
    async def _snooze_auto_unsnooze_before(self):
        await self.bot.wait_until_ready()

    async def process_dm_modmail(self, message: discord.Message) -> None:
        # ... existing code ...
        # Before processing, check if thread is snoozed and auto-unsnooze
        thread = await self.threads.find(recipient=message.author)
        if thread and thread.snoozed:
            await thread.restore_from_snooze()
            # Ensure the thread object in the cache is updated with the new channel
            self.threads.cache[thread.id] = thread
        # ... rest of the method unchanged ...

    @commands.command()
    @checks.has_permissions(PermissionLevel.OWNER)
    async def clearsnoozed(self, ctx):
        """
        List all snoozed threads and ask for confirmation before clearing (unsnoozing) all of them.
        Only proceed if the user confirms.
        """
        snoozed = await self.bot.api.logs.find({"snoozed": True}).to_list(None)
        if not snoozed:
            await ctx.send("No threads are currently snoozed.")
            return
        lines = []
        for entry in snoozed:
            user = entry.get("recipient", {}).get("name", "Unknown")
            user_id = entry.get("recipient", {}).get("id", "?")
            lines.append(f"- {user} (`{user_id}`)")
        msg = await ctx.send(
            "The following threads are currently snoozed and will be unsnoozed if you confirm:\n"
            + "\n".join(lines)
            + "\n\nType `yes` to confirm, or anything else to cancel."
        )

        def check(m):
            return m.author == ctx.author and m.channel == ctx.channel

        try:
            reply = await self.bot.wait_for("message", check=check, timeout=30)
        except asyncio.TimeoutError:
            await ctx.send("Timed out. No threads were unsnoozed.")
            return
        if reply.content.strip().lower() != "yes":
            await ctx.send("Cancelled. No threads were unsnoozed.")
            return
        count = 0
        for entry in snoozed:
            user_id = entry.get("recipient", {}).get("id")
            if not user_id:
                continue
            user_obj = None
            try:
                user_obj = await self.bot.get_or_fetch_user(int(user_id))
            except Exception:
                user_obj = discord.Object(int(user_id))
            thread = await self.bot.threads.find(recipient=user_obj)
            if thread and thread.snoozed:
                ok = await thread.restore_from_snooze()
                if ok:
                    self.bot.threads.cache[thread.id] = thread
                    count += 1
        await ctx.send(f"Unsnoozed {count} threads.")


async def setup(bot):
    await bot.add_cog(Modmail(bot))
