import base64
import re
import typing
from itertools import zip_longest


SAFE_AI_REPLY_COMMANDS = frozenset({"reply", "freply", "formatreply"})
FORMATTED_AI_REPLY_COMMANDS = frozenset({"freply", "formatreply"})


def parse_alias(alias: str, *, split: bool = True) -> typing.List[str]:
    """Parse quoted, optionally multi-step aliases while preserving embedded ``&&``."""

    def encode_alias(match):
        encoded = base64.b64encode(match.group(1).encode()).decode()
        return "\x1aU" + encoded + "\x1aU"

    def decode_alias(match):
        return base64.b64decode(match.group(1).encode()).decode()

    alias = re.sub(
        r'(?:(?<=^)(?:\s*(?<!\\)(?:")\s*)|(?<=&&)(?:\s*(?<!\\)(?:")\s*))(.+?)'
        r'(?:(?:\s*(?<!\\)(?:")\s*)(?=&&)|(?:\s*(?<!\\)(?:")\s*)(?=$))',
        encode_alias,
        str(alias or ""),
        flags=re.DOTALL,
    ).strip()

    if not alias:
        return []

    iterate = re.split(r"\s*&&\s*", alias) if split else [alias]
    aliases = []
    for step in iterate:
        step = re.sub(r"\x1AU(.+?)\x1AU", decode_alias, step)
        if len(step) >= 2 and step[0] == step[-1] == '"':
            step = step[1:-1]
        aliases.append(step)
    return aliases


def normalize_alias(alias: str, message: str = "") -> typing.List[str]:
    aliases = parse_alias(alias)
    contents = parse_alias(message, split=False)

    normalized = []
    for step, content in zip_longest(aliases, contents):
        if step is None:
            break
        normalized.append(f"{step} {content}" if content else step)
    return normalized


def parse_reply_alias(alias: str) -> typing.Optional[typing.List[typing.Tuple[str, str]]]:
    """Return safe reply-command steps, or None if an alias contains another command."""
    parsed = []
    for step in parse_alias(alias):
        parts = step.strip().split(maxsplit=1)
        if len(parts) != 2:
            return None
        command, message = parts[0].casefold(), parts[1].strip()
        if command not in SAFE_AI_REPLY_COMMANDS or not message:
            return None
        parsed.append((command, message))
    return parsed or None


def parse_autoreply_rule_spec(name_argument: str, value: str) -> typing.Dict[str, typing.Any]:
    """Parse ``NAME``/``MUST MENTION``/alias syntax used by ``?autoreply create``."""
    name_match = re.fullmatch(r"\s*name\s*:\s*(.+?)\s*", str(name_argument or ""), re.IGNORECASE | re.DOTALL)
    if name_match is None:
        raise ValueError('The first argument must use "NAME: <display name>".')

    value_match = re.fullmatch(
        r"\s*\[\s*[\"']?must\s+mention\s+to\s+check[\"']?\s*:\s*(.*?)\s*\]\s+(.+?)\s*",
        str(value or ""),
        re.IGNORECASE | re.DOTALL,
    )
    if value_match is None:
        raise ValueError(
            'Use ["MUST MENTION TO CHECK": word, another word] followed by the alias name.'
        )

    display_name = name_match.group(1).strip()
    triggers = [item.strip().strip("\"'") for item in value_match.group(1).split(",")]
    triggers = list(dict.fromkeys(item.casefold() for item in triggers if item))
    alias_name = value_match.group(2).strip()
    if len(alias_name) >= 2 and alias_name[0] == alias_name[-1] and alias_name[0] in "\"'":
        alias_name = alias_name[1:-1].strip()
    alias_name = alias_name.casefold()

    if not display_name:
        raise ValueError("The autoreply display name cannot be empty.")
    if len(display_name) > 100:
        raise ValueError("The autoreply display name cannot be longer than 100 characters.")
    if not triggers:
        raise ValueError("Configure at least one must-mention word or phrase.")
    if len(triggers) > 25:
        raise ValueError("Configure no more than 25 must-mention words or phrases.")
    if any(len(item) > 100 for item in triggers):
        raise ValueError("Must-mention words or phrases cannot exceed 100 characters.")
    if not alias_name or len(alias_name) > 120:
        raise ValueError("Provide a valid alias name of no more than 120 characters.")

    return {"name": display_name, "triggers": triggers, "alias": alias_name}
