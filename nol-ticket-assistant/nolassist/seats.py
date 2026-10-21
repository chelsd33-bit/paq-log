"""좌석 후보 선정(순수 함수). 브라우저와 무관하게 단위 테스트할 수 있다."""
from __future__ import annotations

from dataclasses import dataclass

from .config import SeatPreference


@dataclass(frozen=True)
class Seat:
    seat_id: str
    grade: str
    row: int
    col: int


class SeatStateUnclear(ValueError):
    """좌석 속성을 해석할 수 없음 — 추측하지 않고 중단해야 한다."""


def parse_seat(attrs: dict[str, str | None]) -> Seat:
    try:
        seat_id = attrs["id"]
        grade = attrs["grade"]
        row = int(attrs["row"])  # type: ignore[arg-type]
        col = int(attrs["col"])  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError) as e:
        raise SeatStateUnclear(f"좌석 속성을 해석할 수 없습니다: {attrs}") from e
    if not seat_id or not grade:
        raise SeatStateUnclear(f"좌석 ID/등급이 비어 있습니다: {attrs}")
    return Seat(seat_id, grade, row, col)


def choose_seats(available: list[Seat], pref: SeatPreference) -> list[Seat] | None:
    """선호 조건에 맞는 좌석 묶음을 반환. 없으면 None.

    등급 우선순위 → (앞 열 우선) → 열 내 왼쪽부터 순으로 첫 조합을 고른다.
    grades 가 비어 있으면 모든 등급을 허용한다.
    """
    pref.validate()
    grades = pref.grades or sorted({s.grade for s in available})
    for grade in grades:
        pool = [s for s in available if s.grade == grade]
        if pref.max_row is not None:
            pool = [s for s in pool if s.row <= pref.max_row]
        rows = sorted({s.row for s in pool}, reverse=not pref.prefer_front)
        for row in rows:
            in_row = sorted((s for s in pool if s.row == row), key=lambda s: s.col)
            if pref.adjacent:
                for i in range(len(in_row) - pref.quantity + 1):
                    block = in_row[i:i + pref.quantity]
                    if block[-1].col - block[0].col == pref.quantity - 1:
                        return block
            elif len(in_row) >= pref.quantity:
                return in_row[:pref.quantity]
        if not pref.adjacent:
            ordered = sorted(pool, key=lambda s: (s.row if pref.prefer_front else -s.row, s.col))
            if len(ordered) >= pref.quantity:
                return ordered[:pref.quantity]
    return None
