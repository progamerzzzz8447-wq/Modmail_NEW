"""Read the published application batch schedule without guessing applicant outcomes."""

import asyncio
import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

CAREERS_CHANNEL_ID = 1362419467753361519
CAREERS_URL = "https://discord.com/channels/1308444031188992090/1362419467753361519"
READING_MARKER = "[NEXT_APPLICATION_READING]"
LABEL = re.compile(r"next\s+scheduled\s+reading\s*:\s*([^\n]*(?:\n[^\n]*)?)", re.I)
STAMP = re.compile(r"<t:(\d{9,12})(?::[tTdDfFRs])?>")
DATE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4}),?\s+(\d{1,2}:\d{2})\b")
DISCLAIMER = (
    "This time is an estimate. The schedule may change, and your result may arrive later."
)
logger = logging.getLogger(__name__)


def describe_dynamic_reply(text):
    """Give the selector meaningful content without doing speculative record lookups."""
    return text.replace(
        "[APPLICATION_STATUS]",
        "Look up this ticket recipient's application status and receipt in the academy system. "
        "Show the recorded outcome; accepted or declined applicants should check Careers DMs. "
        "Submitted applications receive the next scheduled reading time. "
        "If the record is missing, unavailable or unclear, explain that staff need to check.",
    ).replace(
        READING_MARKER,
        "Read the next scheduled application-reading batch from the Careers channel and show "
        "the published time with a reminder that results are not guaranteed at that time.",
    )


def schedule_timestamp(text):
    """Return (label found, timestamp); an invalid latest notice supersedes older ones."""
    match = LABEL.search(text.replace("**", "").replace("__", ""))
    if not match:
        return False, None
    value = match.group(1).strip()
    # Only parse the label's value/next line, never arbitrary timestamps elsewhere.
    if re.search(r"\b(cancelled|canceled|tba|tbd|postponed)\b", value, re.I):
        return True, None
    stamp = STAMP.search(value)
    try:
        if stamp:
            timestamp = int(stamp.group(1))
            datetime.fromtimestamp(timestamp, timezone.utc)
            return True, timestamp
        date = DATE.search(value)
        if date:
            local = datetime.strptime(" ".join(date.groups()), "%d/%m/%Y %H:%M")
            zone = ZoneInfo("Europe/London")
            # Reject ambiguous/nonexistent clock-change times rather than guess.
            a, b = local.replace(tzinfo=zone, fold=0), local.replace(tzinfo=zone, fold=1)
            if a.utcoffset() != b.utcoffset():
                return True, None
            return True, int(a.timestamp())
    except (ValueError, OverflowError, OSError):
        pass
    return True, None


def message_schedule_text(message):
    parts = [getattr(message, "content", "") or ""]
    for embed in getattr(message, "embeds", []):
        parts.extend([getattr(embed, "title", "") or "", getattr(embed, "description", "") or ""])
        for field in getattr(embed, "fields", []):
            parts.append(f"{field.name.rstrip(':')}: {field.value}")
    return "\n".join(parts)


async def application_reading_reply(bot, *, now=None):
    now = now or datetime.now(timezone.utc)

    async def read():
        channel = bot.get_channel(CAREERS_CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(CAREERS_CHANNEL_ID)
        async for message in channel.history(limit=100, oldest_first=False):
            found, timestamp = schedule_timestamp(message_schedule_text(message))
            if found:
                return timestamp
        return None

    try:
        timestamp = await asyncio.wait_for(read(), timeout=8)
    except Exception:
        logger.warning("Could not read the Careers application schedule", exc_info=True)
        timestamp = None
    if timestamp is None or timestamp <= now.timestamp():
        return (
            "I couldn't confirm a future application-reading time from the Careers channel. "
            f"Please check [Careers]({CAREERS_URL}) for updates.\n\n{DISCLAIMER}"
        )
    return (
        f"**Next application reading**\n<t:{timestamp}:f> · <t:{timestamp}:R>\n\n"
        f"{DISCLAIMER}\n[View Careers updates]({CAREERS_URL})"
    )


async def expand_application_reading(bot, text, *, recipient=None):
    # Status lookup uses the actual ticket recipient, never a supplied username.
    from core.application_status import STATUS_MARKER, application_status_reply

    if STATUS_MARKER in text:
        text = text.replace(STATUS_MARKER, await application_status_reply(bot, recipient))
    if READING_MARKER not in text:
        return text
    text = text.replace("Application review timing: ", "")
    return text.replace(READING_MARKER, await application_reading_reply(bot))
