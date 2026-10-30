from agent2telegramtopic.routing import (
    GENERAL_THREAD_ID,
    LaneKey,
    is_allowed,
    reply_params,
    route_update,
    topic_event,
)


def update(text="hello", *, thread_id=None, chat_type="private", sender_bot=False):
    message = {
        "chat": {"id": 123, "type": chat_type},
        "from": {"id": 9, "is_bot": sender_bot},
        "text": text,
    }
    if thread_id is not None:
        message["message_thread_id"] = thread_id
    return {"message": message}


def test_thread_ids_in_same_chat_have_distinct_lane_keys():
    a = route_update(update(thread_id=10))
    b = route_update(update(thread_id=11))
    assert a.key == LaneKey(123, 10)
    assert b.key == LaneKey(123, 11)
    assert a.key != b.key


def test_sender_allowlist_requires_allowed_private_sender():
    allowed = frozenset({9})
    assert is_allowed(update(), allowed)
    assert not is_allowed(update() | {"message": update()["message"] | {"from": {"id": 10}}}, allowed)
    assert not is_allowed(update(chat_type="group"), allowed)
    assert not is_allowed({"message": {"chat": {"id": 123, "type": "private"}}}, allowed)


def test_missing_thread_routes_to_general_and_omits_reply_thread():
    result = route_update(update())
    assert result.key == LaneKey(123, GENERAL_THREAD_ID)
    assert reply_params(result.key) == {"chat_id": 123}


def test_reply_params_include_non_general_thread():
    assert reply_params(LaneKey(123, 12)) == {"chat_id": 123, "message_thread_id": 12}


def test_commands_are_recognized_only_as_whole_text():
    assert route_update(update("/new")).command == "new"
    assert route_update(update("/close")).command == "close"
    assert route_update(update("/new@ExampleBot")).command == "new"
    assert route_update(update("please use /new now")).command is None


def test_unsupported_message_kinds_are_ignored():
    assert route_update(update(chat_type="group")) is None
    assert route_update(update(sender_bot=True)) is None
    assert route_update({"edited_message": update("edited text")["message"]}).text == "edited text"
    non_text_edit = update()["message"] | {"text": None, "photo": [{"file_id": "x"}]}
    assert route_update({"edited_message": non_text_edit}).key == LaneKey(123, 1)


def test_topic_created_service_event_is_reported_but_not_routed():
    service = {"chat": {"id": 123, "type": "private"}, "message_thread_id": 42,
               "forum_topic_created": {"name": "Build"}}
    update_dict = {"message": service}
    assert route_update(update_dict) is None
    assert topic_event(update_dict) == (LaneKey(123, 42), "created")


def test_media_and_caption_messages_route_by_topic_including_edits():
    for payload in ({"voice": {"file_id": "v"}}, {"audio": {"file_id": "a"}},
                    {"photo": [{"file_id": "p"}]},
                    {"document": {"file_id": "d"}}, {"photo": [{"file_id": "p"}], "caption": "look"}):
        msg = {"chat": {"id": 123, "type": "private"}, "from": {"id": 9},
               "message_thread_id": 37, **payload}
        routed = route_update({"message": msg})
        assert routed.key == LaneKey(123, 37)
        assert routed.text == payload.get("caption", "")
        edited = route_update({"edited_message": msg})
        assert edited.key == routed.key
        assert edited.text == routed.text


def test_sticker_does_not_route():
    assert route_update({"message": {"chat": {"id": 123, "type": "private"},
                         "from": {"id": 9}, "sticker": {"file_id": "s"}}}) is None
