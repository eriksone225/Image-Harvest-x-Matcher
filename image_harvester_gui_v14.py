#!/usr/bin/env python3
from __future__ import annotations

import csv
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import json
import urllib.request
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from tkinter import ttk

from PIL import Image, ImageTk, ImageOps


APP_DIR = Path(__file__).resolve().parent
HARVESTER = APP_DIR / "site_image_harvester_v12.py"
MATCHER = APP_DIR / "local_harvest_matcher_v4.py"
CONVERTER = APP_DIR / "media_converter_v1.py"


def is_windows() -> bool:
    return os.name == "nt"


def find_chrome() -> str:
    if is_windows():
        candidates = [
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        ]
        for p in candidates:
            if p.exists():
                return str(p)
    else:
        for name in ["google-chrome", "chromium", "chromium-browser", "microsoft-edge"]:
            found = shutil.which(name)
            if found:
                return found
    return ""


def open_folder(path: str):
    p = Path(path)
    if p.is_file():
        p = p.parent
    p.mkdir(parents=True, exist_ok=True)
    if is_windows():
        os.startfile(str(p))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Image/Video Harvester x Matcher GUI v14")
        self.geometry("1320x860")
        self.minsize(1160, 760)

        self.proc: subprocess.Popen | None = None
        self.q: queue.Queue[str] = queue.Queue()
        self.preview_refs = []
        self.match_rows = []

        self._style()
        self._vars()
        self._ui()
        self._check_files()
        self.after(100, self._poll_log)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _style(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Header.TLabel", font=("Segoe UI", 17, "bold"))
        style.configure("Hint.TLabel", font=("Segoe UI", 10))
        style.configure("Section.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))
        style.configure("Danger.TButton", foreground="#9b0000")

    def _vars(self):
        self.site = tk.StringVar(value="https://pemersatu.store/")
        self.verify_url = tk.StringVar(value="https://pemersatu.store/")
        self.crawl_mode = tk.StringVar(value="Auto detect")
        self.page_start = tk.StringVar(value="1")
        self.page_end = tk.StringVar(value="100")
        self.max_pages = tk.StringVar(value="100")
        self.page_template = tk.StringVar(value="")
        self.url_include = tk.StringVar(value="")
        self.url_exclude = tk.StringVar(value="/tag/,/author/,/search/,/category/")
        self.harvest_videos = tk.BooleanVar(value=False)
        self.same_domain_videos_only = tk.BooleanVar(value=False)
        self.max_video_mb = tk.StringVar(value="100")
        self.video_concurrency = tk.StringVar(value="2")
        self.video_timeout_ms = tk.StringVar(value="45000")
        self.probe_video_playback = tk.BooleanVar(value=True)
        self.video_probe_seconds = tk.StringVar(value="4")
        self.max_video_elements = tk.StringVar(value="8")
        self.auto_open_cdp_chrome = tk.BooleanVar(value=True)
        self.block_ads = tk.BooleanVar(value=True)
        self.close_popups = tk.BooleanVar(value=True)
        self.scroll_until_bottom = tk.BooleanVar(value=True)
        self.max_bottom_scroll_rounds = tk.StringVar(value="30")
        self.bottom_scroll_delay_ms = tk.StringVar(value="800")
        self.lazy_final_wait_ms = tk.StringVar(value="2500")
        self.wait_load_state = tk.StringVar(value="load")
        self.extra_settle_ms = tk.StringVar(value="1000")
        self.download_hls_segments = tk.BooleanVar(value=True)
        self.concat_detected_ts_segments = tk.BooleanVar(value=True)
        self.force_lazy_media = tk.BooleanVar(value=True)
        self.page_fetch_images = tk.BooleanVar(value=True)
        self.screenshot_image_fallback = tk.BooleanVar(value=False)
        self.lazy_force_wait_ms = tk.StringVar(value="1500")
        self.hls_max_segments = tk.StringVar(value="0")
        self.hls_concurrency = tk.StringVar(value="4")
        self.harvest_out = tk.StringVar(value=str(APP_DIR / "site_image_harvest"))
        self.auto_convert_ts = tk.BooleanVar(value=True)
        self.convert_existing_input = tk.StringVar(value=str(Path(self.harvest_out.get()) / "videos"))
        self.convert_output_dir = tk.StringVar(value=str(Path(self.harvest_out.get()) / "videos_mp4"))
        self.ffmpeg_path = tk.StringVar(value="")
        self.convert_mode = tk.StringVar(value="auto")
        self.convert_recursive = tk.BooleanVar(value=True)
        self.convert_overwrite = tk.BooleanVar(value=False)

        self.mode = tk.StringVar(value="cdp")
        self.cdp = tk.StringVar(value="http://127.0.0.1:9222")
        self.chrome = tk.StringVar(value=find_chrome())

        self.preset = tk.StringVar(value="Thorough")
        self.scroll = tk.StringVar(value="8")
        self.wait_ms = tk.StringVar(value="6500")
        self.delay_ms = tk.StringVar(value="1000")
        self.conc = tk.StringVar(value="4")
        self.network_only = tk.BooleanVar(value=False)
        self.no_shots = tk.BooleanVar(value=True)
        self.no_html = tk.BooleanVar(value=False)
        self.sitemaps = tk.BooleanVar(value=False)
        self.auto_refresh = tk.BooleanVar(value=True)
        self.refresh_retries = tk.StringVar(value="5")
        self.refresh_delay = tk.StringVar(value="10000")

        self.target = tk.StringVar(value="")
        self.target_face_image = tk.StringVar(value="")
        self.metadata = tk.StringVar(value=str(Path(self.harvest_out.get()) / "images_metadata.csv"))
        self.match_out = tk.StringVar(value=str(APP_DIR / "local_match_results"))
        self.top = tk.StringVar(value="100")
        self.copy_top = tk.StringVar(value="50")

        self.body_detect = tk.BooleanVar(value=True)
        self.clip = tk.BooleanVar(value=True)
        self.no_face_match = tk.BooleanVar(value=False)
        self.face_fallback_center = tk.BooleanVar(value=True)
        self.candidate_face_fallback_center = tk.BooleanVar(value=False)
        self.use_mtcnn = tk.BooleanVar(value=True)

        self.status = tk.StringVar(value="Ready")

    def _ui(self):
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        ttk.Label(root, text="Image/Video Harvester x Matcher GUI v14", style="Header.TLabel").pack(anchor="w")
        ttk.Label(
            root,
            text="Universal image/video harvester x image matcher. Settings scroll; Run buttons stay visible.",
            style="Hint.TLabel"
        ).pack(anchor="w", pady=(0, 8))

        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True)

        self.harvest_tab = ttk.Frame(self.nb, padding=10)
        self.match_tab = ttk.Frame(self.nb, padding=10)
        self.review_tab = ttk.Frame(self.nb, padding=10)
        self.tools_tab = ttk.Frame(self.nb, padding=10)
        self.log_tab = ttk.Frame(self.nb, padding=10)

        self.nb.add(self.harvest_tab, text="1. Harvest")
        self.nb.add(self.match_tab, text="2. Match")
        self.nb.add(self.review_tab, text="3. Review Results")
        self.nb.add(self.tools_tab, text="Tools")
        self.nb.add(self.log_tab, text="Logs")

        self._harvest_ui()
        self._match_ui()
        self._review_ui()
        self._tools_ui()
        self._log_ui()

        bottom = ttk.Frame(root)
        bottom.pack(fill="x", pady=(8, 0))
        ttk.Label(bottom, textvariable=self.status).pack(side="left")
        ttk.Button(bottom, text="Send Enter", command=self.send_enter).pack(side="right", padx=(6, 0))
        ttk.Button(bottom, text="Stop", style="Danger.TButton", command=self.stop).pack(side="right", padx=(6, 0))
        ttk.Button(bottom, text="Open Output", command=self.open_current_output).pack(side="right")

    def row(self, parent, text, var, r, browse=None):
        parent.columnconfigure(1, weight=1)
        ttk.Label(parent, text=text).grid(row=r, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(parent, textvariable=var).grid(row=r, column=1, sticky="ew", pady=4)
        if browse:
            ttk.Button(parent, text="Browse", command=lambda: self.browse(var, browse)).grid(row=r, column=2, padx=(6, 0), pady=4)

    def make_scrollable_tab(self, tab):
        """
        Create a vertically scrollable area inside a Notebook tab.
        Returns the inner frame where tab content should be placed.
        """
        outer = ttk.Frame(tab)
        outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)

        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def configure_inner(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def configure_canvas(event):
            canvas.itemconfigure(window_id, width=event.width)

        def on_mousewheel(event):
            # Windows/macOS/Linux wheel support
            if event.num == 4:
                canvas.yview_scroll(-3, "units")
            elif event.num == 5:
                canvas.yview_scroll(3, "units")
            else:
                delta = int(-1 * (event.delta / 120))
                canvas.yview_scroll(delta * 3, "units")

        inner.bind("<Configure>", configure_inner)
        canvas.bind("<Configure>", configure_canvas)
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Bind when cursor is over the scroll area, unbind when leaving so other widgets behave normally.
        def bind_wheel(_event=None):
            canvas.bind_all("<MouseWheel>", on_mousewheel)
            canvas.bind_all("<Button-4>", on_mousewheel)
            canvas.bind_all("<Button-5>", on_mousewheel)

        def unbind_wheel(_event=None):
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")

        outer.bind("<Enter>", bind_wheel)
        outer.bind("<Leave>", unbind_wheel)

        return inner

    def _harvest_ui(self):
        # Keep the Run panel pinned on the right. Only the large settings area scrolls.
        content = ttk.Frame(self.harvest_tab)
        content.pack(fill="both", expand=True)

        left_holder = ttk.Frame(content)
        left_holder.pack(side="left", fill="both", expand=True)

        right = ttk.Frame(content)
        right.pack(side="right", fill="y", padx=(10, 0))

        left = self.make_scrollable_tab(left_holder)

        box = ttk.LabelFrame(left, text="Website / crawl mechanics", padding=10)
        box.pack(fill="x", pady=(0, 8))
        self.row(box, "Start URL", self.site, 0)
        self.row(box, "Output folder", self.harvest_out, 1, "folder")

        mode_line = ttk.Frame(box)
        mode_line.grid(row=2, column=0, columnspan=3, sticky="ew", pady=4)
        ttk.Label(mode_line, text="Crawl mechanic").pack(side="left", padx=(0, 4))
        mode_cb = ttk.Combobox(
            mode_line,
            textvariable=self.crawl_mode,
            values=[
                "Auto detect",
                "Numbered pages",
                "Sitemaps",
                "Find links by itself",
                "Single page only",
            ],
            state="readonly",
            width=24,
        )
        mode_cb.pack(side="left", padx=(0, 16))
        ttk.Label(mode_line, text="Auto: numbered if page start/end > 0, otherwise sitemap if enabled, otherwise link crawl.").pack(side="left")

        line = ttk.Frame(box)
        line.grid(row=3, column=0, columnspan=3, sticky="ew", pady=4)
        for label, var in [("Page start", self.page_start), ("Page end", self.page_end), ("Max pages", self.max_pages)]:
            ttk.Label(line, text=label).pack(side="left", padx=(0, 4))
            ttk.Entry(line, textvariable=var, width=10).pack(side="left", padx=(0, 16))

        self.row(box, "Page template optional, use {n}", self.page_template, 4)
        self.row(box, "URL include filter optional", self.url_include, 5)
        self.row(box, "URL exclude filter", self.url_exclude, 6)

        media_box = ttk.LabelFrame(left, text="Media harvesting", padding=10)
        media_box.pack(fill="x", pady=(0, 8))
        media_row = ttk.Frame(media_box)
        media_row.pack(fill="x", pady=(0, 6))
        ttk.Checkbutton(media_row, text="Harvest videos/media too", variable=self.harvest_videos).pack(side="left")
        ttk.Checkbutton(media_row, text="Same-domain videos only", variable=self.same_domain_videos_only).pack(side="left", padx=(14, 0))
        ttk.Checkbutton(media_row, text="Play/probe video elements to reveal source", variable=self.probe_video_playback).pack(side="left", padx=(14, 0))

        media_row2 = ttk.Frame(media_box)
        media_row2.pack(fill="x", pady=(0, 6))
        ttk.Label(media_row2, text="Max video MB").pack(side="left")
        ttk.Entry(media_row2, textvariable=self.max_video_mb, width=8).pack(side="left", padx=(4, 16))
        ttk.Label(media_row2, text="Video concurrency").pack(side="left")
        ttk.Entry(media_row2, textvariable=self.video_concurrency, width=8).pack(side="left", padx=(4, 16))
        ttk.Label(media_row2, text="Video timeout ms").pack(side="left")
        ttk.Entry(media_row2, textvariable=self.video_timeout_ms, width=10).pack(side="left", padx=(4, 16))

        media_row3 = ttk.Frame(media_box)
        media_row3.pack(fill="x", pady=(0, 6))
        ttk.Label(media_row3, text="Probe seconds").pack(side="left")
        ttk.Entry(media_row3, textvariable=self.video_probe_seconds, width=8).pack(side="left", padx=(4, 16))
        ttk.Label(media_row3, text="Max videos to probe").pack(side="left")
        ttk.Entry(media_row3, textvariable=self.max_video_elements, width=8).pack(side="left", padx=(4, 16))

        media_row4 = ttk.Frame(media_box)
        media_row4.pack(fill="x")
        ttk.Checkbutton(media_row4, text="Download non-encrypted HLS .ts segments from .m3u8", variable=self.download_hls_segments).pack(side="left")
        ttk.Checkbutton(media_row4, text="Concat observed .ts segments if no playlist", variable=self.concat_detected_ts_segments).pack(side="left", padx=(14, 0))
        ttk.Label(media_row4, text="Max HLS/TS segments").pack(side="left", padx=(14, 4))
        ttk.Entry(media_row4, textvariable=self.hls_max_segments, width=8).pack(side="left", padx=(0, 14))
        ttk.Label(media_row4, text="HLS concurrency").pack(side="left", padx=(0, 4))
        ttk.Entry(media_row4, textvariable=self.hls_concurrency, width=8).pack(side="left", padx=(0, 14))
        ttk.Label(media_row4, text="0 max segments = no count limit; max video MB still applies.").pack(side="left")

        convert_box = ttk.LabelFrame(left, text="TS/HLS to MP4 conversion", padding=10)
        convert_box.pack(fill="x", pady=(0, 8))
        convert_row = ttk.Frame(convert_box)
        convert_row.pack(fill="x", pady=(0, 6))
        ttk.Checkbutton(convert_row, text="Auto-convert harvested .ts/.m3u8 to MP4 after harvest", variable=self.auto_convert_ts).pack(side="left")
        ttk.Label(convert_row, text="Mode").pack(side="left", padx=(14, 4))
        ttk.Combobox(convert_row, textvariable=self.convert_mode, values=["auto", "remux", "reencode"], state="readonly", width=10).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(convert_row, text="Recursive", variable=self.convert_recursive).pack(side="left")
        ttk.Checkbutton(convert_row, text="Overwrite", variable=self.convert_overwrite).pack(side="left", padx=(14, 0))

        convert_row2 = ttk.Frame(convert_box)
        convert_row2.pack(fill="x", pady=(0, 6))
        ttk.Label(convert_row2, text="FFmpeg path optional").pack(side="left")
        ttk.Entry(convert_row2, textvariable=self.ffmpeg_path, width=42).pack(side="left", padx=(4, 6))
        ttk.Button(convert_row2, text="Browse", command=lambda: self.browse(self.ffmpeg_path, "file")).pack(side="left")
        ttk.Label(convert_row2, text="Leave blank if ffmpeg is in PATH.").pack(side="left", padx=(10, 0))

        convert_row3 = ttk.Frame(convert_box)
        convert_row3.pack(fill="x")
        ttk.Button(convert_row3, text="Convert Existing TS/M3U8 Folder", command=self.start_convert_existing).pack(side="left")
        ttk.Label(convert_row3, text="Input").pack(side="left", padx=(14, 4))
        ttk.Entry(convert_row3, textvariable=self.convert_existing_input, width=34).pack(side="left", padx=(0, 6))
        ttk.Button(convert_row3, text="Browse", command=lambda: self.browse(self.convert_existing_input, "folder")).pack(side="left")
        ttk.Label(convert_row3, text="MP4 Output").pack(side="left", padx=(14, 4))
        ttk.Entry(convert_row3, textvariable=self.convert_output_dir, width=34).pack(side="left", padx=(0, 6))
        ttk.Button(convert_row3, text="Browse", command=lambda: self.browse(self.convert_output_dir, "folder")).pack(side="left")

        clean_box = ttk.LabelFrame(left, text="Ad / popup cleanup", padding=10)
        clean_box.pack(fill="x", pady=(0, 8))
        clean_row = ttk.Frame(clean_box)
        clean_row.pack(fill="x")
        ttk.Checkbutton(clean_row, text="Block common ad/tracker requests", variable=self.block_ads).pack(side="left")
        ttk.Checkbutton(clean_row, text="Close popup tabs and remove overlay ads", variable=self.close_popups).pack(side="left", padx=(14, 0))
        ttk.Label(clean_row, text="Best-effort cleanup only; it does not bypass paywalls, login walls, or anti-bot checks.").pack(side="left", padx=(14, 0))

        lazy_box = ttk.LabelFrame(left, text="Full page load / lazy-load handling", padding=10)
        lazy_box.pack(fill="x", pady=(0, 8))
        lazy_row = ttk.Frame(lazy_box)
        lazy_row.pack(fill="x", pady=(0, 6))
        ttk.Checkbutton(lazy_row, text="Scroll to bottom until page height stabilizes", variable=self.scroll_until_bottom).pack(side="left")
        ttk.Label(lazy_row, text="Max rounds").pack(side="left", padx=(14, 4))
        ttk.Entry(lazy_row, textvariable=self.max_bottom_scroll_rounds, width=8).pack(side="left", padx=(0, 14))
        ttk.Label(lazy_row, text="Round delay ms").pack(side="left", padx=(0, 4))
        ttk.Entry(lazy_row, textvariable=self.bottom_scroll_delay_ms, width=8).pack(side="left", padx=(0, 14))
        ttk.Label(lazy_row, text="Final wait ms").pack(side="left", padx=(0, 4))
        ttk.Entry(lazy_row, textvariable=self.lazy_final_wait_ms, width=8).pack(side="left")

        lazy_row2 = ttk.Frame(lazy_box)
        lazy_row2.pack(fill="x")
        ttk.Label(lazy_row2, text="Wait load state").pack(side="left")
        ttk.Combobox(lazy_row2, textvariable=self.wait_load_state, values=["", "domcontentloaded", "load", "networkidle"], state="readonly", width=16).pack(side="left", padx=(4, 14))
        ttk.Label(lazy_row2, text="Extra settle ms").pack(side="left", padx=(0, 4))
        ttk.Entry(lazy_row2, textvariable=self.extra_settle_ms, width=8).pack(side="left", padx=(0, 14))
        ttk.Label(lazy_row2, text="Use networkidle carefully; streaming sites may never go idle.").pack(side="left")

        image_box = ttk.LabelFrame(left, text="Image recovery strategies", padding=10)
        image_box.pack(fill="x", pady=(0, 8))
        image_row = ttk.Frame(image_box)
        image_row.pack(fill="x", pady=(0, 6))
        ttk.Checkbutton(image_row, text="Force lazy media into src/srcset", variable=self.force_lazy_media).pack(side="left")
        ttk.Checkbutton(image_row, text="Page-context fetch images with credentials", variable=self.page_fetch_images).pack(side="left", padx=(14, 0))
        ttk.Checkbutton(image_row, text="Rendered screenshot fallback", variable=self.screenshot_image_fallback).pack(side="left", padx=(14, 0))
        image_row2 = ttk.Frame(image_box)
        image_row2.pack(fill="x")
        ttk.Label(image_row2, text="Lazy-force wait ms").pack(side="left")
        ttk.Entry(image_row2, textvariable=self.lazy_force_wait_ms, width=8).pack(side="left", padx=(4, 14))
        ttk.Label(image_row2, text="Screenshot fallback is slower/lower quality but can capture what the browser displays.").pack(side="left")

        b = ttk.LabelFrame(left, text="Browser / verification", padding=10)
        b.pack(fill="x", pady=(0, 8))
        ttk.Radiobutton(b, text="Attach to verified Chrome on port 9222", variable=self.mode, value="cdp").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(b, text="Open Playwright Chromium and pause for verification", variable=self.mode, value="headful").grid(row=1, column=0, columnspan=3, sticky="w")
        self.row(b, "CDP URL", self.cdp, 2)
        self.row(b, "Chrome/Edge path", self.chrome, 3, "file")
        self.row(b, "Verification URL", self.verify_url, 4)
        auto_line = ttk.Frame(b)
        auto_line.grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Checkbutton(auto_line, text="Auto-open verification Chrome if CDP is not running", variable=self.auto_open_cdp_chrome).pack(side="left")

        buttons = ttk.Frame(b)
        buttons.grid(row=6, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(buttons, text="Open Verification Chrome", command=self.open_chrome).pack(side="left")
        ttk.Button(buttons, text="Test CDP Connection", command=self.test_cdp_popup).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Send Enter After Verification", command=self.send_enter).pack(side="left", padx=(6, 0))

        s = ttk.LabelFrame(left, text="Speed / evidence / recovery", padding=10)
        s.pack(fill="x")
        ttk.Label(s, text="Preset").grid(row=0, column=0, sticky="w")
        cb = ttk.Combobox(s, textvariable=self.preset, values=["Fastest", "Fast", "Balanced", "Thorough"], state="readonly", width=16)
        cb.grid(row=0, column=1, sticky="w")
        cb.bind("<<ComboboxSelected>>", lambda _e: self.apply_preset())

        for c, (label, var) in enumerate([("Scroll steps", self.scroll), ("Wait ms", self.wait_ms), ("Delay ms", self.delay_ms), ("Concurrency", self.conc)]):
            ttk.Label(s, text=label).grid(row=1 + c // 2, column=(c % 2) * 2, sticky="w", pady=4, padx=(0, 4))
            ttk.Entry(s, textvariable=var, width=10).grid(row=1 + c // 2, column=(c % 2) * 2 + 1, sticky="w", padx=(0, 16))

        checks = ttk.Frame(s)
        checks.grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Checkbutton(checks, text="Network-only", variable=self.network_only).pack(side="left")
        ttk.Checkbutton(checks, text="No screenshots", variable=self.no_shots).pack(side="left", padx=(10, 0))
        ttk.Checkbutton(checks, text="No HTML/text", variable=self.no_html).pack(side="left", padx=(10, 0))
        ttk.Checkbutton(checks, text="Discover sitemaps", variable=self.sitemaps).pack(side="left", padx=(10, 0))
        ttk.Checkbutton(checks, text="Auto-refresh 502/503/504", variable=self.auto_refresh).pack(side="left", padx=(10, 0))

        refresh = ttk.Frame(s)
        refresh.grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Label(refresh, text="Refresh retries").pack(side="left")
        ttk.Entry(refresh, textvariable=self.refresh_retries, width=8).pack(side="left", padx=(4, 16))
        ttk.Label(refresh, text="Refresh delay ms").pack(side="left")
        ttk.Entry(refresh, textvariable=self.refresh_delay, width=10).pack(side="left", padx=(4, 16))

        run = ttk.LabelFrame(right, text="Run", padding=10)
        run.pack(fill="x")
        ttk.Button(run, text="Start Harvest", style="Accent.TButton", command=self.start_harvest).pack(fill="x", pady=(0, 8))
        ttk.Button(run, text="Stop", style="Danger.TButton", command=self.stop).pack(fill="x", pady=(0, 8))
        ttk.Button(run, text="Open Harvest Output", command=lambda: open_folder(self.harvest_out.get())).pack(fill="x")

        helpbox = ttk.LabelFrame(right, text="Workflow", padding=10)
        helpbox.pack(fill="both", expand=True, pady=(8, 0))
        msg = (
            "Crawl mechanics:\n"
            "• Numbered: /page/1/ to /page/N/ or custom {n} template.\n"
            "• Sitemaps: pulls public sitemap URLs.\n"
            "• Find links: starts from Start URL and follows same-site links.\n"
            "• Single: only harvests Start URL.\n\n"
            "Auto-refresh helps 502/503/504 only. It does not bypass human verification."
        )
        ttk.Label(helpbox, text=msg, justify="left", wraplength=310).pack(anchor="w")

    def _match_ui(self):
        t = self.make_scrollable_tab(self.match_tab)
        box = ttk.LabelFrame(t, text="Search harvested images locally", padding=10)
        box.pack(fill="x")
        self.row(box, "Target image", self.target, 0, "file")
        self.row(box, "Manual target face crop (optional)", self.target_face_image, 1, "file")
        self.row(box, "images_metadata.csv", self.metadata, 2, "file")
        self.row(box, "Output folder", self.match_out, 3, "folder")

        scores = ttk.LabelFrame(t, text="Matching options", padding=10)
        scores.pack(fill="x", pady=(10, 0))

        row1 = ttk.Frame(scores)
        row1.pack(fill="x", pady=(0, 8))
        ttk.Label(row1, text="Top").pack(side="left")
        ttk.Entry(row1, textvariable=self.top, width=8).pack(side="left", padx=(4, 12))
        ttk.Label(row1, text="Copy top").pack(side="left")
        ttk.Entry(row1, textvariable=self.copy_top, width=8).pack(side="left", padx=(4, 12))
        ttk.Checkbutton(row1, text="Body detect", variable=self.body_detect).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(row1, text="CLIP visual similarity", variable=self.clip).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(row1, text="Disable face crop match", variable=self.no_face_match).pack(side="left")

        row2 = ttk.Frame(scores)
        row2.pack(fill="x", pady=(0, 8))
        ttk.Label(row2, text="Face detection/crop options:", style="Section.TLabel").pack(side="left", padx=(0, 10))
        ttk.Checkbutton(row2, text="Use MTCNN detector if installed", variable=self.use_mtcnn).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(row2, text="Target center fallback", variable=self.face_fallback_center).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(row2, text="Candidate center fallback", variable=self.candidate_face_fallback_center).pack(side="left")

        note = ttk.Label(scores, text="MTCNN/TensorFlow must be installed. If not installed, the matcher falls back to OpenCV Haar detection.", wraplength=1000)
        note.pack(anchor="w")

        actions = ttk.Frame(t)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, text="Start Matching", style="Accent.TButton", command=self.start_match).pack(side="left")
        ttk.Button(actions, text="Load Results in Review Tab", command=self.load_results).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Open Match Output", command=lambda: open_folder(self.match_out.get())).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Stop", style="Danger.TButton", command=self.stop).pack(side="left", padx=(8, 0))

        expl = ttk.LabelFrame(t, text="Accuracy notes", padding=10)
        expl.pack(fill="both", expand=True, pady=(10, 0))
        ttk.Label(
            expl,
            text=(
                "This is visual triage, not biometric identity recognition. For broader searches, keep CLIP on and use a manual target face crop "
                "if automatic face detection misses the face. MTCNN improves face detection/crops when installed."
            ),
            wraplength=1000,
            justify="left"
        ).pack(anchor="w")

    def _review_ui(self):
        t = self.review_tab
        left = ttk.Frame(t)
        left.pack(side="left", fill="both", expand=True)
        right = ttk.Frame(t)
        right.pack(side="right", fill="both", expand=True, padx=(10, 0))

        controls = ttk.Frame(left)
        controls.pack(fill="x", pady=(0, 6))
        ttk.Button(controls, text="Load matches.csv", command=self.load_results).pack(side="left")
        ttk.Button(controls, text="Open Selected File", command=self.open_selected_file).pack(side="left", padx=(6, 0))
        ttk.Button(controls, text="Open Selected Page", command=self.open_selected_page).pack(side="left", padx=(6, 0))
        ttk.Button(controls, text="Open Face Crops", command=self.open_selected_face_crops).pack(side="left", padx=(6, 0))

        cols = ("rank", "combined", "full", "face", "faces", "bodies", "reason")
        self.results_tree = ttk.Treeview(left, columns=cols, show="headings", height=24)
        headings = {"rank": "Rank", "combined": "Combined", "full": "Full", "face": "Face crop", "faces": "Faces", "bodies": "Bodies", "reason": "Reason"}
        widths = {"rank": 55, "combined": 90, "full": 80, "face": 80, "faces": 55, "bodies": 60, "reason": 300}
        for c in cols:
            self.results_tree.heading(c, text=headings[c])
            self.results_tree.column(c, width=widths[c], anchor="w")
        self.results_tree.pack(side="left", fill="both", expand=True)
        self.results_tree.bind("<<TreeviewSelect>>", lambda _e: self.show_selected_result())

        y = ttk.Scrollbar(left, orient="vertical", command=self.results_tree.yview)
        y.pack(side="right", fill="y")
        self.results_tree.configure(yscrollcommand=y.set)

        ttk.Label(right, text="Selected match preview", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        self.preview_label = ttk.Label(right)
        self.preview_label.pack(fill="both", expand=True, pady=(8, 8))

        detail_frame = ttk.LabelFrame(right, text="Details", padding=8)
        detail_frame.pack(fill="both", expand=False)
        self.detail_text = tk.Text(detail_frame, height=12, wrap="word", font=("Consolas", 9))
        self.detail_text.pack(fill="both", expand=True)

    def _tools_ui(self):
        t = self.make_scrollable_tab(self.tools_tab)
        box = ttk.LabelFrame(t, text="Tools", padding=10)
        box.pack(fill="x")
        ttk.Button(box, text="Open Verification Chrome", command=self.open_chrome).pack(side="left")
        ttk.Button(box, text="Check Files", command=self._check_files).pack(side="left", padx=(8, 0))
        ttk.Button(box, text="Open App Folder", command=lambda: open_folder(str(APP_DIR))).pack(side="left", padx=(8, 0))

        install = ttk.LabelFrame(t, text="Install notes", padding=10)
        install.pack(fill="both", expand=True, pady=(10, 0))
        ttk.Label(
            install,
            text=(
                "Use install_requirements_windows_v14.bat to install the base packages plus optional MTCNN/TensorFlow.\n\n"
                "MTCNN checkbox is visible in Match > Matching options. If MTCNN/TensorFlow is missing, matcher automatically uses OpenCV fallback.\n"
                "MTCNN is only for better face crop detection, not identity recognition."
            ),
            justify="left",
            wraplength=1000
        ).pack(anchor="w")

    def _log_ui(self):
        self.log_text = tk.Text(self.log_tab, wrap="word", state="disabled", font=("Consolas", 9))
        self.log_text.pack(side="left", fill="both", expand=True)
        y = ttk.Scrollbar(self.log_tab, orient="vertical", command=self.log_text.yview)
        y.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=y.set)

    def browse(self, var: tk.StringVar, kind: str):
        if kind == "folder":
            val = filedialog.askdirectory(initialdir=str(APP_DIR))
        else:
            val = filedialog.askopenfilename(initialdir=str(APP_DIR))
        if val:
            var.set(val)

    def apply_preset(self):
        p = self.preset.get().lower()
        if p == "fastest":
            self.scroll.set("2"); self.wait_ms.set("1500"); self.delay_ms.set("0"); self.conc.set("16")
            self.network_only.set(True); self.no_shots.set(True); self.no_html.set(True)
        elif p == "fast":
            self.scroll.set("3"); self.wait_ms.set("2500"); self.delay_ms.set("0"); self.conc.set("16")
            self.network_only.set(False); self.no_shots.set(True); self.no_html.set(False)
        elif p == "balanced":
            self.scroll.set("5"); self.wait_ms.set("3000"); self.delay_ms.set("100"); self.conc.set("12")
            self.network_only.set(False); self.no_shots.set(True); self.no_html.set(False)
        else:
            self.scroll.set("8"); self.wait_ms.set("6500"); self.delay_ms.set("1000"); self.conc.set("4")
            self.network_only.set(False); self.no_shots.set(True); self.no_html.set(False)

    def _check_files(self):
        missing = [p.name for p in [HARVESTER, MATCHER, CONVERTER] if not p.exists()]
        if missing:
            self.log("[check] Missing: " + ", ".join(missing) + "\n")
            messagebox.showwarning("Missing files", "\n".join(missing))
        else:
            self.log("[check] Required scripts found.\n")

    def open_chrome(self):
        chrome = self.chrome.get().strip() or find_chrome()
        if not chrome or not Path(chrome).exists():
            messagebox.showerror("Chrome not found", "Set Chrome/Edge path first.")
            return
        profile = Path(os.environ.get("TEMP", str(APP_DIR))) / "forensic_chrome_profile"
        url = self.verify_url.get().strip() or self.site.get().strip()
        cmd = [chrome, "--remote-debugging-port=9222", "--ignore-certificate-errors", f"--user-data-dir={profile}", url]
        try:
            subprocess.Popen(cmd)
            self.log("[chrome] Verification Chrome opened. Complete verification there, then Start Harvest.\n")
        except Exception as e:
            messagebox.showerror("Failed to open Chrome", str(e))

    def cdp_alive(self) -> bool:
        url = self.cdp.get().strip().rstrip("/") + "/json/version"
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                data = resp.read().decode("utf-8", errors="replace")
                info = json.loads(data)
                return bool(info.get("webSocketDebuggerUrl"))
        except Exception:
            return False

    def test_cdp_popup(self):
        if self.cdp_alive():
            messagebox.showinfo("CDP connection", "CDP is working. Chrome is listening on port 9222.")
            self.log("[cdp] Connection test OK.\n")
        else:
            messagebox.showwarning(
                "CDP not running",
                "Nothing is listening at the CDP URL. Click Open Verification Chrome first, or keep Auto-open enabled."
            )
            self.log("[cdp] Connection test failed. ECONNREFUSED means Chrome was not started with --remote-debugging-port=9222.\n")

    def ensure_cdp_ready(self) -> bool:
        if self.mode.get() != "cdp":
            return True
        if self.cdp_alive():
            return True
        if not self.auto_open_cdp_chrome.get():
            messagebox.showwarning(
                "CDP not running",
                "Chrome CDP is not running at the selected URL. Click Open Verification Chrome first."
            )
            return False

        self.log("[cdp] CDP not running. Opening verification Chrome automatically...\n")
        self.open_chrome()
        self.log("[cdp] Waiting for Chrome CDP port...\n")
        for _ in range(20):
            time.sleep(0.5)
            if self.cdp_alive():
                self.log("[cdp] Chrome CDP is now available.\n")
                messagebox.showinfo(
                    "Verification Chrome opened",
                    "Chrome is open with CDP enabled. If the site needs manual verification/login, complete it in that Chrome window, then click Start Harvest again."
                )
                return False

        messagebox.showerror(
            "CDP failed",
            "Could not start/connect to Chrome CDP on port 9222. Close all Chrome windows and try Open Verification Chrome again."
        )
        return False

    def harvest_cmd(self):
        mode_map = {
            "Auto detect": "auto",
            "Numbered pages": "numbered",
            "Sitemaps": "sitemap",
            "Find links by itself": "link",
            "Single page only": "single",
        }
        selected_mode = mode_map.get(self.crawl_mode.get(), "auto")

        cmd = [
            sys.executable, str(HARVESTER), "crawl", self.site.get().strip(),
            "--crawl-mode", selected_mode,
            "--page-start", self.page_start.get().strip(), "--page-end", self.page_end.get().strip(),
            "--max-pages", self.max_pages.get().strip(), "--scroll-steps", self.scroll.get().strip(),
            "--wait-ms", self.wait_ms.get().strip(), "--delay-ms", self.delay_ms.get().strip(),
            "--image-concurrency", self.conc.get().strip(), "--output", self.harvest_out.get().strip(),
            "--url-include", self.url_include.get().strip(),
            "--url-exclude", self.url_exclude.get().strip(),
        ]
        if self.block_ads.get():
            cmd.append("--block-ads")
        if self.close_popups.get():
            cmd.append("--close-popups")
        if self.page_template.get().strip():
            cmd += ["--page-template", self.page_template.get().strip()]
        if self.mode.get() == "cdp":
            cmd += ["--connect-cdp", self.cdp.get().strip()]
        else:
            cmd += ["--headful", "--pause-first-page", "--verify-url", self.verify_url.get().strip()]
        if self.harvest_videos.get():
            cmd += [
                "--harvest-videos",
                "--max-video-mb", self.max_video_mb.get().strip(),
                "--video-concurrency", self.video_concurrency.get().strip(),
                "--video-timeout-ms", self.video_timeout_ms.get().strip(),
            ]
            if self.probe_video_playback.get():
                cmd += [
                    "--probe-video-playback",
                    "--video-probe-seconds", self.video_probe_seconds.get().strip(),
                    "--max-video-elements", self.max_video_elements.get().strip(),
                ]
            if self.download_hls_segments.get():
                cmd.append("--download-hls-segments")
            if self.concat_detected_ts_segments.get():
                cmd.append("--concat-detected-ts-segments")
            if self.download_hls_segments.get() or self.concat_detected_ts_segments.get():
                cmd += [
                    "--hls-max-segments", self.hls_max_segments.get().strip(),
                    "--hls-concurrency", self.hls_concurrency.get().strip(),
                ]
        if self.same_domain_videos_only.get():
            cmd.append("--same-domain-videos-only")
        if self.scroll_until_bottom.get():
            cmd += [
                "--scroll-until-bottom",
                "--max-bottom-scroll-rounds", self.max_bottom_scroll_rounds.get().strip(),
                "--bottom-scroll-delay-ms", self.bottom_scroll_delay_ms.get().strip(),
                "--lazy-final-wait-ms", self.lazy_final_wait_ms.get().strip(),
            ]
        if self.wait_load_state.get().strip():
            cmd += ["--wait-load-state", self.wait_load_state.get().strip()]
        if self.extra_settle_ms.get().strip():
            cmd += ["--extra-settle-ms", self.extra_settle_ms.get().strip()]
        if self.force_lazy_media.get():
            cmd += ["--force-lazy-media", "--lazy-force-wait-ms", self.lazy_force_wait_ms.get().strip()]
        if self.page_fetch_images.get():
            cmd.append("--page-fetch-images")
        if self.screenshot_image_fallback.get():
            cmd.append("--screenshot-image-fallback")
        if self.network_only.get(): cmd.append("--network-only")
        if self.no_shots.get(): cmd.append("--no-screenshots")
        if self.no_html.get(): cmd.append("--no-html")
        if self.sitemaps.get(): cmd.append("--discover-sitemaps")
        if self.auto_refresh.get():
            cmd += ["--auto-refresh-errors", "--refresh-retries", self.refresh_retries.get().strip(), "--refresh-delay-ms", self.refresh_delay.get().strip()]
        return cmd

    def match_cmd(self):
        cmd = [
            sys.executable, str(MATCHER), self.target.get().strip(),
            "--metadata", self.metadata.get().strip(), "--output", self.match_out.get().strip(),
            "--top", self.top.get().strip(), "--copy-top", self.copy_top.get().strip(),
        ]
        if self.target_face_image.get().strip():
            cmd += ["--target-face-image", self.target_face_image.get().strip()]
        if self.body_detect.get(): cmd.append("--body-detect")
        if self.clip.get(): cmd.append("--clip")
        if self.no_face_match.get(): cmd.append("--no-face-match")
        if self.face_fallback_center.get(): cmd.append("--face-fallback-center")
        if self.candidate_face_fallback_center.get(): cmd.append("--candidate-face-fallback-center")
        if not self.use_mtcnn.get(): cmd.append("--disable-mtcnn")
        return cmd

    def convert_cmd(self, input_path=None, output_dir=None):
        inp = input_path or self.convert_existing_input.get().strip()
        out_dir = output_dir or self.convert_output_dir.get().strip()
        cmd = [
            sys.executable, str(CONVERTER), inp,
            "--output-dir", out_dir,
            "--mode", self.convert_mode.get().strip() or "auto",
        ]
        if self.ffmpeg_path.get().strip():
            cmd += ["--ffmpeg-path", self.ffmpeg_path.get().strip()]
        if self.convert_recursive.get():
            cmd.append("--recursive")
        if self.convert_overwrite.get():
            cmd.append("--overwrite")
        return cmd

    def start_convert_existing(self):
        if not CONVERTER.exists():
            messagebox.showerror("Missing converter", str(CONVERTER))
            return
        if not self.convert_existing_input.get().strip():
            messagebox.showerror("Missing input", "Choose a .ts/.m3u8 file or folder first.")
            return
        self.run(self.convert_cmd(), "convert")

    def after_harvest_convert(self):
        if not self.auto_convert_ts.get():
            return
        videos_dir = Path(self.harvest_out.get()) / "videos"
        if not videos_dir.exists():
            self.log("[convert] No videos folder found, skipping MP4 conversion.\n")
            return
        self.convert_existing_input.set(str(videos_dir))
        self.convert_output_dir.set(str(Path(self.harvest_out.get()) / "videos_mp4"))
        self.run(self.convert_cmd(str(videos_dir), str(Path(self.harvest_out.get()) / "videos_mp4")), "convert")

    def start_harvest(self):
        if not HARVESTER.exists():
            messagebox.showerror("Missing script", str(HARVESTER))
            return
        if not self.ensure_cdp_ready():
            return
        self.metadata.set(str(Path(self.harvest_out.get()) / "images_metadata.csv"))
        self.run(self.harvest_cmd(), "harvest", on_done=self.after_harvest_convert if self.auto_convert_ts.get() else None)

    def start_match(self):
        if not MATCHER.exists():
            messagebox.showerror("Missing script", str(MATCHER))
            return
        if not self.target.get().strip():
            messagebox.showerror("Missing target image", "Choose target image first.")
            return
        if not Path(self.metadata.get().strip()).exists():
            messagebox.showerror("Missing metadata", "images_metadata.csv not found.")
            return
        self.run(self.match_cmd(), "match", on_done=self.load_results)

    def run(self, cmd, name, on_done=None):
        if self.proc and self.proc.poll() is None:
            messagebox.showwarning("Task running", "Stop the current task first.")
            return
        self.nb.select(self.log_tab)
        self.log("\n" + "=" * 90 + "\n")
        self.log(f"[run] Starting {name}\n")
        self.log("[cmd] " + " ".join(f'"{x}"' if " " in x else x for x in cmd) + "\n\n")
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if is_windows() else 0
        try:
            self.proc = subprocess.Popen(
                cmd, cwd=str(APP_DIR), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                env=env, creationflags=flags, bufsize=1,
            )
        except Exception as e:
            messagebox.showerror("Failed to start", str(e))
            return
        self.status.set(f"Running {name}...")
        threading.Thread(target=self._reader, daemon=True).start()
        threading.Thread(target=self._waiter, args=(name, on_done), daemon=True).start()

    def _reader(self):
        if not self.proc or not self.proc.stdout:
            return
        for line in self.proc.stdout:
            self.q.put(line)

    def _waiter(self, name, on_done):
        if not self.proc:
            return
        code = self.proc.wait()
        self.q.put(f"\n[done] {name} exited with code {code}\n")
        self.status.set("Ready")
        if on_done and code == 0:
            self.after(500, on_done)

    def send_enter(self):
        if self.proc and self.proc.poll() is None and self.proc.stdin:
            try:
                self.proc.stdin.write("\n")
                self.proc.stdin.flush()
                self.log("[input] Sent Enter.\n")
            except Exception as e:
                self.log(f"[input-error] {e}\n")
        else:
            self.log("[input] No running process.\n")

    def stop(self):
        if not self.proc or self.proc.poll() is not None:
            self.log("[stop] No running task.\n")
            return
        self.log("[stop] Stopping. Already-written images/CSV remain saved.\n")
        try:
            if is_windows():
                try:
                    self.proc.send_signal(signal.CTRL_BREAK_EVENT)
                    time.sleep(1)
                except Exception:
                    pass
            if self.proc.poll() is None:
                self.proc.terminate()
        except Exception as e:
            self.log(f"[stop-error] {e}\n")

    def load_results(self):
        path = Path(self.match_out.get()) / "matches.csv"
        if not path.exists():
            messagebox.showwarning("No matches.csv", f"Not found:\n{path}")
            return
        self.match_rows = []
        with path.open("r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                self.match_rows.append(row)
        for item in self.results_tree.get_children():
            self.results_tree.delete(item)
        for idx, row in enumerate(self.match_rows):
            face_val = row.get("face_crop_visual_percent", row.get("face_match_percent", ""))
            self.results_tree.insert("", "end", iid=str(idx), values=(
                row.get("rank", ""), row.get("combined_score_percent", ""),
                row.get("full_image_score_percent", ""), face_val,
                row.get("face_count", ""), row.get("body_count", ""),
                row.get("face_match_reason", ""),
            ))
        self.nb.select(self.review_tab)
        if self.match_rows:
            self.results_tree.selection_set("0")
            self.show_selected_result()
        self.log(f"[review] Loaded {len(self.match_rows)} matches from {path}\n")

    def selected_row(self):
        sel = self.results_tree.selection()
        if not sel:
            return None
        try:
            return self.match_rows[int(sel[0])]
        except Exception:
            return None

    def show_selected_result(self):
        row = self.selected_row()
        if not row:
            return
        path = row.get("copied_path") or row.get("local_path") or ""
        self.show_image(path)
        self.detail_text.delete("1.0", "end")
        keys = [
            "rank", "combined_score_percent", "full_image_score_percent", "face_crop_visual_percent",
            "face_match_reason", "detector_used", "hash_score_percent", "clip_cosine", "face_count",
            "body_count", "width", "height", "source_url", "found_on_page", "page_title",
            "page_dates", "local_path", "copied_path", "face_crops_folder", "body_crops_folder"
        ]
        self.detail_text.insert("1.0", "\n".join(f"{k}: {row.get(k, '')}" for k in keys))

    def show_image(self, path: str):
        self.preview_refs.clear()
        p = Path(path)
        if not p.exists():
            self.preview_label.configure(text="Image file not found", image="")
            return
        try:
            img = Image.open(p)
            img = ImageOps.exif_transpose(img)
            img.thumbnail((520, 520))
            tk_img = ImageTk.PhotoImage(img)
            self.preview_refs.append(tk_img)
            self.preview_label.configure(image=tk_img, text="")
        except Exception as e:
            self.preview_label.configure(text=f"Could not preview image:\n{e}", image="")

    def open_selected_file(self):
        row = self.selected_row()
        if not row:
            return
        path = row.get("copied_path") or row.get("local_path") or ""
        if path:
            open_folder(path)

    def open_selected_face_crops(self):
        row = self.selected_row()
        if not row:
            return
        folder = row.get("face_crops_folder") or ""
        if folder:
            open_folder(folder)
        else:
            messagebox.showinfo("No face crops", row.get("face_match_reason", "No face crops for selected image."))

    def open_selected_page(self):
        row = self.selected_row()
        if not row:
            return
        url = row.get("found_on_page") or row.get("source_url") or ""
        if url.startswith("http"):
            import webbrowser
            webbrowser.open(url)
        else:
            messagebox.showinfo("No page URL", "No source page URL found for selected row.")

    def _poll_log(self):
        try:
            while True:
                line = self.q.get_nowait()
                self.log(line)
        except queue.Empty:
            pass
        self.after(100, self._poll_log)

    def log(self, text: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def open_current_output(self):
        tab = self.nb.tab(self.nb.select(), "text")
        open_folder(self.match_out.get() if "Match" in tab or "Review" in tab else self.harvest_out.get())

    def _close(self):
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno("Task running", "Stop task and close?"):
                return
            self.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
