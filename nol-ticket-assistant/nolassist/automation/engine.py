"""단계별 예매 보조 엔진.

원칙
- 각 단계는 프로필의 identify 선택자로 페이지가 확인된 경우에만 진행한다.
- 필요한 선택자가 없거나(미확인) 요소가 모호하면 추측하지 않고 중단한다.
- 프로필이 자동 동작을 허용하지 않으면 해당 조작은 사용자에게 넘기고(USER_ACTION) 결과만 확인한다.
- 주문 요약 페이지에 도달하면 즉시 멈춘다. 결제/주문 버튼을 누르는 코드 경로는 없다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from playwright.sync_api import Error as PlaywrightError

from ..config import BookingRequest
from ..logging_setup import get_logger, redactor
from ..profile import SiteProfile
from ..seats import Seat, SeatStateUnclear, choose_seats, parse_seat
from ..stages import Stage, StageMachine
from ..storage import Storage
from .browser import (
    GENERIC_CAPTCHA,
    SafeBrowser,
    SafetyError,
    StopRequested,
    StopToken,
    StructureError,
)

log = get_logger()


class SafeAbort(Exception):
    """예상된 안전 중단 (좌석 없음, 세션 만료 등)."""


@dataclass
class Timeouts:
    page_load_s: float = 15
    step_s: float = 10
    user_action_s: float = 600       # 사용자가 직접 조작할 때까지 기다리는 시간
    blocker_s: float = 1800          # 대기열/CAPTCHA 를 사용자가 처리할 때까지
    retries: int = 3
    retry_delay_s: float = 2


@dataclass
class OrderSummary:
    title: str = ""
    date: str = ""
    round: str = ""
    seats: str = ""
    price: str = ""

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class RunResult:
    final_stage: Stage
    message: str
    summary: OrderSummary | None = None
    show_title: str = ""
    chosen_seats: list[Seat] = field(default_factory=list)


class BookingEngine:
    def __init__(
        self,
        request: BookingRequest,
        profile: SiteProfile,
        storage: Storage | None = None,
        headless: bool = False,
        user_data_dir: Path | None = None,
        timeouts: Timeouts | None = None,
        keep_open: bool = True,
        on_info: Callable[[str, str], None] | None = None,
    ) -> None:
        self.req = request
        self.profile = profile
        self.storage = storage
        self.t = timeouts or Timeouts()
        self.keep_open = keep_open
        self.on_info = on_info or (lambda key, value: None)
        self.stop_token = StopToken()
        self.release = StopToken()  # 결제 전 중단 후 브라우저를 닫을 시점
        self.machine = StageMachine()
        self.browser = SafeBrowser(self.stop_token, headless=headless, user_data_dir=user_data_dir)
        self.run_id: int | None = None
        self.result: RunResult | None = None
        self.machine.subscribe(self._on_stage)

    # ---- 외부 제어 ---------------------------------------------------------
    def emergency_stop(self) -> None:
        log.warning("긴급 중지 요청 — 추가 입력을 중단합니다")
        self.stop_token.set()

    def close_browser(self) -> None:
        self.release.set()

    # ---- 내부 유틸 ---------------------------------------------------------
    def _on_stage(self, old: Stage, new: Stage) -> None:
        log.info("단계 전환: %s → %s", old.value, new.value)
        if self.storage and self.run_id is not None:
            self.storage.add_event(self.run_id, new.name, f"{old.name} -> {new.name}")

    def _need(self, page: str, key: str) -> str:
        sel = self.profile.sel(page, key)
        if not sel:
            raise StructureError(
                f"프로필 '{self.profile.name}'에 [{page}.{key}] 선택자가 확인되어 있지 않습니다. "
                "추측으로 진행하지 않고 중단합니다."
            )
        return sel

    def _interrupts(self) -> dict[str, str]:
        """어느 단계에서든 나타날 수 있는 페이지/위젯."""
        c: dict[str, str] = {}
        if sel := self.profile.sel("session_expired", "identify"):
            c["session_expired"] = sel
        for i, b in enumerate(self.profile.blockers):
            c[f"blocker{i}"] = b
        c["captcha"] = GENERIC_CAPTCHA
        return c

    def _wait_page(self, expected: list[str], timeout_s: float | None = None) -> str:
        """expected 중 하나의 페이지가 확인될 때까지 대기. 세션 만료/대기열/CAPTCHA 를 처리한다."""
        candidates = {name: self._need(name, "identify") for name in expected}
        while True:
            found = self.browser.wait_for_any({**candidates, **self._interrupts()}, timeout_s or self.t.step_s)
            if found == "session_expired":
                raise SafeAbort("세션이 만료되었습니다. 브라우저에서 다시 로그인한 뒤 처음부터 시작하세요.")
            if found.startswith("blocker") or found == "captcha":
                self._hand_to_user_until_gone(found)
                continue
            return found

    def _hand_to_user_until_gone(self, which: str) -> None:
        sel = GENERIC_CAPTCHA if which == "captcha" else self.profile.blockers[int(which[7:])]
        kind = "보안문자(CAPTCHA)" if which == "captcha" else "대기열/접근 제한 화면"
        self.machine.transition(Stage.USER_ACTION)
        log.warning("%s이 표시되었습니다. 자동화는 이를 우회하지 않습니다 — 브라우저에서 직접 처리하세요.", kind)
        self.browser.wait_until(lambda: not self.browser.present(sel), self.t.blocker_s, f"{kind} 해제 대기")
        log.info("%s 해제 확인 — 다음 단계를 계속합니다", kind)
        self.machine.resume()

    def _manual(self, instruction: str, done: Callable[[], object], what: str) -> None:
        """자동 조작이 허용되지 않은 동작을 사용자에게 넘기고 완료를 확인한다."""
        self.machine.transition(Stage.USER_ACTION)
        log.info("[직접 조작 필요] %s", instruction)
        self.on_info("instruction", instruction)
        self.browser.wait_until(done, self.t.user_action_s, what)
        self.on_info("instruction", "")
        self.machine.resume()

    def _retry(self, fn: Callable[[], object], what: str) -> object:
        last: Exception | None = None
        for attempt in range(1, self.t.retries + 1):
            self.stop_token.check()
            try:
                return fn()
            except (PlaywrightError, StructureError) as e:
                last = e
                log.warning("%s 실패 (%d/%d): %s", what, attempt, self.t.retries, str(e).splitlines()[0])
                if attempt < self.t.retries:
                    self.stop_token.sleep(self.t.retry_delay_s * attempt)
        raise StructureError(f"{what}: {self.t.retries}회 시도 후 실패") from last

    # ---- 실행 --------------------------------------------------------------
    def run(self) -> RunResult:
        try:
            self.req.validate()
        except ValueError as e:
            return self._finish(Stage.FAILED, f"입력값 오류: {e}")
        if not self.req.test_mode and self.profile.name.startswith("mock"):
            return self._finish(Stage.FAILED, "실사용 모드에서 테스트 프로필을 사용할 수 없습니다")
        if self.req.test_mode and not self.profile.name.startswith("mock"):
            return self._finish(Stage.FAILED, "테스트 모드에서는 모의 사이트 프로필만 사용합니다")

        redactor.register(*self.req.booker.values())
        if self.storage:
            self.run_id = self.storage.start_run(self.req.url, self.req.test_mode, self.req.to_safe_dict())
        if not self.profile.verified:
            log.warning("프로필 '%s'은(는) 검증되지 않았습니다. 클릭/입력 자동화는 비활성화됩니다.", self.profile.name)

        try:
            self.browser.launch()
            self._wait_for_start()
            self._step_page_prep()
            self._step_show_select()
            nxt = self._wait_page(["seat", "booker", "summary"], self.t.page_load_s)
            if nxt == "seat":
                self.machine.transition(Stage.SEAT_SELECT)
                self._step_seat_select()
                nxt = self._wait_page(["booker", "summary"], self.t.page_load_s)
            else:
                raise StructureError("좌석 선택 페이지가 확인되지 않았습니다")
            if nxt == "booker":
                self.machine.transition(Stage.BOOKER_INFO)
                self._step_booker()
                self._wait_page(["summary"], self.t.page_load_s)
            summary = self._step_summary()
            result = self._finish(Stage.ORDER_SUMMARY, "주문 요약 페이지에서 자동 실행을 중단했습니다. 내용을 확인하고 직접 결제하세요.", summary)
        except StopRequested:
            result = self._finish(Stage.STOPPED, "사용자가 긴급 중지했습니다. 이후 입력은 수행하지 않습니다.")
        except SafeAbort as e:
            result = self._finish(Stage.ABORTED, str(e))
        except SafetyError as e:
            result = self._finish(Stage.ABORTED, f"안전 장치 작동: {e}")
        except StructureError as e:
            result = self._finish(Stage.ABORTED, f"페이지 구조 확인 실패로 중단: {e}")
        except SeatStateUnclear as e:
            result = self._finish(Stage.ABORTED, f"좌석 상태가 불명확하여 중단: {e}")
        except PlaywrightError as e:
            result = self._finish(Stage.FAILED, f"브라우저 오류: {str(e).splitlines()[0]}")
        except Exception as e:  # noqa: BLE001 — GUI 스레드로 오류를 넘기기 위함
            log.exception("예상치 못한 오류")
            result = self._finish(Stage.FAILED, f"예상치 못한 오류: {e}")
        finally:
            redactor.clear()

        if self.keep_open and self.browser.context is not None:
            # 사용자가 직접 확인/결제할 수 있도록 브라우저를 열어 둔다 (자동 입력은 더 이상 없음).
            log.info("브라우저는 열어 둡니다. 'GUI > 브라우저 닫기' 로 종료할 수 있습니다.")
            self.release.wait()
        self.browser.close()
        return result

    def _finish(self, stage: Stage, message: str, summary: OrderSummary | None = None) -> RunResult:
        if self.machine.stage is not stage and self.machine.can_transition(stage):
            self.machine.transition(stage)
        (log.info if stage is Stage.ORDER_SUMMARY else log.warning)(message)
        if self.storage and self.run_id is not None:
            self.storage.add_event(self.run_id, stage.name, message)
            self.storage.finish_run(self.run_id, stage.name, summary.as_dict() if summary else None)
        prev = self.result
        self.result = RunResult(stage, message, summary,
                                prev.show_title if prev else "", prev.chosen_seats if prev else [])
        return self.result

    # ---- 단계 0: 시작 시간 대기 --------------------------------------------
    def _wait_for_start(self) -> None:
        if not self.req.start_at:
            return
        remaining = (self.req.start_at - datetime.now()).total_seconds()
        if remaining <= 0:
            return
        self.machine.transition(Stage.WAITING_START)
        log.info("예매 시작 시간 %s 까지 대기합니다 (%.0f초)", self.req.start_at.strftime("%H:%M:%S"), remaining)
        while (remaining := (self.req.start_at - datetime.now()).total_seconds()) > 0:
            self.stop_token.sleep(min(remaining, 0.5))

    # ---- 단계 1: 페이지 준비 ----------------------------------------------
    def _step_page_prep(self) -> None:
        self.machine.transition(Stage.PAGE_PREP)
        log.info("공연 페이지를 엽니다: %s", self.req.url)
        self._retry(lambda: self.browser.goto(self.req.url, int(self.t.page_load_s * 1000)), "공연 페이지 로딩")
        # 페이지는 먼저 열어 두고, 확인 수단(선택자)이 없으면 여기서 멈춘다 — 이후는 사용자가 직접 진행.
        title_sel = self._need("show", "title")
        self._wait_page(["show"], self.t.page_load_s)
        title = self.browser.text(title_sel, "공연명")
        self.on_info("title", title)
        self.result = RunResult(Stage.PAGE_PREP, "", show_title=title)
        log.info("공연명 확인: %s", title)
        if self.req.expected_title and self.req.expected_title not in title:
            raise SafeAbort(f"대상 공연이 아닙니다 (기대: {self.req.expected_title!r}, 페이지: {title!r})")

    # ---- 단계 2: 날짜/회차 ------------------------------------------------
    def _pick_option(self, items_key: str, attr_key: str, selected_key: str, value: str, what: str) -> None:
        items_sel = self._need("show", items_key)
        selected_sel = self._need("show", selected_key)
        attr = self.profile.sel("show", attr_key)

        def matches():
            hits = []
            for loc in self.browser.all(items_sel):
                v = loc.get_attribute(attr) if attr else loc.inner_text().strip()
                if v == value:
                    hits.append(loc)
            return hits

        hits = self.browser.wait_until(matches, self.t.step_s, f"{what} '{value}' 표시")
        if len(hits) != 1:
            raise StructureError(f"{what} '{value}' 항목이 {len(hits)}개 있습니다 (정확히 1개 필요)")
        target = hits[0]
        if target.get_attribute("aria-disabled") == "true" or not target.is_enabled():
            raise SafeAbort(f"{what} '{value}' 은(는) 선택할 수 없는 상태입니다 (매진/마감 가능)")

        def selected_ok() -> bool:
            if not self.browser.present(selected_sel):
                return False
            loc = self.browser.page.locator(selected_sel).first
            v = loc.get_attribute(attr) if attr else loc.inner_text().strip()
            return v == value

        if self.profile.can_click():
            self.browser.click(target, f"{what} {value}")
        else:
            self.browser.highlight(target)
            self._manual(f"브라우저에서 {what} '{value}' 를 직접 선택하세요.", selected_ok, f"{what} 선택")
        self.browser.wait_until(selected_ok, self.t.step_s, f"{what} 선택 결과 확인")
        log.info("%s 선택 확인: %s", what, value)

    def _step_show_select(self) -> None:
        self.machine.transition(Stage.SHOW_SELECT)
        self._pick_option("date_items", "date_attr", "selected_date", self.req.date, "날짜")
        self._pick_option("round_items", "round_attr", "selected_round", self.req.round, "회차")
        next_sel = self._need("show", "next_button")
        if self.profile.can_click():
            self.browser.click(self.browser.one(next_sel, "예매 진행 버튼"), "예매 진행 버튼")
        else:
            seat_id = self._need("seat", "identify")
            self._manual("브라우저에서 예매 진행 버튼을 직접 누르세요.",
                         lambda: self.browser.present(seat_id), "좌석 페이지 진입")

    # ---- 단계 3: 좌석 -----------------------------------------------------
    def _read_seats(self) -> list[tuple[Seat, object]]:
        seats_sel = self._need("seat", "available_seats")
        attrs = self.profile.section("seat", "seat_attrs")
        for k in ("id", "grade", "row", "col"):
            if not attrs.get(k):
                raise StructureError(f"프로필에 좌석 속성 [{k}] 가 확인되어 있지 않습니다")
        out = []
        for loc in self.browser.all(seats_sel):
            values = {k: loc.get_attribute(attrs[k]) for k in ("id", "grade", "row", "col")}
            out.append((parse_seat(values), loc))
        return out

    def _step_seat_select(self) -> None:
        # 좌석 맵이 그려질 시간을 짧게 준다. 끝까지 없으면 '좌석 없음'으로 판단.
        seats_sel = self._need("seat", "available_seats")
        soldout_sel = self.profile.sel("seat", "soldout_marker")
        try:
            self.browser.wait_until(
                lambda: self.browser.present(seats_sel) or (soldout_sel and self.browser.present(soldout_sel)),
                self.t.step_s, "좌석 정보 표시")
        except StructureError:
            raise SafeAbort("선택 가능한 좌석 정보가 표시되지 않습니다 (좌석 없음 또는 구조 변경)")
        if soldout_sel and self.browser.present(soldout_sel):
            raise SafeAbort("매진 표시가 확인되었습니다")

        seats = self._read_seats()
        if not seats:
            raise SafeAbort("선택 가능한 좌석이 없습니다")
        log.info("선택 가능 좌석 %d석 확인", len(seats))
        chosen = choose_seats([s for s, _ in seats], self.req.seat)
        if not chosen:
            raise SafeAbort("선호 조건(등급/열/연석/매수)에 맞는 좌석이 없습니다")
        desc = ", ".join(f"{s.grade} {s.row}열 {s.col}번" for s in chosen)
        log.info("조건에 맞는 좌석: %s", desc)
        if self.result:
            self.result.chosen_seats = chosen
        self.on_info("seats", desc)

        selected_sel = self._need("seat", "selected_seats")
        id_attr = self.profile.section("seat", "seat_attrs")["id"]

        def seat_loc(seat_id: str):
            # 클릭 후 클래스가 바뀌어도 같은 좌석을 가리키도록 ID 속성으로 고정
            return self.browser.one(f"[{id_attr}={json.dumps(seat_id)}]", f"좌석 {seat_id}")
        want = {s.seat_id for s in chosen}

        def selection() -> set[str]:
            return {loc.get_attribute(id_attr) for loc in self.browser.all(selected_sel)}

        if self.profile.can_select_seats():
            for s in chosen:
                self.browser.click(seat_loc(s.seat_id), f"좌석 {s.seat_id}")
            self.browser.wait_until(lambda: selection() == want, self.t.step_s, "좌석 선택 결과 확인")
        else:
            for s in chosen:
                self.browser.highlight(seat_loc(s.seat_id))
            self._manual(f"추천 좌석(주황 테두리)을 참고해 {self.req.seat.quantity}석을 직접 선택하세요: {desc}",
                         lambda: len(selection()) == self.req.seat.quantity, "좌석 직접 선택")
            if selection() != want:
                log.info("사용자가 추천과 다른 좌석을 선택했습니다 — 사용자 선택을 따릅니다")
        log.info("좌석 선택 확인 (%d석)", len(selection()))

        next_sel = self._need("seat", "next_button")
        if self.profile.can_click():
            self.browser.click(self.browser.one(next_sel, "좌석 선택 완료 버튼"), "좌석 선택 완료 버튼")
        else:
            targets = [self._need(p, "identify") for p in ("booker", "summary") if self.profile.sel(p, "identify")]
            self._manual("좌석 선택 완료 버튼을 직접 누르세요.",
                         lambda: any(self.browser.present(t) for t in targets), "다음 단계 진입")

    # ---- 단계 4: 예매 정보 ------------------------------------------------
    def _step_booker(self) -> None:
        fields = self.profile.section("booker", "fields")
        values = {"name": self.req.booker.name, "phone": self.req.booker.phone, "email": self.req.booker.email}
        filled = 0
        if self.profile.can_fill():
            for key, sel in fields.items():
                if not sel or not values.get(key):
                    continue
                loc = self.browser.one(sel, f"예매자 {key} 입력란")
                if (loc.input_value() or "").strip():
                    continue  # 사이트가 미리 채운 값은 건드리지 않는다
                self.browser.fill(loc, values[key], f"예매자 {key}")
                filled += 1
                log.info("예매자 %s 입력 완료", key)  # 값은 기록하지 않음
        summary_id = self._need("summary", "identify")
        next_sel = self.profile.sel("booker", "next_button")
        if self.profile.can_click() and next_sel and self._booker_complete(fields):
            self.browser.click(self.browser.one(next_sel, "예매 정보 다음 버튼"), "예매 정보 다음 버튼")
        else:
            msg = "예매자 정보를 확인/입력한 뒤 다음 버튼을 직접 누르세요." if filled == 0 else \
                  "자동 입력된 예매자 정보를 확인한 뒤 다음 버튼을 직접 누르세요."
            self._manual(msg, lambda: self.browser.present(summary_id), "주문 요약 진입")

    def _booker_complete(self, fields: dict) -> bool:
        for key, sel in fields.items():
            if sel and not (self.browser.one(sel, f"예매자 {key} 입력란").input_value() or "").strip():
                return False
        return True

    # ---- 단계 5: 결제 전 중단 ----------------------------------------------
    def _step_summary(self) -> OrderSummary:
        # 여기서는 읽기만 한다. 어떤 클릭/입력도 하지 않는다.
        s = OrderSummary()
        for key in ("title", "date", "round", "seats", "price"):
            sel = self.profile.sel("summary", key)
            if sel and self.browser.present(sel):
                setattr(s, key, " ".join(self.browser.page.locator(sel).first.inner_text().split()))
        for key, value in s.as_dict().items():
            self.on_info(f"summary.{key}", value)
        log.info("주문 요약 — 공연: %s / 날짜: %s / 회차: %s / 좌석: %s / 금액: %s",
                 s.title, s.date, s.round, s.seats, s.price)
        return s
