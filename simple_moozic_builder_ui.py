#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import traceback
import faulthandler
import atexit
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import customtkinter as ctk
except Exception as e:  # pragma: no cover
    raise SystemExit("CustomTkinter is required. Install with: pip install customtkinter") from e

from PIL import Image, ImageTk

try:  # Optional spike dependency for preview playback.
    import miniaudio  # type: ignore
except Exception:
    miniaudio = None

from simple_moozic_builder import (
    _safe_song_stem,
    AudioTrackEntry,
    BuildTrackEvent,
    audio_cache_root,
    app_root,
    bundled_resource_root,
    bootstrap_runtime_folders,
    build_mixed_from_config,
    convert_single_audio_file,
    create_song_from_sources,
    default_assets_root,
    default_audio_root,
    default_cover_root,
    default_output_root,
    locate_ffplay,
    render_workshop_square_image,
    refresh_song_catalog,
    ensure_audio_workspace,
)


STATE_SCHEMA_VERSION = 1
LAST_STATE_FILENAME = ".smb_last_state.json"
LAST_MIX_STATE_FILENAME = ".smb_last_mix_state.json"
RECENT_LIST_FILENAME = ".smb_recent.json"
RECENT_LIMIT = 20
CRASH_LOG_FILENAME = "simple_moozic_builder_crash.log"
FATAL_LOG_FILENAME = "simple_moozic_builder_fatal.log"
MAX_PREVIEW_TILES = 80

KEYCODE_A = 65
KEYCODE_S = 83
KEYCODE_O = 79


def _write_crash_log(exc_type, exc_value, exc_traceback, context: str = "Unhandled exception") -> Path:
    log_path = app_root() / CRASH_LOG_FILENAME
    lines = traceback.format_exception(exc_type, exc_value, exc_traceback)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"\n[{stamp}] {context}\n")
        f.writelines(lines)
    return log_path


_FAULT_HANDLER_STREAM = None


def _enable_fatal_fault_log() -> None:
    global _FAULT_HANDLER_STREAM
    if _FAULT_HANDLER_STREAM is not None:
        return
    log_path = app_root() / FATAL_LOG_FILENAME
    stream = log_path.open("a", encoding="utf-8")
    _FAULT_HANDLER_STREAM = stream
    faulthandler.enable(file=stream, all_threads=True)
    atexit.register(stream.close)


class _MiniAudioPreviewHandle:
    def __init__(self, audio_path: Path):
        self._audio_path = audio_path
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._device = None
        self._start()

    def _start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        if miniaudio is None:
            return
        try:
            stream = miniaudio.stream_file(str(self._audio_path))
            self._device = miniaudio.PlaybackDevice()
            self._device.start(stream)
            while not self._stop_event.is_set():
                time.sleep(0.05)
        except Exception:
            pass
        finally:
            dev = self._device
            self._device = None
            if dev is not None:
                try:
                    dev.close()
                except Exception:
                    pass

    def terminate(self) -> None:
        self._stop_event.set()

    def kill(self) -> None:
        self._stop_event.set()

    def wait(self, timeout: float | None = None) -> None:
        t = self._thread
        if t is None:
            return
        t.join(timeout=timeout)


class Tooltip:
    _active: "Tooltip | None" = None

    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text
        self.tipwindow: tk.Toplevel | None = None
        self._hide_job: str | None = None
        self.widget.bind("<Enter>", self._show, add="+")
        self.widget.bind("<Motion>", self._move, add="+")
        self.widget.bind("<Leave>", self._hide, add="+")

    def _show(self, event=None) -> None:
        if self._hide_job is not None:
            try:
                self.widget.after_cancel(self._hide_job)
            except Exception:
                pass
            self._hide_job = None
        if Tooltip._active is not None and Tooltip._active is not self:
            Tooltip._active._hide_immediate()
        if self.tipwindow or not self.text:
            return
        x = (event.x_root + 12) if event is not None else (self.widget.winfo_rootx() + 16)
        y = (event.y_root + 18) if event is not None else (self.widget.winfo_rooty() + self.widget.winfo_height() + 6)
        self.tipwindow = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            tw,
            text=self.text,
            justify=tk.LEFT,
            background="#1f242c",
            foreground="#f0f4ff",
            relief=tk.SOLID,
            borderwidth=1,
            padx=6,
            pady=4,
        )
        label.pack()
        Tooltip._active = self

    def _move(self, event=None) -> None:
        if self.tipwindow is None or event is None:
            return
        x = event.x_root + 12
        y = event.y_root + 18
        self.tipwindow.wm_geometry(f"+{x}+{y}")

    def _hide(self, _event=None) -> None:
        if self._hide_job is not None:
            try:
                self.widget.after_cancel(self._hide_job)
            except Exception:
                pass
            self._hide_job = None

        def _hide_if_really_outside() -> None:
            self._hide_job = None
            if self.tipwindow is None:
                return
            x = self.widget.winfo_pointerx()
            y = self.widget.winfo_pointery()
            hovered = self.widget.winfo_containing(x, y)
            if hovered is not None and (hovered == self.widget or str(hovered).startswith(str(self.widget))):
                return
            self._hide_immediate()

        self._hide_job = self.widget.after(60, _hide_if_really_outside)

    def _hide_immediate(self) -> None:
        if self._hide_job is not None:
            try:
                self.widget.after_cancel(self._hide_job)
            except Exception:
                pass
            self._hide_job = None
        if self.tipwindow is not None:
            self.tipwindow.destroy()
            self.tipwindow = None
        if Tooltip._active is self:
            Tooltip._active = None


class SimpleMoozicBuilderUI(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Simple Moozic Builder - by Talismon")
        self.geometry("1320x820")

        bootstrap_runtime_folders()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        try:
            # Guard against corrupted DPI/shell state that can produce absurd window/dialog sizes.
            ctk.set_widget_scaling(1.0)
            ctk.set_window_scaling(1.0)
            cur_scale = float(self.tk.call("tk", "scaling"))
            if cur_scale < 0.8 or cur_scale > 2.5:
                self.tk.call("tk", "scaling", 1.3333333333)
        except Exception:
            pass

        self.assets_root = default_assets_root()
        self.cover_root = default_cover_root()
        self.audio_dir_active = default_audio_root()
        self.audio_dir_override: Path | None = None
        self.out_dir = default_output_root()
        self.workshop_dir_override: Path | None = None
        self.last_song_pick_dir = Path.home()
        self.last_image_pick_dir = self.cover_root if self.cover_root.exists() else Path.home()

        self.default_poster_path = self.assets_root / "poster" / "poster.png"
        self.poster_path: Path | None = self.default_poster_path if self.default_poster_path.exists() else None
        self.final_output_dir: Path | None = None
        self.track_rows: list[dict] = []
        self.track_settings: dict[str, dict] = {}
        self.excluded_oggs: set[str] = set()
        self.preview_images: list[ctk.CTkImage] = []
        self.preview_tile_overflow = False
        self.poster_thumb_top: ctk.CTkImage | None = None
        self.recent_projects: list[str] = []
        self.last_save_path: Path | None = None
        self.inline_editor = None
        self.preview_proc: object | None = None
        self.preview_ffplay = locate_ffplay()
        self.preview_backend = "ffplay"
        if miniaudio is not None:
            self.preview_backend = "miniaudio"
        if os.environ.get("SMB_PREVIEW_BACKEND", "").strip().lower() == "ffplay":
            self.preview_backend = "ffplay"
        self._aux_preview_procs: list[object] = []
        self._hover_tip_window: tk.Toplevel | None = None
        self._window_icon_image: tk.PhotoImage | None = None
        self._window_icon_images: list[tk.PhotoImage] = []
        self.cassette_switch_icon = self._load_ui_icon("CassIcon24.png")
        self.vinyl_switch_icon = self._load_ui_icon("VinylIcon24.png")

        self.mod_id_var = tk.StringVar(value="TM_MyPack")
        self.parent_mod_var = tk.StringVar(value="TrueMoozic")
        self.name_var = tk.StringVar(value="My Pack")
        self.author_var = tk.StringVar(value="")
        self.poster_add_name_var = tk.BooleanVar(value=True)
        self.poster_var = tk.StringVar(value="poster.png" if self.poster_path else "Select poster")
        self.audio_status_var = tk.StringVar(value=f"/{self.audio_dir_active.name}")
        self.filter_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="Ready")
        self.completion_var = tk.StringVar(value="")

        self.selected_ogg_name: str | None = None
        self.bulk_cassette_var = tk.BooleanVar(value=True)
        self.bulk_vinyl_var = tk.BooleanVar(value=True)
        self.global_vinyl_mask_var = tk.StringVar(value="inside")
        self.build_progress_var = tk.DoubleVar(value=0.0)

        self.sort_state: dict[str, bool] = {}

        self._load_recent_projects()
        self._build_layout()
        self._apply_window_icon(self)
        self._update_top_poster_preview(self.poster_path)
        self.name_var.trace_add("write", lambda *_: self._update_top_poster_preview(self.poster_path))
        self._bind_shortcuts()
        self._load_last_session_state()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _load_ui_icon(self, filename: str) -> ctk.CTkImage | None:
        exe_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else None
        meipass = Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "_MEIPASS", None) else None
        module_dir = Path(__file__).resolve().parent
        roots = [
            app_root(),
            bundled_resource_root(),
            module_dir,
        ]
        if exe_dir is not None:
            roots.append(exe_dir)
        if meipass is not None:
            roots.append(meipass)
        candidates = []
        for root in roots:
            candidates.append(root / "smb_icons" / filename)
            candidates.append(root / "_internal" / "smb_icons" / filename)
        icon_path = next((p for p in candidates if p.exists() and p.is_file()), None)
        if icon_path is None:
            return None
        try:
            with Image.open(icon_path) as im:
                rgba = im.convert("RGBA")
            size = rgba.size
            return ctk.CTkImage(light_image=rgba, dark_image=rgba, size=size)
        except Exception:
            return None

    def _build_layout(self) -> None:
        self._build_menu_bar()

        root = ctk.CTkFrame(self)
        root.pack(fill="both", expand=True, padx=12, pady=12)

        left = ctk.CTkFrame(root)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))

        right = ctk.CTkFrame(root, width=500)
        right.pack(side="right", fill="both", padx=(8, 0))
        right.pack_propagate(False)

        form = ctk.CTkFrame(left)
        form.pack(fill="x", padx=8, pady=12)

        form_grid = ctk.CTkFrame(form, fg_color="transparent")
        form_grid.pack(fill="x", padx=10, pady=6)
        form_grid.grid_columnconfigure(0, minsize=120)
        form_grid.grid_columnconfigure(1, weight=1)
        form_grid.grid_columnconfigure(2, minsize=96)
        form_grid.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(form_grid, text="Mod ID", width=120, anchor="w").grid(row=0, column=0, sticky="w", pady=(0, 8))
        ctk.CTkEntry(form_grid, textvariable=self.mod_id_var).grid(row=0, column=1, sticky="ew", pady=(0, 8))
        ctk.CTkLabel(form_grid, text="Parent Mod", width=96, anchor="w").grid(row=0, column=2, sticky="w", padx=(16, 0), pady=(0, 8))
        ctk.CTkEntry(form_grid, textvariable=self.parent_mod_var).grid(row=0, column=3, sticky="ew", pady=(0, 8))

        ctk.CTkLabel(form_grid, text="Name", width=120, anchor="w").grid(row=1, column=0, sticky="w", pady=(0, 8))
        ctk.CTkEntry(form_grid, textvariable=self.name_var).grid(row=1, column=1, sticky="ew", pady=(0, 8))
        ctk.CTkLabel(form_grid, text="Author", width=96, anchor="w").grid(row=1, column=2, sticky="w", padx=(16, 0), pady=(0, 8))
        author_entry = ctk.CTkEntry(form_grid, textvariable=self.author_var)
        author_entry.grid(row=1, column=3, sticky="ew", pady=(0, 8))
        Tooltip(author_entry, "Comma-separated authors for mod.info")

        ctk.CTkLabel(form_grid, text="Workshop Poster", width=120, anchor="w").grid(row=2, column=0, sticky="w")
        poster_row = ctk.CTkFrame(form_grid, fg_color="transparent")
        poster_row.grid(row=2, column=1, rowspan=2, sticky="nsw")
        self.poster_top_preview = ctk.CTkButton(
            poster_row,
            text="",
            width=84,
            height=80,
            corner_radius=6,
            border_width=1,
            fg_color=("#2f3742", "#2f3742"),
            hover_color=("#3a4250", "#3a4250"),
            command=self.pick_poster,
        )
        self.poster_top_preview.pack(side="left", padx=(0, 8), pady=2)
        self.poster_top_preview.bind("<Button-3>", self.on_poster_preview_right_click)
        ctk.CTkLabel(poster_row, textvariable=self.poster_var, anchor="w").pack(side="left")
        self.poster_add_name_checkbox = ctk.CTkCheckBox(
            poster_row,
            text="",
            width=20,
            variable=self.poster_add_name_var,
            command=lambda: self._update_top_poster_preview(self.poster_path),
        )
        self.poster_add_name_checkbox.pack(side="left", padx=(8, 0))
        Tooltip(self.poster_add_name_checkbox, "Add Name to Poster (Workshop Only)")

        ctk.CTkLabel(form_grid, text=".ogg Output:", width=96, anchor="w").grid(row=2, column=2, sticky="w", padx=(16, 0))
        self.audio_source_button = ctk.CTkButton(
            form_grid,
            text=self.audio_status_var.get(),
            width=180,
            command=self.pick_audio_source,
        )
        self.audio_source_button.grid(row=2, column=3, sticky="ew")

        controls = ctk.CTkFrame(left)
        controls.pack(fill="x", padx=8, pady=(0, 8))

        controls_row = ctk.CTkFrame(controls, fg_color="transparent")
        controls_row.pack(fill="x", padx=8, pady=(6, 8))

        btn_refresh = ctk.CTkButton(controls_row, text="\u21bb", width=40, command=self.manual_refresh_songs)
        btn_refresh.pack(side="left", padx=(0, 6))
        Tooltip(btn_refresh, "Refresh Songs")

        btn_convert = ctk.CTkButton(controls_row, text="\u21c4", width=40, command=self.convert_audio)
        btn_convert.pack(side="left", padx=(0, 6))
        Tooltip(btn_convert, "Convert Songs")

        btn_poster = ctk.CTkButton(controls_row, text="\u25a7", width=40, command=self.apply_poster_to_all)
        btn_poster.pack(side="left", padx=(0, 6))
        Tooltip(btn_poster, "Apply Poster To All")

        btn_default = ctk.CTkButton(controls_row, text="\u25a3", width=40, command=self.apply_default_to_all)
        btn_default.pack(side="left", padx=(0, 6))
        Tooltip(btn_default, "Apply Mod Default to All (Random Textures)")

        cassette_switch_wrap = ctk.CTkFrame(controls_row, fg_color="transparent")
        cassette_switch_wrap.pack(side="left", padx=(20, 8))
        if self.cassette_switch_icon is not None:
            ctk.CTkLabel(cassette_switch_wrap, text="", image=self.cassette_switch_icon).pack(side="left", padx=(0, 6))
        else:
            ctk.CTkLabel(cassette_switch_wrap, text="Cass").pack(side="left", padx=(0, 6))
        self.bulk_cassette_switch = ctk.CTkSwitch(
            cassette_switch_wrap,
            text="",
            width=44,
            switch_width=36,
            switch_height=18,
            variable=self.bulk_cassette_var,
            command=lambda: self.bulk_set("cassette", bool(self.bulk_cassette_var.get())),
        )
        self.bulk_cassette_switch.pack(side="left")
        Tooltip(self.bulk_cassette_switch, "Toggle All Cassettes")

        vinyl_switch_wrap = ctk.CTkFrame(controls_row, fg_color="transparent")
        vinyl_switch_wrap.pack(side="left", padx=(0, 22))
        if self.vinyl_switch_icon is not None:
            ctk.CTkLabel(vinyl_switch_wrap, text="", image=self.vinyl_switch_icon).pack(side="left", padx=(0, 6))
        else:
            ctk.CTkLabel(vinyl_switch_wrap, text="Vinyl").pack(side="left", padx=(0, 6))
        self.bulk_vinyl_switch = ctk.CTkSwitch(
            vinyl_switch_wrap,
            text="",
            width=44,
            switch_width=36,
            switch_height=18,
            variable=self.bulk_vinyl_var,
            command=lambda: self.bulk_set("vinyl", bool(self.bulk_vinyl_var.get())),
        )
        self.bulk_vinyl_switch.pack(side="left")
        Tooltip(self.bulk_vinyl_switch, "Toggle All Vinyls")

        self.global_vinyl_mask_button = ctk.CTkButton(
            controls_row,
            text="\u25cf Inner",
            width=96,
            command=self.toggle_global_vinyl_mask,
        )
        self.global_vinyl_mask_button.pack(side="left", padx=(0, 6))
        Tooltip(self.global_vinyl_mask_button, "Toggle Vinyl Mask")

        btn_mix = ctk.CTkButton(controls_row, text="\u266b Mixtape", width=104, command=self.open_song_builder_popup)
        btn_mix.pack(side="left", padx=(0, 6))
        Tooltip(btn_mix, "Create Mix")

        ctk.CTkButton(
            controls_row,
            text="Build",
            width=96,
            command=self.build_pack,
            font=ctk.CTkFont(weight="bold"),
        ).pack(side="right", padx=(6, 0))
        self._refresh_global_vinyl_mask_button()

        songs = ctk.CTkFrame(left)
        songs.pack(fill="both", expand=True, padx=8, pady=8)

        filter_row = ctk.CTkFrame(songs, fg_color="transparent")
        filter_row.pack(fill="x", padx=8, pady=(8, 0))
        self.filter_entry = ctk.CTkEntry(filter_row, textvariable=self.filter_var, placeholder_text="Filter songs...")
        self.filter_entry.pack(fill="x", expand=True)
        self.filter_entry.bind("<KeyRelease>", self.on_filter_change)

        table_wrap = ctk.CTkFrame(songs, fg_color="transparent")
        table_wrap.pack(fill="both", expand=True)

        cols = ("source", "preview", "status", "cassette", "vinyl", "bside", "cover")
        self.tree = ttk.Treeview(table_wrap, columns=cols, show="headings", height=14, selectmode="extended")

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Treeview",
            rowheight=24,
            font=("Segoe UI Emoji", 11),
            background="#262b33",
            fieldbackground="#262b33",
            foreground="#e6ebf2",
        )
        style.map("Treeview", background=[("selected", "#355d8c")], foreground=[("selected", "#f5f8ff")])
        style.configure(
            "Treeview.Heading",
            font=("Segoe UI", 10, "bold"),
            background="#313844",
            foreground="#d8dee8",
            relief="flat",
            borderwidth=0,
        )
        style.map("Treeview.Heading", background=[("active", "#3a4250")], foreground=[("active", "#c7ceda")])

        for col, text, w in (
            ("source", "Song", 330),
            ("preview", "", 40),
            ("status", "Status", 120),
            ("cassette", "Cass", 60),
            ("vinyl", "Vinyl", 60),
            ("bside", "B-Side", 140),
            ("cover", "Item Image", 180),
        ):
            self.tree.heading(col, text=text, command=lambda c=col: self.sort_by_column(c))
            self.tree.column(col, width=w, anchor="w")

        self.tree.tag_configure("odd", background="#2b3038", foreground="#e6ebf2")
        self.tree.tag_configure("even", background="#252a32", foreground="#e6ebf2")
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)
        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<Double-1>", self.on_tree_double_click)
        self.tree.bind("<Button-3>", self.on_tree_right_click)
        self.tree.bind("<Delete>", self.on_delete_key)
        self.tree.bind("<Control-KeyPress>", self._on_tree_ctrl_key, add="+")
        self.tree.bind("<ButtonRelease-1>", self.on_tree_release)
        self.tree.bind("<Motion>", self.on_tree_motion)
        self.tree.bind("<Leave>", self.on_tree_leave)

        yscroll = ttk.Scrollbar(table_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=(8, 8))
        yscroll.pack(side="right", fill="y", pady=(8, 8), padx=(0, 8))

        song_actions = ctk.CTkFrame(songs, fg_color="transparent")
        song_actions.configure(height=40)
        song_actions.pack_propagate(False)
        song_actions.pack(fill="x", padx=8, pady=(0, 4))
        self.add_song_button = ctk.CTkButton(song_actions, text="+", width=34, height=34, command=self.add_song_files)
        self.add_song_button.pack(side="left")
        Tooltip(self.add_song_button, "Add Song(s)")
        self.remove_song_button = ctk.CTkButton(song_actions, text="-", width=34, height=34, command=self.remove_selected_songs)
        self.remove_song_button.pack(side="left", padx=(6, 0))
        Tooltip(self.remove_song_button, "Remove Song")
        self.move_song_up_button = ctk.CTkButton(song_actions, text="\u2191", width=34, height=34, command=self.move_selected_songs_up)
        self.move_song_up_button.pack(side="left", padx=(6, 0))
        Tooltip(self.move_song_up_button, "Move Up")
        self.move_song_down_button = ctk.CTkButton(song_actions, text="\u2193", width=34, height=34, command=self.move_selected_songs_down)
        self.move_song_down_button.pack(side="left", padx=(6, 0))
        Tooltip(self.move_song_down_button, "Move Down")

        self.preview_title_label = ctk.CTkLabel(right, text="Build Preview", font=ctk.CTkFont(size=16, weight="bold"))
        self.preview_title_label.pack(anchor="w", padx=10, pady=(10, 4))
        self.progress_bar = ctk.CTkProgressBar(right, variable=self.build_progress_var)
        self.progress_bar.pack(fill="x", padx=10, pady=(0, 4))
        self.progress_bar.set(0)
        self.preview_scroll = ctk.CTkScrollableFrame(right)
        self.preview_scroll.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        ctk.CTkLabel(right, textvariable=self.status_var, anchor="w").pack(fill="x", padx=10, pady=(0, 8))
        complete = ctk.CTkFrame(right)
        complete.pack(fill="x", padx=10, pady=(0, 10))
        ctk.CTkLabel(complete, text="Completion").pack(anchor="w", padx=8, pady=(8, 2))
        complete_buttons = ctk.CTkFrame(complete, fg_color="transparent")
        complete_buttons.pack(padx=8, pady=(4, 10), fill="x")
        ctk.CTkButton(complete_buttons, text="Open Output Folder", command=self.open_output_folder).pack(fill="x", pady=(0, 6))
        ctk.CTkButton(complete_buttons, text="Copy to Workshop", command=self.copy_to_workshop).pack(fill="x", pady=(0, 6))
        ctk.CTkButton(complete_buttons, text="Clear", command=self.clear_preview).pack(fill="x")

    def _build_menu_bar(self) -> None:
        self.menu_bar = tk.Menu(self)
        self.file_menu = tk.Menu(self.menu_bar, tearoff=0)
        self.file_menu.add_command(label="Save", accelerator="Ctrl+S", command=self.menu_save)
        self.file_menu.add_command(label="Load", accelerator="Ctrl+O", command=self.menu_load)
        self.file_menu.add_command(label="Save As", command=self.menu_save_as)
        self.recent_menu = tk.Menu(self.file_menu, tearoff=0)
        self.file_menu.add_cascade(label="Recent", menu=self.recent_menu)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="Exit", command=self.on_close)
        self._refresh_recent_menu()
        self.menu_bar.add_cascade(label="File", menu=self.file_menu)
        self.menu_bar.add_command(label="Create Mix", command=self.open_song_builder_popup)
        self.configure(menu=self.menu_bar)

    def _bind_shortcuts(self) -> None:
        self.bind_all("<Control-KeyPress>", self._on_ctrl_keypress, add="+")

    def _on_ctrl_s(self, _event=None):
        self.menu_save()
        return "break"

    def _is_key(self, event, target_keycode: int, target_keysym: str) -> bool:
        keysym = (getattr(event, "keysym", "") or "").lower()
        keycode = getattr(event, "keycode", None)
        return keycode == target_keycode or keysym == target_keysym

    def _on_ctrl_keypress(self, event):
        if self._is_key(event, KEYCODE_S, "s"):
            return self._on_ctrl_s(event)
        if self._is_key(event, KEYCODE_O, "o"):
            self.menu_load()
            return "break"
        return None

    def _on_tree_ctrl_key(self, event):
        if self._is_key(event, KEYCODE_A, "a"):
            return self.on_tree_select_all(event)
        return None

    def _state_file(self) -> Path:
        return app_root() / LAST_STATE_FILENAME

    def _recent_file(self) -> Path:
        return app_root() / RECENT_LIST_FILENAME

    def _saves_root(self) -> Path:
        root = app_root() / "Saves"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _pack_saves_root(self) -> Path:
        root = self._saves_root() / "packs"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _mix_saves_root(self) -> Path:
        root = self._saves_root() / "mixes"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _mix_state_file(self) -> Path:
        return app_root() / LAST_MIX_STATE_FILENAME

    def _project_snapshot(self) -> dict:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "mod_id": self.mod_id_var.get().strip(),
            "parent_mod_id": self.parent_mod_var.get().strip(),
            "name": self.name_var.get().strip(),
            "author": self.author_var.get().strip(),
            "poster_path": str(self.poster_path) if self.poster_path else None,
            "add_name_to_poster": bool(self.poster_add_name_var.get()),
            "ogg_output_dir": str(self.audio_dir_active),
            "last_song_pick_dir": str(self.last_song_pick_dir),
            "last_image_pick_dir": str(self.last_image_pick_dir),
            "output_dir": str(self.out_dir),
            "workshop_dir": str(self.workshop_dir_override) if self.workshop_dir_override else None,
            "global_vinyl_mask": (self.global_vinyl_mask_var.get() or "inside").strip().lower(),
            "track_settings": self.track_settings,
            "song_order": [row["ogg"].name for row in self.track_rows],
            "excluded_oggs": sorted(self.excluded_oggs),
            "filter_text": self.filter_var.get(),
        }

    def _apply_snapshot(self, data: dict) -> None:
        self.mod_id_var.set(str(data.get("mod_id", "") or self.mod_id_var.get()))
        self.parent_mod_var.set(str(data.get("parent_mod_id", "") or self.parent_mod_var.get()))
        self.name_var.set(str(data.get("name", "") or self.name_var.get()))
        self.author_var.set(str(data.get("author", "") or ""))

        audio_source_raw = data.get("ogg_output_dir") or data.get("audio_source")
        if audio_source_raw:
            p = Path(audio_source_raw)
            if p.exists():
                self.audio_dir_override = p
                self.audio_dir_active = p
                self.audio_status_var.set(f"/{p.name}")
                self._refresh_audio_source_button_text()

        last_song_pick_raw = data.get("last_song_pick_dir")
        if last_song_pick_raw:
            p = Path(last_song_pick_raw)
            if p.exists() and p.is_dir():
                self.last_song_pick_dir = p

        last_image_pick_raw = data.get("last_image_pick_dir")
        if not last_image_pick_raw:
            last_image_pick_raw = data.get("image_source")
        if last_image_pick_raw:
            p = Path(last_image_pick_raw)
            if p.exists() and p.is_dir():
                self.last_image_pick_dir = p

        output_raw = data.get("output_dir")
        if output_raw:
            p = Path(output_raw)
            if p.exists():
                self.out_dir = p
        workshop_raw = data.get("workshop_dir")
        if workshop_raw:
            wp = Path(workshop_raw)
            if wp.exists():
                self.workshop_dir_override = wp

        mask = (data.get("global_vinyl_mask") or "inside").strip().lower()
        self.apply_global_vinyl_mask(mask)

        poster_raw = data.get("poster_path")
        add_name_to_poster = data.get("add_name_to_poster")
        if add_name_to_poster is not None:
            self.poster_add_name_var.set(bool(add_name_to_poster))
        if poster_raw:
            p = Path(poster_raw)
            if p.exists():
                self.poster_path = p
                self.poster_var.set(p.name)
                self._update_top_poster_preview(p)
        else:
            self._update_top_poster_preview(self.poster_path)

        loaded_settings = data.get("track_settings") or {}
        if isinstance(loaded_settings, dict):
            self.track_settings = {
                str(k): dict(v) if isinstance(v, dict) else {} for k, v in loaded_settings.items()
            }
        excluded = data.get("excluded_oggs") or []
        if isinstance(excluded, list):
            self.excluded_oggs = {str(x) for x in excluded}
        self.filter_var.set(str(data.get("filter_text", "") or ""))

        self.refresh_songs()

        song_order = data.get("song_order")
        if isinstance(song_order, list) and song_order:
            order_map = {name: idx for idx, name in enumerate(song_order)}
            self.track_rows.sort(key=lambda row: order_map.get(row["ogg"].name, len(order_map)))
            self._redraw_tree()

    def _load_recent_projects(self) -> None:
        path = self._recent_file()
        if not path.exists():
            self.recent_projects = []
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                self.recent_projects = [str(x) for x in raw if isinstance(x, str)]
            else:
                self.recent_projects = []
        except Exception:
            self.recent_projects = []

    def _save_recent_projects(self) -> None:
        path = self._recent_file()
        try:
            path.write_text(json.dumps(self.recent_projects[:RECENT_LIMIT], indent=2), encoding="utf-8")
        except Exception:
            pass

    def _push_recent_project(self, path: Path) -> None:
        p = str(path.resolve())
        self.recent_projects = [x for x in self.recent_projects if x != p]
        self.recent_projects.insert(0, p)
        self.recent_projects = self.recent_projects[:RECENT_LIMIT]
        self._save_recent_projects()
        self._refresh_recent_menu()

    def _refresh_recent_menu(self) -> None:
        if not hasattr(self, "recent_menu"):
            return
        self.recent_menu.delete(0, "end")
        if not self.recent_projects:
            self.recent_menu.add_command(label="(Empty)", state="disabled")
            return
        for i, path_str in enumerate(self.recent_projects[:RECENT_LIMIT], start=1):
            label = f"{i}. {path_str}"
            self.recent_menu.add_command(label=label, command=lambda p=path_str: self._load_from_path(Path(p)))

    def _write_project_file(self, target: Path) -> bool:
        try:
            self.update_idletasks()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(self._project_snapshot(), indent=2), encoding="utf-8")
            self.last_save_path = target
            self._push_recent_project(target)
            self.status_var.set(f"Saved project: {target}")
            return True
        except Exception as e:
            messagebox.showerror("Save failed", str(e))
            return False

    def _read_project_file(self, source: Path) -> bool:
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Invalid project file format")
            self._apply_snapshot(data)
            self.last_save_path = source
            self._push_recent_project(source)
            self.status_var.set(f"Loaded project: {source}")
            return True
        except Exception as e:
            messagebox.showerror("Load failed", str(e))
            return False

    def menu_save(self) -> None:
        if self.last_save_path is None:
            self.menu_save_as()
            return
        self._write_project_file(self.last_save_path)

    def menu_save_as(self) -> None:
        selected = filedialog.asksaveasfilename(
            title="Save Moozic Builder Project",
            defaultextension=".smbproj.json",
            filetypes=[("Moozic Builder Project", "*.smbproj.json"), ("JSON", "*.json")],
            initialdir=str(self._pack_saves_root()),
            parent=self,
        )
        if not selected:
            return
        self._write_project_file(Path(selected))

    def menu_load(self) -> None:
        selected = filedialog.askopenfilename(
            title="Load Moozic Builder Project",
            filetypes=[("Moozic Builder Project", "*.smbproj.json"), ("JSON", "*.json")],
            initialdir=str(self._pack_saves_root()),
            parent=self,
        )
        if not selected:
            return
        self._load_from_path(Path(selected))

    def _load_from_path(self, path: Path) -> None:
        if not path.exists():
            messagebox.showwarning("Missing file", f"Project file not found:\n{path}")
            self.recent_projects = [x for x in self.recent_projects if x != str(path)]
            self._save_recent_projects()
            self._refresh_recent_menu()
            return
        self._read_project_file(path)

    def _save_last_session_state(self) -> None:
        try:
            self.update_idletasks()
            self._state_file().write_text(json.dumps(self._project_snapshot(), indent=2), encoding="utf-8")
        except Exception:
            pass

    def _load_last_session_state(self) -> None:
        path = self._state_file()
        if not path.exists():
            self.refresh_songs()
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._apply_snapshot(data)
                self.status_var.set("Restored previous session")
                return
        except Exception:
            pass
        self.refresh_songs()

    def on_close(self) -> None:
        self.stop_preview()
        self._hide_hover_tip()
        for proc in list(self._aux_preview_procs):
            try:
                proc.terminate()
            except Exception:
                pass
        self._aux_preview_procs.clear()
        self._save_last_session_state()
        self.destroy()

    def _update_selection_status_hint(self) -> None:
        if self.preview_proc is not None:
            return
        selected = list(self.tree.selection()) if hasattr(self, "tree") else []
        if len(selected) <= 1:
            if self.status_var.get().startswith("Selected:"):
                if self.final_output_dir is not None and self.build_progress_var.get() >= 1.0:
                    self.status_var.set(f"Build complete: {self.final_output_dir}")
                else:
                    self.status_var.set("Ready")
            return
        c = 0
        v = 0
        for key in selected:
            cfg = self.track_settings.get(key, {})
            if cfg.get("cassette"):
                c += 1
            if cfg.get("vinyl"):
                v += 1
        self.status_var.set(f"Selected: Cassette {c} | Vinyl {v}")

    def _audio_file_dialog_initial_dir(self) -> Path:
        if self.last_song_pick_dir.exists():
            return self.last_song_pick_dir
        if self.audio_dir_active.exists():
            return self.audio_dir_active
        return Path.home()

    def _image_file_dialog_initial_dir(self) -> Path:
        if self.last_image_pick_dir.exists():
            return self.last_image_pick_dir
        if self.cover_root.exists():
            return self.cover_root
        return Path.home()

    def _song_status_for_paths(self, source: Path, ogg: Path) -> tuple[str, str]:
        if source.suffix.lower() == ".ogg":
            return "ready", "source ogg"
        if not ogg.exists():
            return "needs convert", "not converted"
        try:
            src_mtime = source.stat().st_mtime
            ogg_mtime = ogg.stat().st_mtime
            if ogg_mtime >= src_mtime:
                return "ready", "up-to-date"
            return "stale", "source newer"
        except Exception:
            return "ready", "up-to-date"

    def _register_linked_song(self, source_file: Path) -> str | None:
        src = source_file.resolve()
        if not src.exists() or not src.is_file():
            return None
        if src.suffix.lower() not in {".ogg", ".mp3", ".wav", ".flac", ".m4a", ".aac", ".wma"}:
            return None

        cache_root = audio_cache_root(self.audio_dir_active.resolve())
        ogg_name = src.name if src.suffix.lower() == ".ogg" else f"{src.stem}.ogg"
        key = ogg_name
        cfg = self.track_settings.setdefault(key, {})
        cfg["source_path"] = str(src)
        cfg["cached_ogg_path"] = str(cache_root / ogg_name)
        self.excluded_oggs.discard(key)
        return key

    def _move_song_to_bottom(self, song_name: str) -> None:
        idx = next((i for i, row in enumerate(self.track_rows) if row["ogg"].name == song_name), None)
        if idx is None:
            return
        row = self.track_rows.pop(idx)
        self.track_rows.append(row)
        self._redraw_tree()

    def add_song_files(self) -> None:
        selected = filedialog.askopenfilenames(
            title="Add Song(s)",
            filetypes=[
                ("Audio", "*.ogg *.mp3 *.wav *.flac *.m4a *.aac *.wma"),
                ("All files", "*.*"),
            ],
            initialdir=str(self._audio_file_dialog_initial_dir()),
            parent=self,
        )
        if not selected:
            return
        added_names: list[str] = []
        for p in selected:
            key = self._register_linked_song(Path(p))
            if key:
                added_names.append(key)
        if selected:
            self.last_song_pick_dir = Path(selected[0]).resolve().parent
        self.refresh_songs()
        for name in added_names:
            self._move_song_to_bottom(name)
        self.status_var.set(f"Added {len(added_names)} song file(s)")

    def _move_selected_rows(self, direction: int) -> None:
        selected = list(self.tree.selection())
        if not selected:
            return
        order = [row["ogg"].name for row in self.track_rows]
        if direction < 0:
            for key in sorted(selected, key=lambda k: order.index(k)):
                idx = order.index(key)
                if idx > 0:
                    order[idx], order[idx - 1] = order[idx - 1], order[idx]
        else:
            for key in sorted(selected, key=lambda k: order.index(k), reverse=True):
                idx = order.index(key)
                if idx < len(order) - 1:
                    order[idx], order[idx + 1] = order[idx + 1], order[idx]
        rank = {name: i for i, name in enumerate(order)}
        self.track_rows.sort(key=lambda row: rank.get(row["ogg"].name, len(rank)))
        self._redraw_tree()
        for key in selected:
            if key in self.tree.get_children():
                self.tree.selection_add(key)

    def move_selected_songs_up(self) -> None:
        self._move_selected_rows(-1)

    def move_selected_songs_down(self) -> None:
        self._move_selected_rows(1)

    def open_selected_file_location(self, row_key: str | None = None) -> None:
        selected = self._selected_keys()
        key = row_key or (selected[0] if selected else None)
        if not key:
            return
        row = next((r for r in self.track_rows if r["ogg"].name == key), None)
        if not row:
            return
        target_file = row["ogg"] if row["ogg"].exists() else row["source"]
        if not target_file.exists():
            return
        try:
            target_str = str(target_file.resolve())
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer.exe", "/select,", target_str])
            elif sys.platform.startswith("darwin"):
                subprocess.Popen(["open", "-R", target_str])
            else: # Linux / Unix
                subprocess.Popen(["xdg-open", str(target_file.parent)])
        except Exception:
            target = target_file.parent
            if target.exists():
                os.startfile(str(target))

    def clear_selected_posters(self) -> None:
        selected = self._selected_keys()
        if not selected:
            return
        for key in selected:
            self.track_settings.setdefault(key, {})["cover"] = None
        self._redraw_tree()
        self.status_var.set(f"Cleared poster on {len(selected)} song(s)")

    def clear_selected_b_sides(self) -> None:
        selected = self._selected_keys()
        if not selected:
            return
        for key in selected:
            self.track_settings.setdefault(key, {})["b_side"] = None
        self._redraw_tree()
        self.status_var.set(f"Cleared B-side on {len(selected)} song(s)")

    def convert_selected_songs(self) -> None:
        selected = self._selected_keys()
        if not selected:
            return
        converted = 0
        errors = 0
        for key in selected:
            row = next((r for r in self.track_rows if r["ogg"].name == key), None)
            if not row:
                continue
            try:
                entry = convert_single_audio_file(row["source"], self.audio_dir_active, force=True)
                row["status"] = entry.status
                row["detail"] = entry.detail
                converted += 1
            except Exception:
                errors += 1
        self.refresh_songs()
        for key in selected:
            if key in self.tree.get_children():
                self.tree.selection_add(key)
        if errors:
            self.status_var.set(f"Converted {converted} song(s), {errors} failed")
        else:
            self.status_var.set(f"Converted {converted} song(s)")

    def remove_selected_songs(self) -> None:
        selected = self._selected_keys()
        if not selected:
            return
        for key in selected:
            self.excluded_oggs.add(key)
            self.track_settings.pop(key, None)
        self.track_rows = [r for r in self.track_rows if r["ogg"].name not in self.excluded_oggs]
        self._redraw_tree()
        self.status_var.set(f"Removed {len(selected)} song(s) from list")

    def open_song_builder_popup(self) -> None:
        popup = ctk.CTkToplevel(self)
        popup.title("Mix Builder")
        popup.minsize(620, 480)

        def place_popup_normal_state() -> None:
            width = 760
            height = 620
            try:
                self.update_idletasks()
                popup.update_idletasks()
                parent_w = max(width, int(self.winfo_width()))
                parent_h = max(height, int(self.winfo_height()))
                x = int(self.winfo_rootx() + (parent_w - width) / 2)
                y = int(self.winfo_rooty() + (parent_h - height) / 2)
                popup.geometry(f"{width}x{height}+{x}+{y}")
            except Exception:
                popup.geometry("760x620")
            # Some Windows/registry states can surface CTkToplevel as maximized/fullscreen.
            try:
                popup.attributes("-fullscreen", False)
            except Exception:
                pass
            try:
                popup.state("normal")
            except Exception:
                pass

        place_popup_normal_state()
        popup.transient(self)
        popup.grab_set()
        popup.focus_set()
        self._apply_window_icon(popup)
        mix_last_save_path: dict[str, Path | None] = {"path": None}

        frame = ctk.CTkFrame(popup)
        frame.pack(fill="both", expand=True, padx=16, pady=16)

        row_name = ctk.CTkFrame(frame, fg_color="transparent")
        row_name.pack(fill="x", padx=8, pady=(8, 10))
        ctk.CTkLabel(row_name, text="Song Name", width=120, anchor="w").pack(side="left")
        song_name_var = tk.StringVar(value="New Song")
        ctk.CTkEntry(row_name, textvariable=song_name_var).pack(side="left", fill="x", expand=True, padx=(6, 0))

        files_label = ctk.CTkLabel(frame, text="Source Files")
        files_label.pack(anchor="w", padx=8)

        table_wrap = ctk.CTkFrame(frame, fg_color="transparent")
        table_wrap.pack(fill="both", expand=True, padx=8, pady=(4, 10))

        files_tree = ttk.Treeview(table_wrap, columns=("source", "preview"), show="headings", height=10, selectmode="extended")
        files_tree.heading("source", text="Song")
        files_tree.column("source", width=600, anchor="w")
        files_tree.heading("preview", text="")
        files_tree.column("preview", width=40, anchor="center")
        yscroll = ttk.Scrollbar(table_wrap, orient="vertical", command=files_tree.yview)
        files_tree.configure(yscrollcommand=yscroll.set)
        files_tree.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")
        song_files: list[Path] = []
        popup_preview_proc: object | None = None
        build_in_progress = {"value": False}
        pulse = {"active": False}
        phase = {"compiling": False}
        pulse_after_id = {"id": None}

        def redraw_files() -> None:
            for iid in files_tree.get_children():
                files_tree.delete(iid)
            for idx, fp in enumerate(song_files, start=1):
                files_tree.insert("", "end", iid=str(idx), values=(fp.name, "\U0001F50A"))

        controls = ctk.CTkFrame(frame, fg_color="transparent")
        controls.configure(height=40)
        controls.pack_propagate(False)
        controls.pack(fill="x", padx=8, pady=(0, 8))

        def add_source_files() -> None:
            selected = filedialog.askopenfilenames(
                title="Add Song(s)",
                filetypes=[
                    ("Audio", "*.ogg *.mp3 *.wav *.flac *.m4a *.aac *.wma"),
                    ("All files", "*.*"),
                ],
                initialdir=str(self._audio_file_dialog_initial_dir()),
                parent=popup,
            )
            if not selected:
                return
            for p in selected:
                fp = Path(p)
                if fp.exists() and fp.is_file():
                    song_files.append(fp)
            self.last_song_pick_dir = Path(selected[0]).resolve().parent
            redraw_files()

        def remove_selected_files() -> None:
            selected_idx = [int(x) - 1 for x in files_tree.selection()]
            if not selected_idx:
                return
            for idx in sorted(selected_idx, reverse=True):
                if 0 <= idx < len(song_files):
                    song_files.pop(idx)
            redraw_files()

        def move_selected_files(direction: int) -> None:
            selected_idx = sorted([int(x) - 1 for x in files_tree.selection()])
            if not selected_idx:
                return
            if direction < 0:
                for idx in selected_idx:
                    if idx <= 0:
                        continue
                    song_files[idx - 1], song_files[idx] = song_files[idx], song_files[idx - 1]
            else:
                for idx in reversed(selected_idx):
                    if idx >= len(song_files) - 1:
                        continue
                    song_files[idx + 1], song_files[idx] = song_files[idx], song_files[idx + 1]
            redraw_files()
            remap = []
            for idx in selected_idx:
                nxt = idx - 1 if direction < 0 else idx + 1
                nxt = max(0, min(len(song_files) - 1, nxt))
                remap.append(str(nxt + 1))
            files_tree.selection_set(remap)

        def clear_files() -> None:
            song_files.clear()
            redraw_files()

        def mix_snapshot() -> dict:
            return {
                "schema_version": 1,
                "name": song_name_var.get().strip(),
                "files": [str(p) for p in song_files],
            }

        def apply_mix_snapshot(data: dict) -> bool:
            if not isinstance(data, dict):
                return False
            loaded_files: list[Path] = []
            for raw in data.get("files", []):
                p = Path(str(raw))
                if p.exists() and p.is_file():
                    loaded_files.append(p)
            song_files.clear()
            song_files.extend(loaded_files)
            song_name_var.set(str(data.get("name", "") or song_name_var.get()))
            redraw_files()
            return True

        def save_mix_to(target: Path) -> bool:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(mix_snapshot(), indent=2), encoding="utf-8")
                mix_last_save_path["path"] = target
                return True
            except Exception as e:
                messagebox.showerror("Mix Builder", f"Failed to save mix:\n{e}", parent=popup)
                return False

        def save_mix_as() -> None:
            default_name = _safe_song_stem(song_name_var.get().strip() or "New Mix")
            selected = filedialog.asksaveasfilename(
                title="Save Mix Project",
                defaultextension=".smbmix.json",
                filetypes=[("Moozic Mix Project", "*.smbmix.json"), ("JSON", "*.json")],
                initialdir=str(self._mix_saves_root()),
                initialfile=f"{default_name}.smbmix.json",
                parent=popup,
            )
            if not selected:
                return
            save_mix_to(Path(selected))

        def save_mix() -> None:
            target = mix_last_save_path["path"]
            if target is None:
                save_mix_as()
                return
            save_mix_to(target)

        def load_mix_file() -> None:
            selected = filedialog.askopenfilename(
                title="Load Mix Project",
                filetypes=[("Moozic Mix Project", "*.smbmix.json"), ("JSON", "*.json")],
                initialdir=str(self._mix_saves_root()),
                parent=popup,
            )
            if not selected:
                return
            path = Path(selected)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                messagebox.showerror("Mix Builder", f"Failed to load mix:\n{e}", parent=popup)
                return
            if apply_mix_snapshot(data):
                mix_last_save_path["path"] = path

        def save_last_mix_state() -> None:
            try:
                self._mix_state_file().write_text(json.dumps(mix_snapshot(), indent=2), encoding="utf-8")
            except Exception:
                pass

        def load_last_mix_state() -> None:
            state_path = self._mix_state_file()
            if not state_path.exists():
                return
            try:
                data = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                return
            apply_mix_snapshot(data)

        popup_menu = tk.Menu(popup)
        popup_file_menu = tk.Menu(popup_menu, tearoff=0)
        popup_file_menu.add_command(label="Save", command=save_mix)
        popup_file_menu.add_command(label="Save As", command=save_mix_as)
        popup_file_menu.add_command(label="Load", command=load_mix_file)
        popup_menu.add_cascade(label="File", menu=popup_file_menu)
        popup.configure(menu=popup_menu)

        add_btn = ctk.CTkButton(controls, text="+", width=34, height=34, command=add_source_files)
        add_btn.pack(side="left")
        Tooltip(add_btn, "Add Song(s)")
        remove_btn = ctk.CTkButton(controls, text="-", width=34, height=34, command=remove_selected_files)
        remove_btn.pack(side="left", padx=(6, 0))
        Tooltip(remove_btn, "Remove Song")
        up_btn = ctk.CTkButton(controls, text="\u2191", width=34, height=34, command=lambda: move_selected_files(-1))
        up_btn.pack(side="left", padx=(6, 0))
        Tooltip(up_btn, "Move Up")
        down_btn = ctk.CTkButton(controls, text="\u2193", width=34, height=34, command=lambda: move_selected_files(1))
        down_btn.pack(side="left", padx=(6, 0))
        Tooltip(down_btn, "Move Down")
        clear_btn = ctk.CTkButton(controls, text="Clear", width=68, height=34, command=clear_files)
        clear_btn.pack(side="left", padx=(6, 0))
        Tooltip(clear_btn, "Clear List")
        action_slot = ctk.CTkFrame(controls, fg_color="transparent")
        action_slot.pack(side="right")

        build_msg_var = tk.StringVar(value="")
        progress_wrap = ctk.CTkFrame(frame, fg_color="transparent")
        progress_label = ctk.CTkLabel(progress_wrap, textvariable=build_msg_var, anchor="w")
        progress_label.pack(fill="x")
        progress = ctk.CTkProgressBar(progress_wrap, mode="determinate")
        progress.pack(fill="x", pady=(2, 0))
        progress.set(0.0)
        progress_wrap.pack(fill="x", padx=8, pady=(0, 6))
        progress_wrap.pack_forget()

        def stop_popup_preview() -> None:
            nonlocal popup_preview_proc
            if popup_preview_proc is None:
                return
            self._stop_audio_handle(popup_preview_proc, timeout=0.4)
            try:
                if popup_preview_proc in self._aux_preview_procs:
                    self._aux_preview_procs.remove(popup_preview_proc)
            except Exception:
                pass
            popup_preview_proc = None

        def on_popup_tree_click(event) -> str | None:
            nonlocal popup_preview_proc
            row_id = files_tree.identify_row(event.y)
            col_id = files_tree.identify_column(event.x)
            if not row_id or col_id != "#2":
                return None
            idx = int(row_id) - 1
            if idx < 0 or idx >= len(song_files):
                return "break"
            if self.preview_backend == "ffplay" and self.preview_ffplay is None:
                build_msg_var.set(self._preview_unavailable_message())
                return "break"
            src = song_files[idx]
            stop_popup_preview()
            try:
                popup_preview_proc = self._start_audio_preview(src)
                if popup_preview_proc is None:
                    build_msg_var.set(self._preview_unavailable_message())
                    return "break"
                self._aux_preview_procs.append(popup_preview_proc)
            except Exception:
                popup_preview_proc = None
            return "break"

        def on_popup_tree_release(_event=None) -> None:
            stop_popup_preview()

        def on_popup_tree_motion(event) -> None:
            region = files_tree.identify("region", event.x, event.y)
            row_id = files_tree.identify_row(event.y)
            col_id = files_tree.identify_column(event.x)
            if region == "cell" and row_id and col_id == "#2":
                files_tree.configure(cursor="hand2")
                self._show_hover_tip(event.x_root, event.y_root, "Click and Hold to play")
            else:
                files_tree.configure(cursor="")
                self._hide_hover_tip()

        def on_popup_tree_leave(_event=None) -> None:
            files_tree.configure(cursor="")
            self._hide_hover_tip()

        def on_popup_tree_ctrl_key(event):
            if self._is_key(event, KEYCODE_A, "a"):
                files_tree.selection_set(files_tree.get_children())
                return "break"
            return None

        files_tree.bind("<Button-1>", on_popup_tree_click)
        files_tree.bind("<ButtonRelease-1>", on_popup_tree_release)
        files_tree.bind("<Delete>", lambda _e=None: (remove_selected_files(), "break")[1])
        files_tree.bind("<Control-KeyPress>", on_popup_tree_ctrl_key, add="+")
        files_tree.bind("<Motion>", on_popup_tree_motion)
        files_tree.bind("<Leave>", on_popup_tree_leave)

        def stop_pulse() -> None:
            pulse["active"] = False
            job = pulse_after_id["id"]
            pulse_after_id["id"] = None
            if job is None:
                return
            try:
                self.after_cancel(job)
            except Exception:
                pass

        def on_cancel() -> None:
            stop_pulse()
            stop_popup_preview()
            self._hide_hover_tip()
            save_last_mix_state()
            try:
                progress_wrap.pack_forget()
            except Exception:
                pass
            popup.destroy()

        popup.protocol("WM_DELETE_WINDOW", on_cancel)
        popup.bind("<Destroy>", lambda _e=None: stop_pulse(), add="+")

        def on_ok() -> None:
            name = song_name_var.get().strip()
            if not name:
                messagebox.showerror("Mix Builder", "Song Name is required.", parent=popup)
                return
            if not song_files:
                messagebox.showerror("Mix Builder", "Add at least one source file.", parent=popup)
                return
            if build_in_progress["value"]:
                return
            src_root = audio_cache_root(self.audio_dir_active.resolve())
            src_root.mkdir(parents=True, exist_ok=True)
            out_stem = _safe_song_stem(name)
            out_path = src_root / f"{out_stem}.ogg"
            overwrite_existing = False
            if out_path.exists():
                overwrite_existing = bool(
                    messagebox.askyesno(
                        "Overwrite Existing Mix",
                        f"A mix named '{out_path.name}' already exists.\n\nOverwrite it?",
                        parent=popup,
                    )
                )
                if not overwrite_existing:
                    return
            build_in_progress["value"] = True
            build_msg_var.set("Building (please wait)...")
            progress.configure(mode="determinate")
            progress.set(0.02)
            if not progress_wrap.winfo_ismapped():
                progress_wrap.pack(fill="x", padx=8, pady=(0, 6))
            btn_ok.configure(state="disabled")
            btn_cancel.configure(state="disabled")

            pulse["active"] = True
            phase["compiling"] = False

            def pulse_progress():
                if not pulse["active"]:
                    return
                try:
                    if not popup.winfo_exists():
                        stop_pulse()
                        return
                    cur = float(progress.get())
                    if cur < 0.88:
                        nxt = min(0.88, cur + 0.025)
                    elif cur < 0.96:
                        if not phase["compiling"]:
                            build_msg_var.set("Compiling mix (please wait)...")
                            phase["compiling"] = True
                        nxt = min(0.96, cur + 0.006)
                    elif cur < 0.985:
                        nxt = min(0.985, cur + 0.0015)
                    else:
                        nxt = cur
                    progress.set(nxt)
                    pulse_after_id["id"] = self.after(160, pulse_progress)
                except Exception:
                    stop_pulse()

            pulse_progress()

            def worker():
                try:
                    out_file = create_song_from_sources(
                        name,
                        song_files,
                        self.audio_dir_active,
                        overwrite_existing=overwrite_existing,
                    )
                    def done_ok():
                        stop_pulse()
                        progress.set(1.0)
                        self.excluded_oggs.discard(out_file.name)
                        self.refresh_songs()
                        self._move_song_to_bottom(out_file.name)
                        self.status_var.set(f"Created song: {out_file.name}")
                        build_in_progress["value"] = False
                        stop_popup_preview()
                        save_last_mix_state()
                        popup.destroy()
                    self.after(0, done_ok)
                except BaseException as e:
                    exc_type, exc_value, exc_traceback = sys.exc_info()
                    err_detail = str(e).strip() or e.__class__.__name__
                    log_path = _write_crash_log(
                        exc_type or type(e),
                        exc_value or e,
                        exc_traceback,
                        context="Create Mix worker exception",
                    )
                    def done_err():
                        stop_pulse()
                        progress.set(0.0)
                        build_msg_var.set("")
                        try:
                            progress_wrap.pack_forget()
                        except Exception:
                            pass
                        build_in_progress["value"] = False
                        btn_ok.configure(state="normal")
                        btn_cancel.configure(state="normal")
                        messagebox.showerror(
                            "Mix Builder",
                            f"{err_detail}\n\nCrash log: {log_path}",
                            parent=popup,
                        )
                    self.after(0, done_err)

            threading.Thread(target=worker, daemon=True).start()

        btn_cancel = ctk.CTkButton(action_slot, text="Cancel", width=94, height=34, command=on_cancel)
        btn_cancel.pack(side="right")
        btn_ok = ctk.CTkButton(action_slot, text="Create", width=94, height=34, command=on_ok)
        btn_ok.pack(side="right", padx=(0, 8))
        load_last_mix_state()

    def _pick_active_audio_dir(self) -> Path:
        target = self.audio_dir_override or self.audio_dir_active
        target.mkdir(parents=True, exist_ok=True)
        self.audio_dir_active = target
        self.audio_status_var.set(f"/{target.name}")
        self._refresh_audio_source_button_text()
        return target

    def pick_audio_source(self) -> None:
        initial = self.audio_dir_active if self.audio_dir_active.exists() else Path.home()
        selected = filedialog.askdirectory(title="Select .ogg output folder", initialdir=str(initial), parent=self)
        if selected:
            self.audio_dir_override = Path(selected)
            self.audio_dir_active = self.audio_dir_override
            self.audio_status_var.set(f"/{self.audio_dir_override.name}")
            self._refresh_audio_source_button_text()
            self.refresh_songs()

    def _refresh_audio_source_button_text(self) -> None:
        if hasattr(self, "audio_source_button"):
            self.audio_source_button.configure(text=self.audio_status_var.get())

    def pick_poster(self) -> None:
        selected = filedialog.askopenfilename(
            title="Select workshop poster",
            initialdir=str(self._image_file_dialog_initial_dir()),
            filetypes=[("Images", "*.png;*.jpg;*.jpeg;*.webp;*.bmp")],
            parent=self,
        )
        if selected:
            self.poster_path = Path(selected)
            self.last_image_pick_dir = self.poster_path.resolve().parent
            self.poster_var.set(self.poster_path.name)
            self._update_top_poster_preview(self.poster_path)

    def on_poster_preview_right_click(self, event) -> str:
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Clear", command=self.clear_workshop_poster)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def clear_workshop_poster(self) -> None:
        if self.default_poster_path.exists():
            self.poster_path = self.default_poster_path
            self.poster_var.set(self.default_poster_path.name)
            self.status_var.set("Workshop poster reset to default")
        else:
            self.poster_path = None
            self.poster_var.set("Select poster")
            self.status_var.set("Workshop poster cleared")
        self._update_top_poster_preview(self.poster_path)

    def _update_top_poster_preview(self, poster: Path | None) -> None:
        if poster is None or not poster.exists():
            self.poster_top_preview.configure(text="Poster", image=None)
            self.poster_thumb_top = None
            return
        try:
            mod_name = self.name_var.get().strip() or self.mod_id_var.get().strip() or "Untitled"
            im = render_workshop_square_image(
                source=poster,
                out_size=128,
                mod_name=mod_name,
                add_name_overlay=bool(self.poster_add_name_var.get()),
            ).convert("RGB")
            im.thumbnail((76, 76), Image.LANCZOS)
            photo = ctk.CTkImage(light_image=im, dark_image=im, size=im.size)
            self.poster_thumb_top = photo
            self.poster_top_preview.configure(text="", image=photo)
        except Exception:
            self.poster_top_preview.configure(text="Poster", image=None)
            self.poster_thumb_top = None

    def manual_refresh_songs(self) -> None:
        self.refresh_songs()

    def refresh_songs(self) -> None:
        self.audio_dir_active = self._pick_active_audio_dir()
        rows = refresh_song_catalog(self.audio_dir_active)
        row_map = {r.ogg.name: r for r in rows}
        _, cache_root = ensure_audio_workspace(self.audio_dir_active.resolve())
        for key, cfg in self.track_settings.items():
            source_raw = cfg.get("source_path")
            if not source_raw or key in row_map:
                continue
            src = Path(source_raw)
            if not src.exists() or not src.is_file():
                continue
            ogg = src if src.suffix.lower() == ".ogg" else (cache_root / key)
            status, detail = self._song_status_for_paths(src, ogg)
            row_map[key] = AudioTrackEntry(source=src, ogg=ogg, status=status, detail=detail)
        merged_rows = list(row_map.values())
        all_keys = {r.ogg.name for r in merged_rows}
        if self.excluded_oggs:
            self.excluded_oggs = {k for k in self.excluded_oggs if k in all_keys}

        visible_rows = [
            {"source": r.source, "ogg": r.ogg, "status": r.status, "detail": r.detail}
            for r in merged_rows
            if r.ogg.name not in self.excluded_oggs
        ]

        self.track_rows = visible_rows

        for row in self.track_rows:
            key = row["ogg"].name
            if key not in self.track_settings:
                self.track_settings[key] = {
                    "cassette": True,
                    "vinyl": True,
                    "cover": None,
                    "b_side": None,
                    "display_name": None,
                    "vinyl_art_placement": self.global_vinyl_mask_var.get(),
                    "source_path": str(row["source"]),
                    "cached_ogg_path": str(row["ogg"]),
                }
            else:
                cfg = self.track_settings[key]
                cfg["source_path"] = str(row["source"])
                cfg["cached_ogg_path"] = str(row["ogg"])
                cover = cfg.get("cover")
                if cover and not Path(cover).exists():
                    cfg["cover"] = None
                b_side = cfg.get("b_side")
                if b_side and not Path(b_side).exists():
                    cfg["b_side"] = None

        self._redraw_tree()
        self.status_var.set(f"Loaded {len(self.track_rows)} songs")

    def _fuzzy_match(self, text: str, query: str) -> bool:
        t = (text or "").lower()
        q = (query or "").strip().lower()
        if not q:
            return True
        if q in t:
            return True
        pos = 0
        for ch in q:
            pos = t.find(ch, pos)
            if pos < 0:
                return False
            pos += 1
        return True

    def _visible_rows(self) -> list[dict]:
        q = self.filter_var.get()
        if not q.strip():
            return list(self.track_rows)
        visible: list[dict] = []
        for row in self.track_rows:
            cfg = self.track_settings.get(row["ogg"].name, {})
            song = str(cfg.get("display_name") or row["source"].name)
            if self._fuzzy_match(song, q):
                visible.append(row)
        return visible

    def on_filter_change(self, _event=None) -> None:
        self._redraw_tree()

    def _selected_keys(self, fallback_row_id: str | None = None) -> list[str]:
        selected = list(self.tree.selection())
        if fallback_row_id and fallback_row_id not in selected:
            selected = [fallback_row_id]
        return selected

    def _redraw_tree(self) -> None:
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        seen_iids: set[str] = set()
        for i, row in enumerate(self._visible_rows()):
            key = row["ogg"].name
            if key in seen_iids:
                continue
            seen_iids.add(key)
            cfg = self.track_settings.get(key, {})
            song_label = str(cfg.get("display_name") or (row["ogg"].name if row["ogg"].exists() else row["source"].name))
            cover = cfg.get("cover")
            if cover:
                cover_path = Path(cover)
                if self.poster_path and cover_path == self.poster_path:
                    cover_text = "(poster override)"
                else:
                    cover_text = cover_path.name
            else:
                cover_text = "(mod default)"
            b_side = cfg.get("b_side")
            if b_side:
                b_side_text = Path(b_side).name
            else:
                b_side_text = ""
            tag = "even" if i % 2 == 0 else "odd"
            self.tree.insert(
                "",
                "end",
                iid=key,
                values=(
                    row["ogg"].name if row["ogg"].exists() else row["source"].name,
                    # source column is editable display label, not source-file rename
                    "\U0001F50A",
                    row["status"],
                    "\u2713" if cfg.get("cassette") else "",
                    "\u2713" if cfg.get("vinyl") else "",
                    b_side_text,
                    cover_text,
                ),
                tags=(tag,),
            )
            self.tree.set(key, "source", song_label)
        self._refresh_bulk_switches()

    def on_tree_select(self, _event=None) -> None:
        selected = self.tree.selection()
        if selected:
            self.selected_ogg_name = selected[0]
        self._update_selection_status_hint()

    def on_tree_click(self, event) -> str | None:
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return None

        row_id = self.tree.identify_row(event.y)
        col_id = self.tree.identify_column(event.x)
        if not row_id:
            return None

        actions = {"#2": "preview", "#4": "cassette", "#5": "vinyl", "#6": "bside", "#7": "cover"}
        action = actions.get(col_id)
        if not action:
            return None

        if action == "preview":
            self.tree.selection_set(row_id)
            self.start_preview_for_row(row_id)
            return "break"

        selected_rows = self._selected_keys(row_id)
        self.tree.selection_set(selected_rows)
        cfg = self.track_settings.setdefault(row_id, {})

        if action in ("cassette", "vinyl"):
            new_value = not bool(cfg.get(action, True))
            for key in selected_rows:
                rcfg = self.track_settings.setdefault(key, {})
                rcfg[action] = new_value
                if action == "vinyl" and not new_value:
                    rcfg["vinyl_art_placement"] = self.global_vinyl_mask_var.get()
            self._redraw_tree()
            self._update_selection_status_hint()
            return "break"

        if action == "bside":
            selected = filedialog.askopenfilename(
                title="Select B-Side audio",
                initialdir=str(self._audio_file_dialog_initial_dir()),
                filetypes=[("Audio", "*.ogg *.mp3 *.wav *.flac *.m4a *.aac *.wma"), ("All files", "*.*")],
                parent=self,
            )
            if selected:
                self.last_song_pick_dir = Path(selected).resolve().parent
                for key in selected_rows:
                    self.track_settings.setdefault(key, {})["b_side"] = str(Path(selected))
                self._redraw_tree()
                self._update_selection_status_hint()
            return "break"

        if action == "cover":
            selected = filedialog.askopenfilename(
                title="Select song cover",
                initialdir=str(self._image_file_dialog_initial_dir()),
                filetypes=[("Images", "*.png;*.jpg;*.jpeg;*.webp;*.bmp")],
                parent=self,
            )
            if selected:
                self.last_image_pick_dir = Path(selected).resolve().parent
                for key in selected_rows:
                    self.track_settings.setdefault(key, {})["cover"] = str(Path(selected))
                self._redraw_tree()
                self._update_selection_status_hint()
            return "break"

        return None

    def on_tree_double_click(self, event) -> str | None:
        region = self.tree.identify("region", event.x, event.y)
        if region != "cell":
            return None
        row_id = self.tree.identify_row(event.y)
        col_id = self.tree.identify_column(event.x)
        if not row_id or col_id != "#1":
            return None
        self.begin_inline_song_rename(row_id)
        return "break"

    def begin_inline_song_rename(self, row_id: str) -> None:
        if self.inline_editor is not None:
            try:
                self.inline_editor.destroy()
            except Exception:
                pass
            self.inline_editor = None
        current_row = next((r for r in self.track_rows if r["ogg"].name == row_id), None)
        if not current_row:
            return
        cfg = self.track_settings.get(row_id, {})
        current_name = str(cfg.get("display_name") or current_row["source"].stem)
        bbox = self.tree.bbox(row_id, "#1")
        if not bbox:
            return
        x, y, w, h = bbox
        editor = tk.Entry(self.tree)
        editor.insert(0, current_name)
        editor.place(x=x, y=y, width=w, height=h)
        editor.focus_set()
        editor.select_range(0, "end")
        self.inline_editor = editor

        def _cancel(_event=None):
            if self.inline_editor is not None:
                try:
                    self.inline_editor.destroy()
                except Exception:
                    pass
                self.inline_editor = None
            return "break"

        def _commit_impl(editor_ref: tk.Entry, new_name: str):
            if self.inline_editor is not editor_ref:
                return "break"
            _cancel()
            if not new_name or new_name == current_name:
                return "break"
            try:
                cfg = self.track_settings.setdefault(row_id, {})
                cfg["display_name"] = new_name
                self._redraw_tree()
                if row_id in self.tree.get_children():
                    self.tree.selection_set(row_id)
                self.status_var.set(f"Updated display name: {new_name}")
            except Exception as e:
                messagebox.showerror("Rename failed", str(e))
            return "break"

        def _commit(_event=None):
            if self.inline_editor is None:
                return "break"
            editor_ref = self.inline_editor
            try:
                new_name = editor_ref.get().strip()
            except Exception:
                return "break"
            return _commit_impl(editor_ref, new_name)

        def _commit_on_focus_out(_event=None):
            # Defer commit until after Tk finishes the focus transition. This avoids
            # tearing down/rebuilding widgets inside the FocusOut callback (IME/Tk crash risk).
            if self.inline_editor is None:
                return "break"
            editor_ref = self.inline_editor
            try:
                pending_name = editor_ref.get().strip()
            except Exception:
                return "break"

            def _deferred():
                try:
                    _commit_impl(editor_ref, pending_name)
                except Exception:
                    pass

            self.after(1, _deferred)
            return "break"

        editor.bind("<Return>", _commit)
        editor.bind("<Escape>", _cancel)
        editor.bind("<FocusOut>", _commit_on_focus_out)

    def _start_audio_preview(self, audio_path: Path) -> object | None:
        if self.preview_backend == "miniaudio" and miniaudio is not None:
            return _MiniAudioPreviewHandle(audio_path)
        if self.preview_ffplay is None:
            return None
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.Popen(
            [self.preview_ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", str(audio_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )

    def _stop_audio_handle(self, handle: object | None, timeout: float = 0.4) -> None:
        if handle is None:
            return
        try:
            getattr(handle, "terminate")()
        except Exception:
            pass
        try:
            getattr(handle, "wait")(timeout=timeout)
        except Exception:
            try:
                getattr(handle, "kill")()
            except Exception:
                pass

    def _preview_unavailable_message(self) -> str:
        has_miniaudio = miniaudio is not None
        has_ffplay = self.preview_ffplay is not None
        if not has_miniaudio and not has_ffplay:
            return "Preview unavailable: miniaudio not bundled and ffplay not found"
        if self.preview_backend == "ffplay" and not has_ffplay:
            return "Preview unavailable: ffplay not found"
        if self.preview_backend == "miniaudio" and not has_miniaudio:
            return "Preview unavailable: miniaudio not bundled"
        if not has_miniaudio:
            return "Preview unavailable: miniaudio not bundled"
        if not has_ffplay:
            return "Preview unavailable: ffplay not found"
        return "Preview unavailable: no audio backend"

    def _active_preview_backend_name(self) -> str:
        if self.preview_backend == "miniaudio" and miniaudio is not None:
            return "miniaudio"
        if self.preview_backend == "ffplay":
            return "ffplay"
        return "none"

    def start_preview_for_row(self, row_id: str) -> None:
        if not row_id:
            return
        backend = self._active_preview_backend_name()
        if backend == "ffplay" and self.preview_ffplay is None:
            self.status_var.set(self._preview_unavailable_message())
            return
        row = next((r for r in self.track_rows if r["ogg"].name == row_id), None)
        if not row:
            return
        audio_path = row["ogg"] if row["ogg"].exists() else row["source"]
        if not audio_path.exists():
            self.status_var.set("Preview unavailable: file missing")
            return
        self.stop_preview()
        try:
            self.preview_proc = self._start_audio_preview(audio_path)
            if self.preview_proc is None:
                self.status_var.set(self._preview_unavailable_message())
                return
            if backend == "miniaudio" and self.preview_ffplay is None:
                self.status_var.set(f"Previewing: {row['source'].name}")
            else:
                self.status_var.set(f"Previewing ({backend}): {row['source'].name}")
        except Exception as e:
            self.preview_proc = None
            self.status_var.set(f"Preview failed: {e}")

    def stop_preview(self) -> None:
        self._stop_audio_handle(self.preview_proc, timeout=0.4)
        self.preview_proc = None

    def on_tree_release(self, _event=None) -> None:
        self.stop_preview()
        self._update_selection_status_hint()

    def on_tree_motion(self, event) -> None:
        region = self.tree.identify("region", event.x, event.y)
        row_id = self.tree.identify_row(event.y)
        if region == "cell" and row_id and self.tree.identify_column(event.x) == "#2":
            self.tree.configure(cursor="hand2")
            self._show_hover_tip(event.x_root, event.y_root, "Click and Hold to play")
        else:
            self.tree.configure(cursor="")
            self._hide_hover_tip()

    def on_tree_leave(self, _event=None) -> None:
        self.tree.configure(cursor="")
        self._hide_hover_tip()

    def _show_hover_tip(self, x_root: int, y_root: int, text: str) -> None:
        if self._hover_tip_window is None:
            tw = tk.Toplevel(self)
            tw.wm_overrideredirect(True)
            label = tk.Label(
                tw,
                text=text,
                justify=tk.LEFT,
                background="#1f242c",
                foreground="#f0f4ff",
                relief=tk.SOLID,
                borderwidth=1,
                padx=6,
                pady=4,
            )
            label.pack()
            self._hover_tip_window = tw
        self._hover_tip_window.wm_geometry(f"+{x_root + 12}+{y_root + 18}")

    def _hide_hover_tip(self) -> None:
        if self._hover_tip_window is not None:
            try:
                self._hover_tip_window.destroy()
            except Exception:
                pass
            self._hover_tip_window = None

    def report_callback_exception(self, exc, val, tb):
        log_path = _write_crash_log(exc, val, tb, context="Tk callback exception")
        try:
            messagebox.showerror(
                "Unexpected Error",
                f"An unexpected error occurred.\n\nA crash log was written to:\n{log_path}",
                parent=self,
            )
        except Exception:
            pass

    def _apply_window_icon(self, window: tk.Misc) -> None:
        if getattr(window, "_smb_icon_applied", False):
            return
        base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        icon_ico = base_dir / "icon.ico"
        if icon_ico.exists():
            try:
                window.iconbitmap(default=str(icon_ico))
                setattr(window, "_smb_icon_applied", True)
            except Exception:
                pass
        # Apply iconphoto only once to the main window for sharper taskbar rendering.
        # Avoiding repeated/popup iconphoto calls prevents native Tk instability.
        if window is self and not self._window_icon_images and icon_ico.exists():
            try:
                ico_img = Image.open(icon_ico).convert("RGBA")
                for size in (256, 128, 64, 48, 40, 32, 24, 20, 16):
                    im = ico_img.resize((size, size), Image.LANCZOS)
                    self._window_icon_images.append(ImageTk.PhotoImage(im))
                if self._window_icon_images:
                    self._window_icon_image = self._window_icon_images[0]
                    self.iconphoto(True, *self._window_icon_images)
            except Exception:
                pass

    def _guard_popup_default_ctk_icon_reset(self, popup: tk.Misc) -> None:
        if sys.platform.startswith("win") is False:
            return
        try:
            original_iconbitmap = popup.iconbitmap
        except Exception:
            return

        def guarded_iconbitmap(bitmap=None, default=None):
            blob = f"{bitmap}|{default}"
            if "CustomTkinter_icon_Windows.ico" in blob:
                return None
            return original_iconbitmap(bitmap, default)

        try:
            popup.iconbitmap = guarded_iconbitmap  # type: ignore[attr-defined]
        except Exception:
            pass

    def on_tree_right_click(self, event) -> str | None:
        row_id = self.tree.identify_row(event.y)
        if row_id:
            if row_id not in self.tree.selection():
                self.tree.selection_set(row_id)
        selected = self._selected_keys(row_id)
        if not selected:
            return "break"
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Rename", command=lambda: self.begin_inline_song_rename(selected[0]))
        menu.add_separator()
        menu.add_command(label="Open File Location", command=lambda: self.open_selected_file_location(row_id))
        menu.add_command(label="Convert", command=self.convert_selected_songs)
        menu.add_command(label="Clear Poster", command=self.clear_selected_posters)
        menu.add_command(label="Clear B-Side", command=self.clear_selected_b_sides)
        menu.add_separator()
        menu.add_command(label="Remove", command=self.remove_selected_songs)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def on_delete_key(self, _event=None) -> str:
        self.remove_selected_songs()
        return "break"

    def on_tree_select_all(self, _event=None) -> str:
        self.tree.selection_set(self.tree.get_children())
        self._update_selection_status_hint()
        return "break"

    def apply_global_vinyl_mask(self, value: str | None = None) -> None:
        mask = (value or self.global_vinyl_mask_var.get() or "inside").strip().lower()
        if mask not in ("inside", "outside"):
            mask = "inside"
        self.global_vinyl_mask_var.set(mask)
        self._refresh_global_vinyl_mask_button()
        for cfg in self.track_settings.values():
            cfg["vinyl_art_placement"] = mask
        self._redraw_tree()

    def toggle_global_vinyl_mask(self) -> None:
        cur = (self.global_vinyl_mask_var.get() or "inside").strip().lower()
        nxt = "outside" if cur == "inside" else "inside"
        self.apply_global_vinyl_mask(nxt)

    def _refresh_global_vinyl_mask_button(self) -> None:
        if not hasattr(self, "global_vinyl_mask_button"):
            return
        cur = (self.global_vinyl_mask_var.get() or "inside").strip().lower()
        label = "\u25c9 Outer" if cur == "outside" else "\u25cf Inner"
        self.global_vinyl_mask_button.configure(text=label)

    def sort_by_column(self, col: str) -> None:
        asc = not self.sort_state.get(col, True)
        self.sort_state[col] = asc

        def k(row: dict):
            key = row["ogg"].name
            cfg = self.track_settings.get(key, {})
            if col == "source":
                return row["source"].name.lower()
            if col == "status":
                return row["status"].lower()
            if col == "cassette":
                return 1 if cfg.get("cassette") else 0
            if col == "vinyl":
                return 1 if cfg.get("vinyl") else 0
            if col == "bside":
                return 1 if cfg.get("b_side") else 0
            if col == "cover":
                cover = cfg.get("cover")
                return Path(cover).name.lower() if cover else ""
            return row["source"].name.lower()

        self.track_rows.sort(key=k, reverse=not asc)
        self._redraw_tree()

    def bulk_set(self, key: str, value: bool) -> None:
        for name in self.track_settings:
            self.track_settings[name][key] = value
            if key == "vinyl" and not value:
                self.track_settings[name]["vinyl_art_placement"] = self.global_vinyl_mask_var.get()
        self._redraw_tree()

    def _refresh_bulk_switches(self) -> None:
        if not self.track_settings:
            return
        self.bulk_cassette_var.set(all(bool(cfg.get("cassette", False)) for cfg in self.track_settings.values()))
        self.bulk_vinyl_var.set(all(bool(cfg.get("vinyl", False)) for cfg in self.track_settings.values()))

    def apply_poster_to_all(self) -> None:
        if self.poster_path is None:
            self.status_var.set("Select workshop poster first to apply poster override")
            return
        for name in self.track_settings:
            self.track_settings[name]["cover"] = str(self.poster_path)
        self._redraw_tree()
        self.status_var.set("All songs set to poster cover")

    def apply_default_to_all(self) -> None:
        for name in self.track_settings:
            self.track_settings[name]["cover"] = None
        self._redraw_tree()
        self.status_var.set("All songs set to mod default cover")

    def convert_audio(self) -> bool:
        self.audio_dir_active = self._pick_active_audio_dir()
        all_rows = list(self.track_rows)
        total_sources = len(all_rows)

        if total_sources <= 0:
            self.status_var.set("No songs found to convert")
            return True

        self.preview_title_label.configure(text="Converting Songs (Please Wait)...")
        self.build_progress_var.set(0.0)
        self.status_var.set("Converting songs...")
        self.update_idletasks()

        processed = 0
        seen_sources: set[str] = set()

        def _on_progress(entry) -> None:
            nonlocal processed
            processed += 1
            src_resolved = str(entry.source.resolve()) if entry and entry.source else ""
            for row in self.track_rows:
                try:
                    if str(row["source"].resolve()) == src_resolved:
                        row["ogg"] = entry.ogg
                        row["status"] = entry.status
                        row["detail"] = entry.detail
                except Exception:
                    pass
            self._redraw_tree()
            self.build_progress_var.set(max(0.0, min(1.0, processed / total_sources)))
            self.status_var.set(f"Converting songs... ({processed}/{total_sources})")
            self.update_idletasks()

        try:
            for row in all_rows:
                src = row["source"]
                src_key = str(src.resolve())
                if src_key in seen_sources:
                    continue
                seen_sources.add(src_key)
                entry = convert_single_audio_file(src, self.audio_dir_active, force=False)
                _on_progress(entry)
        except SystemExit as e:
            messagebox.showerror("Audio Conversion Error", str(e))
            self.status_var.set("Conversion failed")
            return False
        except Exception as e:
            messagebox.showerror("Audio Conversion Error", str(e))
            self.status_var.set("Conversion failed")
            return False
        finally:
            self.preview_title_label.configure(text="Build Preview")

        self.refresh_songs()
        self.build_progress_var.set(1.0)
        self.status_var.set("Converting Complete")
        return True

    def _preflight(self) -> list[str]:
        errors: list[str] = []
        if not self.mod_id_var.get().strip():
            errors.append("Mod ID is required.")
        any_selected = any(cfg.get("cassette") or cfg.get("vinyl") for cfg in self.track_settings.values())
        if not any_selected:
            errors.append("No songs selected for cassette or vinyl.")
        for cfg in self.track_settings.values():
            cover = cfg.get("cover")
            if cover and not Path(cover).exists():
                errors.append(f"Missing cover file: {cover}")
        return errors

    def _compute_generation_counts(self) -> tuple[int, int]:
        present_keys = {row["ogg"].name for row in self.track_rows}
        cassette_count = 0
        vinyl_count = 0
        for key, cfg in self.track_settings.items():
            if key not in present_keys:
                continue
            if bool(cfg.get("cassette")):
                cassette_count += 1
            if bool(cfg.get("vinyl")):
                vinyl_count += 1
        return cassette_count, vinyl_count

    def _build_config(self) -> dict:
        mod_id = self.mod_id_var.get().strip()
        parent_mod_id = self.parent_mod_var.get().strip()
        name = self.name_var.get().strip() or mod_id
        author = self.author_var.get().strip()
        workshop_cover = self.poster_path or (self.assets_root / "poster" / "poster.png")
        global_mask = (self.global_vinyl_mask_var.get() or "inside").strip().lower()

        track_modes: dict[str, dict] = {}
        row_by_key = {row["ogg"].name: row for row in self.track_rows}
        for ogg_name, cfg in self.track_settings.items():
            row = row_by_key.get(ogg_name)
            if row is None:
                continue
            row_cfg = dict(cfg)
            if not row_cfg.get("cover"):
                row_cfg["cover"] = None
            b_side = row_cfg.get("b_side")
            if not b_side or not Path(b_side).exists():
                row_cfg["b_side"] = None
            row_cfg["vinyl_art_placement"] = global_mask
            row_cfg["source_path"] = str(row["source"])
            row_cfg["cached_ogg_path"] = str(row["ogg"])
            track_modes[ogg_name] = row_cfg

        return {
            "mod_id": mod_id,
            "name": name,
            "author": author,
            "audio_dir": self.audio_dir_active,
            "out_dir": self.out_dir,
            "assets_root": self.assets_root,
            "workshop_cover": workshop_cover,
            "add_name_to_poster": bool(self.poster_add_name_var.get()),
            "parent_mod_id": parent_mod_id,
            "standalone_bundle": not bool(parent_mod_id),
            "cover": None,
            "track_modes": track_modes,
        }

    def _pick_default_preview_asset(self, mode: str, song_title: str) -> Path | None:
        tex = self.assets_root / "textures" / "WorldItems"
        candidates: list[Path] = []
        mode_l = mode.lower()

        if mode_l == "vinyl":
            vinyl_dir = tex / "Vinyl"
            candidates = sorted([p for p in vinyl_dir.glob("TCVinylrecord*.png") if p.is_file()])
            if not candidates:
                candidates = sorted([p for p in vinyl_dir.glob("TMVinylrecord*.png") if p.is_file()])
        elif mode_l == "cassette":
            cas_dir = tex / "Cassette"
            candidates = sorted(
                [
                    p
                    for p in cas_dir.glob("TCTape*.png")
                    if p.is_file() and p.name.lower() != "tctape_uv.png"
                ]
            )

        if not candidates:
            return None

        return random.choice(candidates)

    def _add_preview_tile(self, event: BuildTrackEvent) -> None:
        if str(self.progress_bar.cget("mode")) != "determinate":
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate")
        if event.total > 0:
            self.build_progress_var.set(max(0.0, min(1.0, event.index / event.total)))
            self.status_var.set(f"Building {event.index}/{event.total}")

        if len(self.preview_images) >= MAX_PREVIEW_TILES:
            if not self.preview_tile_overflow:
                self.preview_tile_overflow = True
                tile = ctk.CTkFrame(self.preview_scroll)
                tile.pack(fill="x", padx=4, pady=4)
                ctk.CTkLabel(
                    tile,
                    text=f"Preview limited to first {MAX_PREVIEW_TILES} songs for stability.",
                    anchor="w",
                ).pack(side="left", fill="x", expand=True, padx=6, pady=6)
            self.update_idletasks()
            return

        tile = ctk.CTkFrame(self.preview_scroll)
        tile.pack(fill="x", padx=4, pady=4)

        img_label = ctk.CTkLabel(tile, text="Mod default", width=90)
        img_label.pack(side="left", padx=6, pady=6)

        raw_title = event.title
        mode = ""
        if raw_title.lower().startswith("[cassette] "):
            mode = "cassette"
        elif raw_title.lower().startswith("[vinyl] "):
            mode = "vinyl"

        thumb = event.thumbnail if event.thumbnail and event.thumbnail.exists() else None
        if thumb is None and mode:
            thumb = self._pick_default_preview_asset(mode, raw_title)

        if thumb and thumb.exists():
            try:
                with Image.open(thumb) as raw_im:
                    im = raw_im.convert("RGBA")
                im.thumbnail((84, 84))
                photo = ctk.CTkImage(light_image=im, dark_image=im, size=im.size)
                self.preview_images.append(photo)
                img_label.configure(text="", image=photo)
            except Exception:
                pass

        title = raw_title
        if title.lower().startswith("[cassette] "):
            title = title[len("[cassette] ") :]
        if title.lower().startswith("[vinyl] "):
            title = title[len("[vinyl] ") :]

        ctk.CTkLabel(tile, text=title, anchor="w").pack(side="left", fill="x", expand=True, padx=4)

        self.update_idletasks()

    def build_pack(self) -> None:
        self.audio_dir_active = self._pick_active_audio_dir()
        errors = self._preflight()
        if errors:
            messagebox.showerror("Preflight failed", "\n".join(errors))
            self.status_var.set("Preflight failed")
            return
        if not self.convert_audio():
            self.status_var.set("Build stopped: conversion failed")
            return

        for child in self.preview_scroll.winfo_children():
            child.destroy()
        self.preview_images.clear()
        self.preview_tile_overflow = False
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start()
        self.build_progress_var.set(0)
        self.status_var.set("Building (please wait)...")

        try:
            out = build_mixed_from_config(self._build_config(), on_track=self._add_preview_tile)
        except SystemExit as e:
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate")
            messagebox.showerror("Build Error", str(e) or "Build stopped")
            self.status_var.set("Build failed")
            return
        except Exception as e:
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate")
            messagebox.showerror("Build Error", str(e))
            self.status_var.set("Build failed")
            return

        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        self.final_output_dir = out
        self.build_progress_var.set(1.0)
        cassette_count, vinyl_count = self._compute_generation_counts()
        self.completion_var.set(f"Cassettes \U0001F4FC - {cassette_count}    Vinyls \U0001F4BF - {vinyl_count}")
        self.status_var.set(f"Build complete: {out}")

    def clear_preview(self) -> None:
        confirm = messagebox.askyesno(
            "Clear Session",
            "Clear current builder state, preview, and saved last-session memory?",
        )
        if not confirm:
            return
        for child in self.preview_scroll.winfo_children():
            child.destroy()
        self.preview_images.clear()
        self.build_progress_var.set(0.0)
        self.final_output_dir = None
        self.last_save_path = None

        self.mod_id_var.set("TM_MyPack")
        self.parent_mod_var.set("TrueMoozic")
        self.name_var.set("My Pack")
        self.author_var.set("")
        self.poster_path = self.default_poster_path
        if not self.poster_path.exists():
            self.poster_path = None
        self.poster_var.set(self.poster_path.name if self.poster_path else "Select poster")
        self._update_top_poster_preview(self.poster_path)
        self.global_vinyl_mask_var.set("inside")
        self._refresh_global_vinyl_mask_button()
        self.audio_dir_override = None
        self.audio_dir_active = default_audio_root()
        self.last_song_pick_dir = Path.home()
        self.last_image_pick_dir = self.cover_root if self.cover_root.exists() else Path.home()
        self.audio_status_var.set(f"/{self.audio_dir_active.name}")
        self._refresh_audio_source_button_text()
        self.track_settings.clear()
        self.excluded_oggs.clear()
        self.filter_var.set("")
        self.completion_var.set("Cassettes \U0001F4FC - 0    Vinyls \U0001F4BF - 0")
        self.refresh_songs()

        state_path = self._state_file()
        if state_path.exists():
            try:
                state_path.unlink()
            except Exception:
                pass
        self.status_var.set("Session cleared")

    def open_output_folder(self) -> None:
        target = self.final_output_dir or self.out_dir
        target.mkdir(parents=True, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(str(target))
        elif sys.platform.startswith("darwin"):
            subprocess.Popen(["open", str(target)])
        else:  # Linux / Unix
            subprocess.Popen(["xdg-open", str(target)])

    def _detect_default_workshop_dir(self) -> Path | None:
        xdg_data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        candidates = [
            Path.home() / "Zomboid" / "Workshop",
            xdg_data / "Zomboid" / "Workshop"
        ]
        for c in candidates:
            if c.exists() and c.is_dir():
                return c
        return None

    def _resolve_workshop_dir(self) -> Path | None:
        if self.workshop_dir_override and self.workshop_dir_override.exists():
            return self.workshop_dir_override
        detected = self._detect_default_workshop_dir()
        if detected is not None and detected.exists():
            return detected

        shown = str(detected) if detected is not None else "<not found>"

        choose = messagebox.askyesno(
            "Workshop Folder Not Found",
            f"Default workshop folder was not found at:\n{shown}\n\nSelect a workshop folder manually?",
            parent=self,
        )
        if not choose:
            return None
        initial_base = detected.parent if (detected is not None and detected.parent.exists()) else Path.home()
        selected = filedialog.askdirectory(
            title="Select Zomboid Workshop Folder",
            initialdir=str(initial_base),
            parent=self,
        )
        if not selected:
            return None
        chosen = Path(selected)
        self.workshop_dir_override = chosen
        return chosen

    def copy_to_workshop(self) -> None:
        source = self.final_output_dir
        if source is None or not source.exists():
            messagebox.showwarning("Copy to Workshop", "Build a pack first so there is output to copy.", parent=self)
            return

        workshop_dir = self._resolve_workshop_dir()
        if workshop_dir is None:
            self.status_var.set("Copy to Workshop canceled")
            return
        workshop_dir.mkdir(parents=True, exist_ok=True)

        target = workshop_dir / source.name
        if target.exists():
            overwrite = messagebox.askyesno(
                "Overwrite Existing Mod",
                f"Workshop destination already exists:\n{target}\n\nOverwrite it?",
                parent=self,
            )
            if not overwrite:
                self.status_var.set("Copy to Workshop canceled")
                return
            try:
                shutil.rmtree(target)
            except Exception as e:
                messagebox.showerror("Copy to Workshop", f"Failed removing existing folder:\n{e}", parent=self)
                return

        try:
            shutil.copytree(source, target)
            self.status_var.set(f"Copied to Workshop: {target}")
            try:
                if sys.platform.startswith("win"):
                    os.startfile(str(target))
                elif sys.platform.startswith("darwin"):
                    subprocess.Popen(["open", str(target)])
                else:
                    subprocess.Popen(["xdg-open", str(target)])
            except Exception:
                pass
        except Exception as e:
            messagebox.showerror("Copy to Workshop", str(e), parent=self)


if __name__ == "__main__":
    _enable_fatal_fault_log()

    def _global_excepthook(exc_type, exc_value, exc_traceback):
        _write_crash_log(exc_type, exc_value, exc_traceback, context="Global exception")
        sys.__excepthook__(exc_type, exc_value, exc_traceback)

    sys.excepthook = _global_excepthook
    if hasattr(threading, "excepthook"):
        def _thread_excepthook(args):
            _write_crash_log(
                args.exc_type,
                args.exc_value,
                args.exc_traceback,
                context=f"Thread exception: {args.thread.name}",
            )
        threading.excepthook = _thread_excepthook

    app = SimpleMoozicBuilderUI()
    app.mainloop()



