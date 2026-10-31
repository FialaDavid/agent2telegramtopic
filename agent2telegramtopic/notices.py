# Ported from agent2telegram notices.py (MIT)
"""Operational notice text used by the inbound multiplexer."""


def busy_text(cfg):
    if getattr(cfg, 'language', 'en') == 'cs':
        return '⚡ Zprávu mám. Dostanu se k ní, jakmile dokončím rozpracovaný úkol.'
    return "⚡ Got your message. I'll get to it as soon as I finish what I'm on."


TOPICS_DISABLED = (
    "Topics (Threaded Mode) are not enabled for this bot. Open @BotFather → Mini App (Open) → "
    "select the bot → Bot Settings → Threaded Mode → On. Then open the chat with the bot again. "
    "Until then all messages go to one conversation."
)
