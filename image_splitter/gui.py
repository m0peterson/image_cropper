# -*- coding: utf-8 -*-
"""Tkinter front end. Windows 7 .. 11, no dependencies beyond Pillow.

The heavy work (decoding, profiling, writing) happens on worker threads; the Tk
thread only ever touches widgets. Results come back through a queue that is
pumped from an `after` loop.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import traceback
from typing import List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from . import core, preview

APP_TITLE = "Разрезалка длинных картинок"
CONFIG_NAME = "image_splitter.json"

PRESETS = [
    ("1:1, квадрат", 1.0, 1.0),
    ("4:5, вертикальный пост", 4.0, 5.0),
    ("3:4, вертикальный", 3.0, 4.0),
    ("2:3, вертикальный", 2.0, 3.0),
    ("9:16, сторис", 9.0, 16.0),
    ("1:1.41, лист A4", 1000.0, 1414.0),
    ("4:3, горизонтальный", 4.0, 3.0),
    ("3:2, горизонтальный", 3.0, 2.0),
    ("16:9, широкий", 16.0, 9.0),
    ("Своё значение", None, None),
]

MODES = [
    ("Авто: подобрать число частей", core.MODE_AUTO),
    ("Ровно N частей поровну", core.MODE_COUNT),
    ("Точное соотношение у каждой части", core.MODE_EXACT),
]

TAILS = [
    ("продлить назад (все части одного размера)", core.TAIL_EXTEND),
    ("оставить короткий хвост", core.TAIL_KEEP),
    ("дополнить фоном до нужного размера", core.TAIL_PAD),
    ("отбросить хвост", core.TAIL_DROP),
]

AXES = [
    ("авто", core.AXIS_AUTO),
    ("горизонтальные полосы", core.AXIS_Y),
    ("вертикальные полосы", core.AXIS_X),
]

FORMATS = [("PNG", "png"), ("JPEG", "jpeg"), ("WEBP", "webp"),
           ("как у исходника", "same")]

HELP_TEXT = """Что делает программа

Берёт картинку с неудобным соотношением сторон (например 1240 x 11000)
и режет её на несколько частей с нормальным соотношением.

Режимы:
  • Авто: программа сама считает, на сколько частей поделить, чтобы
    каждая была как можно ближе к выбранному соотношению. Все части
    одинаковые, ничего не теряется.
  • Ровно N частей: делит поровну на заданное число кусков.
  • Точное соотношение: каждая часть строго заданного размера,
    а остаток («хвост») обрабатывается так, как выбрано в списке «Хвост».

В режиме «Точное соотношение» умная резка отключается: иначе линии
сдвигались бы и размеры частей переставали быть одинаковыми.

Перекрытие: сколько пикселей соседние части берут друг у друга.
Полезно, когда на границе оказывается строка текста: с перекрытием
50-80 px она целиком попадает в обе части.

Умная резка: вместо того чтобы резать строго по расчётной линии,
программа ищет рядом (в пределах окна поиска) самую «спокойную» строку
пикселей: пустое место между абзацами, однотонную полосу фона. Так рез
реже проходит по лицам и буквам.

Горячие клавиши: Ctrl+O добавить файлы, Delete убрать выбранное,
F5 пересчитать план, Ctrl+Enter разрезать.
"""


class Cancelled(Exception):
    pass


def config_path() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        folder = os.path.join(base, "ImageSplitter")
    else:
        folder = os.path.join(os.path.expanduser("~"), ".config", "image_splitter")
    try:
        if not os.path.isdir(folder):
            os.makedirs(folder)
    except Exception:
        return os.path.join(os.path.expanduser("~"), "." + CONFIG_NAME)
    return os.path.join(folder, CONFIG_NAME)


def resource_path(*parts: str) -> str:
    """Works both from source and from inside a PyInstaller bundle."""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def apply_icon(root: tk.Tk) -> None:
    try:
        ico = resource_path("assets", "icon.ico")
        if os.name == "nt" and os.path.isfile(ico):
            root.iconbitmap(ico)
            return
        png = resource_path("assets", "icon.png")
        if os.path.isfile(png):
            root._icon_image = tk.PhotoImage(file=png)   # keep a reference alive
            root.iconphoto(True, root._icon_image)
    except Exception:
        pass


def enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # Win 8.1+
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()       # Win 7
    except Exception:
        pass


class App(object):
    def __init__(self, root: tk.Tk, dnd_flavour=None):
        self.root = root
        self.dnd_flavour = dnd_flavour
        self.files: List[str] = []
        self.image: Optional[Image.Image] = None
        self.proxy: Optional[Image.Image] = None
        self.profile = None
        self.profile_axis = None
        self.plan: Optional[core.Plan] = None
        self.current: Optional[str] = None
        self.photo = None
        self.load_token = 0
        self.bus: "queue.Queue" = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self._redraw_job = None
        self._plan_job = None

        root.title(APP_TITLE)
        root.geometry("1080x760")
        root.minsize(960, 640)

        self._make_vars()
        self._build()
        self._fit_window()
        self._load_config()
        self._bind_traces()
        self._bind_keys()
        self._enable_dnd()
        self._refresh_enabled()
        self._request_plan()
        root.after(60, self._pump)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ vars
    def _make_vars(self) -> None:
        self.var_preset = tk.StringVar(value=PRESETS[1][0])
        self.var_rw = tk.StringVar(value="4")
        self.var_rh = tk.StringVar(value="5")
        self.var_mode = tk.StringVar(value=core.MODE_AUTO)
        self.var_count = tk.StringVar(value="3")
        self.var_overlap = tk.StringVar(value="0")
        self.var_smart = tk.BooleanVar(value=True)
        self.var_window = tk.StringVar(value="12")
        self.var_tail = tk.StringVar(value=TAILS[0][0])
        self.var_axis = tk.StringVar(value=AXES[0][0])
        self.var_beside = tk.BooleanVar(value=True)
        self.var_outdir = tk.StringVar(value="")
        self.var_format = tk.StringVar(value=FORMATS[0][0])
        self.var_quality = tk.StringVar(value="92")
        self.var_pattern = tk.StringVar(value="{stem}_{i:02d}")
        self.var_open_after = tk.BooleanVar(value=True)
        self.var_preview_mode = tk.StringVar(value=preview.MODE_SOURCE)
        self.var_summary = tk.StringVar(value="Добавьте картинку.")
        self.var_status = tk.StringVar(value="Готово.")
        self.var_bg = tk.StringVar(value="#ffffff")

    # --------------------------------------------------------------- widgets
    def _build(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=0, minsize=470)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        left = ttk.Frame(root, padding=(8, 8, 4, 4))
        left.grid(row=0, column=0, sticky="nsew")
        left.columnconfigure(0, weight=1)
        left.rowconfigure(4, weight=1)      # spacer soaks up the extra height
        self.left = left

        self._build_files(left)
        self._build_split(left)
        self._build_output(left)
        self._build_actions(left)
        ttk.Frame(left).grid(row=4, column=0, sticky="nsew")
        self._build_preview(root)
        self._build_log(root)

    # ----------------------------------------------------------------- files
    def _build_files(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="Файлы", padding=6)
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(0, weight=1)

        wrap = ttk.Frame(box)
        wrap.grid(row=0, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)

        self.listbox = tk.Listbox(wrap, height=4, activestyle="dotbox",
                                  exportselection=False)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(wrap, orient="vertical", command=self.listbox.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=bar.set)
        self.listbox.bind("<<ListboxSelect>>", lambda e: self._on_select())

        row = ttk.Frame(box)
        row.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(row, text="Добавить…", command=self._add_files).pack(side="left")
        ttk.Button(row, text="Убрать", command=self._remove_selected).pack(side="left", padx=4)
        ttk.Button(row, text="Очистить", command=self._clear_files).pack(side="left")
        ttk.Button(row, text="Справка", command=self._show_help).pack(side="right")

    # ----------------------------------------------------------------- split
    def _build_split(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="Нарезка", padding=6)
        box.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="Соотношение части:").grid(row=0, column=0, sticky="w")
        self.cb_preset = ttk.Combobox(box, textvariable=self.var_preset, state="readonly",
                                      values=[p[0] for p in PRESETS], width=28)
        self.cb_preset.grid(row=0, column=1, sticky="ew", padx=(6, 6))
        self.cb_preset.bind("<<ComboboxSelected>>", self._on_preset)

        ratio = ttk.Frame(box)
        ratio.grid(row=0, column=2, sticky="e")
        ttk.Entry(ratio, textvariable=self.var_rw, width=6).pack(side="left")
        ttk.Label(ratio, text=" : ").pack(side="left")
        ttk.Entry(ratio, textvariable=self.var_rh, width=6).pack(side="left")

        mrow = ttk.Frame(box)
        mrow.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        for text, value in MODES:
            ttk.Radiobutton(mrow, text=text, value=value,
                            variable=self.var_mode).pack(anchor="w")

        nums = ttk.Frame(box)
        nums.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Label(nums, text="Частей:").pack(side="left")
        self.sp_count = ttk.Spinbox(nums, from_=1, to=500, width=5,
                                    textvariable=self.var_count)
        self.sp_count.pack(side="left", padx=(4, 12))
        ttk.Label(nums, text="Перекрытие, px:").pack(side="left")
        ttk.Spinbox(nums, from_=0, to=5000, increment=10, width=6,
                    textvariable=self.var_overlap).pack(side="left", padx=(4, 12))
        ttk.Label(nums, text="Ось:").pack(side="left")
        self.cb_axis = ttk.Combobox(nums, textvariable=self.var_axis, state="readonly",
                                    values=[a[0] for a in AXES], width=20)
        self.cb_axis.pack(side="left", padx=(4, 0))

        srow = ttk.Frame(box)
        srow.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.chk_smart = ttk.Checkbutton(
            srow, text="Умная резка: искать пустое место, окно ±",
            variable=self.var_smart)
        self.chk_smart.pack(side="left")
        self.sp_window = ttk.Spinbox(srow, from_=1, to=40, width=4,
                                     textvariable=self.var_window)
        self.sp_window.pack(side="left", padx=(4, 2))
        ttk.Label(srow, text="% от части").pack(side="left")

        trow = ttk.Frame(box)
        trow.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Label(trow, text="Хвост:").pack(side="left")
        self.cb_tail = ttk.Combobox(trow, textvariable=self.var_tail, state="readonly",
                                    values=[t[0] for t in TAILS], width=40)
        self.cb_tail.pack(side="left", padx=(4, 0))

    # ---------------------------------------------------------------- output
    def _build_output(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="Сохранение", padding=6)
        box.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        box.columnconfigure(1, weight=1)

        ttk.Checkbutton(box, text="Рядом с исходником, в папку «<имя>_parts»",
                        variable=self.var_beside,
                        command=self._refresh_enabled).grid(row=0, column=0,
                                                            columnspan=3, sticky="w")

        ttk.Label(box, text="Папка:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.e_outdir = ttk.Entry(box, textvariable=self.var_outdir)
        self.e_outdir.grid(row=1, column=1, sticky="ew", padx=6, pady=(4, 0))
        self.btn_outdir = ttk.Button(box, text="Обзор…", command=self._pick_dir)
        self.btn_outdir.grid(row=1, column=2, sticky="e", pady=(4, 0))

        frow = ttk.Frame(box)
        frow.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Label(frow, text="Формат:").pack(side="left")
        self.cb_format = ttk.Combobox(frow, textvariable=self.var_format, state="readonly",
                                      values=[f[0] for f in FORMATS], width=18)
        self.cb_format.pack(side="left", padx=(4, 12))
        ttk.Label(frow, text="Качество:").pack(side="left")
        self.sp_quality = ttk.Spinbox(frow, from_=1, to=100, width=5,
                                      textvariable=self.var_quality)
        self.sp_quality.pack(side="left", padx=(4, 12))
        ttk.Label(frow, text="Фон:").pack(side="left")
        self.e_bg = ttk.Entry(frow, textvariable=self.var_bg, width=9)
        self.e_bg.pack(side="left", padx=(4, 0))

        prow = ttk.Frame(box)
        prow.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Label(prow, text="Имя файлов:").pack(side="left")
        ttk.Entry(prow, textvariable=self.var_pattern, width=22).pack(side="left", padx=4)
        ttk.Label(prow, text="{stem} {i} {n} {w} {h}").pack(side="left")

        ttk.Checkbutton(box, text="Открыть папку после нарезки",
                        variable=self.var_open_after).grid(row=4, column=0,
                                                           columnspan=3, sticky="w",
                                                           pady=(6, 0))

    # --------------------------------------------------------------- actions
    def _build_actions(self, parent: ttk.Frame) -> None:
        box = ttk.Frame(parent)
        box.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        box.columnconfigure(0, weight=1)

        self.lbl_summary = ttk.Label(box, textvariable=self.var_summary,
                                     wraplength=440, justify="left")
        self.lbl_summary.grid(row=0, column=0, columnspan=2, sticky="ew")

        self.progress = ttk.Progressbar(box, mode="determinate")
        self.progress.grid(row=1, column=0, sticky="ew", pady=(6, 0))

        btns = ttk.Frame(box)
        btns.grid(row=1, column=1, sticky="e", padx=(8, 0), pady=(6, 0))
        self.btn_run = ttk.Button(btns, text="Разрезать", command=self._start_split)
        self.btn_run.pack(side="left")
        self.btn_cancel = ttk.Button(btns, text="Стоп", command=self._cancel_split,
                                     state="disabled")
        self.btn_cancel.pack(side="left", padx=(4, 0))

    # --------------------------------------------------------------- preview
    def _build_preview(self, root: tk.Tk) -> None:
        box = ttk.LabelFrame(root, text="Предпросмотр", padding=6)
        box.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=(8, 4))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(1, weight=1)

        row = ttk.Frame(box)
        row.grid(row=0, column=0, sticky="ew")
        ttk.Radiobutton(row, text="Исходник с линиями реза", value=preview.MODE_SOURCE,
                        variable=self.var_preview_mode,
                        command=self._request_redraw).pack(side="left")
        ttk.Radiobutton(row, text="Готовые части", value=preview.MODE_PARTS,
                        variable=self.var_preview_mode,
                        command=self._request_redraw).pack(side="left", padx=(10, 0))

        self.canvas = tk.Canvas(box, background="#26282c", highlightthickness=0,
                                width=420, height=520)
        self.canvas.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        self.canvas.bind("<Configure>", lambda e: self._request_redraw())

    def _build_log(self, root: tk.Tk) -> None:
        box = ttk.Frame(root, padding=(8, 0, 8, 6))
        box.grid(row=1, column=0, columnspan=2, sticky="ew")
        box.columnconfigure(0, weight=1)

        ttk.Label(box, textvariable=self.var_status).grid(row=0, column=0, sticky="w")
        wrap = ttk.Frame(box)
        wrap.grid(row=1, column=0, sticky="ew")
        wrap.columnconfigure(0, weight=1)
        self.log = tk.Text(wrap, height=4, wrap="none", state="disabled")
        self.log.grid(row=0, column=0, sticky="ew")
        bar = ttk.Scrollbar(wrap, orient="vertical", command=self.log.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=bar.set)

    def _fit_window(self) -> None:
        """Size the window from the widgets themselves: fonts and DPI vary."""
        self.root.update_idletasks()
        need_w = self.left.winfo_reqwidth() + 430
        need_h = self.left.winfo_reqheight() + self.log.winfo_reqheight() + 70
        need_w = max(960, min(1500, need_w))
        need_h = max(640, min(1000, need_h))
        self.root.minsize(need_w, need_h)
        screen_h = self.root.winfo_screenheight()
        self.root.geometry("{0}x{1}".format(need_w + 40,
                                            min(need_h + 60, max(need_h, screen_h - 80))))

    # ------------------------------------------------------------- behaviour
    def _bind_traces(self) -> None:
        for var in (self.var_rw, self.var_rh, self.var_mode, self.var_count,
                    self.var_overlap, self.var_smart, self.var_window,
                    self.var_tail, self.var_axis):
            var.trace_add("write", lambda *a: self._request_plan())
        for var in (self.var_format, self.var_beside, self.var_mode, self.var_smart):
            var.trace_add("write", lambda *a: self._refresh_enabled())
        for var in (self.var_rw, self.var_rh):
            var.trace_add("write", lambda *a: self._sync_preset())

    def _bind_keys(self) -> None:
        self.root.bind("<Control-o>", lambda e: self._add_files())
        self.root.bind("<Delete>", lambda e: self._remove_selected())
        self.root.bind("<F5>", lambda e: self._request_plan(force=True))
        self.root.bind("<Control-Return>", lambda e: self._start_split())

    def _enable_dnd(self) -> None:
        if not self.dnd_flavour:
            return
        try:
            self.listbox.drop_target_register(self.dnd_flavour)
            self.listbox.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            pass

    def _on_drop(self, event) -> None:
        try:
            paths = self.root.tk.splitlist(event.data)
        except Exception:
            paths = [event.data]
        self._add_paths([p for p in paths if os.path.isfile(p)])

    def _refresh_enabled(self) -> None:
        mode = self.var_mode.get()
        self.sp_count.configure(state="normal" if mode == core.MODE_COUNT else "disabled")
        self.cb_tail.configure(state="readonly" if mode == core.MODE_EXACT else "disabled")
        exact = mode == core.MODE_EXACT
        self.chk_smart.configure(state="disabled" if exact else "normal")
        self.sp_window.configure(state="disabled" if exact or not self.var_smart.get()
                                 else "normal")
        state = "disabled" if self.var_beside.get() else "normal"
        self.e_outdir.configure(state=state)
        self.btn_outdir.configure(state=state)
        fmt = self._format_value()
        self.sp_quality.configure(state="normal" if fmt in ("jpeg", "webp", "same")
                                  else "disabled")

    def _sync_preset(self) -> None:
        rw, rh = self._ratio_values()
        for name, pw, ph in PRESETS:
            if pw and ph and abs(rw / rh - pw / ph) < 1e-6:
                if self.var_preset.get() != name:
                    self.var_preset.set(name)
                return
        self.var_preset.set(PRESETS[-1][0])

    def _on_preset(self, _event=None) -> None:
        name = self.var_preset.get()
        for pname, pw, ph in PRESETS:
            if pname == name and pw:
                self.var_rw.set(self._num_text(pw))
                self.var_rh.set(self._num_text(ph))
                return

    @staticmethod
    def _num_text(value: float) -> str:
        return str(int(value)) if float(value).is_integer() else str(value)

    # ---------------------------------------------------------------- inputs
    def _float(self, var: tk.StringVar, default: float, low: float, high: float) -> float:
        try:
            value = float(str(var.get()).replace(",", "."))
        except (TypeError, ValueError):
            return default
        if value != value or value <= 0:
            return default
        return max(low, min(high, value))

    def _int(self, var: tk.StringVar, default: int, low: int, high: int) -> int:
        try:
            value = int(float(str(var.get()).replace(",", ".")))
        except (TypeError, ValueError):
            return default
        return max(low, min(high, value))

    def _ratio_values(self):
        return (self._float(self.var_rw, 4.0, 0.01, 100000.0),
                self._float(self.var_rh, 5.0, 0.01, 100000.0))

    def _combo_value(self, var: tk.StringVar, table, default):
        label = var.get()
        for name, value in table:
            if name == label:
                return value
        return default

    def _format_value(self) -> str:
        return self._combo_value(self.var_format, FORMATS, "png")

    def _background(self):
        text = (self.var_bg.get() or "").strip().lstrip("#")
        if len(text) == 3:
            text = "".join(c * 2 for c in text)
        if len(text) == 6:
            try:
                return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
            except ValueError:
                pass
        return (255, 255, 255)

    def settings(self) -> core.Settings:
        rw, rh = self._ratio_values()
        return core.Settings(
            ratio_w=rw,
            ratio_h=rh,
            mode=self.var_mode.get() or core.MODE_AUTO,
            count=self._int(self.var_count, 3, 1, 500),
            overlap=self._int(self.var_overlap, 0, 0, 100000),
            smart=bool(self.var_smart.get()) and self.var_mode.get() != core.MODE_EXACT,
            smart_window=self._int(self.var_window, 12, 1, 40) / 100.0,
            tail=self._combo_value(self.var_tail, TAILS, core.TAIL_EXTEND),
            axis=self._combo_value(self.var_axis, AXES, core.AXIS_AUTO),
        )

    # ----------------------------------------------------------- file list
    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Выберите картинки",
            filetypes=[("Картинки", " ".join("*" + e for e in core.READABLE_EXT)),
                       ("Все файлы", "*.*")])
        self._add_paths(list(paths))

    def _add_paths(self, paths: List[str]) -> None:
        added = 0
        for path in paths:
            path = os.path.abspath(path)
            if path in self.files or not os.path.isfile(path):
                continue
            label = os.path.basename(path)
            try:
                with Image.open(path) as probe:
                    label = "{0}   {1}x{2}".format(label, probe.width, probe.height)
            except Exception as exc:
                self._log("Не удалось прочитать {0}: {1}".format(path, exc))
                continue
            self.files.append(path)
            self.listbox.insert("end", label)
            added += 1
        if added and self.listbox.size():
            if not self.listbox.curselection():
                self.listbox.selection_set(0)
                self._on_select()
            self._set_status("Добавлено файлов: {0}".format(added))

    def _remove_selected(self) -> None:
        for index in sorted(self.listbox.curselection(), reverse=True):
            self.listbox.delete(index)
            del self.files[index]
        if not self.files:
            self._clear_image()
        elif self.listbox.size():
            self.listbox.selection_set(0)
            self._on_select()

    def _clear_files(self) -> None:
        self.files = []
        self.listbox.delete(0, "end")
        self._clear_image()

    def _clear_image(self) -> None:
        if self.image is not None:
            try:
                self.image.close()
            except Exception:
                pass
        self.image = None
        self.proxy = None
        self.profile = None
        self.profile_axis = None
        self.plan = None
        self.current = None
        self.canvas.delete("all")
        self.var_summary.set("Добавьте картинку.")

    def _on_select(self) -> None:
        selection = self.listbox.curselection()
        if not selection:
            return
        path = self.files[selection[0]]
        if path == self.current:
            return
        self._load_async(path)

    # ------------------------------------------------------------- threading
    def _load_async(self, path: str) -> None:
        self.load_token += 1
        token = self.load_token
        self.current = path
        self._set_status("Открываю {0}…".format(os.path.basename(path)))

        def work():
            try:
                image = core.load_image(path)
                proxy = preview.make_proxy(image)
                axis = core.resolve_axis(image.width, image.height,
                                         self._safe_axis())
                profile = core.activity_profile(image, axis)
                self.bus.put(("loaded", token, path, image, proxy, profile))
            except Exception as exc:
                self.bus.put(("load_failed", token, path, exc,
                              traceback.format_exc()))

        threading.Thread(target=work, daemon=True).start()

    def _safe_axis(self) -> str:
        try:
            return self._combo_value(self.var_axis, AXES, core.AXIS_AUTO)
        except Exception:
            return core.AXIS_AUTO

    def _pump(self) -> None:
        try:
            while True:
                message = self.bus.get_nowait()
                self._handle(message)
        except queue.Empty:
            pass
        self.root.after(60, self._pump)

    def _handle(self, message) -> None:
        kind = message[0]
        if kind == "loaded":
            _, token, path, image, proxy, profile = message
            if token != self.load_token:
                image.close()
                return
            if self.image is not None:
                try:
                    self.image.close()
                except Exception:
                    pass
            self.image, self.proxy, self.profile = image, proxy, profile
            self.profile_axis = core.resolve_axis(image.width, image.height,
                                                  self._safe_axis())
            self._set_status("{0}: {1}x{2}".format(os.path.basename(path),
                                                    image.width, image.height))
            self._request_plan(force=True)
        elif kind == "load_failed":
            _, token, path, exc, tb = message
            if token == self.load_token:
                self._set_status("Ошибка открытия.")
                self._log("Не открылось {0}: {1}".format(path, exc))
        elif kind == "profiled":
            _, token, axis, profile = message
            if token == self.load_token:
                self.profile = profile
                self.profile_axis = axis
                self._request_plan(force=True)
        elif kind == "progress":
            _, done, total, path = message
            self.progress.configure(maximum=max(1, total), value=done)
            self._set_status("{0} ({1}/{2})".format(os.path.basename(path), done, total))
        elif kind == "file_done":
            _, path, written, out_dir = message
            self._log("{0} → {1} шт. в {2}".format(os.path.basename(path),
                                                   len(written), out_dir))
        elif kind == "file_failed":
            _, path, exc = message
            self._log("ОШИБКА {0}: {1}".format(os.path.basename(path), exc))
        elif kind == "done":
            _, total, folders, cancelled = message
            self._finish_split(total, folders, cancelled)

    # ----------------------------------------------------------------- plan
    def _request_plan(self, force: bool = False) -> None:
        if self._plan_job is not None:
            try:
                self.root.after_cancel(self._plan_job)
            except Exception:
                pass
        delay = 1 if force else 120
        self._plan_job = self.root.after(delay, self._recompute)

    def _recompute(self) -> None:
        self._plan_job = None
        self._refresh_enabled()
        if self.image is None:
            self.var_summary.set("Добавьте картинку.")
            return
        settings = self.settings()
        axis = core.resolve_axis(self.image.width, self.image.height, settings.axis)
        profile = None
        if settings.smart:
            needed = self.image.height if axis == core.AXIS_Y else self.image.width
            if self.profile is not None and len(self.profile) == needed:
                profile = self.profile
            else:
                self._profile_async(axis)
        try:
            self.plan = core.plan_pieces(self.image.width, self.image.height,
                                         settings, profile)
        except Exception as exc:
            self.var_summary.set("Не удалось рассчитать: {0}".format(exc))
            return
        source_ratio = core.ratio_text(self.image.width / float(self.image.height))
        text = "Исходник {0}x{1} ({2}).  →  {3}".format(
            self.image.width, self.image.height, source_ratio,
            core.describe_plan(self.plan))
        if self.plan.count == 1:
            actual = self.plan.piece_ratio(self.plan.pieces[0])
            target = settings.ratio()
            if abs(actual - target) > 0.25 * target:
                text += ("\nРезать не получилось: при такой оси и соотношении "
                         "выходит одна часть. Поменяйте соотношение или ось.")
            else:
                text += "\nОдна часть: соотношение уже подходит, резать нечего."
        if self.plan.note:
            text += "\n" + self.plan.note
        self.var_summary.set(text)
        self._request_redraw()

    def _profile_async(self, axis: str) -> None:
        """The cut axis changed, so the cached activity profile no longer fits."""
        if self.image is None or self.profile_axis == axis:
            return
        self.profile_axis = axis
        token = self.load_token
        image = self.image

        def work():
            try:
                self.bus.put(("profiled", token, axis,
                              core.activity_profile(image, axis)))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _request_redraw(self) -> None:
        if self._redraw_job is not None:
            try:
                self.root.after_cancel(self._redraw_job)
            except Exception:
                pass
        self._redraw_job = self.root.after(90, self._redraw)

    def _redraw(self) -> None:
        self._redraw_job = None
        self.canvas.delete("all")
        if self.proxy is None or self.plan is None:
            return
        width = max(40, self.canvas.winfo_width())
        height = max(40, self.canvas.winfo_height())
        try:
            image, labels = preview.render(self.proxy, self.plan, width, height,
                                           self.var_preview_mode.get())
        except Exception as exc:
            self._log("Предпросмотр не отрисован: {0}".format(exc))
            return
        self.photo = ImageTk.PhotoImage(image)
        self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
        for label in labels:
            x, y = int(label["x"]), int(label["y"])
            size = int(label.get("size", 12))
            text = str(label["text"])
            family = "Segoe UI" if os.name == "nt" else "TkDefaultFont"
            half_w = (12 + 7 * len(text)) // 2
            half_h = (size + 8) // 2
            self.canvas.create_rectangle(x - half_w, y - half_h, x + half_w, y + half_h,
                                         fill="#c2362c", outline="#ffffff")
            self.canvas.create_text(x, y, text=text, fill="#ffffff",
                                    font=(family, size, "bold"))

    # ---------------------------------------------------------------- split
    def _pick_dir(self) -> None:
        folder = filedialog.askdirectory(title="Куда складывать части")
        if folder:
            self.var_outdir.set(folder)

    def _start_split(self) -> None:
        if self.busy:
            return
        if not self.files:
            messagebox.showinfo(APP_TITLE, "Сначала добавьте хотя бы одну картинку.")
            return
        out_dir = None if self.var_beside.get() else (self.var_outdir.get() or "").strip()
        if not self.var_beside.get() and not out_dir:
            messagebox.showwarning(APP_TITLE, "Укажите папку для сохранения.")
            return

        if self.plan is not None and self.plan.count >= 40:
            per_file = self.plan.count
            estimate = per_file * len(self.files)
            question = ("Получится {0} {1} из выбранной картинки"
                        "{2}.\nТакой нарезки вы и хотели?").format(
                per_file, core.plural_parts(per_file),
                ", всего около {0} файлов".format(estimate)
                if len(self.files) > 1 else "")
            if not messagebox.askyesno(APP_TITLE, question):
                return

        settings = self.settings()
        fmt = self._format_value()
        quality = self._int(self.var_quality, 92, 1, 100)
        pattern = self.var_pattern.get() or "{stem}_{i:02d}"
        background = self._background()
        files = list(self.files)

        self.cancel.clear()
        self.busy = True
        self.btn_run.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.progress.configure(value=0, maximum=1)
        self._set_status("Режу…")

        def work():
            total = 0
            folders = []
            cancelled = False
            for path in files:
                if self.cancel.is_set():
                    cancelled = True
                    break
                target = out_dir or core.default_output_dir(path)

                def report(done, count, written_path, _path=path):
                    if self.cancel.is_set():
                        raise Cancelled()
                    self.bus.put(("progress", done, count, _path))

                try:
                    written = core.split_file(path, settings, target, fmt, quality,
                                              pattern, background, progress=report)
                    total += len(written)
                    if target not in folders:
                        folders.append(target)
                    self.bus.put(("file_done", path, written, target))
                except Cancelled:
                    cancelled = True
                    break
                except Exception as exc:
                    self.bus.put(("file_failed", path, exc))
            self.bus.put(("done", total, folders, cancelled))

        threading.Thread(target=work, daemon=True).start()

    def _cancel_split(self) -> None:
        self.cancel.set()
        self._set_status("Останавливаю…")

    def _finish_split(self, total: int, folders: List[str], cancelled: bool) -> None:
        self.busy = False
        self.btn_run.configure(state="normal")
        self.btn_cancel.configure(state="disabled")
        if cancelled:
            self._set_status("Остановлено. Успели сохранить: {0}".format(total))
        else:
            self._set_status("Готово. Сохранено файлов: {0}".format(total))
            self.progress.configure(value=self.progress["maximum"])
        if folders and self.var_open_after.get() and not cancelled:
            self._open_folder(folders[0])

    @staticmethod
    def _open_folder(path: str) -> None:
        try:
            if hasattr(os, "startfile"):
                os.startfile(path)  # noqa: B606  (Windows only)
            elif sys.platform == "darwin":
                os.system('open "{0}"'.format(path))
            else:
                os.system('xdg-open "{0}" >/dev/null 2>&1 &'.format(path))
        except Exception:
            pass

    # ------------------------------------------------------------- utilities
    def _set_status(self, text: str) -> None:
        self.var_status.set(text)

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _show_help(self) -> None:
        window = tk.Toplevel(self.root)
        window.title("Справка")
        window.geometry("620x520")
        text = tk.Text(window, wrap="word", padx=10, pady=10)
        text.pack(fill="both", expand=True)
        text.insert("1.0", HELP_TEXT)
        text.configure(state="disabled")
        ttk.Button(window, text="Закрыть", command=window.destroy).pack(pady=6)
        window.transient(self.root)

    # ---------------------------------------------------------------- config
    def _config_payload(self) -> dict:
        return {
            "preset": self.var_preset.get(), "rw": self.var_rw.get(),
            "rh": self.var_rh.get(), "mode": self.var_mode.get(),
            "count": self.var_count.get(), "overlap": self.var_overlap.get(),
            "smart": bool(self.var_smart.get()), "window": self.var_window.get(),
            "tail": self.var_tail.get(), "axis": self.var_axis.get(),
            "beside": bool(self.var_beside.get()), "outdir": self.var_outdir.get(),
            "format": self.var_format.get(), "quality": self.var_quality.get(),
            "pattern": self.var_pattern.get(), "bg": self.var_bg.get(),
            "open_after": bool(self.var_open_after.get()),
            "geometry": self.root.geometry(),
        }

    def _load_config(self) -> None:
        try:
            with open(config_path(), "r") as handle:
                data = json.load(handle)
        except Exception:
            return
        pairs = [("preset", self.var_preset), ("rw", self.var_rw), ("rh", self.var_rh),
                 ("mode", self.var_mode), ("count", self.var_count),
                 ("overlap", self.var_overlap), ("window", self.var_window),
                 ("tail", self.var_tail), ("axis", self.var_axis),
                 ("outdir", self.var_outdir), ("format", self.var_format),
                 ("quality", self.var_quality), ("pattern", self.var_pattern),
                 ("bg", self.var_bg)]
        for key, var in pairs:
            if isinstance(data.get(key), str) and data[key]:
                var.set(data[key])
        for key, var in (("smart", self.var_smart), ("beside", self.var_beside),
                         ("open_after", self.var_open_after)):
            if isinstance(data.get(key), bool):
                var.set(data[key])
        geometry = data.get("geometry")
        if isinstance(geometry, str) and "x" in geometry:
            try:
                self.root.geometry(geometry)
            except Exception:
                pass

    def _save_config(self) -> None:
        try:
            with open(config_path(), "w") as handle:
                json.dump(self._config_payload(), handle, indent=1)
        except Exception:
            pass

    def _on_close(self) -> None:
        self.cancel.set()
        self._save_config()
        self.root.destroy()


def make_root():
    """TkinterDnD when it is installed, plain Tk otherwise."""
    try:
        from tkinterdnd2 import TkinterDnD, DND_FILES
        return TkinterDnD.Tk(), DND_FILES
    except Exception:
        return tk.Tk(), None


def main(argv: Optional[List[str]] = None) -> int:
    enable_dpi_awareness()
    root, flavour = make_root()
    try:
        ttk.Style().theme_use("vista" if os.name == "nt" else "clam")
    except Exception:
        pass
    apply_icon(root)
    app = App(root, flavour)
    argv = list(argv or [])
    if argv:
        app._add_paths([a for a in argv if os.path.isfile(a)])
    root.mainloop()
    return 0
