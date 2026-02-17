#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import customtkinter as ctk
except Exception as e:  # pragma: no cover
    raise SystemExit("CustomTkinter is required. Install with: pip install customtkinter") from e

from PIL import Image

from simple_moozic_builder import (
    _safe_song_stem,
    BuildTrackEvent,
    audio_source_root,
    app_root,
    bootstrap_runtime_folders,
    build_mixed_from_config,
    convert_audio_library,
    convert_single_audio_file,
    create_song_from_sources,
    default_assets_root,
    default_audio_root,
    default_cover_root,
    default_output_root,
    locate_ffplay,
    render_workshop_square_image,
    refresh_song_catalog,
    rename_song_asset,
    ensure_audio_workspace,
)


STATE_SCHEMA_VERSION = 1
LAST_STATE_FILENAME = ".smb_last_state.json"
RECENT_LIST_FILENAME = ".smb_recent.json"
RECENT_LIMIT = 20


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

        self.assets_root = default_assets_root()
        self.cover_root = default_cover_root()
        self.image_dir_new = default_cover_root()
        self.image_dir_active = self.image_dir_new
        self.image_dir_override: Path | None = None
        self.audio_dir_new = default_audio_root()
        self.audio_dir_legacy = Path(__file__).resolve().parent / "Put your .ogg audio here"
        self.audio_dir_active = self.audio_dir_new
        self.audio_dir_override: Path | None = None
        self.out_dir = default_output_root()
        self.workshop_dir_override: Path | None = None

        self.default_poster_path = self.assets_root / "poster" / "poster.png"
        self.poster_path: Path | None = self.default_poster_path if self.default_poster_path.exists() else None
        self.final_output_dir: Path | None = None
        self.track_rows: list[dict] = []
        self.track_settings: dict[str, dict] = {}
        self.excluded_oggs: set[str] = set()
        self.preview_images: list[ctk.CTkImage] = []
        self.poster_thumb_top: ctk.CTkImage | None = None
        self.recent_projects: list[str] = []
        self.last_save_path: Path | None = None
        self.inline_editor = None
        self.preview_proc: subprocess.Popen | None = None
        self.preview_ffplay = locate_ffplay()
        self._aux_preview_procs: list[subprocess.Popen] = []
        self._hover_tip_window: tk.Toplevel | None = None
        self._window_icon_image: tk.PhotoImage | None = None
        self._window_icon_images: list[tk.PhotoImage] = []

        self.mod_id_var = tk.StringVar(value="TM_MyPack")
        self.parent_mod_var = tk.StringVar(value="TrueMoozicTali")
        self.name_var = tk.StringVar(value="My Pack")
        self.author_var = tk.StringVar(value="")
        self.poster_add_name_var = tk.BooleanVar(value=True)
        self.poster_var = tk.StringVar(value="poster.png" if self.poster_path else "Select poster")
        self.audio_status_var = tk.StringVar(value=f"/{self.audio_dir_new.name}")
        self.image_status_var = tk.StringVar(value=f"/{self.image_dir_new.name}")
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
        self.bind("<Map>", lambda _e: self._apply_window_icon(self), add="+")
        self._update_top_poster_preview(self.poster_path)
        self.name_var.trace_add("write", lambda *_: self._update_top_poster_preview(self.poster_path))
        self._bind_shortcuts()
        self._load_last_session_state()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

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

        ctk.CTkLabel(form_grid, text="Audio Source:", width=96, anchor="w").grid(row=2, column=2, sticky="w", padx=(16, 0))
        self.audio_source_button = ctk.CTkButton(
            form_grid,
            text=self.audio_status_var.get(),
            width=180,
            command=self.pick_audio_source,
        )
        self.audio_source_button.grid(row=2, column=3, sticky="ew")
        ctk.CTkLabel(form_grid, text="Image Source:", width=96, anchor="w").grid(row=3, column=2, sticky="w", padx=(16, 0))
        self.image_source_button = ctk.CTkButton(
            form_grid,
            text=self.image_status_var.get(),
            width=180,
            command=self.pick_image_source,
        )
        self.image_source_button.grid(row=3, column=3, sticky="ew")

        controls = ctk.CTkFrame(left)
        controls.pack(fill="x", padx=8, pady=(0, 8))

        controls_grid = ctk.CTkFrame(controls, fg_color="transparent")
        controls_grid.pack(fill="x", padx=8, pady=(6, 8))
        controls_grid.grid_columnconfigure(0, minsize=150)
        controls_grid.grid_columnconfigure(1, minsize=150)
        controls_grid.grid_columnconfigure(2, minsize=180)
        controls_grid.grid_columnconfigure(3, minsize=180)
        controls_grid.grid_columnconfigure(4, weight=1)
        controls_grid.grid_columnconfigure(5, minsize=120)

        ctk.CTkButton(controls_grid, text="Refresh Songs", width=150, command=self.manual_refresh_songs).grid(row=0, column=0, padx=4, pady=6, sticky="w")
        ctk.CTkButton(controls_grid, text="Convert Audio", width=150, command=self.convert_audio).grid(row=0, column=1, padx=4, pady=6, sticky="w")
        ctk.CTkButton(controls_grid, text="Apply Poster To All", width=180, command=self.apply_poster_to_all).grid(row=0, column=2, padx=4, pady=6, sticky="w")
        ctk.CTkButton(controls_grid, text="Apply Default To All", width=180, command=self.apply_default_to_all).grid(row=0, column=3, padx=4, pady=6, sticky="w")

        ctk.CTkSwitch(
            controls_grid,
            text="All Cassette",
            variable=self.bulk_cassette_var,
            command=lambda: self.bulk_set("cassette", bool(self.bulk_cassette_var.get())),
        ).grid(row=1, column=0, padx=8, pady=6, sticky="w")
        ctk.CTkSwitch(
            controls_grid,
            text="All Vinyl",
            variable=self.bulk_vinyl_var,
            command=lambda: self.bulk_set("vinyl", bool(self.bulk_vinyl_var.get())),
        ).grid(row=1, column=1, padx=8, pady=6, sticky="w")

        mask_cell = ctk.CTkFrame(controls_grid, fg_color="transparent", width=180, height=32)
        mask_cell.grid(row=1, column=2, padx=4, pady=6, sticky="w")
        mask_cell.grid_propagate(False)
        ctk.CTkLabel(mask_cell, text="Vinyl Mask:").pack(side="left", padx=(0, 6))
        self.global_vinyl_mask_button = ctk.CTkButton(
            mask_cell,
            text="Inner",
            width=110,
            command=self.toggle_global_vinyl_mask,
        )
        self.global_vinyl_mask_button.pack(side="right", padx=0)

        ctk.CTkButton(controls_grid, text="Create Mix", width=180, command=self.open_song_builder_popup).grid(row=1, column=3, padx=4, pady=6, sticky="w")
        self._refresh_global_vinyl_mask_button()
        ctk.CTkButton(
            controls_grid,
            text="Build",
            command=self.build_pack,
            font=ctk.CTkFont(weight="bold"),
        ).grid(row=0, column=5, rowspan=2, padx=4, pady=6, sticky="e")

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
            ("cover", "Poster", 180),
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
        self.tree.bind("<Control-a>", self.on_tree_select_all)
        self.tree.bind("<Control-A>", self.on_tree_select_all)
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
        Tooltip(self.add_song_button, "Add Song")
        self.remove_song_button = ctk.CTkButton(song_actions, text="-", width=34, height=34, command=self.remove_selected_songs)
        self.remove_song_button.pack(side="left", padx=(6, 0))
        Tooltip(self.remove_song_button, "Remove Song")

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
        self.file_menu.add_command(label="Save As", command=self.menu_save_as)
        self.file_menu.add_command(label="Load", command=self.menu_load)
        self.recent_menu = tk.Menu(self.file_menu, tearoff=0)
        self.file_menu.add_cascade(label="Recent", menu=self.recent_menu)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="Exit", command=self.on_close)
        self._refresh_recent_menu()
        self.menu_bar.add_cascade(label="File", menu=self.file_menu)
        self.menu_bar.add_command(label="Create Mix", command=self.open_song_builder_popup)
        self.configure(menu=self.menu_bar)

    def _bind_shortcuts(self) -> None:
        self.bind_all("<Control-s>", self._on_ctrl_s)

    def _on_ctrl_s(self, _event=None):
        self.menu_save()
        return "break"

    def _state_file(self) -> Path:
        return app_root() / LAST_STATE_FILENAME

    def _recent_file(self) -> Path:
        return app_root() / RECENT_LIST_FILENAME

    def _project_snapshot(self) -> dict:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "mod_id": self.mod_id_var.get().strip(),
            "parent_mod_id": self.parent_mod_var.get().strip(),
            "name": self.name_var.get().strip(),
            "author": self.author_var.get().strip(),
            "poster_path": str(self.poster_path) if self.poster_path else None,
            "add_name_to_poster": bool(self.poster_add_name_var.get()),
            "audio_source": str(self.audio_dir_active),
            "image_source": str(self.cover_root),
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

        audio_source_raw = data.get("audio_source")
        if audio_source_raw:
            p = Path(audio_source_raw)
            if p.exists():
                self.audio_dir_override = p
                self.audio_dir_active = p
                self.audio_status_var.set(f"/{p.name}")
                self._refresh_audio_source_button_text()

        image_source_raw = data.get("image_source")
        if image_source_raw:
            p = Path(image_source_raw)
            if p.exists():
                self.cover_root = p
                self.image_dir_override = p
                self.image_dir_active = p
                self.image_status_var.set(f"/{p.name}")
                self._refresh_image_source_button_text()

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
            initialdir=str(app_root()),
        )
        if not selected:
            return
        self._write_project_file(Path(selected))

    def menu_load(self) -> None:
        selected = filedialog.askopenfilename(
            title="Load Moozic Builder Project",
            filetypes=[("Moozic Builder Project", "*.smbproj.json"), ("JSON", "*.json")],
            initialdir=str(app_root()),
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

    def _copy_audio_file_to_source(self, src: Path) -> Path:
        source_root = audio_source_root(self.audio_dir_active)
        source_root.mkdir(parents=True, exist_ok=True)
        target = source_root / src.name
        if target.resolve() == src.resolve():
            return target
        n = 2
        while target.exists():
            target = source_root / f"{src.stem} ({n}){src.suffix}"
            n += 1
        import shutil

        shutil.copy2(src, target)
        return target

    def _move_song_to_bottom(self, song_name: str) -> None:
        idx = next((i for i, row in enumerate(self.track_rows) if row["ogg"].name == song_name), None)
        if idx is None:
            return
        row = self.track_rows.pop(idx)
        self.track_rows.append(row)
        self._redraw_tree()

    def add_song_files(self) -> None:
        selected = filedialog.askopenfilenames(
            title="Add Song Files",
            filetypes=[
                ("Audio", "*.ogg;*.mp3;*.wav;*.flac;*.m4a;*.aac;*.wma"),
                ("All files", "*.*"),
            ],
            initialdir=str(self.audio_dir_active if self.audio_dir_active.exists() else audio_source_root(self.audio_dir_new)),
        )
        if not selected:
            return
        added_names: list[str] = []
        for p in selected:
            src = Path(p)
            if not src.exists():
                continue
            dst = self._copy_audio_file_to_source(src)
            ogg_name = dst.name if dst.suffix.lower() == ".ogg" else f"{dst.stem}.ogg"
            self.excluded_oggs.discard(ogg_name)
            added_names.append(ogg_name)
        self.refresh_songs()
        for name in added_names:
            self._move_song_to_bottom(name)
        self.status_var.set(f"Added {len(added_names)} song file(s)")

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
            subprocess.Popen(["explorer.exe", "/select,", target_str])
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
        popup.geometry("760x620")
        popup.transient(self)
        popup.grab_set()
        popup.focus_set()
        self._guard_popup_default_ctk_icon_reset(popup)
        self._apply_window_icon(popup)
        popup.bind("<Map>", lambda _e: self._apply_window_icon(popup), add="+")
        popup.after(0, lambda: self._apply_window_icon(popup))
        popup.after(120, lambda: self._apply_window_icon(popup))
        popup.after(400, lambda: self._apply_window_icon(popup))

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
        popup_preview_proc: subprocess.Popen | None = None
        build_in_progress = {"value": False}

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
                title="Select files to stitch",
                filetypes=[
                    ("Audio", "*.ogg;*.mp3;*.wav;*.flac;*.m4a;*.aac;*.wma"),
                    ("All files", "*.*"),
                ],
                initialdir=str(self.audio_dir_active if self.audio_dir_active.exists() else audio_source_root(self.audio_dir_new)),
            )
            if not selected:
                return
            for p in selected:
                fp = Path(p)
                if fp.exists() and fp.is_file():
                    song_files.append(fp)
            redraw_files()

        def remove_selected_files() -> None:
            selected_idx = [int(x) - 1 for x in files_tree.selection()]
            if not selected_idx:
                return
            for idx in sorted(selected_idx, reverse=True):
                if 0 <= idx < len(song_files):
                    song_files.pop(idx)
            redraw_files()

        add_btn = ctk.CTkButton(controls, text="+", width=34, height=34, command=add_source_files)
        add_btn.pack(side="left")
        Tooltip(add_btn, "Add Song")
        remove_btn = ctk.CTkButton(controls, text="-", width=34, height=34, command=remove_selected_files)
        remove_btn.pack(side="left", padx=(6, 0))
        Tooltip(remove_btn, "Remove Song")

        build_msg_var = tk.StringVar(value="")
        ctk.CTkLabel(frame, textvariable=build_msg_var, anchor="w").pack(fill="x", padx=8, pady=(0, 4))
        progress = ctk.CTkProgressBar(frame, mode="determinate")
        progress.pack(fill="x", padx=8, pady=(0, 12))
        progress.set(0.0)

        bottom = ctk.CTkFrame(frame, fg_color="transparent")
        bottom.pack(fill="x", padx=8, pady=(0, 8))

        def stop_popup_preview() -> None:
            nonlocal popup_preview_proc
            if popup_preview_proc is None:
                return
            try:
                popup_preview_proc.terminate()
            except Exception:
                pass
            try:
                popup_preview_proc.wait(timeout=0.4)
            except Exception:
                try:
                    popup_preview_proc.kill()
                except Exception:
                    pass
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
            if self.preview_ffplay is None:
                build_msg_var.set("Preview unavailable: ffplay not found")
                return "break"
            src = song_files[idx]
            stop_popup_preview()
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                popup_preview_proc = subprocess.Popen(
                    [self.preview_ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", str(src)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=flags,
                )
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

        files_tree.bind("<Button-1>", on_popup_tree_click)
        files_tree.bind("<ButtonRelease-1>", on_popup_tree_release)
        files_tree.bind("<Delete>", lambda _e=None: (remove_selected_files(), "break")[1])
        files_tree.bind("<Control-a>", lambda _e=None: (files_tree.selection_set(files_tree.get_children()), "break")[1])
        files_tree.bind("<Control-A>", lambda _e=None: (files_tree.selection_set(files_tree.get_children()), "break")[1])
        files_tree.bind("<Motion>", on_popup_tree_motion)
        files_tree.bind("<Leave>", on_popup_tree_leave)

        def on_cancel() -> None:
            stop_popup_preview()
            self._hide_hover_tip()
            popup.destroy()

        popup.protocol("WM_DELETE_WINDOW", on_cancel)

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
            src_root = audio_source_root(self.audio_dir_active.resolve())
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
            btn_ok.configure(state="disabled")
            btn_cancel.configure(state="disabled")

            pulse = {"active": True}
            phase = {"compiling": False}

            def pulse_progress():
                if not pulse["active"]:
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
                popup.after(160, pulse_progress)

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
                        pulse["active"] = False
                        progress.set(1.0)
                        self.excluded_oggs.discard(out_file.name)
                        self.refresh_songs()
                        self._move_song_to_bottom(out_file.name)
                        self.status_var.set(f"Created song: {out_file.name}")
                        build_in_progress["value"] = False
                        stop_popup_preview()
                        popup.destroy()
                    self.after(0, done_ok)
                except Exception as e:
                    def done_err():
                        pulse["active"] = False
                        progress.set(0.0)
                        build_msg_var.set("")
                        build_in_progress["value"] = False
                        btn_ok.configure(state="normal")
                        btn_cancel.configure(state="normal")
                        messagebox.showerror("Mix Builder", str(e), parent=popup)
                    self.after(0, done_err)

            threading.Thread(target=worker, daemon=True).start()

        btn_cancel = ctk.CTkButton(bottom, text="Cancel", width=110, command=on_cancel)
        btn_cancel.pack(side="right")
        btn_ok = ctk.CTkButton(bottom, text="Create", width=110, command=on_ok)
        btn_ok.pack(side="right", padx=(0, 8))

    def _pick_active_audio_dir(self) -> Path:
        if self.audio_dir_override is not None and self.audio_dir_override.exists():
            self.audio_status_var.set(f"/{self.audio_dir_override.name}")
            self._refresh_audio_source_button_text()
            return self.audio_dir_override

        self.audio_dir_new.mkdir(parents=True, exist_ok=True)
        new_rows = refresh_song_catalog(self.audio_dir_new)
        if new_rows:
            self.audio_status_var.set(f"/{self.audio_dir_new.name}")
            self._refresh_audio_source_button_text()
            return self.audio_dir_new
        if self.audio_dir_legacy.exists():
            legacy_rows = refresh_song_catalog(self.audio_dir_legacy)
            if legacy_rows:
                self.audio_status_var.set(f"/{self.audio_dir_legacy.name}")
                self._refresh_audio_source_button_text()
                return self.audio_dir_legacy
        self.audio_status_var.set(f"/{self.audio_dir_new.name}")
        self._refresh_audio_source_button_text()
        return self.audio_dir_new

    def pick_audio_source(self) -> None:
        initial = self.audio_dir_active if self.audio_dir_active.exists() else self.audio_dir_new
        selected = filedialog.askdirectory(title="Select audio source folder", initialdir=str(initial))
        if selected:
            self.audio_dir_override = Path(selected)
            self.audio_dir_active = self.audio_dir_override
            self.audio_status_var.set(f"/{self.audio_dir_override.name}")
            self._refresh_audio_source_button_text()
            self.refresh_songs()

    def _refresh_audio_source_button_text(self) -> None:
        if hasattr(self, "audio_source_button"):
            self.audio_source_button.configure(text=self.audio_status_var.get())

    def pick_image_source(self) -> None:
        initial = self.image_dir_active if self.image_dir_active.exists() else self.image_dir_new
        selected = filedialog.askdirectory(title="Select image source folder", initialdir=str(initial))
        if selected:
            self.image_dir_override = Path(selected)
            self.image_dir_active = self.image_dir_override
            self.cover_root = self.image_dir_active
            self.image_status_var.set(f"/{self.image_dir_active.name}")
            self._refresh_image_source_button_text()

    def _refresh_image_source_button_text(self) -> None:
        if hasattr(self, "image_source_button"):
            self.image_source_button.configure(text=self.image_status_var.get())

    def pick_poster(self) -> None:
        self.cover_root.mkdir(parents=True, exist_ok=True)
        selected = filedialog.askopenfilename(
            title="Select workshop poster",
            initialdir=str(self.cover_root),
            filetypes=[("Images", "*.png;*.jpg;*.jpeg;*.webp;*.bmp")],
        )
        if selected:
            self.poster_path = Path(selected)
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
        self.refresh_songs(recover_if_empty=True)

    def refresh_songs(self, recover_if_empty: bool = False) -> None:
        self.audio_dir_active = self._pick_active_audio_dir()
        rows = refresh_song_catalog(self.audio_dir_active)
        all_keys = {r.ogg.name for r in rows}
        if self.excluded_oggs:
            self.excluded_oggs = {k for k in self.excluded_oggs if k in all_keys}

        visible_rows = [
            {"source": r.source, "ogg": r.ogg, "status": r.status, "detail": r.detail}
            for r in rows
            if r.ogg.name not in self.excluded_oggs
        ]
        if recover_if_empty and not visible_rows and rows and self.excluded_oggs:
            self.excluded_oggs.clear()
            visible_rows = [{"source": r.source, "ogg": r.ogg, "status": r.status, "detail": r.detail} for r in rows]

        self.track_rows = visible_rows

        for row in self.track_rows:
            key = row["ogg"].name
            if key not in self.track_settings:
                self.track_settings[key] = {
                    "cassette": True,
                    "vinyl": True,
                    "cover": None,
                    "b_side": None,
                    "vinyl_art_placement": self.global_vinyl_mask_var.get(),
                }
            else:
                cfg = self.track_settings[key]
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
            song = row["source"].name
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
        for i, row in enumerate(self._visible_rows()):
            key = row["ogg"].name
            cfg = self.track_settings.get(key, {})
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
                    "\U0001F50A",
                    row["status"],
                    "\u2713" if cfg.get("cassette") else "",
                    "\u2713" if cfg.get("vinyl") else "",
                    b_side_text,
                    cover_text,
                ),
                tags=(tag,),
            )
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
                initialdir=str(self.audio_dir_active if self.audio_dir_active.exists() else audio_source_root(self.audio_dir_new)),
                filetypes=[("Audio", "*.ogg;*.mp3;*.wav;*.flac;*.m4a;*.aac;*.wma"), ("All files", "*.*")],
            )
            if selected:
                for key in selected_rows:
                    self.track_settings.setdefault(key, {})["b_side"] = str(Path(selected))
                self._redraw_tree()
                self._update_selection_status_hint()
            return "break"

        if action == "cover":
            selected = filedialog.askopenfilename(
                title="Select song cover",
                initialdir=str(self.cover_root),
                filetypes=[("Images", "*.png;*.jpg;*.jpeg;*.webp;*.bmp")],
            )
            if selected:
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
        current_name = current_row["source"].stem
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

        def _commit(_event=None):
            if self.inline_editor is None:
                return "break"
            new_name = self.inline_editor.get().strip()
            _cancel()
            if not new_name or new_name == current_name:
                return "break"
            try:
                src_root, cache_root = ensure_audio_workspace(self.audio_dir_active.resolve())
                out_stem = _safe_song_stem(new_name)
                current_row_local = next((r for r in self.track_rows if r["ogg"].name == row_id), None)
                source_suffix = current_row_local["source"].suffix if current_row_local else ".ogg"
                source_target_exists = (src_root / f"{out_stem}{source_suffix}").exists()
                ogg_target_exists = (cache_root / f"{out_stem}.ogg").exists()
                overwrite_existing = False
                if source_target_exists or ogg_target_exists:
                    overwrite_existing = bool(
                        messagebox.askyesno(
                            "Overwrite Existing Song",
                            f"A song named '{out_stem}' already exists.\n\nOverwrite it?",
                            parent=self,
                        )
                    )
                    if not overwrite_existing:
                        return "break"
                new_ogg = rename_song_asset(
                    row_id,
                    new_name,
                    self.audio_dir_active,
                    overwrite_existing=overwrite_existing,
                )
                cfg = self.track_settings.pop(row_id, None)
                if cfg is not None:
                    self.track_settings[new_ogg] = cfg
                self.refresh_songs()
                if new_ogg in self.tree.get_children():
                    self.tree.selection_set(new_ogg)
                self.status_var.set(f"Renamed song to: {new_name}")
            except Exception as e:
                messagebox.showerror("Rename failed", str(e))
            return "break"

        editor.bind("<Return>", _commit)
        editor.bind("<Escape>", _cancel)
        editor.bind("<FocusOut>", _commit)

    def start_preview_for_row(self, row_id: str) -> None:
        if not row_id:
            return
        if self.preview_ffplay is None:
            self.status_var.set("Preview unavailable: ffplay not found")
            return
        row = next((r for r in self.track_rows if r["ogg"].name == row_id), None)
        if not row:
            return
        audio_path = row["ogg"] if row["ogg"].exists() else row["source"]
        if not audio_path.exists():
            self.status_var.set("Preview unavailable: file missing")
            return
        self.stop_preview()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.preview_proc = subprocess.Popen(
                [self.preview_ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", str(audio_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            self.status_var.set(f"Previewing: {row['source'].name}")
        except Exception as e:
            self.preview_proc = None
            self.status_var.set(f"Preview failed: {e}")

    def stop_preview(self) -> None:
        if self.preview_proc is None:
            return
        try:
            self.preview_proc.terminate()
        except Exception:
            pass
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

    def _apply_window_icon(self, window: tk.Misc) -> None:
        base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        icon_ico = base_dir / "icon.ico"
        if icon_ico.exists():
            try:
                window.iconbitmap(default=str(icon_ico))
            except Exception:
                pass
        if not self._window_icon_images:
            icon_candidates = [
                base_dir / "icon.png",
            ]
            for p in icon_candidates:
                if not p.exists():
                    continue
                try:
                    self._window_icon_images.append(tk.PhotoImage(file=str(p)))
                except Exception:
                    pass
            if self._window_icon_images:
                self._window_icon_image = self._window_icon_images[-1]
        if self._window_icon_images:
            try:
                window.iconphoto(True, *self._window_icon_images)
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
        label = "Outer" if cur == "outside" else "Inner"
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

    def convert_audio(self) -> None:
        self.audio_dir_active = self._pick_active_audio_dir()
        catalog = refresh_song_catalog(self.audio_dir_active)
        total_sources = sum(1 for e in catalog if e.detail != "cache-only")

        if total_sources <= 0:
            self.status_var.set("No songs found to convert")
            return

        self.preview_title_label.configure(text="Converting Songs (Please Wait)...")
        self.build_progress_var.set(0.0)
        self.status_var.set("Converting songs...")
        self.update_idletasks()

        processed = 0

        def _on_progress(_entry) -> None:
            nonlocal processed
            processed += 1
            ogg_name = _entry.ogg.name if _entry and _entry.ogg else None
            source_name = _entry.source.name if _entry and _entry.source else None
            if ogg_name or source_name:
                for row in self.track_rows:
                    same_ogg = ogg_name and row["ogg"].name == ogg_name
                    same_source = source_name and row["source"].name == source_name
                    if same_ogg or same_source:
                        row["status"] = "ready"
                        row["detail"] = "up-to-date"
                        break
                self._redraw_tree()
            self.build_progress_var.set(max(0.0, min(1.0, processed / total_sources)))
            self.status_var.set(f"Converting songs... ({processed}/{total_sources})")
            self.update_idletasks()

        try:
            summary = convert_audio_library(audio_dir=self.audio_dir_active, force=False, progress_cb=_on_progress)
        except SystemExit as e:
            messagebox.showerror("Audio Conversion Error", str(e))
            self.status_var.set("Conversion failed")
            return
        except Exception as e:
            messagebox.showerror("Audio Conversion Error", str(e))
            self.status_var.set("Conversion failed")
            return
        finally:
            self.preview_title_label.configure(text="Build Preview")

        self.refresh_songs()
        self.build_progress_var.set(1.0)
        self.status_var.set("Converting Complete")

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
        for ogg_name, cfg in self.track_settings.items():
            row_cfg = dict(cfg)
            if not row_cfg.get("cover"):
                row_cfg["cover"] = None
            b_side = row_cfg.get("b_side")
            if not b_side or not Path(b_side).exists():
                row_cfg["b_side"] = None
            row_cfg["vinyl_art_placement"] = global_mask
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
                im = Image.open(thumb).convert("RGBA")
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

        if event.total > 0:
            self.build_progress_var.set(max(0.0, min(1.0, event.index / event.total)))
            self.status_var.set(f"Building {event.index}/{event.total}")
        self.update_idletasks()

    def build_pack(self) -> None:
        self.audio_dir_active = self._pick_active_audio_dir()
        errors = self._preflight()
        if errors:
            messagebox.showerror("Preflight failed", "\n".join(errors))
            self.status_var.set("Preflight failed")
            return

        for child in self.preview_scroll.winfo_children():
            child.destroy()
        self.preview_images.clear()
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start()
        self.build_progress_var.set(0)
        self.status_var.set("Building (please wait)...")

        try:
            out = build_mixed_from_config(self._build_config(), on_track=self._add_preview_tile)
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
        self.parent_mod_var.set("TrueMoozicTali")
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
        self.image_dir_override = None
        self.audio_dir_active = self.audio_dir_new
        self.image_dir_active = self.image_dir_new
        self.cover_root = self.image_dir_new
        self.audio_status_var.set(f"/{self.audio_dir_new.name}")
        self.image_status_var.set(f"/{self.image_dir_new.name}")
        self._refresh_audio_source_button_text()
        self._refresh_image_source_button_text()
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
        os.startfile(str(target))

    def _detect_default_workshop_dir(self) -> Path:
        candidates = []
        user_profile = os.environ.get("USERPROFILE")
        if user_profile:
            candidates.append(Path(user_profile) / "Zomboid" / "Workshop")
        candidates.append(Path.home() / "Zomboid" / "Workshop")
        for c in candidates:
            if c.exists() and c.is_dir():
                return c
        return candidates[0] if candidates else (Path.home() / "Zomboid" / "Workshop")

    def _resolve_workshop_dir(self) -> Path | None:
        if self.workshop_dir_override and self.workshop_dir_override.exists():
            return self.workshop_dir_override
        detected = self._detect_default_workshop_dir()
        if detected.exists():
            return detected

        choose = messagebox.askyesno(
            "Workshop Folder Not Found",
            f"Default workshop folder was not found at:\n{detected}\n\nSelect a workshop folder manually?",
            parent=self,
        )
        if not choose:
            return None
        selected = filedialog.askdirectory(
            title="Select Zomboid Workshop Folder",
            initialdir=str(detected.parent if detected.parent.exists() else Path.home()),
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
        except Exception as e:
            messagebox.showerror("Copy to Workshop", str(e), parent=self)


if __name__ == "__main__":
    app = SimpleMoozicBuilderUI()
    app.mainloop()



