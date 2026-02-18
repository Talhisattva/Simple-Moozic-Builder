#!/usr/bin/env python3
"""
Simple True MooZic child-mod builder (MVP).

Creates either:
- cassette packs from many .ogg files
- vinyl packs from many .ogg files + one cover PNG

Output structure is compatible with the True Moozic main mod contract:
- script items/sounds/models
- GlobalMusic mappings
- audio files
- model/texture assets
"""

from __future__ import annotations

import argparse
import os
import tempfile
import random
import re
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFont

try:  # Optional spike dependency for lighter conversion/write path.
    import numpy as np  # type: ignore
except Exception:
    np = None

try:  # Optional spike dependency for direct OGG writing.
    import soundfile as sf  # type: ignore
except Exception:
    sf = None

try:  # Optional decoder used to normalize mixed input sources.
    import miniaudio  # type: ignore
except Exception:
    miniaudio = None


CASSETTE_TILE = "tsarcraft_music_01_62"
VINYL_TILE = "tsarcraft_music_01_63"
CASSETTE_VARIANT_MIN = 1
CASSETTE_VARIANT_MAX = 19
VINYL_RECORD_VARIANT_MIN = 1
VINYL_RECORD_VARIANT_MAX = 5
VINYL_ALBUM_VARIANT_MIN = 1
VINYL_ALBUM_VARIANT_MAX = 12
AUDIO_FOLDER_NAME = "Put your audio here"
AUDIO_CACHE_FOLDER_NAME = "_ogg"
AUDIO_SOURCE_EXTENSIONS = {
    ".ogg",
    ".mp3",
    ".wav",
    ".flac",
    ".m4a",
    ".aac",
    ".wma",
}


def _safe_song_stem(name: str) -> str:
    stem = (name or "").strip()
    if not stem:
        stem = "New Song"
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "", stem)
    stem = stem.strip().rstrip(".")
    return stem or "New Song"


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundled_resource_root() -> Path:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass).resolve()
        internal = Path(sys.executable).resolve().parent / "_internal"
        if internal.exists():
            return internal
    return app_root()


def bootstrap_runtime_folders(base_dir: Optional[Path] = None) -> dict[str, Path]:
    root = (base_dir or app_root()).resolve()
    audio = ensure(root / AUDIO_FOLDER_NAME)
    audio_cache = ensure(audio / AUDIO_CACHE_FOLDER_NAME)
    images = ensure(root / "Put your images here")
    output = ensure(root / "OUTPUT")
    return {
        "root": root,
        "audio": audio,
        "audio_cache": audio_cache,
        "images": images,
        "output": output,
    }


@dataclass
class AudioTrackEntry:
    source: Path
    ogg: Path
    status: str
    detail: str = ""


@dataclass
class BuildTrackEvent:
    index: int
    total: int
    title: str
    thumbnail: Optional[Path] = None


def sanitize_id(value: str) -> str:
    base = Path(value).stem
    base = unicodedata.normalize("NFD", base)
    base = "".join(ch for ch in base if unicodedata.category(ch) != "Mn")
    base = re.sub(r"[^A-Za-z0-9]", "", base)
    if not base:
        base = "Track"
    return base[:48]


def display_name_from_file(path: Path) -> str:
    return path.stem.replace("_", " ").strip()


def ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def copy_file_if_exists(src: Path, dst: Path) -> bool:
    if not src.exists() or not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def prune_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    # Bottom-up prune so parent dirs can become empty after children are removed.
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except Exception:
            pass


def default_poster_root() -> Path:
    return default_assets_root() / "poster"


def _is_image_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp")


def _parse_yes_no(value: str, default: bool = True) -> bool:
    raw = (value or "").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes", "true", "1")


def _parse_vinyl_art_placement(value: str, default: str = "inside") -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return default
    if raw in ("i", "in", "inside", "center", "inner"):
        return "inside"
    if raw in ("o", "out", "outside", "outer"):
        return "outside"
    return default


def _collect_numbered_variants(folder: Path, prefix: str) -> list[int]:
    if not folder.exists():
        return []
    out: set[int] = set()
    for p in folder.iterdir():
        if not p.is_file() or not _is_image_file(p):
            continue
        stem = p.stem
        if not stem.startswith(prefix):
            continue
        suffix = stem[len(prefix) :]
        if suffix.isdigit():
            out.add(int(suffix))
    return sorted(out)


def _validate_contiguous_variant_pool(label: str, variants: list[int], expected_min: int, expected_max: int) -> None:
    expected = set(range(expected_min, expected_max + 1))
    found = set(variants)
    if found != expected:
        raise SystemExit(
            f"{label} must be exactly {expected_min}..{expected_max}. "
            f"Found: {sorted(found)}"
        )


def _resolve_cover_choice(cover_dir: Path, choice: str) -> Optional[Path]:
    images = [p for p in cover_dir.iterdir() if _is_image_file(p)] if cover_dir.exists() else []
    if not images:
        return None

    if not choice.strip():
        return None

    needle = choice.strip().lower()

    # Exact name (with extension), case-insensitive
    for p in images:
        if p.name.lower() == needle:
            return p

    # Stem match (without extension), case-insensitive
    for p in images:
        if p.stem.lower() == needle:
            return p

    return None


def _square_letterbox(src: Path, out_size: int) -> Image.Image:
    with Image.open(src) as im:
        im = im.convert("RGBA")
        side = max(im.width, im.height)
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 255))
        x = (side - im.width) // 2
        y = (side - im.height) // 2
        canvas.paste(im, (x, y), im)
        return canvas.resize((out_size, out_size), Image.LANCZOS)


def _load_overlay_font(size: int) -> ImageFont.ImageFont:
    candidates = [
        Path(r"C:\Windows\Fonts\segoeuib.ttf"),
        Path(r"C:\Windows\Fonts\arialbd.ttf"),
        Path(r"C:\Windows\Fonts\calibrib.ttf"),
        Path(r"C:\Windows\Fonts\impact.ttf"),
        Path(r"C:\Windows\Fonts\segoeui.ttf"),
        Path(r"C:\Windows\Fonts\arial.ttf"),
    ]
    for font_path in candidates:
        if font_path.exists():
            try:
                return ImageFont.truetype(str(font_path), size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    max_lines: Optional[int] = None,
    truncate_with_ellipsis: bool = False,
) -> list[str]:
    words = text.strip().split()
    if not words:
        return [text.strip() or "Untitled"]

    lines: list[str] = []
    i = 0
    while i < len(words) and (max_lines is None or len(lines) < max_lines):
        line = words[i]
        i += 1
        while i < len(words):
            candidate = f"{line} {words[i]}"
            if draw.textlength(candidate, font=font) <= max_width:
                line = candidate
                i += 1
            else:
                break
        lines.append(line)

    if truncate_with_ellipsis and i < len(words) and lines:
        ell = "..."
        last = lines[-1]
        while last and draw.textlength(last + ell, font=font) > max_width:
            parts = last.split(" ")
            if len(parts) <= 1:
                last = last[:-1]
            else:
                last = " ".join(parts[:-1])
        lines[-1] = (last + ell).strip()

    return lines


def _apply_mod_name_overlay(img: Image.Image, mod_name: str) -> Image.Image:
    out = img.convert("RGBA")
    draw = ImageDraw.Draw(out, "RGBA")
    w, h = out.size

    x0 = w // 2
    y0 = h // 2
    margin = max(8, w // 32)
    box = (x0 + margin // 2, y0 + margin // 2, w - margin, h - margin)

    text = (mod_name or "").strip() or "Untitled"
    inner = max(8, margin)
    max_w = max(24, (box[2] - box[0]) - inner * 2)
    max_h = max(20, (box[3] - box[1]) - inner * 2)

    stroke = max(2, h // 170)
    line_spacing = max(2, h // 128)
    lines: list[str] = [text]
    font: ImageFont.ImageFont = ImageFont.load_default()
    min_size = max(8, w // 40)
    for size in range(max(14, w // 9), min_size - 1, -1):
        trial_font = _load_overlay_font(size)
        trial_lines = _wrap_text(draw, text, trial_font, max_width=max_w, max_lines=None, truncate_with_ellipsis=False)
        tb = draw.multiline_textbbox(
            (0, 0),
            "\n".join(trial_lines),
            font=trial_font,
            spacing=line_spacing,
            align="right",
            stroke_width=stroke,
        )
        tw = tb[2] - tb[0]
        th = tb[3] - tb[1]
        if tw <= max_w and th <= max_h:
            lines = trial_lines
            font = trial_font
            break
    else:
        # Last-resort fallback: clamp to a reasonable line count and ellipsize.
        fallback_font = _load_overlay_font(min_size)
        lines = _wrap_text(draw, text, fallback_font, max_width=max_w, max_lines=6, truncate_with_ellipsis=True)
        font = fallback_font

    rendered = "\n".join(lines)
    bb = draw.multiline_textbbox((0, 0), rendered, font=font, spacing=line_spacing, align="right", stroke_width=stroke)
    tw = bb[2] - bb[0]
    th = bb[3] - bb[1]
    tx = box[2] - inner - tw
    ty = box[3] - inner - th
    lift = max(4, h // 64)
    ty = max(box[1] + inner, ty - lift)

    draw.multiline_text(
        (tx, ty),
        rendered,
        font=font,
        fill=(0, 0, 0, 245),
        spacing=line_spacing,
        align="right",
        stroke_width=stroke,
        stroke_fill=(255, 255, 255, 235),
    )
    return out


def render_workshop_square_image(source: Path, out_size: int, mod_name: str, add_name_overlay: bool = True) -> Image.Image:
    img = _square_letterbox(source, out_size)
    if add_name_overlay:
        img = _apply_mod_name_overlay(img, mod_name)
    return img


def _save_hr_cover(source: Path, target: Path, max_size: int = 2048) -> None:
    with Image.open(source) as im:
        src = im.convert("RGBA")
        side = min(max_size, max(src.width, src.height))
        out = _cover_letterbox(src, side, side)
    target.parent.mkdir(parents=True, exist_ok=True)
    out.save(target, format="PNG")


def write_workshop_images(
    paths: dict[str, Path],
    selected_cover: Optional[Path],
    mod_name: str,
    add_name_overlay: bool = True,
) -> None:
    poster_root = default_poster_root()
    default_icon = poster_root / "icon.png"
    default_poster = poster_root / "poster.png"
    default_preview = poster_root / "Preview.png"

    if not default_icon.exists():
        raise SystemExit(f"Missing default icon: {default_icon}")
    if not default_poster.exists():
        raise SystemExit(f"Missing default poster: {default_poster}")
    if not default_preview.exists():
        raise SystemExit(f"Missing default preview: {default_preview}")

    # Icon is always copied from default icon.
    for icon_target in (paths["mod_base"] / "icon.png", paths["v42"] / "icon.png"):
        shutil.copy2(default_icon, icon_target)

    # Poster/preview source:
    # - if user selected a cover in "Put your .png cover here", use that for both
    # - otherwise use defaults from assets/poster
    poster_src = selected_cover if selected_cover is not None else default_poster
    preview_src = selected_cover if selected_cover is not None else default_preview

    poster_img = render_workshop_square_image(poster_src, 1024, mod_name, add_name_overlay=add_name_overlay)
    preview_img = render_workshop_square_image(preview_src, 256, mod_name, add_name_overlay=add_name_overlay)

    for poster_target in (paths["mod_base"] / "poster.png", paths["v42"] / "poster.png"):
        poster_img.save(poster_target, format="PNG")

    # Write both name variants to match existing mixed usage in your workshop folders.
    preview_img.save(paths["root"] / "Preview.png", format="PNG")
    preview_img.save(paths["root"] / "preview.png", format="PNG")
    _save_hr_cover(poster_src, paths["hr"] / "Poster.png")


def build_mod_layout(out_dir: Path, mod_id: str) -> dict[str, Path]:
    root = out_dir / mod_id
    mod_base = root / "Contents" / "mods" / mod_id
    v42 = mod_base / "42"
    media = v42 / "media"
    common = mod_base / "common"

    paths = {
        "root": root,
        "mod_base": mod_base,
        "common": common,
        "v42": v42,
        "media": media,
        "scripts": ensure(media / "scripts"),
        "sound": ensure(common / "media" / "sound" / mod_id),
        "models": ensure(media / "models_X" / "WorldItems"),
        "textures": ensure(media / "textures"),
        "wtextures": ensure(media / "textures" / "WorldItems"),
        "lua_shared": ensure(media / "lua" / "shared"),
        "lua_server_items": ensure(media / "lua" / "server" / "Items"),
        "hr": ensure(media / "textures" / "HR"),
    }
    return paths


def write_mod_info(
    mod_base: Path,
    v42: Path,
    name: str,
    mod_id: str,
    parent_mod_id: str = "TrueMoozic",
    author: str = "local-builder",
) -> None:
    lines = [
        f"name={name}",
        "poster=poster.png",
        f"id={mod_id}",
        "versionMin=42.13",
        "icon=icon.png",
    ]
    parent = (parent_mod_id or "").strip()
    if parent:
        lines.append(f"require=\\{parent}")
    lines.extend(
        [
            f"description={name} generated by Simple Moozic Builder",
            f"author={author or 'local-builder'}",
            "",
        ]
    )
    content = "\n".join(lines)
    write(mod_base / "mod.info", content)
    write(v42 / "mod.info", content)


def write_standalone_music_defs(lua_shared: Path) -> None:
    content = "\n".join(
        [
            "if not TCMusic then TCMusic = {} end",
            "if TCMusic.ItemMusicPlayer == nil then TCMusic.ItemMusicPlayer = {} end",
            "if TCMusic.VehicleMusicPlayer == nil then TCMusic.VehicleMusicPlayer = {} end",
            "if TCMusic.WorldMusicPlayer == nil then TCMusic.WorldMusicPlayer = {} end",
            "if TCMusic.WalkmanPlayer == nil then TCMusic.WalkmanPlayer = {} end",
            "if GlobalMusic == nil then GlobalMusic = {} end",
            "",
            'TCMusic.WorldMusicPlayer["tsarcraft_music_01_62"] = "tsarcraft_music_01_62"',
            'TCMusic.WorldMusicPlayer["tsarcraft_music_01_63"] = "tsarcraft_music_01_63"',
            'GlobalMusic["CassetteMainTheme"] = "tsarcraft_music_01_62"',
            'GlobalMusic["VinylMainTheme"] = "tsarcraft_music_01_63"',
            "",
        ]
    )
    write(lua_shared / "TCMusicDefenitions.lua", content)


def bundle_standalone_redundancy(paths: dict[str, Path], assets_root: Path) -> None:
    textures_root = assets_root / "textures"
    models_root = assets_root / "models_X" / "WorldItems"
    out_textures = paths["textures"]
    out_models = paths["models"]

    model_names = [
        "TCTape.fbx",
        "TMVinylrecord.fbx",
        "TMVinylalbum.fbx",
        "TMVinylbooklet.fbx",
    ]
    for model_name in model_names:
        copy_file_if_exists(models_root / model_name, out_models / model_name)

    # Cassette random pools.
    for i in range(CASSETTE_VARIANT_MIN, CASSETTE_VARIANT_MAX + 1):
        copy_file_if_exists(
            textures_root / "Icons" / "Cassette" / f"Item_TCTape{i}.png",
            out_textures / "Icons" / "Cassette" / f"Item_TCTape{i}.png",
        )
        copy_file_if_exists(
            textures_root / "WorldItems" / "Cassette" / f"TCTape{i}.png",
            out_textures / "WorldItems" / "Cassette" / f"TCTape{i}.png",
        )
    copy_file_if_exists(
        textures_root / "WorldItems" / "Cassette" / "TCTape_UV.png",
        out_textures / "WorldItems" / "Cassette" / "TCTape_UV.png",
    )

    # Vinyl random pools.
    for i in range(VINYL_RECORD_VARIANT_MIN, VINYL_RECORD_VARIANT_MAX + 1):
        copy_file_if_exists(
            textures_root / "Icons" / "Vinyl" / f"Item_TCVinylrecord{i}.png",
            out_textures / "Icons" / "Vinyl" / f"Item_TCVinylrecord{i}.png",
        )
    for i in range(VINYL_RECORD_VARIANT_MIN, VINYL_RECORD_VARIANT_MAX + 1):
        copy_file_if_exists(
            textures_root / "WorldItems" / "Vinyl" / f"TMVinylrecord{i}.png",
            out_textures / "WorldItems" / "Vinyl" / f"TMVinylrecord{i}.png",
        )
    for i in range(VINYL_RECORD_VARIANT_MIN, VINYL_ALBUM_VARIANT_MAX + 1):
        copy_file_if_exists(
            textures_root / "WorldItems" / "Vinyl" / f"TCVinylrecord{i}.png",
            out_textures / "WorldItems" / "Vinyl" / f"TCVinylrecord{i}.png",
        )
        copy_file_if_exists(
            textures_root / "Icons" / "Vinyl" / "Album" / f"Item_TCVinylrecord{i}.png",
            out_textures / "Icons" / "Vinyl" / "Album" / f"Item_TCVinylrecord{i}.png",
        )
        copy_file_if_exists(
            textures_root / "WorldItems" / "Vinyl" / "Album" / f"Item_TCVinylrecord{i}.png",
            out_textures / "WorldItems" / "Vinyl" / "Album" / f"Item_TCVinylrecord{i}.png",
        )
        # Album inventory icon alias used by builder output.
        if not copy_file_if_exists(
            textures_root / f"Item_TCAlbum{i}.png",
            out_textures / f"Item_TCAlbum{i}.png",
        ):
            copy_file_if_exists(
                textures_root / "Icons" / "Vinyl" / "Album" / f"Item_TCVinylrecord{i}.png",
                out_textures / f"Item_TCAlbum{i}.png",
            )


def write_workshop(root: Path, name: str, songs: Optional[list[str]] = None) -> None:
    lines = [
        "version=1",
        "id=",
        f"title={name}",
        "description=[i] Generated with Simple Moozic Builder [/i]",
        f"description=[h2]{name}[/h2]",
    ]
    if songs:
        lines.append("description=[h3]Song List[/h3]")
        for song in songs:
            song_name = (song or "").strip()
            if song_name:
                lines.append(f"description={song_name}")
    lines.extend(
        [
            "tags=Build 42;Multiplayer;Music;Simple Moozic Builder",
            "visibility=unlisted",
            "",
        ]
    )
    content = "\n".join(lines)
    write(root / "workshop.txt", content)


def workshop_song_lines(oggs: list[Path], song_b_sides: Optional[dict[str, Path | str]] = None) -> list[str]:
    out: list[str] = []
    b_map = song_b_sides or {}
    for ogg in oggs:
        a_side = display_name_from_file(ogg)
        b_raw = b_map.get(ogg.name)
        b_side_name = ""
        if b_raw:
            try:
                b_path = Path(b_raw)
                if b_path.exists() and b_path.is_file():
                    b_side_name = display_name_from_file(b_path)
            except Exception:
                b_side_name = ""
        if b_side_name:
            out.append(f"{a_side} | B-Side: {b_side_name}")
        else:
            out.append(a_side)
    return out


def audio_source_root(audio_dir: Path) -> Path:
    return audio_dir.parent if audio_dir.name == AUDIO_CACHE_FOLDER_NAME else audio_dir


def audio_cache_root(audio_dir: Path) -> Path:
    return audio_dir if audio_dir.name == AUDIO_CACHE_FOLDER_NAME else (audio_dir / AUDIO_CACHE_FOLDER_NAME)


def ensure_audio_workspace(audio_dir: Path) -> tuple[Path, Path]:
    src_root = ensure(audio_source_root(audio_dir))
    cache_root = ensure(audio_cache_root(audio_dir))
    return src_root, cache_root


def _collect_audio_sources(src_root: Path) -> list[Path]:
    if not src_root.exists():
        return []
    return sorted(
        [
            p
            for p in src_root.iterdir()
            if p.is_file() and p.suffix.lower() in AUDIO_SOURCE_EXTENSIONS
        ]
    )


def _locate_ffmpeg() -> Optional[str]:
    for p in _candidate_binary_paths("ffmpeg.exe"):
        if p.exists() and p.is_file():
            return str(p)
    return shutil.which("ffmpeg")


def locate_ffplay() -> Optional[str]:
    # "ffmplay" typo fallback is intentional for compatibility with misnamed bundles.
    for binary_name in ("ffplay.exe", "ffmplay.exe"):
        for p in _candidate_binary_paths(binary_name):
            if p.exists() and p.is_file():
                return str(p)
    return shutil.which("ffplay")


def _audio_backend_mode() -> str:
    mode = (os.environ.get("SMB_AUDIO_BACKEND", "auto") or "auto").strip().lower()
    if mode in ("soundfile", "ffmpeg"):
        return mode
    return "auto"


def _soundfile_backend_ready() -> bool:
    return sf is not None and np is not None


def _audio_trace_enabled() -> bool:
    return (os.environ.get("SMB_TRACE_AUDIO", "") or "").strip().lower() in ("1", "true", "yes", "on")


def _audio_trace(msg: str) -> None:
    if not _audio_trace_enabled():
        return
    try:
        p = app_root() / "simple_moozic_builder_audio_trace.log"
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with p.open("a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {msg}\n")
    except Exception:
        pass


def _resample_pcm16(data: "np.ndarray", src_rate: int, dst_rate: int) -> "np.ndarray":
    if src_rate == dst_rate:
        return data
    if data.size == 0:
        return data
    frames = data.shape[0]
    channels = data.shape[1]
    out_frames = max(1, int(round(frames * (dst_rate / src_rate))))
    x_old = np.linspace(0.0, 1.0, num=frames, endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=out_frames, endpoint=False)
    out = np.empty((out_frames, channels), dtype=np.float32)
    for c in range(channels):
        out[:, c] = np.interp(x_new, x_old, data[:, c].astype(np.float32))
    return np.clip(out, -32768, 32767).astype(np.int16)


def _decode_to_pcm16(source: Path, target_rate: int = 44100, target_channels: int = 2) -> tuple["np.ndarray", int]:
    if np is None:
        raise SystemExit("numpy is required for the soundfile conversion spike backend.")
    decode_err: Exception | None = None
    if sf is not None:
        try:
            data, sr = sf.read(str(source), dtype="int16", always_2d=True)
        except Exception as e:
            decode_err = e
            data = None
            sr = target_rate
    else:
        data = None
        sr = target_rate
    if data is None:
        if miniaudio is None:
            raise SystemExit(f"No decode backend available (soundfile failed: {decode_err}; miniaudio missing).")
        decoded = miniaudio.decode_file(
            str(source),
            output_format=miniaudio.SampleFormat.SIGNED16,
            nchannels=target_channels,
            sample_rate=target_rate,
        )
        # Copy out of miniaudio-owned buffer so callers don't hold dangling memory.
        arr = np.frombuffer(decoded.samples, dtype=np.int16).copy()
        if target_channels > 1:
            arr = arr.reshape(-1, target_channels)
        else:
            arr = arr.reshape(-1, 1)
        return np.ascontiguousarray(arr), target_rate

    if data.shape[1] != target_channels:
        if data.shape[1] == 1 and target_channels == 2:
            data = np.repeat(data, 2, axis=1)
        elif data.shape[1] >= 2 and target_channels == 1:
            data = np.mean(data[:, :2], axis=1, keepdims=True).astype(np.int16)
        else:
            data = data[:, :target_channels]
            if data.shape[1] < target_channels:
                pad = np.zeros((data.shape[0], target_channels - data.shape[1]), dtype=np.int16)
                data = np.concatenate([data, pad], axis=1)
    if sr != target_rate:
        data = _resample_pcm16(data, sr, target_rate)
    return data.astype(np.int16), target_rate


def _convert_with_soundfile(source: Path, target: Path) -> None:
    if not _soundfile_backend_ready():
        raise SystemExit("soundfile backend unavailable (requires soundfile + numpy).")
    pcm, sr = _decode_to_pcm16(source, target_rate=44100, target_channels=2)
    target.parent.mkdir(parents=True, exist_ok=True)
    with sf.SoundFile(
        str(target),
        mode="w",
        samplerate=sr,
        channels=2,
        format="OGG",
        subtype="VORBIS",
    ) as out_sf:
        if pcm.shape[0] == 0:
            return
        frame_step = 16384
        for i in range(0, pcm.shape[0], frame_step):
            chunk = np.ascontiguousarray(pcm[i : i + frame_step], dtype=np.int16)
            out_sf.buffer_write(chunk.tobytes(), dtype="int16")


def _create_mix_with_soundfile(source_files: list[Path], out_path: Path) -> None:
    if not _soundfile_backend_ready():
        raise SystemExit("soundfile backend unavailable (requires soundfile + numpy).")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Stream chunks into encoder to avoid one huge write call that can crash.
    with sf.SoundFile(
        str(out_path),
        mode="w",
        samplerate=44100,
        channels=2,
        format="OGG",
        subtype="VORBIS",
    ) as out_sf:
        _audio_trace(f"soundfile mix start: out={out_path} sources={len(source_files)}")
        for src in source_files:
            _audio_trace(f"decode start: {src}")
            pcm, _ = _decode_to_pcm16(src, target_rate=44100, target_channels=2)
            if pcm.shape[0] == 0:
                _audio_trace(f"skip empty: {src}")
                continue
            frame_step = 16384
            _audio_trace(f"write start: {src} frames={pcm.shape[0]}")
            for i in range(0, pcm.shape[0], frame_step):
                chunk = np.ascontiguousarray(pcm[i : i + frame_step], dtype=np.int16)
                out_sf.buffer_write(chunk.tobytes(), dtype="int16")
            _audio_trace(f"write done: {src}")
        _audio_trace(f"soundfile mix done: out={out_path}")


def _candidate_binary_paths(binary_name: str) -> list[Path]:
    roots = []
    seen: set[str] = set()
    for root in (
        app_root(),
        bundled_resource_root(),
        app_root() / "_internal",
        bundled_resource_root() / "_internal",
    ):
        key = str(root.resolve()) if root.exists() else str(root)
        if key in seen:
            continue
        seen.add(key)
        roots.append(root)

    rel_dirs = (
        Path("."),
        Path("ffmpeg"),
        Path("bin"),
        Path("ffmpeg") / "bin",
    )
    out: list[Path] = []
    for root in roots:
        for rel in rel_dirs:
            out.append((root / rel / binary_name).resolve())
    return out


def refresh_song_catalog(audio_dir: Path) -> list[AudioTrackEntry]:
    src_root, cache_root = ensure_audio_workspace(audio_dir)
    raw_entries: list[AudioTrackEntry] = []

    sources = _collect_audio_sources(src_root)
    seen_oggs: set[Path] = set()

    for src in sources:
        target = cache_root / f"{src.stem}.ogg"
        src_is_ogg = src.suffix.lower() == ".ogg"
        seen_oggs.add(target.resolve())
        if not target.exists():
            if src_is_ogg:
                status = "ready"
                detail = "source ogg"
                target = src
                seen_oggs.add(src.resolve())
            else:
                status = "needs convert"
                detail = "not converted"
        else:
            src_mtime = src.stat().st_mtime
            dst_mtime = target.stat().st_mtime
            if dst_mtime >= src_mtime:
                status = "ready"
                detail = "up-to-date"
            else:
                status = "stale"
                detail = "source newer"
        raw_entries.append(AudioTrackEntry(source=src, ogg=target, status=status, detail=detail))

    # Include cache-only OGGs (keeps CLI usable when users only drop OGG into _ogg).
    for ogg in sorted([p for p in cache_root.iterdir() if p.is_file() and p.suffix.lower() == ".ogg"]):
        resolved = ogg.resolve()
        if resolved in seen_oggs:
            continue
        raw_entries.append(AudioTrackEntry(source=ogg, ogg=ogg, status="ready", detail="cache-only"))

    # Canonicalize duplicate logical song keys that resolve to the same .ogg filename.
    # This can happen when users have same-stem files (e.g. song.mp3 + song.ogg).
    # Prefer source .ogg rows first, then better status.
    status_rank = {"ready": 3, "stale": 2, "needs convert": 1}
    deduped: dict[str, AudioTrackEntry] = {}
    for entry in raw_entries:
        key = entry.ogg.name
        keep = deduped.get(key)
        if keep is None:
            deduped[key] = entry
            continue
        entry_score = (
            1 if entry.source.suffix.lower() == ".ogg" else 0,
            status_rank.get(entry.status, 0),
            entry.source.stat().st_mtime if entry.source.exists() else 0.0,
        )
        keep_score = (
            1 if keep.source.suffix.lower() == ".ogg" else 0,
            status_rank.get(keep.status, 0),
            keep.source.stat().st_mtime if keep.source.exists() else 0.0,
        )
        if entry_score > keep_score:
            deduped[key] = entry

    return sorted(deduped.values(), key=lambda e: e.source.name.lower())


def collect_available_oggs(audio_dir: Path) -> list[Path]:
    src_root, cache_root = ensure_audio_workspace(audio_dir)
    by_name: dict[str, Path] = {}

    # Prefer converted cache when present.
    for p in sorted([x for x in cache_root.iterdir() if x.is_file() and x.suffix.lower() == ".ogg"]):
        by_name[p.name] = p

    # Fall back to source-root OGG files when no cache entry exists yet.
    for p in sorted([x for x in src_root.iterdir() if x.is_file() and x.suffix.lower() == ".ogg"]):
        if p.name not in by_name:
            by_name[p.name] = p

    return sorted(by_name.values(), key=lambda x: x.name.lower())


def convert_audio_library(
    audio_dir: Path,
    force: bool = False,
    progress_cb: Optional[Callable[[AudioTrackEntry], None]] = None,
) -> dict[str, int]:
    src_root, cache_root = ensure_audio_workspace(audio_dir)
    sources = _collect_audio_sources(src_root)
    backend_mode = _audio_backend_mode()
    prefer_soundfile = backend_mode != "ffmpeg"
    ffmpeg = _locate_ffmpeg()

    summary = {
        "total": len(sources),
        "converted": 0,
        "copied": 0,
        "skipped": 0,
        "failed": 0,
    }

    for src in sources:
        target = cache_root / f"{src.stem}.ogg"
        up_to_date = target.exists() and target.stat().st_mtime >= src.stat().st_mtime
        if not force and up_to_date:
            entry = AudioTrackEntry(source=src, ogg=target, status="ready", detail="up-to-date")
            summary["skipped"] += 1
            if progress_cb:
                progress_cb(entry)
            continue

        if src.suffix.lower() == ".ogg":
            if src.resolve() != target.resolve():
                shutil.copy2(src, target)
            summary["copied"] += 1
            entry = AudioTrackEntry(source=src, ogg=target, status="ready", detail="copied")
            if progress_cb:
                progress_cb(entry)
            continue

        converted = False
        if prefer_soundfile and _soundfile_backend_ready():
            try:
                _convert_with_soundfile(src, target)
                converted = True
                detail = "converted (soundfile)"
            except Exception as e:
                if backend_mode == "soundfile":
                    raise SystemExit(f"soundfile conversion failed for {src.name}: {e}")

        if not converted:
            if not ffmpeg:
                if prefer_soundfile and not _soundfile_backend_ready():
                    raise SystemExit(
                        "soundfile backend unavailable (needs soundfile + numpy) and ffmpeg was not found. "
                        "Install dependencies or set SMB_AUDIO_BACKEND=ffmpeg with ffmpeg available."
                    )
                raise SystemExit(
                    "ffmpeg is required for non-OGG conversion but was not found in PATH. "
                    "Install ffmpeg and ensure `ffmpeg` is available in your terminal."
                )

            cmd = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(src),
                "-vn",
                "-c:a",
                "libvorbis",
                "-q:a",
                "5",
                str(target),
            ]
            completed = subprocess.run(cmd, capture_output=True, text=True)
            if completed.returncode != 0:
                summary["failed"] += 1
                detail_err = (completed.stderr or completed.stdout or "ffmpeg failed").strip()
                entry = AudioTrackEntry(source=src, ogg=target, status="failed", detail=detail_err)
                if progress_cb:
                    progress_cb(entry)
                continue
            detail = "converted (ffmpeg)"

        summary["converted"] += 1
        entry = AudioTrackEntry(source=src, ogg=target, status="ready", detail=detail)
        if progress_cb:
            progress_cb(entry)

    return summary


def create_song_from_sources(
    song_name: str,
    source_files: list[Path],
    audio_dir: Path,
    overwrite_existing: bool = False,
) -> Path:
    if not source_files:
        raise SystemExit("No source files were provided to create the song.")
    backend_mode = _audio_backend_mode()
    prefer_soundfile = backend_mode != "ffmpeg"
    ffmpeg = _locate_ffmpeg()
    if backend_mode == "ffmpeg" and not ffmpeg:
        raise SystemExit("ffmpeg was not found; cannot create a stitched song.")

    resolved_sources: list[Path] = []
    for src in source_files:
        p = Path(src).resolve()
        if not p.exists() or not p.is_file():
            raise SystemExit(f"Source file not found: {p}")
        resolved_sources.append(p)

    src_root = ensure(audio_source_root(Path(audio_dir).resolve()))
    cache_root = ensure(audio_cache_root(Path(audio_dir).resolve()))

    out_stem = _safe_song_stem(song_name)
    out_path = src_root / f"{out_stem}.ogg"
    if not overwrite_existing:
        n = 2
        while out_path.exists():
            out_path = src_root / f"{out_stem} ({n}).ogg"
            n += 1
    elif out_path.exists():
        out_path.unlink()

    created = False
    soundfile_ready = _soundfile_backend_ready()
    soundfile_err: Exception | None = None
    if prefer_soundfile and soundfile_ready:
        try:
            _create_mix_with_soundfile(resolved_sources, out_path)
            created = True
        except Exception as e:
            soundfile_err = e
            if backend_mode == "soundfile":
                raise SystemExit(f"soundfile failed creating song: {e}")
    elif backend_mode == "soundfile":
        raise SystemExit("soundfile backend unavailable (needs soundfile + numpy); cannot create stitched song.")

    if not created:
        if not ffmpeg:
            if prefer_soundfile and not soundfile_ready:
                raise SystemExit(
                    "soundfile backend unavailable (needs soundfile + numpy) and ffmpeg was not found; "
                    "cannot create stitched song."
                )
            if soundfile_err is not None:
                raise SystemExit(f"soundfile failed creating song: {soundfile_err}; ffmpeg fallback was not found.")
            raise SystemExit(
                "ffmpeg was not found; cannot create a stitched song."
            )
        cmd: list[str] = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        for src in resolved_sources:
            cmd.extend(["-i", str(src)])
        concat_inputs = "".join(f"[{idx}:a]" for idx in range(len(resolved_sources)))
        filter_graph = f"{concat_inputs}concat=n={len(resolved_sources)}:v=0:a=1[outa]"
        cmd.extend(
            [
                "-filter_complex",
                filter_graph,
                "-map",
                "[outa]",
                "-vn",
                "-c:a",
                "libvorbis",
                "-q:a",
                "5",
                str(out_path),
            ]
        )
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "ffmpeg failed").strip()
            raise SystemExit(f"ffmpeg failed creating song: {err}")

    cache_out = cache_root / out_path.name
    try:
        shutil.copy2(out_path, cache_out)
    except Exception:
        pass
    return out_path


def convert_single_audio_file(source_file: Path, audio_dir: Path, force: bool = True) -> AudioTrackEntry:
    src_root, cache_root = ensure_audio_workspace(Path(audio_dir).resolve())
    src = Path(source_file).resolve()
    if not src.exists() or not src.is_file():
        raise SystemExit(f"Source file not found: {src}")

    backend_mode = _audio_backend_mode()
    prefer_soundfile = backend_mode != "ffmpeg"
    ffmpeg = _locate_ffmpeg()
    target = cache_root / f"{src.stem}.ogg"
    up_to_date = target.exists() and target.stat().st_mtime >= src.stat().st_mtime
    if not force and up_to_date:
        return AudioTrackEntry(source=src, ogg=target, status="ready", detail="up-to-date")

    if src.suffix.lower() == ".ogg":
        if src.resolve() != target.resolve():
            shutil.copy2(src, target)
        return AudioTrackEntry(source=src, ogg=target, status="ready", detail="copied")

    if prefer_soundfile and _soundfile_backend_ready():
        try:
            _convert_with_soundfile(src, target)
            return AudioTrackEntry(source=src, ogg=target, status="ready", detail="converted (soundfile)")
        except Exception as e:
            if backend_mode == "soundfile":
                raise SystemExit(f"soundfile conversion failed: {e}")

    if not ffmpeg:
        if prefer_soundfile and not _soundfile_backend_ready():
            raise SystemExit(
                "soundfile backend unavailable (needs soundfile + numpy) and ffmpeg was not found."
            )
        raise SystemExit("ffmpeg is required for non-OGG conversion but was not found.")

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-vn",
        "-c:a",
        "libvorbis",
        "-q:a",
        "5",
        str(target),
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "ffmpeg failed").strip()
        raise SystemExit(f"ffmpeg conversion failed: {detail}")
    return AudioTrackEntry(source=src, ogg=target, status="ready", detail="converted (ffmpeg)")


def rename_song_asset(
    ogg_name: str,
    new_title: str,
    audio_dir: Path,
    overwrite_existing: bool = False,
) -> str:
    if not ogg_name:
        raise SystemExit("Missing source song key.")
    new_stem = _safe_song_stem(new_title)
    if not new_stem:
        raise SystemExit("Song name cannot be empty.")

    src_root, cache_root = ensure_audio_workspace(Path(audio_dir).resolve())
    old_ogg = cache_root / ogg_name
    old_stem = Path(ogg_name).stem

    src_candidates = sorted(
        [p for p in src_root.iterdir() if p.is_file() and p.stem == old_stem],
        key=lambda p: 0 if p.suffix.lower() == ".ogg" else 1,
    )
    source_file = src_candidates[0] if src_candidates else None

    def _next_free(path: Path) -> Path:
        if not path.exists():
            return path
        n = 2
        while True:
            candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
            if not candidate.exists():
                return candidate
            n += 1

    if source_file is not None:
        src_target = source_file.with_name(f"{new_stem}{source_file.suffix}")
        if source_file.resolve() != src_target.resolve():
            if src_target.exists():
                if overwrite_existing:
                    src_target.unlink()
                else:
                    src_target = _next_free(src_target)
        if source_file.resolve() != src_target.resolve():
            source_file.rename(src_target)
        source_file = src_target

    new_ogg_name = f"{new_stem}.ogg"
    ogg_target = cache_root / new_ogg_name
    if old_ogg.resolve() != ogg_target.resolve() and ogg_target.exists():
        if overwrite_existing:
            ogg_target.unlink()
        else:
            ogg_target = _next_free(ogg_target)
    if old_ogg.exists():
        if old_ogg.resolve() != ogg_target.resolve():
            old_ogg.rename(ogg_target)
    elif source_file is not None and source_file.suffix.lower() == ".ogg":
        if source_file.resolve() != ogg_target.resolve():
            shutil.copy2(source_file, ogg_target)
    elif source_file is not None:
        convert_single_audio_file(source_file, src_root, force=True)
        generated = cache_root / f"{source_file.stem}.ogg"
        if generated.exists() and generated.resolve() != ogg_target.resolve():
            generated.rename(ogg_target)
    else:
        raise SystemExit(f"Could not locate song asset for key: {ogg_name}")

    return ogg_target.name


def find_oggs(audio_dir: Path) -> list[Path]:
    # Prefer converted cache in the new audio workspace convention.
    cache = audio_cache_root(audio_dir)
    if cache.exists():
        cached = sorted([p for p in cache.iterdir() if p.is_file() and p.suffix.lower() == ".ogg"])
        if cached:
            return cached
    # Backward compatibility for legacy folders containing direct OGG files.
    if audio_dir.exists():
        return sorted([p for p in audio_dir.iterdir() if p.is_file() and p.suffix.lower() == ".ogg"])
    return []


def detect_template_mask_and_bbox(template: Image.Image):
    tpl = template.convert("RGBA")
    w, h = tpl.size
    pix = tpl.load()
    mask = Image.new("L", (w, h), 0)
    mpx = mask.load()
    found = False
    for y in range(h):
        for x in range(w):
            r, g, b, a = pix[x, y]
            if a > 0 and max(r, g, b) <= 40:
                mpx[x, y] = 255
                found = True
    if found:
        return mask, mask.getbbox()
    alpha = tpl.split()[-1]
    non_trans = alpha.point(lambda a: 255 if a > 0 else 0)
    bbox = non_trans.getbbox()
    if bbox:
        return non_trans, bbox
    full = Image.new("L", (w, h), 255)
    return full, (0, 0, w, h)


def compose_with_template(source: Image.Image, template: Image.Image, rotate_source_degrees: float = 0.0) -> Image.Image:
    tpl = template.convert("RGBA")
    mask, bbox = detect_template_mask_and_bbox(tpl)
    bx0, by0, bx1, by1 = bbox
    box_w = max(1, bx1 - bx0)
    box_h = max(1, by1 - by0)

    src = source.convert("RGBA")
    if rotate_source_degrees:
        src = src.rotate(rotate_source_degrees, resample=Image.BICUBIC, expand=True)
    scale = max(box_w / max(1, src.width), box_h / max(1, src.height))
    new_w = max(1, int(src.width * scale))
    new_h = max(1, int(src.height * scale))
    resized = src.resize((new_w, new_h), Image.LANCZOS)

    left = (new_w - box_w) // 2
    top = (new_h - box_h) // 2
    crop = resized.crop((left, top, left + box_w, top + box_h))

    fg = Image.new("RGBA", tpl.size, (0, 0, 0, 0))
    fg.paste(crop, (bx0, by0), crop)
    return Image.composite(fg, tpl, mask)


def _cover_crop(source: Image.Image, out_w: int, out_h: int) -> Image.Image:
    src = source.convert("RGBA")
    scale = max(out_w / max(1, src.width), out_h / max(1, src.height))
    nw = max(1, int(src.width * scale))
    nh = max(1, int(src.height * scale))
    resized = src.resize((nw, nh), Image.LANCZOS)
    left = max(0, (nw - out_w) // 2)
    top = max(0, (nh - out_h) // 2)
    return resized.crop((left, top, left + out_w, top + out_h))


def _cover_letterbox(source: Image.Image, out_w: int, out_h: int) -> Image.Image:
    src = source.convert("RGBA")
    scale = min(out_w / max(1, src.width), out_h / max(1, src.height))
    nw = max(1, int(src.width * scale))
    nh = max(1, int(src.height * scale))
    resized = src.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGBA", (out_w, out_h), (0, 0, 0, 255))
    x = (out_w - nw) // 2
    y = (out_h - nh) // 2
    canvas.paste(resized, (x, y), resized)
    return canvas


def _cover_square_resize(source: Image.Image, out_w: int, out_h: int) -> Image.Image:
    # Intentionally non-letterboxed: stretch to square first, then to target size.
    src = source.convert("RGBA")
    side = max(1, max(src.width, src.height))
    sq = src.resize((side, side), Image.LANCZOS)
    return sq.resize((out_w, out_h), Image.LANCZOS)


def _column_warp_trapezoid(source: Image.Image, bl_up_px: int, tr_down_px: int) -> Image.Image:
    src = source.convert("RGBA")
    w, h = src.size
    if w <= 1 or h <= 1:
        return src

    # Corner-constrained warp:
    # - top-left stays
    # - bottom-left moves up by bl_up_px
    # - top-right moves down by tr_down_px
    # - bottom-right stays
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    max_x = max(1, w - 1)
    for x in range(w):
        t = x / max_x
        top_y = tr_down_px * t
        bottom_y = (h - 1 - bl_up_px) * (1.0 - t) + (h - 1) * t
        # Use floor/ceil bounds to avoid subpixel rounding holes along edges.
        y0 = int(top_y // 1)
        y1 = int(-(-bottom_y // 1))  # ceil for positive values without importing math
        band_h = y1 - y0 + 1
        if band_h <= 0:
            continue
        col = src.crop((x, 0, x + 1, h)).resize((1, band_h), Image.BICUBIC)
        out.paste(col, (x, y0), col)
    return out


def compose_item_album_with_skew(source: Image.Image, template: Image.Image, target_w: int = 23, target_h: int = 32, bl_up_px: int = 9, tr_down_px: int = 9) -> Image.Image:
    tpl = template.convert("RGBA")
    mask, bbox = detect_template_mask_and_bbox(tpl)
    bx0, by0, bx1, by1 = bbox
    box_w = max(1, bx1 - bx0)
    box_h = max(1, by1 - by0)

    # For album icon only: no letterbox, square-resize first, then warp.
    base = _cover_square_resize(source, target_w, target_h)
    skewed = _column_warp_trapezoid(base, bl_up_px=bl_up_px, tr_down_px=tr_down_px)

    fg = Image.new("RGBA", tpl.size, (0, 0, 0, 0))
    px = bx0 + (box_w - skewed.width) // 2
    py = by0 + (box_h - skewed.height) // 2
    fg.paste(skewed, (px, py), skewed)
    return Image.composite(fg, tpl, mask)


def _red_key_dual_masks(mask_img: Image.Image) -> tuple[Image.Image, Image.Image]:
    src = mask_img.convert("RGBA")
    w, h = src.size
    main_mask = Image.new("L", (w, h), 0)
    trim_mask = Image.new("L", (w, h), 0)
    spx = src.load()
    mpx = main_mask.load()
    tpx = trim_mask.load()
    # Strict keys:
    # - Main: #FF00FF (magenta)
    # - Trim: #00FFFF (cyan)
    main_targets = [(0xFF, 0x00, 0xFF)]
    trim_targets = [(0x00, 0xFF, 0xFF)]
    main_tol = 24
    trim_tol = 24
    for y in range(h):
        for x in range(w):
            r, g, b, a = spx[x, y]
            # Ignore near-transparent antialias fringe noise in key masks.
            if a < 8:
                continue
            d_trim = min(abs(r - tr) + abs(g - tg) + abs(b - tb) for tr, tg, tb in trim_targets)
            d_main = min(abs(r - tr) + abs(g - tg) + abs(b - tb) for tr, tg, tb in main_targets)
            if d_trim <= trim_tol:
                tpx[x, y] = max(tpx[x, y], a)
            elif d_main <= main_tol:
                mpx[x, y] = max(mpx[x, y], a)
    return main_mask, trim_mask


def _letterbox_square_image(source: Image.Image) -> Image.Image:
    src = source.convert("RGBA")
    side = max(src.width, src.height)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 255))
    x = (side - src.width) // 2
    y = (side - src.height) // 2
    canvas.paste(src, (x, y), src)
    return canvas


def _clear_low_alpha_noise(img: Image.Image, alpha_threshold: int = 8) -> Image.Image:
    out = img.convert("RGBA")
    px = out.load()
    w, h = out.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a < alpha_threshold:
                px[x, y] = (0, 0, 0, 0)
    return out


def _apply_softlight_rgba(base_img: Image.Image, softlight_img: Image.Image) -> Image.Image:
    base = base_img.convert("RGBA")
    soft = softlight_img.convert("RGBA")
    if soft.size != base.size:
        soft = soft.resize(base.size, Image.LANCZOS)
    blended_rgb = ImageChops.soft_light(base.convert("RGB"), soft.convert("RGB"))
    br, bg, bb = blended_rgb.split()
    ba = base.split()[-1]
    blended = Image.merge("RGBA", (br, bg, bb, ba))
    sa = soft.split()[-1]
    if sa.getbbox():
        return Image.composite(blended, base, sa)
    return blended


def compose_record_with_mask_overlay(
    source: Image.Image,
    mask_img: Image.Image,
    overlay_img: Image.Image,
    pixelate_to: Optional[int] = None,
    trim_darken_factor: float = (214.0 / 255.0),
    softlight_img: Optional[Image.Image] = None,
) -> Image.Image:
    base = Image.new("RGBA", overlay_img.size, (0, 0, 0, 0))
    main_mask, trim_mask = _red_key_dual_masks(mask_img)
    region_mask = ImageChops.lighter(main_mask, trim_mask)
    bbox = region_mask.getbbox()
    if not bbox:
        return compose_with_template(source, overlay_img)

    bx0, by0, bx1, by1 = bbox
    tw, th = bx1 - bx0, by1 - by0
    src = source.convert("RGBA")
    if pixelate_to is not None and pixelate_to > 0:
        # Build a tiny square color sample, then scale up with nearest-neighbor
        # to keep blocky 6x6-like cover colors.
        sampled = _letterbox_square_image(src).resize((pixelate_to, pixelate_to), Image.LANCZOS)
        sampled = ImageEnhance.Color(sampled).enhance(1.2)
        sampled = ImageEnhance.Brightness(sampled).enhance(1.08)
        crop = sampled.resize((tw, th), Image.NEAREST)
    else:
        # Use cover+crop fitting (not contain) so non-square covers always fill
        # the label region without transparent bands.
        scale = max(tw / max(1, src.width), th / max(1, src.height))
        nw = max(1, int(src.width * scale))
        nh = max(1, int(src.height * scale))
        resized = src.resize((nw, nh), Image.LANCZOS)
        cx = max(0, (nw - tw) // 2)
        cy = max(0, (nh - th) // 2)
        crop = resized.crop((cx, cy, cx + tw, cy + th))

    layer = Image.new("RGBA", overlay_img.size, (0, 0, 0, 0))
    layer.paste(crop, (bx0, by0), crop)

    base = Image.composite(layer, base, main_mask)
    if trim_mask.getbbox():
        mul = max(0, min(255, int(round(255.0 * trim_darken_factor))))
        gray_mul = Image.new("RGBA", layer.size, (mul, mul, mul, 255))
        dark_layer = ImageChops.multiply(layer, gray_mul)
        base = Image.composite(dark_layer, base, trim_mask)
    cleaned_overlay = _clear_low_alpha_noise(overlay_img, alpha_threshold=8)
    base.alpha_composite(cleaned_overlay)
    if softlight_img is not None:
        base = _apply_softlight_rgba(base, softlight_img)
    return base


def build_cassette(args, on_track: Optional[Callable[[BuildTrackEvent], None]] = None) -> Path:
    oggs = find_oggs(args.audio_dir)
    if not oggs:
        raise SystemExit(f"No .ogg files found in: {args.audio_dir}")
    if args.custom_cassettes and (not getattr(args, "cover", None) or not args.cover.is_file()) and not getattr(args, "song_covers", None):
        raise SystemExit(f"Cover not found: {args.cover}")

    if args.seed is not None:
        random.seed(args.seed)

    paths = build_mod_layout(args.out_dir, args.mod_id)
    write_mod_info(
        paths["mod_base"],
        paths["v42"],
        args.name,
        args.mod_id,
        getattr(args, "parent_mod_id", "TrueMoozic"),
        getattr(args, "author", "local-builder"),
    )
    write_workshop(
        paths["root"],
        args.name,
        workshop_song_lines(oggs, getattr(args, "song_b_sides", {}) or {}),
    )
    write_workshop_images(
        paths,
        args.workshop_cover,
        args.name,
        add_name_overlay=bool(getattr(args, "add_name_to_poster", True)),
    )
    if bool(getattr(args, "standalone_bundle", False)):
        bundle_standalone_redundancy(paths, args.assets_root)
        write_standalone_music_defs(paths["lua_shared"])

    sounds = [f"module {args.mod_id}", "{"]  # script sounds
    items = [f"module {args.mod_id}", "{", "\timports", "\t{", "\t\tBase", "\t}", ""]
    models = [f"module {args.mod_id}", "{", "\timports", "\t{", "\t\tBase", "\t}", ""]
    musicdefs = ['require "TCMusicDefenitions"', ""]
    cassette_assignments: list[tuple[str, int]] = []
    cover_cache: dict[Path, Image.Image] = {}

    t_item_uv = None
    t_item_mask = None
    t_item_overlay = None
    t_world_uv = None
    t_world_mask = None
    t_world_overlay = None

    if args.custom_cassettes:
        tpl_base = args.assets_root / "template"
        if not tpl_base.exists():
            tpl_base = args.assets_root / "templatte"
        tpl_dir = tpl_base / "cassette" if (tpl_base / "cassette").exists() else tpl_base
        t_item_uv = Image.open(tpl_dir / "item_TMCassette_uv.png")
        t_world_uv = Image.open(tpl_dir / "TMCassette_uv.png")
        t_item_mask = Image.open(tpl_dir / "item_TMCassette_Mask.png") if (tpl_dir / "item_TMCassette_Mask.png").exists() else None
        t_item_overlay = Image.open(tpl_dir / "item_TMCassette_Overlay.png") if (tpl_dir / "item_TMCassette_Overlay.png").exists() else None
        t_world_mask = Image.open(tpl_dir / "TMCassette_Mask.png") if (tpl_dir / "TMCassette_Mask.png").exists() else None
        t_world_overlay = Image.open(tpl_dir / "TMCassette_Overlay.png") if (tpl_dir / "TMCassette_Overlay.png").exists() else None

    song_b_sides = getattr(args, "song_b_sides", {}) or {}
    song_use_random_cassette = set(getattr(args, "song_use_random_cassette", []) or [])
    total_tracks = len(oggs)
    for idx, ogg in enumerate(oggs, start=1):
        iid = sanitize_id(ogg.name)
        disp = display_name_from_file(ogg)
        icon = ""
        model_name = ""
        thumb_path: Optional[Path] = None
        side_b_path = None
        use_random_for_song = (not args.custom_cassettes) or (ogg.name in song_use_random_cassette)
        raw_b = song_b_sides.get(ogg.name)
        if raw_b:
            cand = Path(raw_b)
            if cand.exists() and cand.is_file():
                side_b_path = cand.resolve()
                shutil.copy2(side_b_path, paths["sound"] / side_b_path.name)
        if not use_random_for_song:
            cover_path = args.cover
            if getattr(args, "song_covers", None):
                cover_path = args.song_covers.get(ogg.name, args.cover)
            thumb_path = cover_path
            if cover_path not in cover_cache:
                cover_cache[cover_path] = Image.open(cover_path).convert("RGBA")
            cover = cover_cache[cover_path]
            _save_hr_cover(cover_path, paths["hr"] / f"Cassette_{iid}.png")

            icon = f"TMCassette_{iid}"
            model_name = f"TMCassette_{iid}"
            cassette_assignments.append((disp, -1))

            item_png = f"item_TMCassette_{iid}.png"
            world_png = f"TMCassette_{iid}.png"

            if t_item_mask is not None and t_item_overlay is not None:
                compose_record_with_mask_overlay(
                    cover,
                    t_item_mask,
                    t_item_overlay,
                    trim_darken_factor=0.5,
                ).save(paths["textures"] / item_png)
            else:
                compose_with_template(cover, t_item_uv).save(paths["textures"] / item_png)

            if t_world_mask is not None and t_world_overlay is not None:
                compose_record_with_mask_overlay(
                    cover,
                    t_world_mask,
                    t_world_overlay,
                    trim_darken_factor=0.5,
                ).save(paths["wtextures"] / world_png)
            else:
                compose_with_template(cover, t_world_uv).save(paths["wtextures"] / world_png)

            generated_thumb = paths["wtextures"] / world_png
            if generated_thumb.exists():
                thumb_path = generated_thumb

            models.extend(
                [
                    f"\tmodel {model_name}",
                    "\t{",
                    "\t\tmesh = WorldItems/TCTape,",
                    f"\t\ttexture = WorldItems/TMCassette_{iid},",
                    "\t\tscale = 0.0005,",
                    "\t}",
                    "",
                ]
            )
        else:
            tape_n = random.randint(CASSETTE_VARIANT_MIN, CASSETTE_VARIANT_MAX)
            icon = f"TCTape{tape_n}"
            model_name = icon
            cassette_assignments.append((disp, tape_n))
            candidate_thumb = args.assets_root / "textures" / "WorldItems" / "Cassette" / f"TCTape{tape_n}.png"
            if candidate_thumb.exists():
                thumb_path = candidate_thumb

        shutil.copy2(ogg, paths["sound"] / ogg.name)

        sounds.extend(
            [
                f"\tsound Cassette{iid}",
                "\t{",
                "\t\tcategory = True Music,",
                "\t\tmaster = Ambient,",
                "\t\tclip",
                "\t\t{",
                f"\t\t\tfile = media/sound/{args.mod_id}/{ogg.name},",
                "\t\t\tdistanceMax = 75,",
                "\t\t}",
                "\t}",
            ]
        )

        items.extend(
            [
                f"\titem Cassette{iid}",
                "\t{",
                "\t\tItemType\t\t=\tbase:normal,",
                "\t\tDisplayCategory = Entertainment,",
                "\t\tWeight\t\t\t=\t0.02,",
                f"\t\tIcon\t\t\t=\t{icon},",
                f"\t\tDisplayName\t\t=\tCassette {disp} (A-Side),",
                f"\t\tWorldStaticModel = {args.mod_id}.{model_name},",
                "\t\tCanSpawn\t\t=\ttrue,",
                "\t}",
                "",
            ]
        )

        musicdefs.append(f'GlobalMusic["Cassette{iid}"] = "{CASSETTE_TILE}"')
        if side_b_path is not None:
            sounds.extend(
                [
                    f"\tsound Cassette{iid}SideB",
                    "\t{",
                    "\t\tcategory = True Music,",
                    "\t\tmaster = Ambient,",
                    "\t\tclip",
                    "\t\t{",
                    f"\t\t\tfile = media/sound/{args.mod_id}/{side_b_path.name},",
                    "\t\t\tdistanceMax = 75,",
                    "\t\t}",
                    "\t}",
                ]
            )
            items.extend(
                [
                    f"\titem Cassette{iid}SideB",
                    "\t{",
                    "\t\tItemType\t\t=\tbase:normal,",
                    "\t\tDisplayCategory = Entertainment,",
                    "\t\tWeight\t\t\t=\t0.02,",
                    f"\t\tIcon\t\t\t=\t{icon},",
                    f"\t\tDisplayName\t\t=\tCassette {disp} (B-Side),",
                    f"\t\tWorldStaticModel = {args.mod_id}.{model_name},",
                    "\t\tCanSpawn\t\t=\ttrue,",
                    "\t}",
                    "",
                ]
            )
            musicdefs.append(f'GlobalMusic["Cassette{iid}SideB"] = "{CASSETTE_TILE}"')

        if on_track:
            on_track(BuildTrackEvent(index=idx, total=total_tracks, title=disp, thumbnail=thumb_path))

    sounds.append("}")
    items.append("}")
    if not args.custom_cassettes:
        for i in range(CASSETTE_VARIANT_MIN, CASSETTE_VARIANT_MAX + 1):
            models.extend(
                [
                    f"\tmodel TCTape{i}",
                    "\t{",
                    "\t\tmesh = WorldItems/TCTape,",
                    f"\t\ttexture = WorldItems/TCTape{i},",
                    "\t\tscale = 0.0005,",
                    "\t}",
                    "",
                ]
            )
    models.append("}")

    write(paths["scripts"] / f"{args.mod_id}_Sounds.txt", "\n".join(sounds))
    write(paths["scripts"] / f"{args.mod_id}_Items.txt", "\n".join(items))
    write(paths["scripts"] / f"{args.mod_id}_Models.txt", "\n".join(models))
    write(paths["lua_shared"] / f"{args.mod_id}_MusicDefs.lua", "\n".join(musicdefs) + "\n")

    if cassette_assignments and not args.custom_cassettes:
        print("")
        print("Random Cassette Assignments")
        print("---------------------------")
        for idx, (name, tape_n) in enumerate(cassette_assignments, start=1):
            print(f"{idx:>2}. {name}: tape {tape_n}")
    if cassette_assignments and args.custom_cassettes:
        print("")
        print("Custom Cassette Covers")
        print("----------------------")
        for idx, (name, _) in enumerate(cassette_assignments, start=1):
            print(f"{idx:>2}. {name}: generated from selected cover")

    for im in cover_cache.values():
        try:
            im.close()
        except Exception:
            pass
    for im in (t_item_uv, t_item_mask, t_item_overlay, t_world_uv, t_world_mask, t_world_overlay):
        if im is None:
            continue
        try:
            im.close()
        except Exception:
            pass

    prune_empty_dirs(paths["media"])
    return paths["root"]


def build_vinyl(args, on_track: Optional[Callable[[BuildTrackEvent], None]] = None) -> Path:
    oggs = find_oggs(args.audio_dir)
    if not oggs:
        raise SystemExit(f"No .ogg files found in: {args.audio_dir}")
    if args.custom_vinyls and (not getattr(args, "cover", None) or not args.cover.is_file()):
        raise SystemExit(f"Cover not found: {args.cover}")

    paths = build_mod_layout(args.out_dir, args.mod_id)
    write_mod_info(
        paths["mod_base"],
        paths["v42"],
        args.name,
        args.mod_id,
        getattr(args, "parent_mod_id", "TrueMoozic"),
        getattr(args, "author", "local-builder"),
    )
    write_workshop(
        paths["root"],
        args.name,
        workshop_song_lines(oggs, getattr(args, "song_b_sides", {}) or {}),
    )
    write_workshop_images(
        paths,
        args.workshop_cover,
        args.name,
        add_name_overlay=bool(getattr(args, "add_name_to_poster", True)),
    )
    if bool(getattr(args, "standalone_bundle", False)):
        bundle_standalone_redundancy(paths, args.assets_root)
        write_standalone_music_defs(paths["lua_shared"])

    module_name = args.mod_id
    vinyl_art_placement = _parse_vinyl_art_placement(getattr(args, "vinyl_art_placement", "inside"), default="inside")
    song_vinyl_art_placement = getattr(args, "song_vinyl_art_placement", {}) or {}
    song_use_random_vinyl = set(getattr(args, "song_use_random_vinyl", []) or [])
    cover_cache: dict[Path, Image.Image] = {}

    tpl_base = args.assets_root / "template"
    if not tpl_base.exists():
        tpl_base = args.assets_root / "templatte"
    tpl_dir = tpl_base / "vinyl" if (tpl_base / "vinyl").exists() else tpl_base

    t_item_album = None
    t_item_record = None
    t_item_record_inside_mask = None
    t_item_record_inside_overlay = None
    t_item_record_outside_mask = None
    t_item_record_outside_overlay = None
    t_album = None
    t_record = None
    t_record_inside_mask = None
    t_record_inside_overlay = None
    t_record_outside_mask = None
    t_record_outside_overlay = None
    t_item_record_outside_softlight = None
    t_record_outside_softlight = None

    needs_random_pool = (not args.custom_vinyls) or bool(song_use_random_vinyl)

    if args.custom_vinyls:
        item_album_new = tpl_dir / "item_TMVinylalbum_uv_new.png"
        t_item_album = Image.open(item_album_new if item_album_new.exists() else (tpl_dir / "item_TMVinylalbum_uv.png"))
        t_item_record = Image.open(tpl_dir / "item_TMVinylrecord_uv.png")
        t_album = Image.open(tpl_dir / "TMVinylalbum_uv.png")
        t_record = Image.open(tpl_dir / "TMVinylrecord_uv.png")
        t_item_record_inside_mask = Image.open(tpl_dir / "item_TMVinylrecord_Mask.png") if (tpl_dir / "item_TMVinylrecord_Mask.png").exists() else None
        t_item_record_inside_overlay = Image.open(tpl_dir / "item_TMVinylrecord_Overlay.png") if (tpl_dir / "item_TMVinylrecord_Overlay.png").exists() else None
        t_record_inside_mask = Image.open(tpl_dir / "TMVinylrecord_Mask.png") if (tpl_dir / "TMVinylrecord_Mask.png").exists() else None
        t_record_inside_overlay = Image.open(tpl_dir / "TMVinylrecord_Overlay.png") if (tpl_dir / "TMVinylrecord_Overlay.png").exists() else None

        world_outer_mask = tpl_dir / "TMVinylrecord_Outer_Mask.png"
        world_outer_overlay = tpl_dir / "TMVinylrecord_Outer_Overlay.png"
        world_outer_softlight = tpl_dir / "TMVinylrecord_Outer_SoftLight.png"
        if world_outer_mask.exists() and world_outer_overlay.exists():
            t_record_outside_mask = Image.open(world_outer_mask)
            t_record_outside_overlay = Image.open(world_outer_overlay)
            t_record_outside_softlight = Image.open(world_outer_softlight) if world_outer_softlight.exists() else None

        item_outer_mask = tpl_dir / "item_TMVinylrecord_Outer_Mask.png"
        item_outer_overlay = tpl_dir / "item_TMVinylrecord_Outer_Overlay.png"
        item_outer_softlight = tpl_dir / "item_TMVinylrecord_Outer_SoftLight.png"
        if item_outer_mask.exists() and item_outer_overlay.exists():
            t_item_record_outside_mask = Image.open(item_outer_mask)
            t_item_record_outside_overlay = Image.open(item_outer_overlay)
            t_item_record_outside_softlight = Image.open(item_outer_softlight) if item_outer_softlight.exists() else t_record_outside_softlight
    if needs_random_pool:
        assets_tex = args.assets_root / "textures"
        rec_icon = _collect_numbered_variants(assets_tex / "Icons" / "Vinyl", "Item_TCVinylrecord")
        rec_world = _collect_numbered_variants(assets_tex / "WorldItems" / "Vinyl", "TMVinylrecord")
        record_variants = sorted(set(rec_icon).intersection(rec_world))
        alb_icon = _collect_numbered_variants(assets_tex / "Icons" / "Vinyl" / "Album", "Item_TCVinylrecord")
        alb_world = _collect_numbered_variants(assets_tex / "WorldItems" / "Vinyl" / "Album", "Item_TCVinylrecord")
        album_variants = sorted(set(alb_icon).intersection(alb_world))
        if not record_variants:
            raise SystemExit("No random vinyl-record variants found in assets/textures Icons+WorldItems Vinyl folders.")
        if not album_variants:
            raise SystemExit("No random vinyl-album variants found in assets/textures Icons+WorldItems Vinyl/Album folders.")
        _validate_contiguous_variant_pool(
            "Random vinyl record variant pool",
            record_variants,
            VINYL_RECORD_VARIANT_MIN,
            VINYL_RECORD_VARIANT_MAX,
        )
        _validate_contiguous_variant_pool(
            "Random vinyl album variant pool",
            album_variants,
            VINYL_ALBUM_VARIANT_MIN,
            VINYL_ALBUM_VARIANT_MAX,
        )
        print(
            f"Random vinyl pool detected: records {VINYL_RECORD_VARIANT_MIN}..{VINYL_RECORD_VARIANT_MAX} "
            f"({len(record_variants)}), albums {VINYL_ALBUM_VARIANT_MIN}..{VINYL_ALBUM_VARIANT_MAX} "
            f"({len(album_variants)})"
        )

    sounds = [f"module {module_name}", "{", "\timports", "\t{", "\t\tBase", "\t}", ""]
    items = [f"module {module_name}", "{", "\timports", "\t{", "\t\tBase", "\t}", ""]
    models = [f"module {module_name}", "{", "\timports", "\t{", "\t\tBase", "\t}", ""]
    musicdefs = ['require "TCMusicDefenitions"', ""]
    random_assignments: list[tuple[str, int, int]] = []

    song_b_sides = getattr(args, "song_b_sides", {}) or {}
    total_tracks = len(oggs)
    for idx, ogg in enumerate(oggs, start=1):
        iid = sanitize_id(ogg.name)
        disp = display_name_from_file(ogg)
        thumb_path: Optional[Path] = None
        use_random_for_song = (not args.custom_vinyls) or (ogg.name in song_use_random_vinyl)
        side_b_path = None
        raw_b = song_b_sides.get(ogg.name)
        if raw_b:
            cand = Path(raw_b)
            if cand.exists() and cand.is_file():
                side_b_path = cand.resolve()
                shutil.copy2(side_b_path, paths["sound"] / side_b_path.name)

        if not use_random_for_song:
            cover_path = args.cover
            if getattr(args, "song_covers", None):
                cover_path = args.song_covers.get(ogg.name, args.cover)
            thumb_path = cover_path
            if cover_path not in cover_cache:
                cover_cache[cover_path] = Image.open(cover_path).convert("RGBA")
            cover = cover_cache[cover_path]
            _save_hr_cover(cover_path, paths["hr"] / f"VinylAlbum_{iid}.png")
        else:
            record_n = random.choice(record_variants)
            album_n = random.choice(album_variants)
            random_assignments.append((disp, record_n, album_n))
            # Builder visualization: prefer album-roll art for random vinyl rows
            # so preview tiles show the more distinct 1..12 covers.
            candidate_thumb = args.assets_root / "textures" / "WorldItems" / "Vinyl" / f"TCVinylrecord{album_n}.png"
            if not candidate_thumb.exists():
                candidate_thumb = args.assets_root / "textures" / "WorldItems" / "Vinyl" / f"TMVinylrecord{record_n}.png"
            if candidate_thumb.exists():
                thumb_path = candidate_thumb

        shutil.copy2(ogg, paths["sound"] / ogg.name)

        item_album_png = f"item_TMVinylalbum_{iid}.png"
        item_record_png = f"item_TMVinylrecord_{iid}.png"
        album_png = f"TMVinylalbum_{iid}.png"
        record_png = f"TMVinylrecord_{iid}.png"
        item_album_icon = f"TMVinylalbum_{iid}"
        item_record_icon = f"TMVinylrecord_{iid}"
        album_texture_id = f"WorldItems/{album_png[:-4]}"
        record_texture_id = f"WorldItems/{record_png[:-4]}"

        if not use_random_for_song:
            per_song_placement = _parse_vinyl_art_placement(
                song_vinyl_art_placement.get(ogg.name, vinyl_art_placement),
                default=vinyl_art_placement,
            )
            compose_item_album_with_skew(
                cover,
                t_item_album,
                target_w=23,
                target_h=32,
                bl_up_px=9,
                tr_down_px=9,
            ).save(paths["textures"] / item_album_png)
            item_mask = t_item_record_inside_mask
            item_overlay = t_item_record_inside_overlay
            world_mask = t_record_inside_mask
            world_overlay = t_record_inside_overlay
            item_softlight = None
            world_softlight = None

            if per_song_placement == "outside":
                if t_record_outside_mask is None or t_record_outside_overlay is None:
                    raise SystemExit(
                        "Outside vinyl placement selected, but required templates are missing: "
                        "TMVinylrecord_Outer_Mask.png and/or TMVinylrecord_Outer_Overlay.png"
                    )
                world_mask = t_record_outside_mask
                world_overlay = t_record_outside_overlay
                world_softlight = t_record_outside_softlight
                if t_item_record_outside_mask is not None and t_item_record_outside_overlay is not None:
                    item_mask = t_item_record_outside_mask
                    item_overlay = t_item_record_outside_overlay
                    item_softlight = t_item_record_outside_softlight
                else:
                    item_mask = world_mask
                    item_overlay = world_overlay
                    item_softlight = world_softlight

            if item_mask is not None and item_overlay is not None:
                compose_record_with_mask_overlay(
                    cover,
                    item_mask,
                    item_overlay,
                    pixelate_to=6,
                    softlight_img=item_softlight,
                ).resize((32, 32), Image.LANCZOS).save(paths["textures"] / item_record_png)
            else:
                compose_with_template(cover, t_item_record).resize((32, 32), Image.LANCZOS).save(paths["textures"] / item_record_png)
            compose_with_template(cover, t_album).resize((150, 150), Image.LANCZOS).save(paths["wtextures"] / album_png)
            if world_mask is not None and world_overlay is not None:
                compose_record_with_mask_overlay(
                    cover,
                    world_mask,
                    world_overlay,
                    softlight_img=world_softlight,
                ).resize((150, 150), Image.LANCZOS).save(paths["wtextures"] / record_png)
            else:
                compose_with_template(cover, t_record).resize((150, 150), Image.LANCZOS).save(paths["wtextures"] / record_png)

            generated_thumb = paths["wtextures"] / record_png
            if generated_thumb.exists():
                thumb_path = generated_thumb
        else:
            item_album_icon = f"TCAlbum{album_n}"
            item_record_icon = f"TCVinylrecord{record_n}"
            album_texture_id = f"WorldItems/Vinyl/TCVinylrecord{album_n}"
            record_texture_id = f"WorldItems/Vinyl/TMVinylrecord{record_n}"

        models.extend(
            [
                f"\tmodel TMVinylrecord_{iid}",
                "\t{",
                "\t\tmesh = WorldItems/TMVinylrecord,",
                f"\t\ttexture = {record_texture_id},",
                "\t\tscale = 0.12,",
                "\t}",
                "",
                f"\tmodel TMVinylalbum_{iid}",
                "\t{",
                "\t\tmesh = WorldItems/TMVinylalbum,",
                f"\t\ttexture = {album_texture_id},",
                "\t\tscale = 0.12,",
                "\t}",
                "",
            ]
        )

        items.extend(
            [
                f"\titem VinylAlbum{iid}",
                "\t{",
                "\t\tItemType\t\t=\tbase:normal,",
                "\t\tDisplayCategory = Entertainment,",
                "\t\tWeight\t\t\t=\t0.05,",
                f"\t\tIcon\t\t\t=\t{item_album_icon},",
                f"\t\tDisplayName\t\t=\tVinyl Album {disp},",
                f"\t\tWorldStaticModel = {module_name}.TMVinylalbum_{iid},",
                "\t\tCanSpawn\t\t=\ttrue,",
                "\t}",
                "",
                f"\titem Vinyl{iid}",
                "\t{",
                "\t\tItemType\t\t=\tbase:normal,",
                "\t\tDisplayCategory = Entertainment,",
                "\t\tWeight\t\t\t=\t0.02,",
                f"\t\tIcon\t\t\t=\t{item_record_icon},",
                f"\t\tDisplayName\t\t=\tVinyl {disp} (A-Side),",
                f"\t\tWorldStaticModel = {module_name}.TMVinylrecord_{iid},",
                "\t\tCanSpawn\t\t=\ttrue,",
                "\t}",
                "",
            ]
        )

        sounds.extend(
            [
                f"\tsound Vinyl{iid}",
                "\t{",
                "\t\tcategory = True Music,",
                "\t\tmaster = Ambient,",
                "\t\tclip",
                "\t\t{",
                f"\t\t\tfile = media/sound/{args.mod_id}/{ogg.name},",
                "\t\t\tdistanceMax = 75,",
                "\t\t}",
                "\t}",
            ]
        )

        musicdefs.append(f'GlobalMusic["Vinyl{iid}"] = "{VINYL_TILE}"')
        musicdefs.append(f'GlobalMusic["VinylAlbum{iid}"] = "{VINYL_TILE}"')
        if side_b_path is not None:
            sounds.extend(
                [
                    f"\tsound Vinyl{iid}SideB",
                    "\t{",
                    "\t\tcategory = True Music,",
                    "\t\tmaster = Ambient,",
                    "\t\tclip",
                    "\t\t{",
                    f"\t\t\tfile = media/sound/{args.mod_id}/{side_b_path.name},",
                    "\t\t\tdistanceMax = 75,",
                    "\t\t}",
                    "\t}",
                ]
            )
            items.extend(
                [
                    f"\titem Vinyl{iid}SideB",
                    "\t{",
                    "\t\tItemType\t\t=\tbase:normal,",
                    "\t\tDisplayCategory = Entertainment,",
                    "\t\tWeight\t\t\t=\t0.02,",
                    f"\t\tIcon\t\t\t=\t{item_record_icon},",
                    f"\t\tDisplayName\t\t=\tVinyl {disp} (B-Side),",
                    f"\t\tWorldStaticModel = {module_name}.TMVinylrecord_{iid},",
                    "\t\tCanSpawn\t\t=\ttrue,",
                    "\t}",
                    "",
                ]
            )
            musicdefs.append(f'GlobalMusic["Vinyl{iid}SideB"] = "{VINYL_TILE}"')

        if on_track:
            on_track(BuildTrackEvent(index=idx, total=total_tracks, title=disp, thumbnail=thumb_path))

    sounds.append("}")
    items.append("}")
    models.append("}")

    write(paths["scripts"] / f"{args.mod_id}_Vinyl_Sounds.txt", "\n".join(sounds))
    write(paths["scripts"] / f"{args.mod_id}_Vinyl_Items.txt", "\n".join(items))
    write(paths["scripts"] / f"{args.mod_id}_Vinyl_Models.txt", "\n".join(models))
    write(paths["lua_shared"] / f"{args.mod_id}_MusicDefs_Vinyl.lua", "\n".join(musicdefs) + "\n")

    if random_assignments:
        print("")
        print("Random Vinyl Assignments")
        print("------------------------")
        for idx, (name, rec_n, alb_n) in enumerate(random_assignments, start=1):
            print(f"{idx:>2}. {name}: record {rec_n}, album {alb_n}")
    if args.custom_vinyls and any(name not in song_use_random_vinyl for name in [p.name for p in oggs]):
        print("")
        print(f"Custom Vinyl Art Placement: {vinyl_art_placement}")

    for im in cover_cache.values():
        try:
            im.close()
        except Exception:
            pass
    for im in (
        t_item_album,
        t_item_record,
        t_item_record_inside_mask,
        t_item_record_inside_overlay,
        t_item_record_outside_mask,
        t_item_record_outside_overlay,
        t_item_record_outside_softlight,
        t_album,
        t_record,
        t_record_inside_mask,
        t_record_inside_overlay,
        t_record_outside_mask,
        t_record_outside_overlay,
        t_record_outside_softlight,
    ):
        if im is None:
            continue
        try:
            im.close()
        except Exception:
            pass

    prune_empty_dirs(paths["media"])
    return paths["root"]


def default_assets_root() -> Path:
    return bundled_resource_root() / "assets"


def default_audio_root() -> Path:
    return app_root() / AUDIO_FOLDER_NAME


def default_output_root() -> Path:
    return app_root() / "OUTPUT"


def default_cover_root() -> Path:
    return app_root() / "Put your images here"


def build_mod_from_config(config: dict, on_track: Optional[Callable[[BuildTrackEvent], None]] = None) -> Path:
    mode = (config.get("mode") or "cassette").lower()
    args = argparse.Namespace(**config)
    args.name = getattr(args, "name", None) or args.mod_id
    args.author = str(getattr(args, "author", "local-builder") or "local-builder").strip()
    args.audio_dir = Path(args.audio_dir).resolve()
    args.out_dir = Path(args.out_dir).resolve()
    args.assets_root = Path(args.assets_root).resolve()
    args.workshop_cover = Path(args.workshop_cover).resolve() if getattr(args, "workshop_cover", None) else None
    args.add_name_to_poster = bool(getattr(args, "add_name_to_poster", True))
    if getattr(args, "cover", None):
        args.cover = Path(args.cover).resolve()
    if getattr(args, "song_covers", None):
        args.song_covers = {k: Path(v).resolve() for k, v in args.song_covers.items()}
    args.parent_mod_id = str(getattr(args, "parent_mod_id", "TrueMoozic") or "").strip()
    args.standalone_bundle = bool(getattr(args, "standalone_bundle", False))

    args.audio_dir = audio_cache_root(args.audio_dir)
    if mode == "vinyl":
        args.custom_vinyls = bool(getattr(args, "custom_vinyls", True))
        args.vinyl_art_placement = _parse_vinyl_art_placement(getattr(args, "vinyl_art_placement", "inside"), default="inside")
        return build_vinyl(args, on_track=on_track)

    args.custom_cassettes = bool(getattr(args, "custom_cassettes", False))
    args.seed = getattr(args, "seed", None)
    return build_cassette(args, on_track=on_track)


def build_mixed_from_config(config: dict, on_track: Optional[Callable[[BuildTrackEvent], None]] = None) -> Path:
    base_audio_dir = Path(config.get("audio_dir", default_audio_root())).resolve()
    workshop_cover_raw = config.get("workshop_cover")
    fallback_cover = Path(workshop_cover_raw).resolve() if workshop_cover_raw else (default_poster_root() / "poster.png").resolve()
    if not fallback_cover.exists():
        raise SystemExit(f"Fallback cover not found: {fallback_cover}")

    all_oggs = collect_available_oggs(base_audio_dir)
    ogg_by_name = {p.name: p for p in all_oggs}

    track_modes = config.get("track_modes") or {}
    cassette_oggs: list[Path] = []
    vinyl_oggs: list[Path] = []
    cassette_covers: dict[str, Path] = {}
    vinyl_covers: dict[str, Path] = {}
    cassette_b_sides: dict[str, Path] = {}
    cassette_use_random: set[str] = set()
    vinyl_b_sides: dict[str, Path] = {}
    vinyl_use_random: set[str] = set()
    vinyl_placements: dict[str, str] = {}

    for ogg_name, mode_cfg in track_modes.items():
        ogg_path = ogg_by_name.get(ogg_name)
        if ogg_path is None:
            continue

        include_cassette = bool(mode_cfg.get("cassette", False))
        include_vinyl = bool(mode_cfg.get("vinyl", False))
        cover = mode_cfg.get("cover")
        b_side = mode_cfg.get("b_side")
        placement = _parse_vinyl_art_placement(mode_cfg.get("vinyl_art_placement", "inside"), default="inside")

        if include_cassette:
            cassette_oggs.append(ogg_path)
            if cover:
                cassette_covers[ogg_name] = Path(cover)
            else:
                cassette_use_random.add(ogg_name)
            if b_side:
                cassette_b_sides[ogg_name] = Path(b_side)
        if include_vinyl:
            vinyl_oggs.append(ogg_path)
            if cover:
                vinyl_covers[ogg_name] = Path(cover)
            else:
                vinyl_use_random.add(ogg_name)
            if b_side:
                vinyl_b_sides[ogg_name] = Path(b_side)
            vinyl_placements[ogg_name] = placement

    if not cassette_oggs and not vinyl_oggs:
        raise SystemExit("No songs selected for cassette or vinyl build.")

    total_tracks = len(cassette_oggs) + len(vinyl_oggs)
    emitted = 0

    def make_wrapped_cb(offset: int, mode_label: str):
        def _wrapped(event: BuildTrackEvent):
            if not on_track:
                return
            on_track(
                BuildTrackEvent(
                    index=offset + event.index,
                    total=total_tracks,
                    title=f"[{mode_label}] {event.title}",
                    thumbnail=event.thumbnail,
                )
            )

        return _wrapped

    output_path: Path | None = None
    with tempfile.TemporaryDirectory(prefix="smb_mixed_") as tmp_root:
        tmp_root_path = Path(tmp_root)

        if cassette_oggs:
            cassette_cache = tmp_root_path / "cassette" / "_ogg"
            cassette_cache.mkdir(parents=True, exist_ok=True)
            for p in cassette_oggs:
                shutil.copy2(p, cassette_cache / p.name)

            cassette_cfg = dict(config)
            cassette_cfg.update(
                {
                    "mode": "cassette",
                    "audio_dir": cassette_cache,
                    "cover": fallback_cover,
                    "song_covers": cassette_covers,
                    "song_b_sides": cassette_b_sides,
                    "song_use_random_cassette": sorted(cassette_use_random),
                    "custom_cassettes": bool(cassette_covers),
                }
            )
            output_path = build_mod_from_config(cassette_cfg, on_track=make_wrapped_cb(emitted, "cassette"))
            emitted += len(cassette_oggs)

        if vinyl_oggs:
            vinyl_cache = tmp_root_path / "vinyl" / "_ogg"
            vinyl_cache.mkdir(parents=True, exist_ok=True)
            for p in vinyl_oggs:
                shutil.copy2(p, vinyl_cache / p.name)

            vinyl_cfg = dict(config)
            vinyl_cfg.update(
                {
                    "mode": "vinyl",
                    "audio_dir": vinyl_cache,
                    "cover": fallback_cover,
                    "song_covers": vinyl_covers,
                    "song_b_sides": vinyl_b_sides,
                    "song_use_random_vinyl": sorted(vinyl_use_random),
                    "song_vinyl_art_placement": vinyl_placements,
                    "custom_vinyls": bool(vinyl_covers),
                }
            )
            output_path = build_mod_from_config(vinyl_cfg, on_track=make_wrapped_cb(emitted, "vinyl"))

    if output_path is None:
        raise SystemExit("Build failed to produce output path.")
    return output_path


def _prompt_path(prompt: str, default: Path | None = None) -> Path:
    suffix = f" [{default}]" if default else ""
    while True:
        raw = input(f"{prompt}{suffix}: ").strip()
        if not raw and default is not None:
            return default
        if raw:
            return Path(raw)
        print("Value required.")


def _prompt_text(prompt: str, default: str | None = None, required: bool = True) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        raw = input(f"{prompt}{suffix}: ").strip()
        if not raw and default is not None:
            return default
        if raw:
            return raw
        if not required:
            return ""
        print("Value required.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simple True Moozic child-mod builder")
    sub = parser.add_subparsers(dest="mode", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--mod-id", required=True, help="Generated mod id/folder name")
    common.add_argument("--name", help="Display name (default: mod id)")
    common.add_argument("--author", default="local-builder", help="Author field for mod.info (comma-separated supported)")
    common.add_argument("--audio-dir", type=Path, default=default_audio_root(), help="Folder containing .ogg files")
    common.add_argument("--out-dir", type=Path, default=default_output_root(), help="Output folder")
    common.add_argument("--assets-root", type=Path, default=default_assets_root(), help="Path to builder assets folder")
    common.add_argument("--parent-mod-id", default="TrueMoozic", help="Optional required parent mod id; leave blank for standalone")
    common.add_argument("--convert-audio", action="store_true", help="Convert supported audio into _ogg cache before build")
    common.add_argument("--force-rebuild-ogg", action="store_true", help="Force rebuild all cached OGG files")

    c = sub.add_parser("cassette", parents=[common], help="Build cassette pack")
    c.add_argument("--seed", type=int, help="Random seed for cassette texture picks")
    c.add_argument("--cover", type=Path, help="Cover image (PNG/JPG) for custom cassette mode")
    c.add_argument("--custom-cassettes", choices=("y", "n", "yes", "no"), default="n", help="y=build custom per-song cassette textures, n=use random built-in cassette variants")

    v = sub.add_parser("vinyl", parents=[common], help="Build vinyl pack")
    v.add_argument("--cover", type=Path, help="Cover image (PNG/JPG) for custom vinyl mode")
    v.add_argument("--custom-vinyls", choices=("y", "n", "yes", "no"), default="y", help="y=build custom per-song vinyl textures, n=use random built-in vinyl variants")
    v.add_argument(
        "--vinyl-art-placement",
        default="inside",
        help="Custom vinyl placement mode: inside or outside",
    )

    if len(sys.argv) > 1:
        ns = parser.parse_args()
        if getattr(ns, "mode", None) == "cassette":
            ns.custom_cassettes = _parse_yes_no(ns.custom_cassettes, default=False)
        if getattr(ns, "mode", None) == "vinyl":
            ns.custom_vinyls = _parse_yes_no(ns.custom_vinyls, default=True)
            ns.vinyl_art_placement = _parse_vinyl_art_placement(ns.vinyl_art_placement, default="inside")
        return ns

    while True:
        print("Simple True Moozic Builder (interactive)")
        mode_raw = _prompt_text("Mode (cassette/vinyl)", required=False).lower()
        if not mode_raw:
            mode_raw = "cassette"
        mode = "vinyl" if mode_raw.startswith("v") else "cassette"

        mod_id = _prompt_text("Mod ID (folder/module id)")
        name = _prompt_text("Display name", mod_id, required=False) or mod_id

        audio_dir = default_audio_root()
        audio_dir.mkdir(parents=True, exist_ok=True)
        catalog = refresh_song_catalog(audio_dir)
        found_oggs = [e.ogg for e in catalog if e.ogg.exists()]
        print(f"Audio folder: {audio_dir}")
        print(f"Found {len(catalog)} source audio file(s)")
        print(f"Ready {len(found_oggs)} cached .ogg file(s) in {audio_cache_root(audio_dir)}")

        convert_now = _parse_yes_no(input("Convert/refresh _ogg cache now? (y/n): ").strip().lower(), default=True)
        if convert_now:
            force = _parse_yes_no(input("Rebuild all OGG files? (y/n): ").strip().lower(), default=False)
            summary = convert_audio_library(audio_dir=audio_dir, force=force)
            print(
                "Conversion summary: "
                f"total={summary['total']} converted={summary['converted']} copied={summary['copied']} "
                f"skipped={summary['skipped']} failed={summary['failed']}"
            )

        proceed = input("Proceed? (y/n): ").strip().lower()
        if proceed == "n":
            print("Restarting setup...")
            continue
        if proceed != "y":
            print("Please enter 'y' or 'n'. Restarting setup...")
            continue

        out_dir = default_output_root()
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"Output folder: {out_dir}")
        assets_root = default_assets_root()
        print(f"Assets root: {assets_root}")
        cover_root = default_cover_root()
        cover_root.mkdir(parents=True, exist_ok=True)
        cover_images = [p for p in cover_root.iterdir() if _is_image_file(p)]
        print(f"Cover folder: {cover_root}")
        print(f"Found {len(cover_images)} image file(s)")
        workshop_cover = None
        while True:
            cover_raw = input("Do you have a workshop cover image in the folder? (Hit Enter for Default) : ").strip()
            if not cover_raw:
                break
            workshop_cover = _resolve_cover_choice(cover_root, cover_raw)
            if workshop_cover is not None:
                print(f"Using workshop cover image: {workshop_cover.name}")
                break
            print("No matching image found in cover folder. Please try again or press Enter for default.")

        ns = argparse.Namespace(
            mode=mode,
            mod_id=mod_id,
            name=name,
            author="local-builder",
            audio_dir=audio_dir,
            out_dir=out_dir,
            assets_root=assets_root,
            parent_mod_id="TrueMoozic",
            workshop_cover=workshop_cover,
            convert_audio=False,
            force_rebuild_ogg=False,
        )

        if mode == "vinyl":
            custom_raw = input("Custom Vinyls? (y/n): ").strip().lower()
            ns.custom_vinyls = _parse_yes_no(custom_raw, default=True)
            if ns.custom_vinyls:
                place_raw = input("Would you like the image on the inside or outside of the record? (inside/outside): ").strip().lower()
                ns.vinyl_art_placement = _parse_vinyl_art_placement(place_raw, default="inside")
                default_song_cover = default_poster_root() / "poster.png"
                song_covers: dict[str, Path] = {}
                for idx, ogg in enumerate(found_oggs, start=1):
                    while True:
                        cover_raw = input(f"Song {idx} cover image: ").strip()
                        if not cover_raw:
                            song_covers[ogg.name] = default_song_cover
                            break
                        resolved = _resolve_cover_choice(cover_root, cover_raw)
                        if resolved is not None:
                            song_covers[ogg.name] = resolved
                            break
                        print("No matching image found in cover folder. Please try again or press Enter for default.")
                ns.song_covers = song_covers
                ns.cover = default_song_cover
            else:
                ns.vinyl_art_placement = "inside"
                ns.song_covers = {}
                ns.cover = None
        else:
            custom_raw = input("Custom Cassettes? (y/n): ").strip().lower()
            ns.custom_cassettes = _parse_yes_no(custom_raw, default=False)
            ns.seed = None
            if ns.custom_cassettes:
                default_song_cover = default_poster_root() / "poster.png"
                song_covers: dict[str, Path] = {}
                for idx, ogg in enumerate(found_oggs, start=1):
                    while True:
                        cover_raw = input(f"Song {idx} cover image: ").strip()
                        if not cover_raw:
                            song_covers[ogg.name] = default_song_cover
                            break
                        resolved = _resolve_cover_choice(cover_root, cover_raw)
                        if resolved is not None:
                            song_covers[ogg.name] = resolved
                            break
                        print("No matching image found in cover folder. Please try again or press Enter for default.")
                ns.song_covers = song_covers
                ns.cover = default_song_cover
            else:
                ns.song_covers = {}
                ns.cover = None

        return ns


def main() -> int:
    bootstrap_runtime_folders()
    args = parse_args()
    args.name = args.name or args.mod_id
    args.audio_dir = args.audio_dir.resolve()
    args.out_dir = args.out_dir.resolve()
    args.assets_root = args.assets_root.resolve()
    args.workshop_cover = args.workshop_cover.resolve() if getattr(args, "workshop_cover", None) else None
    args.parent_mod_id = str(getattr(args, "parent_mod_id", "TrueMoozic") or "").strip()
    args.standalone_bundle = bool(getattr(args, "standalone_bundle", False) or not args.parent_mod_id)
    if getattr(args, "cover", None) is not None:
        args.cover = args.cover.resolve()
    if getattr(args, "song_covers", None):
        args.song_covers = {k: v.resolve() for k, v in args.song_covers.items()}

    if not args.assets_root.exists():
        raise SystemExit(f"Assets folder not found: {args.assets_root}")
    if not args.audio_dir.exists():
        raise SystemExit(f"Audio folder not found: {args.audio_dir}")

    if getattr(args, "convert_audio", False) or getattr(args, "force_rebuild_ogg", False):
        summary = convert_audio_library(
            audio_dir=args.audio_dir,
            force=getattr(args, "force_rebuild_ogg", False),
        )
        print(
            "Conversion summary: "
            f"total={summary['total']} converted={summary['converted']} copied={summary['copied']} "
            f"skipped={summary['skipped']} failed={summary['failed']}"
        )

    if args.mode == "cassette":
        out = build_cassette(args)
    else:
        out = build_vinyl(args)

    print(f"Built mod at: {out}")
    return 0


if __name__ == "__main__":
    interactive_launch = len(sys.argv) == 1
    try:
        code = main()
        if interactive_launch:
            input("Done. Press Enter to exit...")
        raise SystemExit(code)
    except SystemExit as e:
        if interactive_launch and e.code not in (0, None):
            try:
                input("Failed. Press Enter to exit...")
            except Exception:
                pass
        raise
    except Exception as e:
        print(f"ERROR: {e}")
        if interactive_launch:
            try:
                input("Failed. Press Enter to exit...")
            except Exception:
                pass
        raise SystemExit(1)
