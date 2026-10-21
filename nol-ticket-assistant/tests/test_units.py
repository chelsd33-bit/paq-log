import logging

import pytest

from nolassist.config import BookerInfo, BookingRequest, SeatPreference
from nolassist.logging_setup import get_logger, redactor
from nolassist.profile import live_profile, mock_profile
from nolassist.seats import Seat, SeatStateUnclear, choose_seats, parse_seat
from nolassist.stages import InvalidTransition, Stage, StageMachine
from nolassist.storage import Storage


# ---- 상태 머신 -------------------------------------------------------------
def test_forward_path_and_terminal():
    m = StageMachine()
    for s in (Stage.PAGE_PREP, Stage.SHOW_SELECT, Stage.SEAT_SELECT, Stage.BOOKER_INFO, Stage.ORDER_SUMMARY):
        m.transition(s)
    assert m.stage is Stage.ORDER_SUMMARY
    with pytest.raises(InvalidTransition):
        m.transition(Stage.STOPPED)  # 종료 상태에서는 더 이상 전이하지 않음


def test_cannot_skip_stages():
    m = StageMachine()
    m.transition(Stage.PAGE_PREP)
    with pytest.raises(InvalidTransition):
        m.transition(Stage.SEAT_SELECT)


def test_user_action_returns_to_previous():
    m = StageMachine()
    m.transition(Stage.PAGE_PREP)
    m.transition(Stage.SHOW_SELECT)
    m.transition(Stage.USER_ACTION)
    with pytest.raises(InvalidTransition):
        m.transition(Stage.SEAT_SELECT)
    m.resume()
    assert m.stage is Stage.SHOW_SELECT


def test_stop_allowed_from_user_action():
    m = StageMachine()
    m.transition(Stage.PAGE_PREP)
    m.transition(Stage.USER_ACTION)
    m.transition(Stage.STOPPED)
    assert m.stage is Stage.STOPPED


# ---- 좌석 선정 -------------------------------------------------------------
def _seats():
    out = []
    for row, grade in [(1, "VIP"), (2, "VIP"), (3, "R")]:
        for col in (1, 2, 4, 5, 6):
            out.append(Seat(f"{row}-{col}", grade, row, col))
    return out


def test_choose_adjacent_front_first():
    got = choose_seats(_seats(), SeatPreference(grades=["VIP"], quantity=3))
    assert [s.seat_id for s in got] == ["1-4", "1-5", "1-6"]


def test_choose_grade_priority_fallback():
    got = choose_seats(_seats(), SeatPreference(grades=["S", "R"], quantity=2))
    assert [s.grade for s in got] == ["R", "R"]


def test_choose_back_rows_and_max_row():
    assert choose_seats(_seats(), SeatPreference(grades=["VIP"], quantity=2, prefer_front=False))[0].row == 2
    assert choose_seats(_seats(), SeatPreference(grades=["R"], quantity=1, max_row=2)) is None


def test_choose_none_when_no_adjacent_block():
    seats = [Seat("a", "VIP", 1, 1), Seat("b", "VIP", 1, 3)]
    assert choose_seats(seats, SeatPreference(grades=["VIP"], quantity=2)) is None
    assert len(choose_seats(seats, SeatPreference(grades=["VIP"], quantity=2, adjacent=False))) == 2


def test_parse_seat_unclear():
    with pytest.raises(SeatStateUnclear):
        parse_seat({"id": "x", "grade": "VIP", "row": None, "col": "3"})


# ---- 입력 검증 -------------------------------------------------------------
def test_request_validation():
    with pytest.raises(ValueError):
        BookingRequest(url="nol.yanolja.com", date="d", round="1").validate()
    with pytest.raises(ValueError):
        BookingRequest(url="https://x", date="d", round="1", seat=SeatPreference(quantity=9)).validate()


def test_safe_dict_excludes_booker():
    req = BookingRequest(url="https://x", date="d", round="1", booker=BookerInfo("홍길동", "01012345678", "a@b.c"))
    d = req.to_safe_dict()
    assert "booker" not in d and "홍길동" not in str(d)
    assert "홍길동" not in repr(req.booker)


# ---- 개인정보 마스킹 --------------------------------------------------------
def test_redaction_patterns_and_registered(caplog):
    redactor.register("김철수")
    try:
        with caplog.at_level(logging.INFO, logger="nolassist"):
            get_logger().info("이름 김철수 / 010-9876-5432 / me@test.com / 1234-5678-9012-3456")
        text = caplog.text
        for secret in ("김철수", "9876", "me@test.com", "9012"):
            assert secret not in text
    finally:
        redactor.clear()


def test_storage_redacts_and_roundtrips(tmp_path):
    st = Storage(tmp_path / "t.db")
    rid = st.start_run("https://x", True, {"date": "d"})
    st.add_event(rid, "X", "연락처 010-1111-2222")
    st.finish_run(rid, "ORDER_SUMMARY", {"price": "1원"})
    assert "1111" not in st.events(rid)[0][2]
    assert st.recent_runs()[0]["final_stage"] == "ORDER_SUMMARY"
    st.save_setting("last", {"a": 1})
    assert st.load_setting("last") == {"a": 1}


# ---- 프로필 ---------------------------------------------------------------
def test_live_profile_is_unverified_and_has_no_guessed_selectors():
    p = live_profile()
    assert not p.verified and not p.can_click() and not p.can_select_seats() and not p.can_fill()
    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                yield from walk(x)
        else:
            yield v
    assert all(v is None for v in walk(p.pages))
    assert p.blockers == []


def test_unverified_profile_never_grants_permissions():
    p = mock_profile()
    p.verified = False
    assert not (p.can_click() or p.can_select_seats() or p.can_fill())
