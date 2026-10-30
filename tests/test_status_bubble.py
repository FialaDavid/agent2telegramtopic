"""Live one-line tool/status bubble (Phase B3): port of agent2telegram tests/test_tui_scrape.py and
tests/test_attach_backstop.py StatusBubbleLifetimeTests, adapted to LaneBridge/LaneKey, plus the
per-lane behaviour (persisted bubble file, orphan cleanup, own-thread delivery)."""

import json
import threading
import time

from agent2telegramtopic.lane import LaneBridge, TUI_BUBBLE_GRACE, _extract_tui_tools
from agent2telegramtopic.readers import Ev
from agent2telegramtopic.routing import LaneKey
from agent2telegramtopic.telegram import TelegramClient
from telegram_fakes import TransportOpener

# A real Codex TUI capture: agent text on bullet lines, a command on a bullet line with its
# output nested under "└", a file read, and trailing UI chrome.
PANE_DISK = """\
  [TG] How much disk space is left?
● Let me check the free space on the main partition.
● Ran df -h /
  └ Filesystem      Size  Used Avail Use% Mounted on
    /dev/sda1        48G  2.3G   46G   5% /
● Read /etc/hosts
● The main disk / has 46 GB free out of 48 GB.
> 0;10;1c
  gpt-5.5 default · ~
"""

PANE_WEB = """\
● Let me check the web, because figures like this are often miscopied.
● Searching the web
● Searched the web for example corporation founding year official
● I could not find a reliably sourced figure.
"""


# ---- ported from a2t test_tui_scrape.py ------------------------------------------------

def test_system_call_on_bullet_line():
    assert "🛠️ Ran df -h /" in _extract_tui_tools(PANE_DISK)


def test_file_read_bullet_line():
    assert "📄 Read /etc/hosts" in _extract_tui_tools(PANE_DISK)


def test_command_output_is_not_a_bubble():
    out = _extract_tui_tools(PANE_DISK)
    assert not any("Filesystem" in s or "/dev/sda1" in s for s in out)


def test_agent_text_is_not_a_bubble():
    out = _extract_tui_tools(PANE_DISK)
    assert not any("Let me check" in s or "free out of" in s for s in out)


def test_web_search_query_extracted():
    out = _extract_tui_tools(PANE_WEB)
    assert any(s.startswith("🔎 Web search:") and "example corporation" in s for s in out)


def test_web_search_in_progress():
    assert _extract_tui_tools("● Searching the web") == ["🔎 Searching the web"]


def test_stale_lines_not_reemitted():
    seen = set(_extract_tui_tools(PANE_DISK))
    assert [s for s in _extract_tui_tools(PANE_DISK) if s not in seen] == []


def test_new_call_after_seed_is_emitted():
    seen = set(_extract_tui_tools(PANE_DISK))
    assert [s for s in _extract_tui_tools(PANE_DISK + "● Ran echo hello\n") if s not in seen] == ["🛠️ Ran echo hello"]


def test_empty_pane():
    assert _extract_tui_tools("") == []


# ---- lane fixtures ---------------------------------------------------------------------

class Telegram:
    def __init__(self):
        self.events = []
        self.next_id = 55

    def send_message(self, key, text, **kwargs):
        self.events.append(("send", key, text))
        self.next_id += 1
        return [self.next_id]

    def send_plain_id(self, key, text, **kwargs):
        self.events.append(("bubble", key, text, kwargs))
        self.next_id += 1
        return self.next_id

    def edit_plain(self, key, message_id, text, **kwargs):
        self.events.append(("edit", key, message_id, text))

    def delete_message(self, key, message_id):
        self.events.append(("delete", key, message_id))

    def send_chat_action(self, key, action):
        pass


class Session:
    def __init__(self, pane=""):
        self.pane = pane
        self.injected = []

    def inject(self, text):
        self.injected.append(text)

    def _capture(self):
        return self.pane


def make_lane(tmp_path, key=LaneKey(44, 12), client=None, agent=None):
    state = tmp_path / str(key.thread_id)
    lane = LaneBridge(client or Telegram(), key, state / "sent", state / "queue", agent=agent)
    lane._session = Session()
    lane._stop.wait = lambda _: None
    lane._retry_last_assistant_text = lambda: None
    return lane


def kinds(tg):
    return [e[0] for e in tg.events]


# ---- ported from a2t StatusBubbleLifetimeTests -----------------------------------------

def test_no_bubble_is_created_outside_a_turn(tmp_path):
    tg = Telegram()
    lane = make_lane(tmp_path, client=tg)
    lane._status_push("✏️ Editing MEMORY.md")
    assert lane._status["mid"] is None
    assert tg.events == []


def test_bubble_is_still_created_during_a_turn(tmp_path):
    tg = Telegram()
    lane = make_lane(tmp_path, client=tg)
    lane._turn_active.set()
    lane._status_push("📄 Read foo.py")
    assert lane._status["mid"] == 56
    assert tg.events[0][:2] == ("bubble", LaneKey(44, 12))
    assert "Read foo.py" in tg.events[0][2]


# ---- bubble behaviour -------------------------------------------------------------------

def test_consecutive_tool_calls_edit_one_bubble_in_place(tmp_path):
    tg = Telegram()
    lane = make_lane(tmp_path, client=tg, agent="claude-code")
    lane._turn_active.set()
    lane._turn_from_tg = True
    lane._handle_event(Ev("tool", text="📄 Read a.py", key="t1"))
    lane._handle_event(Ev("tool", text="✏️ Edit a.py", key="t2"))
    lane._handle_event(Ev("tool", text="✏️ Edit a.py", key="t2"))      # repeat is ignored
    assert kinds(tg) == ["bubble", "edit"]
    assert tg.events[1][2] == 56 and "Edit a.py" in tg.events[1][3]


def test_the_bubble_id_is_persisted_in_this_lanes_state_dir_and_removed_with_it(tmp_path):
    tg = Telegram()
    lane = make_lane(tmp_path, client=tg)
    lane._turn_active.set()
    lane._status_push("📄 Read a.py")
    assert (tmp_path / "12" / "status_bubble").read_text() == "56"
    lane._status_clear()
    assert not (tmp_path / "12" / "status_bubble").exists()
    assert tg.events[-1] == ("delete", LaneKey(44, 12), 56)


def test_bubble_is_deleted_before_the_next_text_so_it_trails_below_it(tmp_path):
    tg = Telegram()
    lane = make_lane(tmp_path, client=tg, agent="claude-code")
    lane._turn_active.set()
    lane._turn_from_tg = True
    lane._handle_event(Ev("tool", text="📄 Read a.py", key="t1"))
    lane._handle_event(Ev("text", text="[TG] intro", key="u1"))
    lane._flush_pending()
    assert kinds(tg)[:3] == ["bubble", "delete", "send"]
    lane._handle_event(Ev("tool", text="📄 Read b.py", key="t3"))
    assert kinds(tg)[-1] == "bubble"           # re-created below the text


def test_bubble_is_deleted_at_the_end_of_the_turn(tmp_path):
    tg = Telegram()
    lane = make_lane(tmp_path, client=tg)
    lane._drain_transcript = lambda: None
    lane._begin_turn()
    lane._turn_text_sent = True
    lane._status_push("📄 Read a.py")
    lane._finish_turn()
    assert tg.events[-1] == ("delete", LaneKey(44, 12), 56)
    assert lane._status == {"mid": None, "shown": ""}
    assert not (tmp_path / "12" / "status_bubble").exists()


def test_codex_tool_records_are_left_to_the_tui_scraper(tmp_path):
    tg = Telegram()
    lane = make_lane(tmp_path, client=tg, agent="codex")
    lane._turn_active.set()
    lane._turn_from_tg = True
    lane._handle_event(Ev("tool", text="🛠️ Ran ls", key="t1"))
    assert tg.events == []


def test_orphan_bubble_from_a_dead_run_is_deleted_when_the_lane_starts(tmp_path):
    tg = Telegram()
    lane = make_lane(tmp_path, client=tg)
    (tmp_path / "12").mkdir(parents=True)
    (tmp_path / "12" / "status_bubble").write_text("777")
    lane._prepare_start()
    assert tg.events == [("delete", LaneKey(44, 12), 777)]
    assert not (tmp_path / "12" / "status_bubble").exists()


# ---- Codex TUI scrape + the _turn_text_sent gate ----------------------------------------

def run_scrape_once(lane):
    original = lane._stop.wait
    def stop_after_one_pass(_):
        lane._stop.set()
    lane._stop.wait = stop_after_one_pass
    lane._tui_scrape_loop()
    lane._stop.clear()
    lane._stop.wait = original


def codex_turn(tmp_path, tg, pane_before, pane_now):
    lane = make_lane(tmp_path, client=tg, agent="codex")
    lane._session.pane = pane_before
    lane._begin_turn()
    lane._session.pane = pane_now
    return lane


def test_scraper_seeds_with_stale_lines_at_turn_start_and_shows_only_new_calls(tmp_path):
    tg = Telegram()
    lane = codex_turn(tmp_path, tg, PANE_DISK, PANE_DISK + "● Ran echo hello\n")
    lane._turn_text_sent = True
    run_scrape_once(lane)
    assert kinds(tg) == ["bubble"]
    assert "Ran echo hello" in tg.events[0][2]


def test_scraper_holds_the_bubble_until_the_intro_text_is_forwarded(tmp_path):
    tg = Telegram()
    lane = codex_turn(tmp_path, tg, "", "● Ran ls\n")
    run_scrape_once(lane)
    assert tg.events == []                      # intro text not out yet, grace not elapsed
    lane._turn_text_sent = True
    run_scrape_once(lane)
    assert kinds(tg) == ["bubble"]


def test_scraper_shows_the_bubble_after_the_grace_even_without_intro_text(tmp_path):
    tg = Telegram()
    lane = codex_turn(tmp_path, tg, "", "● Ran ls\n")
    lane._turn_started = time.monotonic() - TUI_BUBBLE_GRACE - 1
    run_scrape_once(lane)
    assert kinds(tg) == ["bubble"]


# ---- two lanes, own threads (real client, real request parameters) -----------------------

def test_two_lanes_bubbles_go_to_their_own_topic(tmp_path):
    calls = []
    def transport(method, params):
        calls.append((method, params))
        return {"ok": True, "result": {"message_id": 100 + len(calls)}}
    client = TelegramClient("secret", opener=TransportOpener(transport))
    first = make_lane(tmp_path, key=LaneKey(44, 12), client=client)
    second = make_lane(tmp_path, key=LaneKey(44, 13), client=client)
    for lane in (first, second):
        lane._turn_active.set()
        lane._status_push("📄 Read a.py")
        lane._status_push("✏️ Edit a.py")
        lane._status_clear()
    by_thread = {}
    for method, params in calls:
        by_thread.setdefault(params["message_thread_id"], []).append(method)
    assert by_thread == {12: ["sendMessage", "editMessageText", "deleteMessage"],
                         13: ["sendMessage", "editMessageText", "deleteMessage"]}
    assert all(p["chat_id"] == 44 for _, p in calls)
    send = next(p for m, p in calls if m == "sendMessage")
    assert send["parse_mode"] == "HTML" and send["text"].startswith("<i>")


def test_starting_a_codex_lane_starts_its_tui_scraper_and_a_claude_lane_does_not(tmp_path):
    from agent2telegramtopic.multiplexer import Multiplexer
    started = {}
    def factory(key):
        agent = "codex" if key.thread_id == 12 else "claude-code"
        lane = make_lane(tmp_path, key=key, agent=agent)
        lane._outbound_loop = lambda: None
        lane._typing_loop = lambda: None
        started[key.thread_id] = threading.Event()
        lane._tui_scrape_loop = started[key.thread_id].set
        return lane
    mux = Multiplexer(Telegram(), {8}, tmp_path / "bot", factory, lane_states_root=tmp_path / "lanes")
    mux._start_lane(LaneKey(44, 12))
    mux._start_lane(LaneKey(44, 13))
    assert started[12].wait(2)
    time.sleep(0.05)
    assert not started[13].is_set()
