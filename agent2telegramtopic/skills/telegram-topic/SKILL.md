---
name: telegram-topic
description: Name and iconify the current Telegram topic. Use when the bridge's first message of a new topic carries the "(New topic: ...)" note, and whenever the user asks to rename the topic or change its icon.
---

# Telegram topic name and icon

You are running in a Telegram topic managed by Agent2TelegramTopic. Your working directory is the
topic's directory.

**When to name the topic**

- Automatically, only when the user's message ends with the bridge's note
  `(New topic: ...)`. The bridge adds it to the first message of a topic it has just seen created.
- Never because the session merely looks empty. After `/new` or `/clear` the conversation is blank
  but the topic already has its name; without the note, leave it alone.
- On an explicit request from the user, at any time: rename it or change its icon as asked.

**How**

1. Pick a short name for the message's subject, in the language the user writes in.
2. Pick an icon emoji from the allowed topic icons.
3. Run: `python3 -m agent2telegramtopic topic-edit "<name>" "<emoji>"`
   (use the exact command given in the bridge's note if it differs, for example with a PYTHONPATH prefix).
4. `ok` means done. If the emoji is not allowed the command exits non-zero and lists the allowed
   ones; choose one of those and run it again.
5. Mention the new name in one short sentence, then answer the user's message normally.

The bot token is read from the bridge's config by the command; never print or look for it.
