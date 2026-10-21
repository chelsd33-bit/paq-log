"""Playwright 래퍼: 실제 DOM 요소만 다루고, 결제/주문 버튼은 절대 누르지 않는다."""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

from playwright.sync_api import BrowserContext, Locator, Page, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError

# 이 패턴에 걸리는 요소는 어떤 경우에도 클릭하지 않는다.
FORBIDDEN_CLICK = re.compile(
    r"결제|payment|\bpay\b|checkout|purchase|주문\s*(하기|완료|확정|제출)|구매\s*(하기|확정|완료)|예매\s*(확정|완료)",
    re.IGNORECASE,
)

# 사이트와 무관하게 알려진 CAPTCHA 위젯 iframe (발견 시 사용자에게 넘긴다. 우회하지 않음)
GENERIC_CAPTCHA = 'iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"]'


class StopRequested(Exception):
    """긴급 중지."""


class StructureError(Exception):
    """기대한 페이지 요소가 없거나 모호함 — 추측하지 않고 중단."""


class SafetyError(Exception):
    """결제/주문 성격의 요소를 누르려 함 — 즉시 중단."""


class StopToken:
    def __init__(self) -> None:
        self._ev = threading.Event()

    def set(self) -> None:
        self._ev.set()

    def is_set(self) -> bool:
        return self._ev.is_set()

    def check(self) -> None:
        if self._ev.is_set():
            raise StopRequested()

    def wait(self) -> None:
        self._ev.wait()

    def sleep(self, seconds: float) -> None:
        """중지 요청을 즉시 반영하는 sleep."""
        if self._ev.wait(max(0.0, seconds)):
            raise StopRequested()


class SafeBrowser:
    POLL = 0.25

    def __init__(self, stop: StopToken, headless: bool = False, user_data_dir: Path | None = None) -> None:
        self.stop = stop
        self.headless = headless
        self.user_data_dir = user_data_dir
        self._pw: Playwright | None = None
        self.context: BrowserContext | None = None

    # ---- 수명 주기 ---------------------------------------------------------
    def launch(self) -> None:
        self._pw = sync_playwright().start()
        kwargs: dict = {"headless": self.headless}
        exe = os.environ.get("NOLASSIST_CHROMIUM_PATH")
        if exe:
            kwargs["executable_path"] = exe
        if self.user_data_dir:
            # 로그인은 사용자가 브라우저에서 직접 한다. 세션은 이 프로필 폴더에만 남는다.
            self.user_data_dir.mkdir(parents=True, exist_ok=True)
            self.context = self._pw.chromium.launch_persistent_context(str(self.user_data_dir), **kwargs)
        else:
            browser = self._pw.chromium.launch(**kwargs)
            self.context = browser.new_context()
        self.context.set_default_timeout(5000)

    def close(self) -> None:
        try:
            if self.context:
                self.context.close()
        except PlaywrightError:
            pass
        finally:
            if self._pw:
                self._pw.stop()
            self.context, self._pw = None, None

    @property
    def page(self) -> Page:
        """가장 최근에 열린(팝업 포함) 페이지."""
        assert self.context is not None
        pages = [p for p in self.context.pages if not p.is_closed()]
        if not pages:
            return self.context.new_page()
        return pages[-1]

    # ---- 탐색 --------------------------------------------------------------
    def goto(self, url: str, timeout_ms: int = 15000) -> None:
        self.stop.check()
        self.page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

    def reload(self, timeout_ms: int = 15000) -> None:
        self.stop.check()
        self.page.reload(wait_until="domcontentloaded", timeout=timeout_ms)

    def present(self, selector: str) -> bool:
        """현재 페이지에 보이는 요소가 하나 이상 있는가 (예외 없이)."""
        try:
            loc = self.page.locator(selector)
            return loc.count() > 0 and loc.first.is_visible()
        except PlaywrightError:
            return False

    def wait_for_any(self, candidates: dict[str, str], timeout_s: float) -> str:
        """candidates(이름→선택자) 중 먼저 나타나는 것의 이름을 반환. 중지 요청을 계속 확인한다."""
        deadline = time.monotonic() + timeout_s
        while True:
            self.stop.check()
            for name, selector in candidates.items():
                if selector and self.present(selector):
                    return name
            if time.monotonic() >= deadline:
                raise StructureError(f"제한 시간 내에 예상한 페이지가 확인되지 않았습니다: {list(candidates)}")
            self.stop.sleep(self.POLL)

    def wait_until(self, predicate, timeout_s: float, what: str):
        deadline = time.monotonic() + timeout_s
        while True:
            self.stop.check()
            result = predicate()
            if result:
                return result
            if time.monotonic() >= deadline:
                raise StructureError(f"제한 시간 초과: {what}")
            self.stop.sleep(self.POLL)

    # ---- 요소 조회 ---------------------------------------------------------
    def one(self, selector: str, what: str) -> Locator:
        """정확히 하나의 보이는 요소. 0개/여러 개면 구조 변경으로 보고 중단."""
        self.stop.check()
        loc = self.page.locator(selector)
        n = loc.count()
        if n != 1:
            raise StructureError(f"{what}: 요소 {n}개 발견 (정확히 1개 필요) — 선택자 {selector!r}")
        if not loc.is_visible():
            raise StructureError(f"{what}: 요소가 화면에 보이지 않습니다")
        return loc

    def all(self, selector: str) -> list[Locator]:
        self.stop.check()
        return self.page.locator(selector).all()

    def text(self, selector: str, what: str) -> str:
        return self.one(selector, what).inner_text().strip()

    # ---- 입력 (모든 입력은 중지 확인 + 안전 검사를 거친다) -------------------
    def describe(self, loc: Locator) -> str:
        parts = [
            loc.inner_text(timeout=2000) if loc.evaluate("e => e.tagName") != "INPUT" else "",
            loc.get_attribute("value") or "",
            loc.get_attribute("aria-label") or "",
            loc.get_attribute("title") or "",
        ]
        return " ".join(p.strip() for p in parts if p)

    def click(self, loc: Locator, what: str) -> None:
        self.stop.check()
        label = self.describe(loc)
        if FORBIDDEN_CLICK.search(label):
            raise SafetyError(f"결제/주문 성격의 요소는 클릭하지 않습니다: {what} ({label!r})")
        if not loc.is_enabled() or loc.get_attribute("aria-disabled") == "true":
            raise StructureError(f"{what}: 비활성화된 요소입니다")
        loc.click()  # 좌표가 아닌 요소 기준 클릭

    def fill(self, loc: Locator, value: str, what: str) -> None:
        self.stop.check()
        if loc.get_attribute("type") in ("password",):
            raise SafetyError(f"비밀번호 입력란은 자동 입력하지 않습니다: {what}")
        loc.fill(value)

    def highlight(self, loc: Locator) -> None:
        """사용자 참고용 강조 표시 (클릭하지 않음)."""
        try:
            loc.evaluate("e => { e.style.outline = '3px solid #ff6a00'; e.style.outlineOffset = '1px'; }")
        except PlaywrightError:
            pass
