import io
import re
import zipfile


def ticket_log_filename(log, index):
    """Return a unique filesystem-safe JSON filename for one ticket log."""
    recipient = log.get("recipient") or {}
    identity = log.get("key") or log.get("channel_id") or recipient.get("id") or f"ticket-{index}"
    identity = re.sub(r"[^A-Za-z0-9._-]+", "-", str(identity)).strip("-.") or f"ticket-{index}"
    return f"{index:06d}-{identity[:120]}.json"


def build_ticket_log_zip(entries):
    """Build one ZIP payload from ``(filename, bytes)`` ticket-log entries."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for filename, payload in entries:
            archive.writestr(filename, payload)
    return output.getvalue()
