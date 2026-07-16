import base64
import json
import re
import typing
from itertools import zip_longest


SAFE_AI_REPLY_COMMANDS = frozenset(
    {
        "reply",
        "freply",
        "formatreply",
        "areply",
        "anonreply",
        "anonymousreply",
        "fareply",
        "formatanonreply",
        "preply",
        "plainreply",
        "fpreply",
        "formatplainreply",
        "pareply",
        "plainanonreply",
        "plainanonymousreply",
        "fpareply",
        "formatplainanonreply",
    }
)
FORMATTED_AI_REPLY_COMMANDS = frozenset(
    {
        "freply",
        "formatreply",
        "fareply",
        "formatanonreply",
        "fpreply",
        "formatplainreply",
        "fpareply",
        "formatplainanonreply",
    }
)


def normalize_compact_fakeautoreply_invocation(
    content: str, invoked_prefix: str
) -> typing.Optional[str]:
    """Insert the optional separator after ``fakeautoreply``.

    Discord's command parser normally treats ``?fakeautoreplyPlease`` as a
    completely different command.  This compatibility shim is deliberately
    limited to the manual fake-autoreply command so ordinary command names and
    aliases retain their existing parsing behavior.
    """
    command_text = f"{invoked_prefix}fakeautoreply"
    content = str(content or "")
    if not content.casefold().startswith(command_text.casefold()):
        return None

    message = content[len(command_text) :]
    if not message or message[0].isspace():
        return None
    return f"{command_text} {message}"


class DeferredDeleteMessage:
    """Proxy a real message while postponing deletion until alias execution completes.

    Reactions, edits, and other operations are delegated to the real message. This lets
    later commands in a multi-step alias interact with it after an earlier reply command
    requested deletion.
    """

    def __init__(self, message):
        self._message = message
        self._delete_requested = False
        self._delete_delay = None

    def __getattr__(self, name: str):
        return getattr(self._message, name)

    def __bool__(self):
        return bool(self._message)

    async def delete(self, *, delay=None):
        if not self._delete_requested:
            self._delete_requested = True
            self._delete_delay = delay
        elif self._delete_delay is None or delay is None:
            # An immediate deletion request always takes precedence.
            self._delete_delay = None
        else:
            self._delete_delay = min(self._delete_delay, delay)

    async def finalize_delete(self):
        if not self._delete_requested:
            return
        await self._message.delete(delay=self._delete_delay)


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
    """Extract reply-command steps while ignoring unrelated alias actions."""
    parsed = []
    for step in parse_alias(alias):
        parts = step.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        command, message = parts[0].casefold(), parts[1].strip()
        if command not in SAFE_AI_REPLY_COMMANDS or not message:
            continue
        parsed.append((command, message))
    return parsed or None


def _extract_autoreply_alternatives(value: str) -> typing.Tuple[str, typing.List[typing.Dict[str, str]]]:
    """Remove and parse an optional ``ALTERNATIVES`` block from a rule value."""
    marker = re.search(
        r"\[\s*[\"']?alternatives[\"']?\s*:\s*",
        value,
        re.IGNORECASE | re.DOTALL,
    )
    if marker is None:
        return value, []

    quote_char = None
    escaped = False
    closing_index = None
    for index in range(marker.end(), len(value)):
        character = value[index]
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if quote_char is not None:
            if character == quote_char:
                quote_char = None
            continue
        if character in "\"'":
            quote_char = character
        elif character == "]":
            closing_index = index
            break

    if closing_index is None:
        raise ValueError('The ["ALTERNATIVES": ...] block is missing its closing bracket.')

    body = value[marker.end() : closing_index]
    alternatives = []
    cursor = 0
    entry_pattern = re.compile(
        r"\{\s*([\"'])(.*?)\1\s*:\s*(?:([\"'])(.*?)\3|([^,{}]+?))\s*\}",
        re.DOTALL,
    )
    while True:
        while cursor < len(body) and body[cursor].isspace():
            cursor += 1
        if cursor >= len(body):
            break
        if alternatives:
            if body[cursor] != ",":
                raise ValueError("Separate alternative entries with commas.")
            cursor += 1
            while cursor < len(body) and body[cursor].isspace():
                cursor += 1
            if cursor >= len(body):
                break  # Permit a trailing comma.

        match = entry_pattern.match(body, cursor)
        if match is None:
            raise ValueError(
                'Use alternatives like {"Display name": alias-name}.'
            )
        alternative_name = match.group(2).strip()
        alternative_alias = (match.group(4) or match.group(5) or "").strip()
        if (
            len(alternative_alias) >= 2
            and alternative_alias[0] == alternative_alias[-1]
            and alternative_alias[0] in "\"'"
        ):
            alternative_alias = alternative_alias[1:-1].strip()
        alternatives.append(
            {"name": alternative_name, "alias": alternative_alias.casefold()}
        )
        cursor = match.end()

    if not alternatives:
        raise ValueError("Configure at least one named alternative alias.")
    alias_value = (value[: marker.start()] + " " + value[closing_index + 1 :]).strip()
    if re.search(r"\[\s*[\"']?alternatives[\"']?\s*:", alias_value, re.IGNORECASE):
        raise ValueError("Configure only one ALTERNATIVES block per autoreply rule.")
    return alias_value, alternatives


AUTOREPLY_DISPLAY_NAME_LIMIT = 200
AUTOREPLY_ADDITIONAL_INFO_LIMIT = 2_000
AUTOREPLY_TOTAL_CHOICE_LIMIT = 50


def _extract_autoreply_additional_info(
    value: str,
) -> typing.Tuple[str, typing.Optional[str]]:
    """Remove and parse an optional trailing ``ADDITIONAL INFO`` guidance block."""
    marker_pattern = re.compile(
        r"\[\s*[\"']?additional\s+info[\"']?\s*:\s*",
        re.IGNORECASE | re.DOTALL,
    )
    marker = marker_pattern.search(value)
    if marker is None:
        return value, None

    quote_char = None
    escaped = False
    closing_index = None
    for index in range(marker.end(), len(value)):
        character = value[index]
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if quote_char is not None:
            if character == quote_char:
                quote_char = None
            continue
        if character in "\"'":
            quote_char = character
        elif character == "]":
            closing_index = index
            break

    if closing_index is None:
        raise ValueError('The ["ADDITIONAL INFO": ...] block is missing its closing bracket.')
    if value[closing_index + 1 :].strip():
        raise ValueError('The ["ADDITIONAL INFO": ...] block must be at the end of the rule.')
    if marker_pattern.search(value[: marker.start()]):
        raise ValueError("Configure only one ADDITIONAL INFO block per autoreply rule.")

    raw_info = value[marker.end() : closing_index].strip()
    if not raw_info:
        raise ValueError("ADDITIONAL INFO cannot be empty.")
    if len(raw_info) >= 2 and raw_info[0] == raw_info[-1] and raw_info[0] in "\"'":
        if raw_info[0] == '"':
            try:
                json_compatible = (
                    raw_info[0]
                    + raw_info[1:-1]
                    .replace("\r", "\\r")
                    .replace("\n", "\\n")
                    .replace("\t", "\\t")
                    + raw_info[-1]
                )
                additional_info = json.loads(json_compatible)
            except json.JSONDecodeError as exc:
                raise ValueError("ADDITIONAL INFO contains invalid quoted text.") from exc
        else:
            additional_info = raw_info[1:-1].replace("\\'", "'").replace("\\\\", "\\")
    else:
        additional_info = raw_info

    additional_info = str(additional_info).strip()
    if not additional_info:
        raise ValueError("ADDITIONAL INFO cannot be empty.")
    if len(additional_info) > AUTOREPLY_ADDITIONAL_INFO_LIMIT:
        raise ValueError(
            f"ADDITIONAL INFO cannot exceed {AUTOREPLY_ADDITIONAL_INFO_LIMIT} characters."
        )
    return value[: marker.start()].strip(), additional_info


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
    rule_value, additional_info = _extract_autoreply_additional_info(
        value_match.group(2).strip()
    )
    alias_value, alternatives = _extract_autoreply_alternatives(rule_value)
    alias_name = alias_value.strip()
    if len(alias_name) >= 2 and alias_name[0] == alias_name[-1] and alias_name[0] in "\"'":
        alias_name = alias_name[1:-1].strip()
    alias_name = alias_name.casefold()

    if not display_name:
        raise ValueError("The autoreply display name cannot be empty.")
    if len(display_name) > AUTOREPLY_DISPLAY_NAME_LIMIT:
        raise ValueError(
            "The autoreply display name cannot be longer than "
            f"{AUTOREPLY_DISPLAY_NAME_LIMIT} characters."
        )
    if not triggers:
        raise ValueError("Configure at least one must-mention word or phrase.")
    if len(triggers) > 25:
        raise ValueError("Configure no more than 25 must-mention words or phrases.")
    if any(len(item) > 100 for item in triggers):
        raise ValueError("Must-mention words or phrases cannot exceed 100 characters.")
    if not alias_name or len(alias_name) > 120:
        raise ValueError("Provide a valid alias name of no more than 120 characters.")

    seen_names = {display_name.casefold()}
    for alternative in alternatives:
        alternative_name = alternative["name"]
        alternative_alias = alternative["alias"]
        if not alternative_name or len(alternative_name) > AUTOREPLY_DISPLAY_NAME_LIMIT:
            raise ValueError(
                "Alternative display names must be between 1 and "
                f"{AUTOREPLY_DISPLAY_NAME_LIMIT} characters."
            )
        if not alternative_alias or len(alternative_alias) > 120:
            raise ValueError("Alternative alias names must be between 1 and 120 characters.")
        normalized_name = alternative_name.casefold()
        if normalized_name in seen_names:
            raise ValueError("Every primary and alternative display name must be unique.")
        seen_names.add(normalized_name)
    if len(alternatives) > 24:
        raise ValueError("Configure no more than 24 alternatives for one autoreply rule.")

    result = {"name": display_name, "triggers": triggers, "alias": alias_name}
    if alternatives:
        result["alternatives"] = alternatives
    if additional_info is not None:
        result["additional_info"] = additional_info
    return result


def format_autoreply_rule_spec(key: str, entry: typing.Any) -> str:
    """Render an autoreply as copyable arguments for ``?autoreply edit``."""

    def quote(value: typing.Any) -> str:
        return json.dumps(str(value), ensure_ascii=False)

    if not isinstance(entry, dict):
        return f"{quote(key)} {entry}"

    display_name = str(entry.get("name") or key)
    triggers = ", ".join(quote(trigger) for trigger in (entry.get("triggers") or []))
    alias_name = quote(entry.get("alias") or key)
    result = (
        f'{quote("NAME: " + display_name)} '
        f'["MUST MENTION TO CHECK": {triggers}] {alias_name}'
    )
    alternatives = [
        alternative
        for alternative in (entry.get("alternatives") or [])
        if isinstance(alternative, dict)
    ]
    if alternatives:
        formatted_alternatives = ", ".join(
            "{" + quote(alternative.get("name") or "") + ": "
            + quote(alternative.get("alias") or "") + "}"
            for alternative in alternatives
        )
        result += f' ["ALTERNATIVES": {formatted_alternatives}]'
    additional_info = str(entry.get("additional_info") or "").strip()
    if additional_info:
        result += f' ["ADDITIONAL INFO": {quote(additional_info)}]'
    return result
