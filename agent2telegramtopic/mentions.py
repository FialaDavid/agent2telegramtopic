# Ported from agent2telegram mentions.py (MIT)
"""Optional group mention gate."""
import re


def accepts_message(cfg, msg):
    if not getattr(cfg, "require_mention", False):
        return True
    if (msg.get("chat") or {}).get("type") not in ("group", "supergroup"):
        return True
    username = (getattr(cfg, "bot_username", "") or "").lstrip("@").lower()
    if not username:
        return False
    sender = ((msg.get("reply_to_message") or {}).get("from") or {})
    if sender.get("is_bot") is True and str(sender.get("username", "")).lower() == username:
        return True
    text = msg.get("text") or msg.get("caption") or ""
    mention = re.search(r"(?<![\w@])@" + re.escape(username) + r"(?!\w)", text, re.IGNORECASE)
    command = re.match(r"^/[a-z0-9_]+@" + re.escape(username) + r"(?:\s|$)", text, re.IGNORECASE)
    return bool(mention or command)
