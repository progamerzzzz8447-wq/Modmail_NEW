from functools import lru_cache
import re
import typing


ABUSE_AUTO_CLOSE_MESSAGE = (
    "Unfortunately, we do not tolerate abuse directed towards our staff team. This ticket has "
    "now been automatically closed.\n\n"
    "You may open a new ticket if you still require assistance. However, any further abusive "
    "behaviour will result in you being blocked from contacting support."
)

_LEET_CHARACTERS = {
    "a": r"[a@4]",
    "e": r"[e3]",
    # Do not treat q as an obfuscated g: a custom term such as "fag" would otherwise match the
    # ordinary abbreviation "FAQ" and automatically close legitimate support tickets.
    "g": r"[g69]",
    "i": r"[i1!|]",
    "o": r"[o0]",
    "s": r"[s$5]",
    "t": r"[t7+]",
}


def _obfuscated_word(word: str) -> str:
    """Build a whole-word pattern allowing common leetspeak and punctuation separators."""
    letters = [
        f"(?:{_LEET_CHARACTERS.get(character, re.escape(character))})+"
        for character in word
    ]
    return r"(?<![a-z0-9])" + r"[\W_]*".join(letters) + r"(?![a-z0-9])"


def normalize_custom_abuse_term(term: str) -> str:
    """Normalize an administrator-supplied plain word or phrase for storage and matching."""
    words = re.findall(r"[^\W_]+", str(term or "").casefold(), re.UNICODE)
    return " ".join(words)


@lru_cache(maxsize=64)
def _compile_custom_patterns(terms: typing.Tuple[str, ...]) -> typing.Tuple[re.Pattern, ...]:
    patterns = []
    for term in terms:
        words = term.split()
        if not words:
            continue
        phrase_pattern = r"[\W_]+".join(_obfuscated_word(word) for word in words)
        patterns.append(re.compile(phrase_pattern, re.IGNORECASE))
    return tuple(patterns)


# Keep short or ambiguous profanity out of this list. These patterns are reserved for the
# explicitly blocked term and severe slurs/threats where an automatic close is proportionate.
_ABUSE_PATTERNS: typing.Tuple[re.Pattern, ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?<![a-z0-9])g[o0]{2}n(?![a-z0-9])",
        r"(?<![a-z0-9])n[\W_]*words?(?![a-z0-9])",
        _obfuscated_word("nigger"),
        _obfuscated_word("niggers"),
        _obfuscated_word("nigga"),
        _obfuscated_word("niggas"),
        _obfuscated_word("faggot"),
        _obfuscated_word("faggots"),
        _obfuscated_word("retard"),
        _obfuscated_word("retards"),
        _obfuscated_word("retarded"),
        _obfuscated_word("chink"),
        _obfuscated_word("chinks"),
        _obfuscated_word("spic"),
        _obfuscated_word("spics"),
        _obfuscated_word("kike"),
        _obfuscated_word("kikes"),
        _obfuscated_word("paki"),
        _obfuscated_word("pakis"),
        _obfuscated_word("tranny"),
        _obfuscated_word("trannies"),
        _obfuscated_word("coon"),
        _obfuscated_word("coons"),
        _obfuscated_word("cunt"),
        _obfuscated_word("cunts"),
        r"(?<![a-z0-9])k[\W_]*y[\W_]*s(?![a-z0-9])",
        r"(?<![a-z0-9])kill[\W_]+yourself(?![a-z0-9])",
        r"(?<![a-z0-9])go[\W_]+fuck[\W_]+yourself(?![a-z0-9])",
        r"(?<![a-z0-9])fuck[\W_]+you(?![a-z0-9])",
    )
)


def contains_abusive_language(
    text: str,
    *,
    extra_terms: typing.Iterable[str] = (),
) -> bool:
    """Return whether text contains a built-in or administrator-added blocked term."""
    value = str(text or "")
    if any(pattern.search(value) is not None for pattern in _ABUSE_PATTERNS):
        return True

    if isinstance(extra_terms, str):
        extra_terms = (extra_terms,)
    normalized_terms = tuple(
        dict.fromkeys(
            normalized
            for normalized in (normalize_custom_abuse_term(term) for term in extra_terms)
            if normalized
        )
    )
    return any(
        pattern.search(value) is not None
        for pattern in _compile_custom_patterns(normalized_terms)
    )
