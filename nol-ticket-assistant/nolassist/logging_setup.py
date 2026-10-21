"""로깅 설정과 개인정보 마스킹."""
from __future__ import annotations

import logging
import queue
import re
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "nolassist"

_PATTERNS = [
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),                      # 이메일
    re.compile(r"\b01[016789][-\s.]?\d{3,4}[-\s.]?\d{4}\b"),      # 휴대전화
    re.compile(r"\b\d{6}[-\s]?[1-4]\d{6}\b"),                     # 주민등록번호
    re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b"),                   # 카드번호
]


class RedactingFilter(logging.Filter):
    """정규식 + 실행 중 등록된 민감값을 [REDACTED] 로 치환."""

    def __init__(self) -> None:
        super().__init__()
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def register(self, *values: str) -> None:
        with self._lock:
            self._secrets.update(v for v in values if v and len(v) >= 2)

    def clear(self) -> None:
        with self._lock:
            self._secrets.clear()

    def redact(self, text: str) -> str:
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
        for s in secrets:
            text = text.replace(s, "[REDACTED]")
        for p in _PATTERNS:
            text = p.sub("[REDACTED]", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = None
        return True


redactor = RedactingFilter()


class QueueHandler(logging.Handler):
    """GUI 스레드가 꺼내 갈 수 있도록 포맷된 로그를 큐에 넣는다."""

    def __init__(self, q: "queue.Queue[tuple[str, str]]") -> None:
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        self.q.put((record.levelname, self.format(record)))


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def setup_logging(log_dir: Path | None = None, gui_queue: "queue.Queue | None" = None) -> logging.Logger:
    logger = get_logger()
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.filters.clear()
    logger.addFilter(redactor)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(log_dir / "nolassist.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(fh)
    if gui_queue is not None:
        qh = QueueHandler(gui_queue)
        qh.setFormatter(fmt)
        logger.addHandler(qh)
    if not logger.handlers:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return logger
