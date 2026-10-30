# Ported from agent2telegram notices.py (MIT)
"""Operational notice text used by the inbound multiplexer."""


def busy_text(cfg):
    if getattr(cfg, 'language', 'en') == 'cs':
        return '⚡ Zprávu mám. Dostanu se k ní, jakmile dokončím rozpracovaný úkol.'
    return "⚡ Got your message. I'll get to it as soon as I finish what I'm on."
