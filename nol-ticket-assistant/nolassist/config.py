"""사용자 예매 조건. 개인정보(BookerInfo)는 메모리에만 두고 저장/로그하지 않는다."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime


@dataclass
class SeatPreference:
    grades: list[str] = field(default_factory=list)  # 우선순위 순, 예: ["VIP", "R"]
    quantity: int = 1
    adjacent: bool = True        # 같은 열 연속 좌석만 허용
    prefer_front: bool = True    # 앞 열 우선
    max_row: int | None = None   # 이 열 번호 이하만 허용

    def validate(self) -> None:
        if not 1 <= self.quantity <= 4:
            raise ValueError("매수는 1~4 사이여야 합니다")
        if self.max_row is not None and self.max_row < 1:
            raise ValueError("최대 열 번호는 1 이상이어야 합니다")


@dataclass(repr=False)
class BookerInfo:
    name: str = ""
    phone: str = ""
    email: str = ""

    def __repr__(self) -> str:  # 실수로 로그에 찍혀도 값이 노출되지 않도록
        return "BookerInfo(<redacted>)"

    def values(self) -> list[str]:
        return [v for v in (self.name, self.phone, self.email) if v]


@dataclass
class BookingRequest:
    url: str
    date: str                    # 사이트에 표시/속성으로 쓰이는 날짜 값, 예: "2026-11-15"
    round: str                   # 회차 값, 예: "1" 또는 "19:00"
    seat: SeatPreference = field(default_factory=SeatPreference)
    expected_title: str = ""     # 비어 있지 않으면 페이지 공연명에 포함되어야 함
    start_at: datetime | None = None
    test_mode: bool = True
    booker: BookerInfo = field(default_factory=BookerInfo)

    def validate(self) -> None:
        if not self.url.startswith(("http://", "https://")):
            raise ValueError("공연 URL은 http(s):// 로 시작해야 합니다")
        if not self.date.strip():
            raise ValueError("희망 날짜를 입력하세요")
        if not self.round.strip():
            raise ValueError("회차를 입력하세요")
        self.seat.validate()

    def to_safe_dict(self) -> dict:
        """DB 저장용. 예매자 정보는 제외한다."""
        d = asdict(self)
        d.pop("booker", None)
        d["start_at"] = self.start_at.isoformat() if self.start_at else None
        return d
