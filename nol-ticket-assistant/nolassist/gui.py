"""Tkinter GUI. 엔진은 작업 스레드에서 돌고, GUI 는 큐를 폴링해 상태를 갱신한다."""
from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

from .automation.engine import BookingEngine
from .config import BookerInfo, BookingRequest, SeatPreference
from .logging_setup import get_logger, setup_logging
from .mock_server import MockServer
from .profile import live_profile, mock_profile
from .stages import Stage
from .storage import Storage

log = get_logger()

PROGRESS = [Stage.PAGE_PREP, Stage.SHOW_SELECT, Stage.SEAT_SELECT, Stage.BOOKER_INFO, Stage.ORDER_SUMMARY]
SHORT = {Stage.PAGE_PREP: "1 준비", Stage.SHOW_SELECT: "2 날짜/회차", Stage.SEAT_SELECT: "3 좌석",
         Stage.BOOKER_INFO: "4 예매정보", Stage.ORDER_SUMMARY: "5 결제 전 중단"}
SCENARIOS = ["normal", "soldout", "noseats", "expired", "queue", "changed"]
LIVE_DEFAULT_URL = "https://nol.yanolja.com/ticket"


def data_dir() -> Path:
    base = os.environ.get("APPDATA") if sys.platform == "win32" else None
    return Path(base) / "NolAssist" if base else Path.home() / ".nolassist"


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.log_q: "queue.Queue[tuple[str, str]]" = queue.Queue()
        setup_logging(data_dir() / "logs", self.log_q)
        self.storage = Storage(data_dir() / "nolassist.db")
        self.server: MockServer | None = None
        self.engine: BookingEngine | None = None
        self.worker: threading.Thread | None = None

        root.title("NOL 티켓 예매 보조 (결제 전 자동 중단)")
        root.geometry("1120x800")
        root.minsize(860, 640)
        self._build()
        self._load_settings()
        self._on_mode_change()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._poll)

    # ---- 화면 구성 ---------------------------------------------------------
    def _build(self) -> None:
        pad = {"padx": 6, "pady": 3}
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="both", expand=True)
        left = ttk.Frame(top)
        left.pack(side="left", fill="y")
        right = ttk.Frame(top)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        # 모드
        mode = ttk.LabelFrame(left, text="모드", padding=6)
        mode.pack(fill="x", **pad)
        self.test_mode = tk.BooleanVar(value=True)
        ttk.Checkbutton(mode, text="테스트 모드 (로컬 모의 사이트, 실제 결제 없음)",
                        variable=self.test_mode, command=self._on_mode_change).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(mode, text="시나리오").grid(row=1, column=0, sticky="w")
        self.scenario = tk.StringVar(value="normal")
        self.scenario_box = ttk.Combobox(mode, textvariable=self.scenario, values=SCENARIOS, state="readonly", width=12)
        self.scenario_box.grid(row=1, column=1, sticky="w")
        self.scenario_box.bind("<<ComboboxSelected>>", lambda e: self._on_mode_change())
        self.headless = tk.BooleanVar(value=False)
        ttk.Checkbutton(mode, text="브라우저 숨김 (테스트 모드 전용)", variable=self.headless).grid(row=2, column=0, columnspan=2, sticky="w")

        # 공연
        show = ttk.LabelFrame(left, text="공연", padding=6)
        show.pack(fill="x", **pad)
        self.url = tk.StringVar()
        self.expected_title = tk.StringVar()
        self.date = tk.StringVar()
        self.round = tk.StringVar()
        self.start_at = tk.StringVar()
        self.title_var = tk.StringVar(value="(시작 후 페이지에서 확인)")
        rows = [("공연 URL", self.url, 44), ("공연명 포함 문구(검증용)", self.expected_title, 30),
                ("희망 날짜", self.date, 16), ("회차 값", self.round, 16),
                ("예매 시작 시간", self.start_at, 22)]
        for i, (label, var, w) in enumerate(rows):
            ttk.Label(show, text=label).grid(row=i, column=0, sticky="w")
            ttk.Entry(show, textvariable=var, width=w).grid(row=i, column=1, sticky="w")
        ttk.Label(show, text="시작 시간 형식: YYYY-MM-DD HH:MM:SS (비우면 즉시)", foreground="#666").grid(row=5, column=0, columnspan=2, sticky="w")
        ttk.Label(show, text="공연명").grid(row=6, column=0, sticky="w")
        ttk.Label(show, textvariable=self.title_var, foreground="#1f5f74", wraplength=300).grid(row=6, column=1, sticky="w")

        # 좌석
        seat = ttk.LabelFrame(left, text="좌석 조건", padding=6)
        seat.pack(fill="x", **pad)
        self.grades = tk.StringVar(value="VIP,R")
        self.qty = tk.IntVar(value=2)
        self.adjacent = tk.BooleanVar(value=True)
        self.front = tk.BooleanVar(value=True)
        self.max_row = tk.StringVar()
        ttk.Label(seat, text="등급(우선순위, 쉼표)").grid(row=0, column=0, sticky="w")
        ttk.Entry(seat, textvariable=self.grades, width=18).grid(row=0, column=1, sticky="w")
        ttk.Label(seat, text="매수").grid(row=1, column=0, sticky="w")
        ttk.Spinbox(seat, from_=1, to=4, textvariable=self.qty, width=5).grid(row=1, column=1, sticky="w")
        ttk.Label(seat, text="최대 열 번호(선택)").grid(row=2, column=0, sticky="w")
        ttk.Entry(seat, textvariable=self.max_row, width=6).grid(row=2, column=1, sticky="w")
        ttk.Checkbutton(seat, text="연석만", variable=self.adjacent).grid(row=3, column=0, sticky="w")
        ttk.Checkbutton(seat, text="앞 열 우선", variable=self.front).grid(row=3, column=1, sticky="w")

        # 예매자 (저장하지 않음)
        booker = ttk.LabelFrame(left, text="예매자 정보 (선택 · 저장/기록 안 함)", padding=6)
        booker.pack(fill="x", **pad)
        self.b_name, self.b_phone, self.b_email = tk.StringVar(), tk.StringVar(), tk.StringVar()
        for i, (label, var) in enumerate([("이름", self.b_name), ("휴대전화", self.b_phone), ("이메일", self.b_email)]):
            ttk.Label(booker, text=label).grid(row=i, column=0, sticky="w")
            ttk.Entry(booker, textvariable=var, width=26).grid(row=i, column=1, sticky="w")

        # 버튼
        btns = ttk.Frame(left)
        btns.pack(fill="x", **pad)
        self.start_btn = ttk.Button(btns, text="▶ 시작", command=self._start)
        self.start_btn.pack(side="left", padx=2)
        self.stop_btn = tk.Button(btns, text="■ 긴급 중지", command=self._stop, bg="#c62828", fg="white",
                                  activebackground="#8e0000", disabledforeground="#eeeeee")
        self._set_stop_enabled(False)
        self.stop_btn.pack(side="left", padx=2)
        self.close_btn = ttk.Button(btns, text="브라우저 닫기", command=self._close_browser, state="disabled")
        self.close_btn.pack(side="left", padx=2)

        # 오른쪽: 단계 / 안내 / 요약 / 로그
        stage = ttk.LabelFrame(right, text="현재 예매 단계", padding=6)
        stage.pack(fill="x")
        self.stage_var = tk.StringVar(value=Stage.IDLE.value)
        tk.Label(stage, textvariable=self.stage_var, font=("Malgun Gothic", 14, "bold"), fg="#1f5f74").pack(anchor="w")
        bar = ttk.Frame(stage)
        bar.pack(fill="x", pady=4)
        self.step_labels: dict[Stage, tk.Label] = {}
        for s in PROGRESS:
            lab = tk.Label(bar, text=SHORT[s], bg="#e6e9e8", padx=5, pady=3)
            lab.pack(side="left", padx=2)
            self.step_labels[s] = lab
        self.instruction = tk.StringVar()
        self.instr_label = tk.Label(stage, textvariable=self.instruction, fg="#b45309", wraplength=400, justify="left")
        self.instr_label.pack(anchor="w", fill="x")
        stage.bind("<Configure>", lambda e: self.instr_label.configure(wraplength=max(200, e.width - 20)))

        summ = ttk.LabelFrame(right, text="주문 요약 (결제는 브라우저에서 직접)", padding=6)
        summ.pack(fill="x", pady=6)
        self.summary_vars = {k: tk.StringVar(value="-") for k in ("title", "date", "round", "seats", "price")}
        for i, (k, label) in enumerate([("title", "공연명"), ("date", "날짜"), ("round", "회차"), ("seats", "좌석"), ("price", "금액")]):
            ttk.Label(summ, text=label, width=8).grid(row=i, column=0, sticky="w")
            ttk.Label(summ, textvariable=self.summary_vars[k], wraplength=440).grid(row=i, column=1, sticky="w")
        self.recommend = tk.StringVar(value="")
        ttk.Label(summ, text="추천 좌석", width=8).grid(row=5, column=0, sticky="w")
        ttk.Label(summ, textvariable=self.recommend, wraplength=440).grid(row=5, column=1, sticky="w")

        logf = ttk.LabelFrame(right, text="진행 / 오류 로그", padding=4)
        logf.pack(fill="both", expand=True)
        self.log_text = tk.Text(logf, height=16, state="disabled", wrap="word", font=("Consolas", 9))
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.log_text.tag_configure("WARNING", foreground="#b45309")
        self.log_text.tag_configure("ERROR", foreground="#c62828")

    # ---- 설정 저장/복원 (개인정보 제외) -------------------------------------
    def _settings(self) -> dict:
        return {"live_url": self.url.get() if not self.test_mode.get() else self._saved_live_url,
                "expected_title": self.expected_title.get(), "date": self.date.get(), "round": self.round.get(),
                "grades": self.grades.get(), "qty": self.qty.get(), "adjacent": self.adjacent.get(),
                "front": self.front.get(), "max_row": self.max_row.get()}

    def _load_settings(self) -> None:
        s = self.storage.load_setting("last") or {}
        self._saved_live_url = s.get("live_url") or LIVE_DEFAULT_URL
        self.expected_title.set(s.get("expected_title", "테스트 오페라"))
        self.date.set(s.get("date", "2026-11-15"))
        self.round.set(s.get("round", "2"))
        self.grades.set(s.get("grades", "VIP,R"))
        self.qty.set(s.get("qty", 2))
        self.adjacent.set(s.get("adjacent", True))
        self.front.set(s.get("front", True))
        self.max_row.set(s.get("max_row", ""))

    def _on_mode_change(self) -> None:
        if self.test_mode.get():
            if self.url.get() and not self.url.get().startswith("http://127.0.0.1"):
                self._saved_live_url = self.url.get()
            if self.server is None:
                self.server = MockServer().start()
            self.url.set(self.server.show_url(self.scenario.get()))
            self.scenario_box.configure(state="readonly")
        else:
            self.url.set(self._saved_live_url)
            self.scenario_box.configure(state="disabled")
            self.headless.set(False)

    # ---- 실행 제어 ---------------------------------------------------------
    def _build_request(self) -> BookingRequest:
        start_at = None
        if self.start_at.get().strip():
            try:
                start_at = datetime.strptime(self.start_at.get().strip(), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                raise ValueError("시작 시간 형식이 올바르지 않습니다 (YYYY-MM-DD HH:MM:SS)")
        max_row = int(self.max_row.get()) if self.max_row.get().strip() else None
        req = BookingRequest(
            url=self.url.get().strip(), date=self.date.get().strip(), round=self.round.get().strip(),
            seat=SeatPreference(grades=[g.strip() for g in self.grades.get().split(",") if g.strip()],
                                quantity=int(self.qty.get()), adjacent=self.adjacent.get(),
                                prefer_front=self.front.get(), max_row=max_row),
            expected_title=self.expected_title.get().strip(), start_at=start_at, test_mode=self.test_mode.get(),
            booker=BookerInfo(self.b_name.get().strip(), self.b_phone.get().strip(), self.b_email.get().strip()),
        )
        req.validate()
        return req

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("실행 중", "이미 실행 중입니다. 브라우저를 닫은 뒤 다시 시작하세요.")
            return
        try:
            req = self._build_request()
        except (ValueError, tk.TclError) as e:
            messagebox.showerror("입력 오류", str(e))
            return
        profile = mock_profile() if req.test_mode else live_profile()
        if not req.test_mode:
            ok = messagebox.askokcancel(
                "실사용 모드 확인",
                "실제 사이트에 접속합니다.\n\n"
                "• 사이트 이용약관이 자동화 도구 사용을 허용하는지 직접 확인하세요.\n"
                "• 대기열·보안문자·구매 제한은 우회하지 않으며, 표시되면 직접 처리해야 합니다.\n"
                "• 결제는 절대 자동으로 진행하지 않습니다.\n"
                + ("" if profile.verified else
                   "\n현재 실사이트 프로필은 '미검증' 상태입니다. 페이지를 연 뒤 자동 진행 없이 중단되며, "
                   "이후 과정은 브라우저에서 직접 진행해야 합니다.\n")
                + "\n계속할까요?")
            if not ok:
                return
        self.storage.save_setting("last", self._settings())
        self._reset_view()
        self.engine = BookingEngine(
            req, profile, storage=self.storage,
            headless=self.headless.get() and req.test_mode,
            user_data_dir=None if req.test_mode else data_dir() / "browser-profile",
            on_info=lambda k, v: self.events.put(("info", k, v)),
        )
        self.engine.machine.subscribe(lambda old, new: self.events.put(("stage", new)))
        self.worker = threading.Thread(target=self._run_engine, args=(self.engine,), daemon=True)
        self.worker.start()
        self.start_btn.configure(state="disabled")
        self._set_stop_enabled(True)
        self.close_btn.configure(state="normal")

    def _set_stop_enabled(self, on: bool) -> None:
        self.stop_btn.configure(state="normal" if on else "disabled", bg="#c62828" if on else "#b0b0b0")

    def _run_engine(self, engine: BookingEngine) -> None:
        result = engine.run()
        self.events.put(("done", result))

    def _stop(self) -> None:
        if self.engine:
            self.engine.emergency_stop()
            self._set_stop_enabled(False)

    def _close_browser(self) -> None:
        if self.engine:
            self.engine.emergency_stop()
            self.engine.close_browser()

    def _reset_view(self) -> None:
        self.title_var.set("(확인 중)")
        self.instruction.set("")
        self.recommend.set("")
        for v in self.summary_vars.values():
            v.set("-")
        self._paint_stage(Stage.IDLE)

    def _paint_stage(self, stage: Stage) -> None:
        self.stage_var.set(stage.value)
        reached = PROGRESS.index(stage) if stage in PROGRESS else -1
        for i, s in enumerate(PROGRESS):
            if stage is s:
                color = "#f59e0b" if s is Stage.ORDER_SUMMARY else "#1f5f74"
                self.step_labels[s].configure(bg=color, fg="white")
            elif i < reached:
                self.step_labels[s].configure(bg="#9cc3cf", fg="black")
            elif stage in PROGRESS or stage is Stage.IDLE:
                self.step_labels[s].configure(bg="#e6e9e8", fg="black")

    # ---- 큐 폴링 -----------------------------------------------------------
    def _poll(self) -> None:
        while not self.log_q.empty():
            level, line = self.log_q.get_nowait()
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n", level)
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        while not self.events.empty():
            ev = self.events.get_nowait()
            if ev[0] == "stage":
                self._paint_stage(ev[1])
                if ev[1].is_terminal:
                    self._set_stop_enabled(False)
                if ev[1] is Stage.ORDER_SUMMARY:
                    self.root.bell()
                    self.instruction.set("주문 요약에서 자동 실행을 멈췄습니다. 브라우저에서 내용을 확인하고 직접 결제하세요.")
            elif ev[0] == "info":
                _, key, value = ev
                if key == "title":
                    self.title_var.set(value)
                elif key == "instruction":
                    self.instruction.set(value)
                elif key == "seats":
                    self.recommend.set(value)
                elif key.startswith("summary."):
                    self.summary_vars[key.split(".", 1)[1]].set(value or "-")
            elif ev[0] == "done":
                result = ev[1]
                if result.final_stage is not Stage.ORDER_SUMMARY:
                    self.instruction.set(result.message)
                self.start_btn.configure(state="normal")
                self._set_stop_enabled(False)
                self.close_btn.configure(state="disabled")
                self.engine = None
        self.root.after(100, self._poll)

    def _on_close(self) -> None:
        if self.engine:
            self.engine.emergency_stop()
            self.engine.close_browser()
        if self.worker:
            self.worker.join(timeout=5)
        if self.server:
            self.server.stop()
        self.storage.close()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()
