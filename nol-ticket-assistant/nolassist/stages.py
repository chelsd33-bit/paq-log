"""예매 단계 정의와 상태 전이 관리."""
from __future__ import annotations

import threading
from enum import Enum
from typing import Callable


class Stage(str, Enum):
    IDLE = "대기"
    WAITING_START = "시작 시간 대기"
    PAGE_PREP = "1. 페이지 준비"
    SHOW_SELECT = "2. 날짜/회차 선택"
    SEAT_SELECT = "3. 좌석 선택"
    BOOKER_INFO = "4. 예매 정보"
    ORDER_SUMMARY = "5. 결제 전 중단 (사용자 확인 필요)"
    USER_ACTION = "사용자 조치 필요"
    STOPPED = "긴급 중지됨"
    ABORTED = "안전 중단"
    FAILED = "오류로 종료"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STAGES


TERMINAL_STAGES = frozenset({Stage.ORDER_SUMMARY, Stage.STOPPED, Stage.ABORTED, Stage.FAILED})

# 정상 진행 경로. USER_ACTION 은 진행 중 어느 단계에서든 들어갔다가 원래 단계로 돌아온다.
_FORWARD: dict[Stage, set[Stage]] = {
    Stage.IDLE: {Stage.WAITING_START, Stage.PAGE_PREP},
    Stage.WAITING_START: {Stage.PAGE_PREP},
    Stage.PAGE_PREP: {Stage.SHOW_SELECT},
    Stage.SHOW_SELECT: {Stage.SEAT_SELECT},
    Stage.SEAT_SELECT: {Stage.BOOKER_INFO, Stage.ORDER_SUMMARY},
    Stage.BOOKER_INFO: {Stage.ORDER_SUMMARY},
}
_ACTIVE = {Stage.WAITING_START, Stage.PAGE_PREP, Stage.SHOW_SELECT, Stage.SEAT_SELECT, Stage.BOOKER_INFO}


class InvalidTransition(RuntimeError):
    pass


class StageMachine:
    """허용된 전이만 통과시키고, 변경 시 리스너에 알린다. 스레드 안전."""

    def __init__(self) -> None:
        self._stage = Stage.IDLE
        self._resume_to: Stage | None = None
        self._lock = threading.Lock()
        self._listeners: list[Callable[[Stage, Stage], None]] = []

    @property
    def stage(self) -> Stage:
        return self._stage

    def subscribe(self, fn: Callable[[Stage, Stage], None]) -> None:
        self._listeners.append(fn)

    def can_transition(self, new: Stage) -> bool:
        cur = self._stage
        if cur.is_terminal:
            return False
        if new in (Stage.STOPPED, Stage.ABORTED, Stage.FAILED):
            return True
        if new is Stage.USER_ACTION:
            return cur in _ACTIVE
        if cur is Stage.USER_ACTION:
            return new is self._resume_to
        return new in _FORWARD.get(cur, set())

    def transition(self, new: Stage) -> None:
        with self._lock:
            old = self._stage
            if not self.can_transition(new):
                raise InvalidTransition(f"{old.name} -> {new.name} 전이는 허용되지 않습니다")
            if new is Stage.USER_ACTION:
                self._resume_to = old
            elif old is Stage.USER_ACTION:
                self._resume_to = None
            self._stage = new
        for fn in list(self._listeners):
            fn(old, new)

    def resume(self) -> None:
        """USER_ACTION 에서 이전 단계로 복귀."""
        if self._stage is not Stage.USER_ACTION or self._resume_to is None:
            raise InvalidTransition("사용자 조치 상태가 아닙니다")
        self.transition(self._resume_to)

    def reset(self) -> None:
        with self._lock:
            old, self._stage, self._resume_to = self._stage, Stage.IDLE, None
        for fn in list(self._listeners):
            fn(old, Stage.IDLE)
