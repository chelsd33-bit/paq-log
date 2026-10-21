"""모의 사이트(mock_site/)를 대상으로 한 통합 테스트. 실제 사이트에는 접속하지 않는다."""
import threading
import time
from datetime import datetime, timedelta

from nolassist.automation.browser import FORBIDDEN_CLICK
from nolassist.automation.engine import BookingEngine, Timeouts
from nolassist.config import BookerInfo, BookingRequest, SeatPreference
from nolassist.profile import live_profile, mock_profile
from nolassist.stages import Stage
from nolassist.storage import Storage

FAST = Timeouts(page_load_s=5, step_s=3, user_action_s=2, blocker_s=10, retries=2, retry_delay_s=0.2)
BOOKER = BookerInfo("홍길동", "010-1234-5678", "tester@example.com")

def make_engine(url, profile=None, storage=None, keep_open=False, **req_kw):
    kw = dict(url=url, date="2026-11-15", round="2", seat=SeatPreference(grades=["VIP", "R"], quantity=2),
              expected_title="테스트 오페라", booker=BOOKER, test_mode=True)
    kw.update(req_kw)
    return BookingEngine(BookingRequest(**kw), profile or mock_profile(), storage=storage,
                         headless=True, keep_open=keep_open, timeouts=FAST)

def test_full_flow_stops_before_payment(server, tmp_path):
    st = Storage(tmp_path / "db.sqlite")
    eng = make_engine(server.show_url(), storage=st, keep_open=True)
    t = threading.Thread(target=eng.run)
    t.start()
    try:
        deadline = time.time() + 40
        while not (eng.result and eng.result.final_stage.is_terminal) and time.time() < deadline:
            time.sleep(0.1)
        r = eng.result
        assert r.final_stage is Stage.ORDER_SUMMARY, r.message
        assert r.summary.title.startswith("뮤지컬")
        assert r.summary.date == "2026-11-15"
        assert r.summary.round == "2회 19:00"
        assert r.summary.seats == "VIP 1-5, VIP 1-6"
        assert r.summary.price == "340,000원"
    finally:
        eng.close_browser()
        t.join(10)
    # DB 에 결과가 남고, 예매자 개인정보는 기록되지 않았다.
    run = st.recent_runs()[0]
    assert run["final_stage"] == "ORDER_SUMMARY"
    dump = " ".join(m for _, _, m in st.events(run["id"]))
    assert "홍길동" not in dump and "1234-5678" not in dump and "tester@example.com" not in dump

def test_payment_button_not_clicked(server):
    """엔진 종료 후 같은 브라우저 컨텍스트에서 모의 결제 플래그를 직접 확인한다."""
    eng = make_engine(server.show_url())
    eng.keep_open = False
    captured = {}
    orig_close = eng.browser.close

    def close_with_check():
        page = eng.browser.page
        captured["paid"] = page.evaluate("localStorage.getItem('mock.paid')")
        captured["summary_visible"] = page.locator("#summary-page").is_visible()
        orig_close()

    eng.browser.close = close_with_check
    r = eng.run()
    assert r.final_stage is Stage.ORDER_SUMMARY
    assert captured == {"paid": None, "summary_visible": True}

def test_forbidden_click_pattern():
    for label in ("결제하기", "Pay now", "주문하기", "구매 확정", "Checkout", "예매 완료"):
        assert FORBIDDEN_CLICK.search(label), label
    for label in ("예매하기", "좌석선택완료", "다음 단계", "1회 14:00"):
        assert not FORBIDDEN_CLICK.search(label), label

def test_navigation_failure_retries_then_fails():
    eng = make_engine("http://127.0.0.1:9/show.html")  # 닫힌 포트
    r = eng.run()
    assert r.final_stage is Stage.ABORTED
    assert "공연 페이지 로딩: 2회 시도 후 실패" in r.message

def test_wrong_show_title_aborts(server):
    r = make_engine(server.show_url(), expected_title="다른 공연").run()
    assert r.final_stage is Stage.ABORTED and "대상 공연이 아닙니다" in r.message

def test_unavailable_date_aborts(server):
    r = make_engine(server.show_url(), date="2026-11-14").run()
    assert r.final_stage is Stage.ABORTED and "선택할 수 없는" in r.message

def test_missing_date_aborts(server):
    r = make_engine(server.show_url(), date="2030-01-01").run()
    assert r.final_stage is Stage.ABORTED

def test_soldout(server):
    r = make_engine(server.show_url("soldout")).run()
    assert r.final_stage is Stage.ABORTED and "매진" in r.message

def test_no_seats(server):
    r = make_engine(server.show_url("noseats")).run()
    assert r.final_stage is Stage.ABORTED and "좌석" in r.message

def test_no_matching_seats(server):
    r = make_engine(server.show_url(), seat=SeatPreference(grades=["VIP"], quantity=4, max_row=1)).run()
    assert r.final_stage is Stage.ABORTED and "선호 조건" in r.message

def test_session_expired(server):
    r = make_engine(server.show_url("expired")).run()
    assert r.final_stage is Stage.ABORTED and "세션이 만료" in r.message

def test_queue_is_handed_to_user_not_bypassed(server):
    eng = make_engine(server.show_url("queue"))
    seen = []
    eng.machine.subscribe(lambda old, new: seen.append(new))
    r = eng.run()
    assert Stage.USER_ACTION in seen  # 대기열 화면 동안 사용자 조치 상태로 대기
    assert r.final_stage is Stage.ORDER_SUMMARY, r.message

def test_changed_structure_aborts(server):
    r = make_engine(server.show_url("changed")).run()
    assert r.final_stage is Stage.ABORTED and "좌석 상태가 불명확" in r.message

def test_emergency_stop_during_wait(server):
    eng = make_engine(server.show_url(), start_at=datetime.now() + timedelta(seconds=30))
    threading.Timer(1.0, eng.emergency_stop).start()
    t0 = time.time()
    r = eng.run()
    assert r.final_stage is Stage.STOPPED
    assert time.time() - t0 < 10

def test_emergency_stop_mid_flow_halts_input(server):
    eng = make_engine(server.show_url())
    eng.machine.subscribe(lambda old, new: new is Stage.SEAT_SELECT and eng.emergency_stop())
    r = eng.run()
    assert r.final_stage is Stage.STOPPED
    assert r.chosen_seats == []  # 좌석 단계에서 아무 것도 선택하지 않음

def test_assist_mode_hands_clicks_to_user(server):
    """자동 클릭 비허용 프로필: 클릭하지 않고 사용자 조치를 기다리다 시간 초과로 안전 중단."""
    p = mock_profile()
    p.allow_auto_click = p.allow_auto_seat_select = p.allow_auto_fill = False
    eng = make_engine(server.show_url(), profile=p)
    seen = []
    eng.machine.subscribe(lambda old, new: seen.append(new))
    r = eng.run()
    assert Stage.USER_ACTION in seen
    assert r.final_stage is Stage.ABORTED and "날짜 선택" in r.message

def test_live_profile_aborts_without_guessing(server):
    """미검증 실사이트 프로필은 선택자가 없으므로 어떤 페이지에서도 클릭하지 않고 중단."""
    eng = make_engine(server.show_url(), profile=live_profile(), test_mode=False)
    opened = {}
    orig_close = eng.browser.close
    eng.browser.close = lambda: (opened.setdefault("url", eng.browser.page.url), orig_close())
    r = eng.run()
    assert r.final_stage is Stage.ABORTED and "선택자가 확인되어 있지 않습니다" in r.message
    assert opened["url"].endswith("show.html?scenario=normal")  # 페이지는 열어 둔 상태

def test_mode_profile_mismatch_rejected(server):
    assert make_engine(server.show_url(), test_mode=False).run().final_stage is Stage.FAILED
    assert make_engine(server.show_url(), profile=live_profile()).run().final_stage is Stage.FAILED
