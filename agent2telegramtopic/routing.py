from dataclasses import dataclass
import re


GENERAL_THREAD_ID = 1


@dataclass(frozen=True)
class LaneKey:
    chat_id: int
    thread_id: int


@dataclass(frozen=True)
class InboundMessage:
    key: LaneKey
    text: str
    topic_name: str | None
    command: str | None


_COMMAND_RE = re.compile(r"^/(new|close)(?:@[^\s]+)?$")
_TOPIC_EVENTS = {
    "forum_topic_created": "created",
    "forum_topic_closed": "closed",
    "forum_topic_edited": "edited",
    "forum_topic_deleted": "deleted",
}


def is_allowed(update: dict, allowed_users) -> bool:
    # Delta (B2): a reaction update names its chat and reacting user directly.
    reaction = update.get("message_reaction")
    if isinstance(reaction, dict):
        chat = reaction.get("chat")
        user = reaction.get("user")
        return (isinstance(chat, dict) and chat.get("type") == "private"
                and isinstance(user, dict) and user.get("id") in allowed_users)
    message = update.get("message")
    if not isinstance(message, dict):
        message = update.get("edited_message")
    if not isinstance(message, dict):
        return False
    chat = message.get("chat")
    sender = message.get("from")
    return (isinstance(chat, dict) and chat.get("type") == "private"
            and isinstance(sender, dict) and sender.get("id") in allowed_users)


def _message_key(message: dict) -> LaneKey | None:
    chat = message.get("chat")
    if not isinstance(chat, dict) or "id" not in chat:
        return None
    return LaneKey(chat["id"], message.get("message_thread_id", GENERAL_THREAD_ID))


def topic_event(update: dict) -> tuple[LaneKey, str] | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    key = _message_key(message)
    if key is None:
        return None
    for field, event in _TOPIC_EVENTS.items():
        if field in message:
            return key, event
    return None


def route_update(update: dict) -> InboundMessage | None:
    message = update.get("message")
    # Delta (9a): route supported media updates and edits through the same lane as text.
    if not isinstance(message, dict):
        message = update.get("edited_message")
    if not isinstance(message, dict):
        return None
    text = message.get("text")
    caption = message.get("caption")
    # Delta (9.3): text, caption, voice, audio, photo and document updates route by topic.
    media = any(message.get(kind) for kind in ("voice", "audio", "photo", "document"))
    if not isinstance(text, str) and not isinstance(caption, str) and not media:
        return None
    chat = message.get("chat")
    if not isinstance(chat, dict) or chat.get("type") != "private":
        return None
    sender = message.get("from")
    if isinstance(sender, dict) and sender.get("is_bot"):
        return None
    key = _message_key(message)
    if key is None:
        return None
    topic_created = message.get("forum_topic_created")
    if not isinstance(topic_created, dict):
        reply = message.get("reply_to_message")
        topic_created = reply.get("forum_topic_created") if isinstance(reply, dict) else None
    topic_name = topic_created.get("name") if isinstance(topic_created, dict) else None
    content = text if isinstance(text, str) else caption if isinstance(caption, str) else ""
    match = _COMMAND_RE.fullmatch(content)
    return InboundMessage(key, content, topic_name, match.group(1) if match else None)


def reply_params(key: LaneKey) -> dict:
    result = {"chat_id": key.chat_id}
    if key.thread_id != GENERAL_THREAD_ID:
        result["message_thread_id"] = key.thread_id
    return result
