import re


AUTOFILL_STAFF_USER_ID = 1458951643377963178
AUTOFILL_CLOSING = (
    "Can I assist you with anything else?\n\n"
    "Progamerzzzz8447 | David S.\n"
    "`TUI Airways — Human Resources`"
)


def expand_staff_reply_markers(message: str, recipient_name: str) -> str:
    """Expand exact leading ``hi`` and trailing ``bye`` markers in a staff reply."""
    text = str(message or "").strip()
    has_greeting = re.match(r"^hi(?:\s|$)", text, flags=re.IGNORECASE) is not None
    has_closing = re.search(r"(?:^|\s)bye$", text, flags=re.IGNORECASE) is not None

    if has_greeting:
        text = re.sub(r"^hi(?:\s+|$)", "", text, count=1, flags=re.IGNORECASE).strip()
    if has_closing:
        text = re.sub(r"(?:^|\s+)bye$", "", text, count=1, flags=re.IGNORECASE).strip()

    sections = []
    if has_greeting:
        sections.append(f"Hello {recipient_name},\n\nThank you for writing in.")
    if text:
        sections.append(text)
    if has_closing:
        sections.append(AUTOFILL_CLOSING)
    return "\n\n".join(sections)
