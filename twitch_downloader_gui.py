#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Twitch Downloader GUI"
APP_VERSION = "1.0.0"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "twitch-downloader-gui"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_OUTPUT = Path.home() / "Videos" / "Twitch"
DEFAULT_CHAT_OUTPUT = Path.home() / "Videos" / "Twitch" / "Chat"


def find_cli() -> str:
    candidates: list[Path] = []
    on_path = shutil.which("TwitchDownloaderCLI")
    if on_path:
        candidates.append(Path(on_path))
    here = Path(__file__).resolve().parent
    candidates += [
        here / "bin" / "TwitchDownloaderCLI",
        here / "TwitchDownloaderCLI",
        Path.home() / ".local" / "bin" / "TwitchDownloaderCLI",
        Path.home() / "Applications" / "TwitchDownloaderCLI",
        Path.home() / "TwitchDownloaderCLI",
        Path.home() / "Downloads" / "TwitchDownloaderCLI",
    ]
    for p in candidates:
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        if p.is_file():
            return str(p)
    return ""


def find_ffmpeg() -> str:
    return shutil.which("ffmpeg") or ""


def ensure_dir(path: str | Path) -> None:
    Path(path).expanduser().mkdir(parents=True, exist_ok=True)


def parse_seconds(value: str) -> Optional[float]:
    value = value.strip()
    if not value:
        return None
    try:
        if re.fullmatch(r"\d+(?:\.\d+)?", value):
            return float(value)
        m = re.fullmatch(r"(?:(\d+):)?(\d{1,2}):(\d{2})(?:\.(\d+))?", value)
        if m:
            h = int(m.group(1) or 0)
            mm = int(m.group(2))
            ss = int(m.group(3))
            frac = float("0." + (m.group(4) or "0"))
            return h * 3600 + mm * 60 + ss + frac
        m = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m|h)", value, re.I)
        if m:
            n = float(m.group(1))
            unit = m.group(2).lower()
            return n / 1000 if unit == "ms" else n if unit == "s" else n * 60 if unit == "m" else n * 3600
    except ValueError:
        return None
    return None


def format_duration(seconds: Any) -> str:
    try:
        total = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return "—"
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def human_size(n: Optional[int]) -> str:
    if n is None:
        return "—"
    x = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if x < 1024 or unit == "TiB":
            return f"{x:.1f} {unit}" if unit != "B" else f"{int(x)} B"
        x /= 1024
    return "—"


def deep_find(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            result = deep_find(v, key)
            if result is not None:
                return result
    elif isinstance(obj, list):
        for v in obj:
            result = deep_find(v, key)
            if result is not None:
                return result
    return None


@dataclass
class Quality:
    name: str
    resolution: str = ""
    fps: str = ""
    bitrate: Optional[int] = None
    size: Optional[int] = None

    def label(self) -> str:
        parts = [self.name]
        if self.resolution:
            parts.append(self.resolution)
        if self.fps:
            parts.append(f"{self.fps} fps")
        if self.size:
            parts.append(f"~{human_size(self.size)}")
        return " · ".join(parts)


@dataclass
class MediaInfo:
    kind: str
    media_id: str
    title: str = ""
    streamer: str = ""
    category: str = ""
    duration: Optional[float] = None
    views: Optional[int] = None
    created_at: str = ""
    vod_id: str = ""
    vod_offset: Optional[float] = None
    description: str = ""
    qualities: list[Quality] = field(default_factory=list)


@dataclass
class Settings:
    cli_path: str = field(default_factory=find_cli)
    ffmpeg_path: str = field(default_factory=find_ffmpeg)
    output_dir: str = str(DEFAULT_OUTPUT)
    chat_output_dir: str = str(DEFAULT_CHAT_OUTPUT)
    temp_dir: str = ""
    threads: int = 4
    bandwidth: int = -1
    collision: str = "Prompt"
    oauth: str = ""
    auto_queue: bool = False
    log_lines: int = 2000

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            base = cls()
            for k, v in data.items():
                if hasattr(base, k):
                    setattr(base, k, v)
            return base
        except Exception:
            return cls()

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            CONFIG_FILE.chmod(0o600)
        except OSError:
            pass


@dataclass
class Job:
    name: str
    command: list[str]
    output: str = ""
    status: str = "Ожидает"
    progress: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    returncode: Optional[int] = None
    log: str = ""


class CliError(RuntimeError):
    pass


class CliRunner:
    def __init__(self, settings: Settings):
        self.settings = settings

    def cli(self) -> str:
        cli = self.settings.cli_path.strip()
        if not cli:
            cli = find_cli()
        if not cli:
            raise CliError("TwitchDownloaderCLI не найден. Укажите путь в Настройках.")
        if not Path(cli).expanduser().exists() and shutil.which(cli) is None:
            raise CliError(f"Файл TwitchDownloaderCLI не найден: {cli}")
        return str(Path(cli).expanduser())

    def common(self, cmd: list[str], quiet: bool = False) -> list[str]:
        if quiet:
            cmd += ["--banner=false", "--log-level", "None"]
        return cmd

    def info_command(self, url: str) -> list[str]:
        return self.common([self.cli(), "info", "--id", url, "--format", "raw"], quiet=True)

    def build_video(self, url: str, output: str, quality: str, beginning: str, ending: str,
                    threads: int, bandwidth: int, trim_mode: str, collision: str, oauth: str,
                    ffmpeg_path: str, temp_path: str) -> list[str]:
        cmd = [self.cli(), "videodownload", "--id", url, "--output", output]
        if quality and quality != "Автоматически — максимальное доступное":
            cmd += ["--quality", quality]
        if beginning.strip(): cmd += ["--beginning", normalize_time_arg(beginning)]
        if ending.strip(): cmd += ["--ending", normalize_time_arg(ending)]
        cmd += ["--threads", str(threads), "--bandwidth", str(bandwidth), "--trim-mode", trim_mode]
        if oauth.strip(): cmd += ["--oauth", oauth.strip()]
        if ffmpeg_path.strip(): cmd += ["--ffmpeg-path", ffmpeg_path.strip()]
        if temp_path.strip(): cmd += ["--temp-path", temp_path.strip()]
        cmd += ["--collision", collision]
        return cmd

    def build_clip(self, url: str, output: str, quality: str, bandwidth: int, encode_metadata: bool,
                   collision: str, ffmpeg_path: str, temp_path: str) -> list[str]:
        cmd = [self.cli(), "clipdownload", "--id", url, "--output", output]
        if quality and quality != "Автоматически — максимальное доступное": cmd += ["--quality", quality]
        cmd += ["--bandwidth", str(bandwidth), "--encode-metadata=true" if encode_metadata else "--encode-metadata=false"]
        if ffmpeg_path.strip(): cmd += ["--ffmpeg-path", ffmpeg_path.strip()]
        if temp_path.strip(): cmd += ["--temp-path", temp_path.strip()]
        cmd += ["--collision", collision]
        return cmd

    def build_chat_download(self, url: str, output: str, compression: str, beginning: str, ending: str,
                            embed: bool, bttv: bool, ffz: bool, stv: bool, timestamp: str,
                            threads: int, collision: str, temp_path: str) -> list[str]:
        cmd = [self.cli(), "chatdownload", "--id", url, "--output", output, "--compression", compression,
               "--timestamp-format", timestamp, "--threads", str(threads)]
        if beginning.strip(): cmd += ["--beginning", normalize_time_arg(beginning)]
        if ending.strip(): cmd += ["--ending", normalize_time_arg(ending)]
        if embed: cmd += ["--embed-images"]
        cmd += [f"--bttv={'true' if bttv else 'false'}", f"--ffz={'true' if ffz else 'false'}", f"--stv={'true' if stv else 'false'}"]
        if temp_path.strip(): cmd += ["--temp-path", temp_path.strip()]
        cmd += ["--collision", collision]
        return cmd

    def build_chat_update(self, input_file: str, output: str, compression: str, beginning: str, ending: str,
                          embed_missing: bool, replace_embeds: bool, bttv: bool, ffz: bool, stv: bool,
                          timestamp: str, collision: str, temp_path: str) -> list[str]:
        cmd = [self.cli(), "chatupdate", "--input", input_file, "--output", output, "--compression", compression,
               f"--embed-missing={'true' if embed_missing else 'false'}",
               f"--replace-embeds={'true' if replace_embeds else 'false'}",
               f"--bttv={'true' if bttv else 'false'}", f"--ffz={'true' if ffz else 'false'}", f"--stv={'true' if stv else 'false'}",
               "--timestamp-format", timestamp]
        if beginning.strip(): cmd += ["--beginning", normalize_time_arg(beginning)]
        if ending.strip(): cmd += ["--ending", normalize_time_arg(ending)]
        if temp_path.strip(): cmd += ["--temp-path", temp_path.strip()]
        cmd += ["--collision", collision]
        return cmd

    def build_chat_render(self, values: dict[str, Any]) -> list[str]:
        cmd = [self.cli(), "chatrender", "--input", values["input"], "--output", values["output"]]
        args = {
            "--background-color": values["background"],
            "--alt-background-color": values["alt_background"],
            "--highlight-user-color": values["highlight_color"],
            "--message-color": values["message_color"],
            "--chat-width": values["width"], "--chat-height": values["height"],
            "--font": values["font"], "--font-size": values["font_size"],
            "--message-fontstyle": values["message_style"], "--username-fontstyle": values["username_style"],
            "--framerate": values["fps"], "--update-rate": values["update_rate"],
            "--emoji-vendor": values["emoji_vendor"], "--badge-filter": values["badge_filter"],
            "--outline-size": values["outline_size"],
            "--scale-emote": values["scale_emote"], "--scale-badge": values["scale_badge"], "--scale-emoji": values["scale_emoji"],
            "--scale-avatar": values["scale_avatar"], "--scale-username": values["scale_username"],
            "--scale-vertical": values["scale_vertical"], "--scale-side-padding": values["scale_side_padding"],
            "--scale-section-height": values["scale_section_height"], "--scale-word-space": values["scale_word_space"],
            "--scale-emote-space": values["scale_emote_space"], "--scale-highlight-stroke": values["scale_highlight_stroke"],
            "--scale-highlight-indent": values["scale_highlight_indent"],
        }
        for k, v in args.items():
            cmd += [k, str(v)]
        if values["beginning"].strip(): cmd += ["--beginning", normalize_time_arg(values["beginning"])]
        if values["ending"].strip(): cmd += ["--ending", normalize_time_arg(values["ending"])]
        for flag in ("bttv", "ffz", "stv", "allow_unlisted", "sub_messages", "badges", "timestamp", "generate_mask",
                     "sharpening", "dispersion", "alternate_backgrounds", "readable_colors", "avatars", "offline", "skip_drive_waiting"):
            cli_name = {
                "allow_unlisted": "allow-unlisted-emotes", "sub_messages": "sub-messages", "generate_mask": "generate-mask",
                "alternate_backgrounds": "alternate-backgrounds", "readable_colors": "readable-colors", "skip_drive_waiting": "skip-drive-waiting",
            }.get(flag, flag)
            default_true = flag in {"bttv", "ffz", "stv", "allow_unlisted", "sub_messages", "badges", "readable_colors"}
            if default_true:
                cmd += [f"--{cli_name}={'true' if values[flag] else 'false'}"]
            elif values[flag]:
                cmd += [f"--{cli_name}"]
        for flag, cli_name in (("input_args", "input-args"), ("output_args", "output-args"), ("ignore_users", "ignore-users"),
                               ("ban_words", "ban-words"), ("highlight_users", "highlight-users")):
            if values[flag].strip(): cmd += [f"--{cli_name}", values[flag]]
        if values["ffmpeg_path"].strip(): cmd += ["--ffmpeg-path", values["ffmpeg_path"].strip()]
        if values["temp_path"].strip(): cmd += ["--temp-path", values["temp_path"].strip()]
        cmd += ["--collision", values["collision"]]
        return cmd


def normalize_time_arg(value: str) -> str:
    value = value.strip()
    if not value:
        return value
    if re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?", value):
        return value
    seconds = parse_seconds(value)
    if seconds is not None:
        return f"{seconds:g}s"
    raise ValueError(f"Некорректное время: {value}")


def split_raw_output(text: str) -> list[str]:
    text = text.replace("\r\n", "\n")
    start = text.find("#EXTM3U")
    head = text if start < 0 else text[:start]
    lines = [ln.strip() for ln in head.splitlines() if ln.strip() and not ln.startswith("[")]
    return lines


def parse_info_output(text: str, input_id: str) -> MediaInfo:
    text = text.replace("\r\n", "\n")
    playlist_start = text.find("#EXTM3U")
    json_part = text if playlist_start < 0 else text[:playlist_start]
    objs: list[Any] = []
    for line in split_raw_output(json_part):
        try:
            objs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not objs:
        raise CliError("CLI не вернул распознаваемую информацию. Проверьте URL, доступ к VOD/клипу и OAuth.")

    if any(deep_find(obj, "clip") is not None for obj in objs):
        root = next((obj for obj in objs if deep_find(obj, "clip") is not None), objs[0])
        clip = deep_find(root, "clip") or {}
        kind = "Клип"
        title = clip.get("title") or ""
        broadcaster = clip.get("broadcaster") or {}
        streamer = broadcaster.get("displayName") or broadcaster.get("login") or ""
        category = (clip.get("game") or {}).get("displayName") or ""
        media_id = input_id
        duration = clip.get("durationSeconds")
        views = clip.get("viewCount")
        created = clip.get("createdAt") or ""
        vod = clip.get("video") or {}
        vod_id = str(vod.get("id") or "")
        vod_offset = clip.get("videoOffsetSeconds")
    else:
        video_obj = next((deep_find(obj, "video") for obj in objs if deep_find(obj, "video") is not None), {}) or {}
        kind = "VOD / Highlight"
        title = video_obj.get("title") or ""
        owner = video_obj.get("owner") or {}
        streamer = owner.get("displayName") or owner.get("login") or ""
        category = (video_obj.get("game") or {}).get("displayName") or ""
        media_id = str(video_obj.get("id") or input_id)
        duration = video_obj.get("lengthSeconds")
        views = video_obj.get("viewCount")
        created = video_obj.get("createdAt") or ""
        vod_id = media_id
        vod_offset = None
    qualities = parse_m3u8_qualities(text[playlist_start:] if playlist_start >= 0 else "", duration)
    return MediaInfo(kind, media_id, title, streamer, category, duration, parse_int(views), created, vod_id, vod_offset, video_obj.get("description", "") if kind.startswith("VOD") else "", qualities)


def parse_m3u8_qualities(playlist: str, duration: Any) -> list[Quality]:
    if not playlist:
        return []
    lines = playlist.splitlines()
    result: list[Quality] = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        attrs: dict[str, str] = {}
        for part in re.split(r",(?=[A-Z0-9-]+=)", line.split(":", 1)[1]):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            attrs[k.strip()] = v.strip().strip('"')
        name = attrs.get("VIDEO") or attrs.get("NAME") or ""
        resolution = attrs.get("RESOLUTION", "")
        fps = attrs.get("FRAME-RATE", "")
        bitrate = parse_int(attrs.get("BANDWIDTH"))
        estimated = None
        try:
            if bitrate and duration:
                estimated = int((bitrate / 8) * float(duration))
        except (TypeError, ValueError):
            pass
        if not name:
            name = resolution or f"Поток {len(result) + 1}"
        q = Quality(name, resolution, fps, bitrate, estimated)
        if q.name not in [x.name for x in result]: result.append(q)
    result.sort(key=lambda q: (
        int(re.match(r"(\d+)", q.resolution).group(1)) if re.match(r"(\d+)", q.resolution) else 0,
        float(q.fps or 0), q.bitrate or 0
    ), reverse=True)
    return result


class TwitchDownloaderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        #self.geometry("1180x820")
        self.minsize(980, 700)
        self.settings = Settings.load()
        self.runner = CliRunner(self.settings)
        self.jobs: list[Job] = []
        self.running_job: Optional[Job] = None
        self.running_process: Optional[subprocess.Popen[str]] = None
        self.ui_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.info_by_tab: dict[str, MediaInfo] = {}
        self._build_style()
        self._build_ui()
        self._load_settings_into_ui()
        self.after(100, self._poll_events)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try: style.theme_use("clam")
        except tk.TclError: pass
        style.configure("Title.TLabel", font=("TkDefaultFont", 18, "bold"))
        style.configure("Section.TLabelframe.Label", font=("TkDefaultFont", 10, "bold"))
        style.configure("Primary.TButton", font=("TkDefaultFont", 10, "bold"), padding=(14, 7))
        style.configure("Small.TButton", padding=(8, 4))
        style.configure("Info.TLabel", foreground="#534358")
        style.configure("Card.TFrame", relief="groove", borderwidth=1)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)
        header = ttk.Frame(root)
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text="Twitch Downloader", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text=f"GUI {APP_VERSION}", style="Info.TLabel").pack(side="left", padx=10, pady=(5,0))
        self.cli_status = ttk.Label(header, text="CLI: не найден", style="Info.TLabel")
        self.cli_status.pack(side="right")
        if self.settings.cli_path:
            self.cli_status.configure(text=f"CLI: {Path(self.settings.cli_path).name}")

        self.tabs = ttk.Notebook(root)
        self.tabs.pack(fill="both", expand=True)
        self.download_tab = ttk.Frame(self.tabs, padding=8)
        self.chat_tab = ttk.Frame(self.tabs, padding=8)
        self.tools_tab = ttk.Frame(self.tabs, padding=8)
        self.queue_tab = ttk.Frame(self.tabs, padding=8)
        self.settings_tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(self.download_tab, text="Скачать")
        self.tabs.add(self.chat_tab, text="Чат")
        self.tabs.add(self.tools_tab, text="Инструменты")
        self.tabs.add(self.queue_tab, text="Очередь")
        self.tabs.add(self.settings_tab, text="Настройки")
        self._build_download_tab()
        self._build_chat_tab()
        self._build_tools_tab()
        self._build_queue_tab()
        self._build_settings_tab()
        self._refresh_queue()

    def _build_url_bar(self, parent: ttk.Frame, on_info: Callable[[], None]) -> tuple[ttk.Entry, ttk.LabelFrame]:
        frame = ttk.LabelFrame(parent, text="Источник", padding=8)
        frame.pack(fill="x", pady=(0, 8))
        row = ttk.Frame(frame)
        row.pack(fill="x")
        url = ttk.Entry(row)
        url.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Получить информацию", command=on_info, style="Primary.TButton").pack(side="left", padx=(8,0))
        info = ttk.LabelFrame(parent, text="Информация", padding=8)
        info.pack(fill="x", pady=(0, 8))
        return url, info

    def _make_info_panel(self, parent: ttk.LabelFrame) -> dict[str, ttk.Label]:
        labels: dict[str, ttk.Label] = {}
        grid = ttk.Frame(parent)
        grid.pack(fill="x")
        fields = [("title", "Название"), ("streamer", "Канал"), ("category", "Категория"), ("duration", "Длительность"),
                  ("views", "Просмотры"), ("created", "Создано"), ("vod", "VOD")]
        for i, (key, caption) in enumerate(fields):
            r, c = divmod(i, 2)
            ttk.Label(grid, text=caption + ":", font=("TkDefaultFont", 9, "bold")).grid(row=r, column=c*2, sticky="w", padx=(0,6), pady=2)
            lab = ttk.Label(grid, text="—", style="Info.TLabel")
            lab.grid(row=r, column=c*2+1, sticky="w", padx=(0,20), pady=2)
            labels[key] = lab
        grid.columnconfigure(1, weight=1); grid.columnconfigure(3, weight=1)
        return labels

    def _show_info(self, labels: dict[str, ttk.Label], info: MediaInfo) -> None:
        labels["title"].configure(text=info.title or "—")
        labels["streamer"].configure(text=info.streamer or "—")
        labels["category"].configure(text=info.category or "—")
        labels["duration"].configure(text=format_duration(info.duration))
        labels["views"].configure(text=f"{info.views:,}".replace(",", " ") if info.views is not None else "—")
        labels["created"].configure(text=info.created_at or "—")
        labels["vod"].configure(text=info.vod_id or "—")

    def _build_quality_row(self, parent: ttk.Frame, label: str = "Качество") -> tuple[ttk.Combobox, ttk.Label]:
        ttk.Label(parent, text=label).grid(row=0, column=0, sticky="w", pady=5)
        combo = ttk.Combobox(parent, state="readonly", values=["Автоматически — максимальное доступное"], width=48)
        combo.current(0); combo.grid(row=0, column=1, sticky="ew", pady=5, padx=8)
        detail = ttk.Label(parent, text="", style="Info.TLabel")
        detail.grid(row=0, column=2, sticky="w")
        parent.columnconfigure(1, weight=1)
        return combo, detail

    def _build_download_tab(self) -> None:
        self.download_subtabs = ttk.Notebook(self.download_tab)
        self.download_subtabs.pack(fill="both", expand=True)
        self.vod_frame = ttk.Frame(self.download_subtabs, padding=4)
        self.clip_frame = ttk.Frame(self.download_subtabs, padding=4)
        self.chat_download_frame = ttk.Frame(self.download_subtabs, padding=4)
        self.download_subtabs.add(self.vod_frame, text="VOD / стрим")
        self.download_subtabs.add(self.clip_frame, text="Клип")
        self.download_subtabs.add(self.chat_download_frame, text="Чат")
        self._build_vod_page()
        self._build_clip_page()
        self._build_chat_download_page()

    def _build_vod_page(self) -> None:
        self.vod_url, info = self._build_url_bar(self.vod_frame, self._get_vod_info)
        self.vod_info_labels = self._make_info_panel(info)
        form = ttk.LabelFrame(self.vod_frame, text="Основные параметры", padding=8); form.pack(fill="x", pady=8)
        self.vod_quality, self.vod_quality_detail = self._build_quality_row(form)
        ttk.Label(form, text="Начало").grid(row=1,column=0,sticky="w",pady=5); self.vod_begin = ttk.Entry(form); self.vod_begin.grid(row=1,column=1,sticky="ew",padx=8); ttk.Label(form,text="hh:mm:ss / 10s",style="Info.TLabel").grid(row=1,column=2,sticky="w")
        ttk.Label(form, text="Конец").grid(row=2,column=0,sticky="w",pady=5); self.vod_end = ttk.Entry(form); self.vod_end.grid(row=2,column=1,sticky="ew",padx=8); ttk.Label(form,text="пусто = до конца",style="Info.TLabel").grid(row=2,column=2,sticky="w")
        ttk.Label(form, text="Файл").grid(row=3,column=0,sticky="w",pady=5); self.vod_output=ttk.Entry(form); self.vod_output.grid(row=3,column=1,sticky="ew",padx=8); ttk.Button(form,text="Выбрать…",command=lambda:self._choose_save(self.vod_output,[("MP4","*.mp4"), ("M4A","*.m4a")])).grid(row=3,column=2)
        adv = ttk.LabelFrame(self.vod_frame, text="Дополнительно", padding=8); adv.pack(fill="x", pady=8)
        ttk.Label(adv,text="Потоки").grid(row=0,column=0,sticky="w",pady=4); self.vod_threads=ttk.Spinbox(adv,from_=1,to=64,width=8); self.vod_threads.grid(row=0,column=1,sticky="w",padx=8)
        ttk.Label(adv,text="Лимит KiB/s").grid(row=0,column=2,sticky="w",pady=4); self.vod_band=ttk.Entry(adv,width=12); self.vod_band.grid(row=0,column=3,sticky="w",padx=8)
        ttk.Label(adv,text="Обрезка").grid(row=1,column=0,sticky="w",pady=4); self.vod_trim=ttk.Combobox(adv,state="readonly",values=["Exact","Safe"],width=14); self.vod_trim.current(0); self.vod_trim.grid(row=1,column=1,sticky="w",padx=8)
        ttk.Label(adv,text="Конфликт файла").grid(row=1,column=2,sticky="w",pady=4); self.vod_collision=ttk.Combobox(adv,state="readonly",values=["Prompt","Overwrite","Exit","Rename"],width=14); self.vod_collision.current(0); self.vod_collision.grid(row=1,column=3,sticky="w",padx=8)
        ttk.Label(adv,text="OAuth").grid(row=2,column=0,sticky="w",pady=4); self.vod_oauth=ttk.Entry(adv,show="•"); self.vod_oauth.grid(row=2,column=1,columnspan=3,sticky="ew",padx=8)
        ttk.Label(adv,text="FFmpeg").grid(row=3,column=0,sticky="w",pady=4); self.vod_ffmpeg=ttk.Entry(adv); self.vod_ffmpeg.grid(row=3,column=1,columnspan=2,sticky="ew",padx=8); ttk.Button(adv,text="…",command=lambda:self._choose_file(self.vod_ffmpeg)).grid(row=3,column=3)
        ttk.Label(adv,text="Temporary").grid(row=4,column=0,sticky="w",pady=4); self.vod_temp=ttk.Entry(adv); self.vod_temp.grid(row=4,column=1,columnspan=2,sticky="ew",padx=8); ttk.Button(adv,text="…",command=lambda:self._choose_dir(self.vod_temp)).grid(row=4,column=3)
        adv.columnconfigure(1,weight=1); adv.columnconfigure(2,weight=0)
        self._build_action_bar(self.vod_frame, self._queue_vod)

    def _build_clip_page(self) -> None:
        self.clip_url, info = self._build_url_bar(self.clip_frame, self._get_clip_info)
        self.clip_info_labels = self._make_info_panel(info)
        form=ttk.LabelFrame(self.clip_frame,text="Основные параметры",padding=8); form.pack(fill="x",pady=8)
        self.clip_quality,self.clip_quality_detail=self._build_quality_row(form)
        ttk.Label(form,text="Файл").grid(row=1,column=0,sticky="w",pady=5); self.clip_output=ttk.Entry(form); self.clip_output.grid(row=1,column=1,sticky="ew",padx=8); ttk.Button(form,text="Выбрать…",command=lambda:self._choose_save(self.clip_output,[("MP4","*.mp4")])).grid(row=1,column=2)
        adv=ttk.LabelFrame(self.clip_frame,text="Дополнительно",padding=8); adv.pack(fill="x",pady=8)
        ttk.Label(adv,text="Лимит KiB/s").grid(row=0,column=0,sticky="w"); self.clip_band=ttk.Entry(adv,width=12); self.clip_band.grid(row=0,column=1,sticky="w",padx=8)
        self.clip_encode=tk.BooleanVar(value=True); ttk.Checkbutton(adv,text="Добавлять metadata",variable=self.clip_encode).grid(row=0,column=2,sticky="w")
        ttk.Label(adv,text="Конфликт файла").grid(row=1,column=0,sticky="w",pady=5); self.clip_collision=ttk.Combobox(adv,state="readonly",values=["Prompt","Overwrite","Exit","Rename"],width=14); self.clip_collision.current(0); self.clip_collision.grid(row=1,column=1,sticky="w",padx=8)
        ttk.Label(adv,text="FFmpeg").grid(row=2,column=0,sticky="w"); self.clip_ffmpeg=ttk.Entry(adv); self.clip_ffmpeg.grid(row=2,column=1,columnspan=2,sticky="ew",padx=8); ttk.Button(adv,text="…",command=lambda:self._choose_file(self.clip_ffmpeg)).grid(row=2,column=3)
        ttk.Label(adv,text="Temporary").grid(row=3,column=0,sticky="w"); self.clip_temp=ttk.Entry(adv); self.clip_temp.grid(row=3,column=1,columnspan=2,sticky="ew",padx=8); ttk.Button(adv,text="…",command=lambda:self._choose_dir(self.clip_temp)).grid(row=3,column=3)
        adv.columnconfigure(1,weight=1)
        self._build_action_bar(self.clip_frame, self._queue_clip)

    def _build_chat_download_page(self) -> None:
        self.chat_url, info = self._build_url_bar(self.chat_download_frame, self._get_chat_info)
        self.chat_info_labels=self._make_info_panel(info)
        form=ttk.LabelFrame(self.chat_download_frame,text="Основные параметры",padding=8); form.pack(fill="x",pady=8)
        ttk.Label(form,text="Формат").grid(row=0,column=0,sticky="w"); self.chat_format=ttk.Combobox(form,state="readonly",values=["JSON","JSON.GZ","HTML","TXT"],width=20); self.chat_format.current(0); self.chat_format.grid(row=0,column=1,sticky="w",padx=8)
        ttk.Label(form,text="Файл").grid(row=1,column=0,sticky="w",pady=5); self.chat_output=ttk.Entry(form); self.chat_output.grid(row=1,column=1,sticky="ew",padx=8); ttk.Button(form,text="Выбрать…",command=lambda:self._choose_save(self.chat_output,[("Chat","*.json *.json.gz *.html *.txt")])).grid(row=1,column=2)
        ttk.Label(form,text="Начало").grid(row=2,column=0,sticky="w"); self.chat_begin=ttk.Entry(form); self.chat_begin.grid(row=2,column=1,sticky="ew",padx=8)
        ttk.Label(form,text="Конец").grid(row=3,column=0,sticky="w"); self.chat_end=ttk.Entry(form); self.chat_end.grid(row=3,column=1,sticky="ew",padx=8)
        adv=ttk.LabelFrame(self.chat_download_frame,text="Дополнительно",padding=8); adv.pack(fill="x",pady=8)
        self.chat_embed=tk.BooleanVar(value=False); ttk.Checkbutton(adv,text="Встраивать изображения / emotes",variable=self.chat_embed).grid(row=0,column=0,columnspan=2,sticky="w")
        self.chat_bttv=tk.BooleanVar(value=True); self.chat_ffz=tk.BooleanVar(value=True); self.chat_stv=tk.BooleanVar(value=True)
        ttk.Checkbutton(adv,text="BTTV",variable=self.chat_bttv).grid(row=1,column=0,sticky="w"); ttk.Checkbutton(adv,text="FFZ",variable=self.chat_ffz).grid(row=1,column=1,sticky="w"); ttk.Checkbutton(adv,text="7TV",variable=self.chat_stv).grid(row=1,column=2,sticky="w")
        ttk.Label(adv,text="Время в TXT").grid(row=2,column=0,sticky="w",pady=5); self.chat_timestamp=ttk.Combobox(adv,state="readonly",values=["Relative","Utc","UtcFull","None"],width=14); self.chat_timestamp.current(0); self.chat_timestamp.grid(row=2,column=1,sticky="w",padx=8)
        ttk.Label(adv,text="Потоки").grid(row=3,column=0,sticky="w"); self.chat_threads=ttk.Spinbox(adv,from_=1,to=64,width=8); self.chat_threads.grid(row=3,column=1,sticky="w",padx=8)
        ttk.Label(adv,text="Конфликт файла").grid(row=4,column=0,sticky="w"); self.chat_collision=ttk.Combobox(adv,state="readonly",values=["Prompt","Overwrite","Exit","Rename"],width=14); self.chat_collision.current(0); self.chat_collision.grid(row=4,column=1,sticky="w",padx=8)
        adv.columnconfigure(3,weight=1)
        self._build_action_bar(self.chat_download_frame, self._queue_chat_download)

    def _build_action_bar(self,parent:ttk.Frame, callback:Callable[[],None]) -> None:
        bar=ttk.Frame(parent); bar.pack(fill="x",pady=(4,0)); ttk.Button(bar,text="Добавить в очередь",command=callback,style="Primary.TButton").pack(side="right"); ttk.Label(bar,text="Задание будет запущено из вкладки «Очередь».",style="Info.TLabel").pack(side="left")

    def _build_chat_tab(self) -> None:
        nb=ttk.Notebook(self.chat_tab); nb.pack(fill="both",expand=True)
        update=ttk.Frame(nb,padding=8); render=ttk.Frame(nb,padding=8); nb.add(update,text="Обновить / конвертировать"); nb.add(render,text="Рендер")
        self._build_chat_update(update); self._build_chat_render(render)

    def _build_chat_update(self,p:ttk.Frame)->None:
        form=ttk.LabelFrame(p,text="Файлы",padding=8); form.pack(fill="x")
        ttk.Label(form,text="Входной JSON").grid(row=0,column=0,sticky="w"); self.cu_input=ttk.Entry(form); self.cu_input.grid(row=0,column=1,sticky="ew",padx=8); ttk.Button(form,text="…",command=lambda:self._choose_file(self.cu_input)).grid(row=0,column=2)
        ttk.Label(form,text="Выходной файл").grid(row=1,column=0,sticky="w",pady=5); self.cu_output=ttk.Entry(form); self.cu_output.grid(row=1,column=1,sticky="ew",padx=8); ttk.Button(form,text="…",command=lambda:self._choose_save(self.cu_output,[("Chat","*.json *.json.gz *.html *.txt")])).grid(row=1,column=2)
        ttk.Label(form,text="Компрессия").grid(row=2,column=0,sticky="w"); self.cu_compression=ttk.Combobox(form,state="readonly",values=["None","Gzip"],width=12); self.cu_compression.current(0); self.cu_compression.grid(row=2,column=1,sticky="w",padx=8)
        form.columnconfigure(1,weight=1)
        opt=ttk.LabelFrame(p,text="Параметры",padding=8); opt.pack(fill="x",pady=8)
        self.cu_embed=tk.BooleanVar(value=False); self.cu_replace=tk.BooleanVar(value=False); self.cu_bttv=tk.BooleanVar(value=True); self.cu_ffz=tk.BooleanVar(value=True); self.cu_stv=tk.BooleanVar(value=True)
        ttk.Checkbutton(opt,text="Встроить отсутствующие",variable=self.cu_embed).grid(row=0,column=0,sticky="w"); ttk.Checkbutton(opt,text="Заменять embeds",variable=self.cu_replace).grid(row=0,column=1,sticky="w")
        ttk.Checkbutton(opt,text="BTTV",variable=self.cu_bttv).grid(row=1,column=0,sticky="w"); ttk.Checkbutton(opt,text="FFZ",variable=self.cu_ffz).grid(row=1,column=1,sticky="w"); ttk.Checkbutton(opt,text="7TV",variable=self.cu_stv).grid(row=1,column=2,sticky="w")
        ttk.Label(opt,text="Timestamp").grid(row=2,column=0,sticky="w"); self.cu_timestamp=ttk.Combobox(opt,state="readonly",values=["Relative","Utc","None"],width=12); self.cu_timestamp.current(0); self.cu_timestamp.grid(row=2,column=1,sticky="w",padx=8)
        ttk.Label(opt,text="Начало").grid(row=3,column=0,sticky="w"); self.cu_begin=ttk.Entry(opt,width=16); self.cu_begin.grid(row=3,column=1,sticky="w",padx=8); ttk.Label(opt,text="Конец").grid(row=3,column=2,sticky="w"); self.cu_end=ttk.Entry(opt,width=16); self.cu_end.grid(row=3,column=3,sticky="w",padx=8)
        ttk.Label(opt,text="Конфликт").grid(row=4,column=0,sticky="w"); self.cu_collision=ttk.Combobox(opt,state="readonly",values=["Prompt","Overwrite","Exit","Rename"],width=12); self.cu_collision.current(0); self.cu_collision.grid(row=4,column=1,sticky="w",padx=8)
        ttk.Button(p,text="Добавить в очередь",command=self._queue_chat_update,style="Primary.TButton").pack(anchor="e",pady=6)

    def _build_chat_render(self,p:ttk.Frame)->None:
        top=ttk.LabelFrame(p,text="Файлы",padding=8); top.pack(fill="x")
        ttk.Label(top,text="Chat JSON").grid(row=0,column=0,sticky="w"); self.cr_input=ttk.Entry(top); self.cr_input.grid(row=0,column=1,sticky="ew",padx=8); ttk.Button(top,text="…",command=lambda:self._choose_file(self.cr_input)).grid(row=0,column=2)
        ttk.Label(top,text="Выход MP4").grid(row=1,column=0,sticky="w",pady=5); self.cr_output=ttk.Entry(top); self.cr_output.grid(row=1,column=1,sticky="ew",padx=8); ttk.Button(top,text="…",command=lambda:self._choose_save(self.cr_output,[("MP4","*.mp4")])).grid(row=1,column=2); top.columnconfigure(1,weight=1)
        nb=ttk.Notebook(p); nb.pack(fill="both",expand=True,pady=8)
        basic=ttk.Frame(nb,padding=8); visual=ttk.Frame(nb,padding=8); filterp=ttk.Frame(nb,padding=8); advanced=ttk.Frame(nb,padding=8)
        nb.add(basic,text="Основные"); nb.add(visual,text="Цвета и текст"); nb.add(filterp,text="Элементы"); nb.add(advanced,text="FFmpeg и масштаб")
        self.cr_values:dict[str,Any]={}
        self._render_basic(basic); self._render_visual(visual); self._render_filters(filterp); self._render_advanced(advanced)
        ttk.Button(p,text="Добавить в очередь",command=self._queue_chat_render,style="Primary.TButton").pack(anchor="e")

    def _add_entry(self,p,row,key,label,default,width=18):
        ttk.Label(p,text=label).grid(row=row,column=0,sticky="w",pady=3); e=ttk.Entry(p,width=width); e.insert(0,str(default)); e.grid(row=row,column=1,sticky="ew",padx=8,pady=3); self.cr_values[key]=e; return e
    def _add_combo(self,p,row,key,label,values,default=None,width=18):
        ttk.Label(p,text=label).grid(row=row,column=0,sticky="w",pady=3); c=ttk.Combobox(p,state="readonly",values=values,width=width); c.set(default if default is not None else values[0]); c.grid(row=row,column=1,sticky="ew",padx=8,pady=3); self.cr_values[key]=c; return c
    def _render_basic(self,p):
        self._add_entry(p,0,"width","Ширина",350); self._add_entry(p,1,"height","Высота",600); self._add_entry(p,2,"fps","FPS",30); self._add_entry(p,3,"update_rate","Update rate",0.2); self._add_entry(p,4,"font","Шрифт","Inter Embedded"); self._add_entry(p,5,"font_size","Размер шрифта",12); self._add_entry(p,6,"beginning","Начало",""); self._add_entry(p,7,"ending","Конец","")
        p.columnconfigure(1,weight=1)
    def _render_visual(self,p):
        self._add_entry(p,0,"background","Фон","#111111"); self._add_entry(p,1,"alt_background","Чередующийся фон","#191919"); self._add_entry(p,2,"highlight_color","Highlight","#C8FF0059"); self._add_entry(p,3,"message_color","Текст","#ffffff"); self._add_combo(p,4,"message_style","Стиль сообщений",["normal","bold","italic","bolditalic"],"normal"); self._add_combo(p,5,"username_style","Стиль имени",["normal","bold","italic","bolditalic"],"bold"); self._add_entry(p,6,"outline_size","Outline size",4); self._add_combo(p,7,"emoji_vendor","Emoji",["notocolor","twemoji","none"],"notocolor"); p.columnconfigure(1,weight=1)
    def _render_filters(self,p):
        bools=[("bttv","BTTV",True),("ffz","FFZ",True),("stv","7TV",True),("allow_unlisted","Невключённые 7TV",True),("sub_messages","Sub messages",True),("badges","Badges",True),("timestamp","Timestamp",False),("generate_mask","Generate mask",False),("sharpening","Sharpening",False),("dispersion","Dispersion",False),("alternate_backgrounds","Чередовать фон",False),("readable_colors","Readable colors",True),("avatars","Avatars",False),("offline","Offline",False)]
        for i,(k,text,v) in enumerate(bools): self.cr_values[k]=tk.BooleanVar(value=v); ttk.Checkbutton(p,text=text,variable=self.cr_values[k]).grid(row=i//3,column=i%3,sticky="w",padx=8,pady=4)
        self._add_entry(p,5,"ignore_users","Игнорировать пользователей",""); self._add_entry(p,6,"ban_words","Ban words",""); self._add_entry(p,7,"highlight_users","Подсветить пользователей",""); self._add_entry(p,8,"badge_filter","Badge filter",0); p.columnconfigure(1,weight=1)
    def _render_advanced(self,p):
        keys=[("input_args","Input args",""),("output_args","Output args",'-c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p "{save_path}"'),("ffmpeg_path","FFmpeg",self.settings.ffmpeg_path),("temp_path","Temporary",self.settings.temp_dir),("scale_emote","Scale emote",1.0),("scale_badge","Scale badge",1.0),("scale_emoji","Scale emoji",1.0),("scale_avatar","Scale avatar",1.0),("scale_username","Scale username",1.0),("scale_vertical","Scale vertical",1.0),("scale_side_padding","Side padding",1.0),("scale_section_height","Section height",1.0),("scale_word_space","Word space",1.0),("scale_emote_space","Emote space",1.0),("scale_highlight_stroke","Highlight stroke",1.0),("scale_highlight_indent","Highlight indent",1.0)]
        for i,(k,l,d) in enumerate(keys): self._add_entry(p,i,k,l,d,60)
        self.cr_values["skip_drive_waiting"]=tk.BooleanVar(value=False); ttk.Checkbutton(p,text="Skip drive waiting",variable=self.cr_values["skip_drive_waiting"]).grid(row=len(keys),column=0,sticky="w",pady=4); self._add_combo(p,len(keys)+1,"collision","Конфликт",["Prompt","Overwrite","Exit","Rename"],"Prompt"); p.columnconfigure(1,weight=1)

    def _build_tools_tab(self)->None:
        body=ttk.Frame(self.tools_tab); body.pack(fill="both",expand=True)
        left=ttk.LabelFrame(body,text="Операции CLI",padding=10); left.pack(side="left",fill="y",padx=(0,10))
        operations=[("Показать help","help",False),("Скачать FFmpeg","ffmpeg --download",True),("Очистить cache","cache --clear --force-clear",True),("Обновить CLI","update --force",True),("Собрать TS","tsmerge",False)]
        for label,op,_ in operations:
            if op=="help": ttk.Button(left,text=label,command=lambda:self._tool_help()).pack(fill="x",pady=4)
            elif op=="tsmerge": ttk.Button(left,text=label,command=self._tool_tsmerge).pack(fill="x",pady=4)
            else: ttk.Button(left,text=label,command=lambda o=op:self._run_tool(o.split())).pack(fill="x",pady=4)
        right=ttk.LabelFrame(body,text="Вывод",padding=8); right.pack(side="left",fill="both",expand=True); self.tool_log=tk.Text(right,wrap="none",state="disabled"); self.tool_log.pack(fill="both",expand=True)

    def _build_queue_tab(self)->None:
        top=ttk.Frame(self.queue_tab); top.pack(fill="x",pady=(0,8))
        ttk.Button(top,text="Запустить очередь",command=self._start_queue,style="Primary.TButton").pack(side="left")
        ttk.Button(top,text="Остановить текущую",command=self._stop_current).pack(side="left",padx=8)
        ttk.Button(top,text="Удалить выбранные",command=self._delete_selected).pack(side="left")
        ttk.Button(top,text="Очистить завершённые",command=self._clear_finished).pack(side="left",padx=8)
        cols=("job","status","progress","output")
        self.queue_tree=ttk.Treeview(self.queue_tab,columns=cols,show="headings",selectmode="extended")
        for c,t,w in (("job","Задание",300),("status","Статус",140),("progress","Прогресс",100),("output","Выходной файл",500)):
            self.queue_tree.heading(c,text=t); self.queue_tree.column(c,width=w,anchor="w")
        self.queue_tree.pack(fill="both",expand=True)
        self.queue_log=ttk.Entry(self.queue_tab,state="readonly"); self.queue_log.pack(fill="x",pady=(8,0))

    def _build_settings_tab(self)->None:
        p=self.settings_tab
        paths=ttk.LabelFrame(p,text="Пути",padding=8); paths.pack(fill="x",pady=(0,8))
        self.set_cli=self._path_row(paths,0,"TwitchDownloaderCLI",file=True)
        self.set_ffmpeg=self._path_row(paths,1,"FFmpeg",file=True)
        self.set_output=self._path_row(paths,2,"Каталог VOD / clip",file=False)
        self.set_chat_output=self._path_row(paths,3,"Каталог chat",file=False)
        self.set_temp=self._path_row(paths,4,"Temporary",file=False)
        opts=ttk.LabelFrame(p,text="Значения по умолчанию",padding=8); opts.pack(fill="x",pady=8)
        ttk.Label(opts,text="Потоки").grid(row=0,column=0,sticky="w"); self.set_threads=ttk.Spinbox(opts,from_=1,to=64,width=8); self.set_threads.grid(row=0,column=1,sticky="w",padx=8)
        ttk.Label(opts,text="Bandwidth KiB/s (-1 = без лимита)").grid(row=0,column=2,sticky="w"); self.set_band=ttk.Entry(opts,width=10); self.set_band.grid(row=0,column=3,sticky="w",padx=8)
        ttk.Label(opts,text="Collision").grid(row=1,column=0,sticky="w",pady=5); self.set_collision=ttk.Combobox(opts,state="readonly",values=["Prompt","Overwrite","Exit","Rename"],width=14); self.set_collision.grid(row=1,column=1,sticky="w",padx=8)
        self.set_auto=tk.BooleanVar(); ttk.Checkbutton(opts,text="После добавления автоматически запускать очередь",variable=self.set_auto).grid(row=1,column=2,columnspan=2,sticky="w")
        auth=ttk.LabelFrame(p,text="OAuth",padding=8); auth.pack(fill="x",pady=8)
        ttk.Label(auth,text="Токен Twitch").pack(side="left"); self.set_oauth=ttk.Entry(auth,show="•"); self.set_oauth.pack(side="left",fill="x",expand=True,padx=10); ttk.Label(auth,text="Хранится локально в config.json.",style="Info.TLabel").pack(side="right")
        ttk.Button(p,text="Сохранить настройки",command=self._save_settings,style="Primary.TButton").pack(anchor="e")
        ttk.Label(p,text=f"Конфиг: {CONFIG_FILE}",style="Info.TLabel").pack(anchor="w",pady=(10,0))

    def _path_row(self,p,row,label,file=True):
        ttk.Label(p,text=label).grid(row=row,column=0,sticky="w",pady=4); e=ttk.Entry(p); e.grid(row=row,column=1,sticky="ew",padx=8); ttk.Button(p,text="…",command=(lambda e=e:self._choose_file(e) if file else self._choose_dir(e))).grid(row=row,column=2); p.columnconfigure(1,weight=1); return e

    def _load_settings_into_ui(self):
        self._set_entry(self.set_cli,self.settings.cli_path); self._set_entry(self.set_ffmpeg,self.settings.ffmpeg_path); self._set_entry(self.set_output,self.settings.output_dir); self._set_entry(self.set_chat_output,self.settings.chat_output_dir); self._set_entry(self.set_temp,self.settings.temp_dir)
        self._set_entry(self.set_threads,str(self.settings.threads)); self._set_entry(self.set_band,str(self.settings.bandwidth)); self.set_collision.set(self.settings.collision); self.set_auto.set(self.settings.auto_queue); self._set_entry(self.set_oauth,self.settings.oauth)
        self._set_entry(self.vod_threads,str(self.settings.threads)); self._set_entry(self.vod_band,str(self.settings.bandwidth)); self._set_entry(self.vod_oauth,str(self.settings.oauth)); self._set_entry(self.vod_ffmpeg,self.settings.ffmpeg_path); self._set_entry(self.vod_temp,self.settings.temp_dir)
        self._set_entry(self.clip_band,str(self.settings.bandwidth)); self._set_entry(self.clip_ffmpeg,self.settings.ffmpeg_path); self._set_entry(self.clip_temp,self.settings.temp_dir); self._set_entry(self.chat_threads,str(self.settings.threads))
        self._update_cli_status()

    @staticmethod
    def _set_entry(widget, value):
        widget.configure(state="normal"); widget.delete(0,"end"); widget.insert(0,value); widget.configure(state="normal")

    def _save_settings(self):
        try:
            self.settings.cli_path=self.set_cli.get().strip(); self.settings.ffmpeg_path=self.set_ffmpeg.get().strip(); self.settings.output_dir=self.set_output.get().strip() or str(DEFAULT_OUTPUT); self.settings.chat_output_dir=self.set_chat_output.get().strip() or str(DEFAULT_CHAT_OUTPUT); self.settings.temp_dir=self.set_temp.get().strip(); self.settings.threads=int(self.set_threads.get()); self.settings.bandwidth=int(self.set_band.get()); self.settings.collision=self.set_collision.get() or "Prompt"; self.settings.auto_queue=self.set_auto.get(); self.settings.oauth=self.set_oauth.get(); self.settings.save(); self.runner.settings=self.settings; self._load_settings_into_ui(); messagebox.showinfo(APP_NAME,"Настройки сохранены.")
        except ValueError as e: messagebox.showerror(APP_NAME,f"Проверьте числовые значения: {e}")

    def _update_cli_status(self):
        p=self.settings.cli_path or find_cli(); self.cli_status.configure(text=f"CLI: {Path(p).name}" if p else "CLI: не найден")

    def _choose_file(self,e):
        p=filedialog.askopenfilename();
        if p: self._set_entry(e,p)
    def _choose_dir(self,e):
        p=filedialog.askdirectory();
        if p: self._set_entry(e,p)
    def _choose_save(self,e,filetypes):
        p=filedialog.asksaveasfilename(filetypes=filetypes); 
        if p: self._set_entry(e,p)
    
    def _thread_task(self, fn:Callable, *args):
        def worker():
            try: self.ui_queue.put(("result", fn(*args)))
            except Exception as e: self.ui_queue.put(("error", str(e)))
        threading.Thread(target=worker,daemon=True).start()

    def _info_for(self,url:str,target:str,labels):
        self._thread_task(self._fetch_info_sync,url,target,labels)
    def _fetch_info_sync(self,url,target,labels):
        if not url.strip(): raise CliError("Введите URL или ID.")
        cmd=self.runner.info_command(url.strip())
        proc=subprocess.run(cmd,text=True,capture_output=True,encoding="utf-8",errors="replace")
        if proc.returncode!=0: raise CliError((proc.stderr or proc.stdout).strip() or f"CLI завершился с кодом {proc.returncode}")
        info=parse_info_output(proc.stdout,url.strip()); self.ui_queue.put(("info",(target,labels,info))); return info
    def _get_vod_info(self): self._info_for(self.vod_url.get(),"vod",self.vod_info_labels)
    def _get_clip_info(self): self._info_for(self.clip_url.get(),"clip",self.clip_info_labels)
    def _get_chat_info(self): self._info_for(self.chat_url.get(),"chat",self.chat_info_labels)

    def _fill_quality(self,combo,detail,info):
        values=["Автоматически — максимальное доступное"]+[q.label() for q in info.qualities]; combo["values"]=values; combo.current(0); detail.configure(text=f"Доступно: {len(info.qualities)}")

    def _queue_vod(self):
        try:
            url=self.vod_url.get().strip(); out=self.vod_output.get().strip()
            if not url: raise ValueError("Введите URL/ID VOD")
            if not out: out=str(Path(self.settings.output_dir).expanduser()/f"{url.split('/')[-1]}.mp4")
            ensure_dir(Path(out).parent)
            q=self.vod_quality.get(); cmd=self.runner.build_video(url,out,self._quality_name(self.vod_quality.get(),self.info_by_tab.get("vod")),self.vod_begin.get(),self.vod_end.get(),int(self.vod_threads.get()),int(self.vod_band.get()),self.vod_trim.get(),self.vod_collision.get(),self.vod_oauth.get(),self.vod_ffmpeg.get(),self.vod_temp.get())
            self._add_job(Job(f"VOD: {url}",cmd,out)); self._maybe_autostart()
        except Exception as e: messagebox.showerror(APP_NAME,str(e))

    def _queue_clip(self):
        try:
            url=self.clip_url.get().strip(); out=self.clip_output.get().strip()
            if not url: raise ValueError("Введите URL/ID клипа")
            if not out: out=str(Path(self.settings.output_dir).expanduser()/f"{url.split('/')[-1]}.mp4")
            ensure_dir(Path(out).parent); cmd=self.runner.build_clip(url,out,self._quality_name(self.clip_quality.get(),self.info_by_tab.get("clip")),int(self.clip_band.get()),self.clip_encode.get(),self.clip_collision.get(),self.clip_ffmpeg.get(),self.clip_temp.get()); self._add_job(Job(f"Клип: {url}",cmd,out)); self._maybe_autostart()
        except Exception as e: messagebox.showerror(APP_NAME,str(e))

    def _queue_chat_download(self):
        try:
            url=self.chat_url.get().strip(); out=self.chat_output.get().strip(); fmt=self.chat_format.get()
            if not url: raise ValueError("Введите URL/ID.")
            if not out:
                ext={"JSON":".json","JSON.GZ":".json.gz","HTML":".html","TXT":".txt"}[fmt]; out=str(Path(self.settings.chat_output_dir).expanduser()/f"{url.split('/')[-1]}_chat{ext}")
            compression="Gzip" if fmt=="JSON.GZ" else "None"; ensure_dir(Path(out).parent)
            cmd=self.runner.build_chat_download(url,out,compression,self.chat_begin.get(),self.chat_end.get(),self.chat_embed.get(),self.chat_bttv.get(),self.chat_ffz.get(),self.chat_stv.get(),self.chat_timestamp.get(),int(self.chat_threads.get()),self.chat_collision.get(),self.settings.temp_dir)
            self._add_job(Job(f"Чат: {url}",cmd,out)); self._maybe_autostart()
        except Exception as e: messagebox.showerror(APP_NAME,str(e))

    def _queue_chat_update(self):
        try:
            inp=self.cu_input.get().strip(); out=self.cu_output.get().strip()
            if not inp or not out: raise ValueError("Укажите входной и выходной файл.")
            cmd=self.runner.build_chat_update(inp,out,self.cu_compression.get(),self.cu_begin.get(),self.cu_end.get(),self.cu_embed.get(),self.cu_replace.get(),self.cu_bttv.get(),self.cu_ffz.get(),self.cu_stv.get(),self.cu_timestamp.get(),self.cu_collision.get(),self.settings.temp_dir)
            self._add_job(Job(f"Обновление чата: {Path(inp).name}",cmd,out)); self._maybe_autostart()
        except Exception as e: messagebox.showerror(APP_NAME,str(e))

    def _collect_cr(self):
        v={k:(x.get() if hasattr(x,"get") and not isinstance(x,tk.BooleanVar) else x.get()) for k,x in self.cr_values.items()}
        return v
    def _queue_chat_render(self):
        try:
            v=self._collect_cr(); inp=self.cr_input.get().strip(); out=self.cr_output.get().strip()
            if not inp or not out: raise ValueError("Укажите input и output.")
            v["input"]=inp; v["output"]=out; cmd=self.runner.build_chat_render(v); ensure_dir(Path(out).parent); self._add_job(Job(f"Рендер чата: {Path(inp).name}",cmd,out)); self._maybe_autostart()
        except Exception as e: messagebox.showerror(APP_NAME,str(e))

    def _quality_name(self,combo_value:str,info:Optional[MediaInfo])->str:
        if not info or combo_value.startswith("Автоматически"): return ""
        for q in info.qualities:
            if q.label()==combo_value: return q.name
        return combo_value.split(" · ",1)[0]

    def _add_job(self,job:Job): self.jobs.append(job); self._refresh_queue()
    def _maybe_autostart(self):
        if self.settings.auto_queue and self.running_process is None: self._start_queue()

    def _refresh_queue(self):
        if not hasattr(self,"queue_tree"): return
        selected=set(self.queue_tree.selection()); self.queue_tree.delete(*self.queue_tree.get_children())
        for i,j in enumerate(self.jobs):
            item=self.queue_tree.insert("","end",iid=str(i),values=(j.name,j.status,f"{j.progress}%",j.output))
            if str(i) in selected: self.queue_tree.selection_add(item)

    def _start_queue(self):
        if self.running_process is not None: return
        job=next((j for j in self.jobs if j.status in {"Ожидает","Ошибка"}),None)
        if job is None: return
        self.running_job=job; job.status="Запуск…"; job.progress=0; self._refresh_queue();
        threading.Thread(target=self._run_job, args=(job,),daemon=True).start()

    def _run_job(self,job:Job):
        try:
            self.ui_queue.put(("job-start",job))
            proc=subprocess.Popen(job.command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding="utf-8",errors="replace",bufsize=1)
            self.running_process=proc
            for line in proc.stdout or []:
                self.ui_queue.put(("job-line",(job,line.rstrip("\n"))))
            rc=proc.wait(); self.ui_queue.put(("job-done",(job,rc)))
        except Exception as e:
            self.ui_queue.put(("job-exception",(job,str(e))))

    def _stop_current(self):
        p=self.running_process
        if p and p.poll() is None:
            try: p.terminate()
            except Exception: pass

    def _delete_selected(self):
        ids=sorted([int(x) for x in self.queue_tree.selection()],reverse=True)
        for i in ids:
            if self.jobs[i] is not self.running_job: self.jobs.pop(i)
        self._refresh_queue()
    def _clear_finished(self):
        self.jobs=[j for j in self.jobs if j.status not in {"Готово","Ошибка","Остановлено"} or j is self.running_job]; self._refresh_queue()

    def _poll_events(self):
        try:
            while True:
                kind,data=self.ui_queue.get_nowait()
                if kind=="error": messagebox.showerror(APP_NAME,str(data))
                elif kind=="info":
                    target,labels,info=data; self.info_by_tab[target]=info; self._show_info(labels,info)
                    if target=="vod": self._fill_quality(self.vod_quality,self.vod_quality_detail,info)
                    elif target=="clip": self._fill_quality(self.clip_quality,self.clip_quality_detail,info)
                elif kind=="job-start": data.status="Выполняется"; data.started_at=time.time(); self._refresh_queue()
                elif kind=="job-line":
                    job,line=data; job.log=(job.log+line+"\n")[-12000:]; m=re.search(r"(\d{1,3})%",line); 
                    if m: job.progress=min(100,int(m.group(1))); self._refresh_queue(); self.queue_log.configure(state="normal"); self.queue_log.delete(0,"end"); self.queue_log.insert(0,line); self.queue_log.configure(state="readonly")
                elif kind=="job-done":
                    job,rc=data; job.returncode=rc; job.finished_at=time.time(); job.status="Готово" if rc==0 else "Ошибка"; job.progress=100 if rc==0 else job.progress; self.running_process=None; self.running_job=None; self._refresh_queue(); self._start_queue()
                elif kind=="job-exception":
                    job,error=data; job.status="Ошибка"; job.log += error; self.running_process=None; self.running_job=None; self._refresh_queue(); self._start_queue()
                elif kind=="tool":
                    self.tool_log.configure(state="normal"); self.tool_log.delete("1.0","end"); self.tool_log.insert("1.0",str(data)); self.tool_log.configure(state="disabled")
                elif kind=="result": pass
        except queue.Empty: pass
        self.after(100,self._poll_events)

    def _tool_help(self): self._run_tool(["help"])
    def _run_tool(self, args:list[str]):
        def worker():
            try:
                proc=subprocess.run([self.runner.cli(),*args],capture_output=True,text=True,encoding="utf-8",errors="replace")
                self.ui_queue.put(("tool",proc.stdout+"\n"+proc.stderr))
            except Exception as e:self.ui_queue.put(("tool",str(e)))
        threading.Thread(target=worker,daemon=True).start()
    def _tool_tsmerge(self):
        inp=filedialog.askopenfilename(title="Список TS / M3U8",filetypes=[("Playlist","*.txt *.m3u *.m3u8"), ("All","*")]);
        if not inp:return
        out=filedialog.asksaveasfilename(title="Выходной TS",defaultextension=".ts",filetypes=[("TS","*.ts"),("All","*")]);
        if not out:return
        try:
            cmd=[self.runner.cli(),"tsmerge","--input",inp,"--output",out,"--collision","Prompt"]; self._add_job(Job(f"TS merge: {Path(inp).name}",cmd,out)); self._maybe_autostart()
        except Exception as e: messagebox.showerror(APP_NAME,str(e))

    def _close(self):
        if self.running_process and self.running_process.poll() is None:
            if not messagebox.askyesno(APP_NAME,"Завершить текущую операцию и выйти?"): return
            self._stop_current()
        self._save_settings_silent(); self.destroy()
    def _save_settings_silent(self):
        try:
            self.settings.cli_path=self.set_cli.get().strip(); self.settings.ffmpeg_path=self.set_ffmpeg.get().strip(); self.settings.output_dir=self.set_output.get().strip(); self.settings.chat_output_dir=self.set_chat_output.get().strip(); self.settings.temp_dir=self.set_temp.get().strip(); self.settings.threads=int(self.set_threads.get()); self.settings.bandwidth=int(self.set_band.get()); self.settings.collision=self.set_collision.get() or "Prompt"; self.settings.auto_queue=self.set_auto.get(); self.settings.oauth=self.set_oauth.get(); self.settings.save()
        except Exception: pass


def main() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit("Требуется Python 3.10 или новее.")
    ensure_dir(DEFAULT_OUTPUT); ensure_dir(DEFAULT_CHAT_OUTPUT)
    app=TwitchDownloaderApp(); app.mainloop()

if __name__=="__main__": main()
