"""사이트 선택자 프로필.

선택자는 코드에 하드코딩하지 않고 JSON 프로필로 분리한다. 실제 사이트용 프로필은
사용자가 개발자 도구로 직접 확인한 값만 채워 넣고 verified=true 로 표시해야 한다.
값이 비어 있는(null) 항목은 "확인되지 않음"으로 취급되어 해당 단계에서 자동 동작을 하지 않는다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROFILE_DIR = Path(__file__).resolve().parent.parent / "profiles"


@dataclass
class SiteProfile:
    name: str
    verified: bool
    allow_auto_click: bool
    allow_auto_seat_select: bool
    allow_auto_fill: bool
    pages: dict[str, Any] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    notes: str = ""

    @classmethod
    def load(cls, path: Path | str) -> "SiteProfile":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            name=data["name"],
            verified=bool(data.get("verified", False)),
            allow_auto_click=bool(data.get("allow_auto_click", False)),
            allow_auto_seat_select=bool(data.get("allow_auto_seat_select", False)),
            allow_auto_fill=bool(data.get("allow_auto_fill", False)),
            pages=data.get("pages", {}),
            blockers=[b for b in data.get("blockers", []) if b],
            notes=data.get("notes", ""),
        )

    def sel(self, page: str, key: str) -> str | None:
        """선택자 조회. 없거나 빈 값이면 None (= 미확인)."""
        value = self.pages.get(page, {}).get(key)
        return value or None

    def section(self, page: str, key: str) -> dict:
        return self.pages.get(page, {}).get(key) or {}

    # 아래 권한은 verified 프로필에서만 유효하다.
    def can_click(self) -> bool:
        return self.verified and self.allow_auto_click

    def can_select_seats(self) -> bool:
        return self.verified and self.allow_auto_seat_select

    def can_fill(self) -> bool:
        return self.verified and self.allow_auto_fill


def mock_profile() -> SiteProfile:
    return SiteProfile.load(PROFILE_DIR / "mock_site.json")


def live_profile() -> SiteProfile:
    return SiteProfile.load(PROFILE_DIR / "nol_live.json")
