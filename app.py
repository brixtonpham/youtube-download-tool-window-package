import customtkinter as ctk
import hashlib
import threading
import queue
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import sys
import yt_dlp


def get_ffmpeg_location():
    app_dir = os.path.dirname(os.path.abspath(sys.argv[0] if sys.argv[0] else __file__))
    local_ffmpeg = os.path.join(app_dir, "ffmpeg", "ffmpeg.exe")
    if os.path.isfile(local_ffmpeg):
        return os.path.join(app_dir, "ffmpeg")
    return None


def is_ffmpeg_available():
    local = get_ffmpeg_location()
    if local:
        return True
    import shutil
    return shutil.which("ffmpeg") is not None


# ============================================================
# Database: per-channel progress tracking
# ============================================================

class DownloadDB:
    def __init__(self, db_path):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.Lock()
        self._init_tables()

    def _init_tables(self):
        with self.lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS channels (
                    id          TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    url         TEXT NOT NULL,
                    handle      TEXT,
                    created_at  TEXT DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS videos (
                    id          TEXT PRIMARY KEY,
                    channel_id  TEXT NOT NULL REFERENCES channels(id),
                    title       TEXT NOT NULL,
                    url         TEXT NOT NULL,
                    position    INTEGER DEFAULT 0,
                    status      TEXT NOT NULL DEFAULT 'pending',
                    file_path   TEXT,
                    downloaded_at TEXT,
                    error_msg   TEXT,
                    created_at  TEXT DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id);
                CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
            """)
            self.conn.execute("UPDATE videos SET status='pending' WHERE status='downloading'")
            self.conn.commit()

    def upsert_channel(self, channel_id, name, url, handle=None):
        with self.lock:
            self.conn.execute(
                "INSERT INTO channels (id, name, url, handle) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, url=excluded.url, handle=excluded.handle",
                (channel_id, name, url, handle))
            self.conn.commit()

    def bulk_upsert_videos(self, videos_data):
        with self.lock:
            for pos, (video_id, channel_id, title, url) in enumerate(videos_data):
                self.conn.execute(
                    "INSERT INTO videos (id, channel_id, title, url, position) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET position=excluded.position",
                    (video_id, channel_id, title, url, pos))
            self.conn.commit()

    def bulk_get_statuses(self, channel_id):
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, status FROM videos WHERE channel_id=?", (channel_id,)).fetchall()
            return {r["id"]: r["status"] for r in rows}

    def set_video_status(self, video_id, status, file_path=None, error_msg=None):
        with self.lock:
            downloaded_at = datetime.now(timezone.utc).isoformat() if status == "completed" else None
            self.conn.execute(
                "UPDATE videos SET status=?, file_path=?, downloaded_at=?, error_msg=? WHERE id=?",
                (status, file_path, downloaded_at, error_msg, video_id))
            self.conn.commit()

    def get_video_status(self, video_id):
        with self.lock:
            row = self.conn.execute("SELECT status FROM videos WHERE id=?", (video_id,)).fetchone()
            return row["status"] if row else None

    def get_channel_videos(self, channel_id):
        with self.lock:
            return self.conn.execute(
                "SELECT id, title, url, status FROM videos WHERE channel_id=? ORDER BY position, rowid",
                (channel_id,)).fetchall()

    def get_all_channels(self):
        with self.lock:
            return self.conn.execute(
                "SELECT c.id, c.name, c.handle, c.url, "
                "  (SELECT COUNT(*) FROM videos v WHERE v.channel_id = c.id) as total, "
                "  (SELECT COUNT(*) FROM videos v WHERE v.channel_id = c.id AND v.status = 'completed') as done "
                "FROM channels c ORDER BY c.created_at DESC").fetchall()

    def get_channel_stats(self, channel_id):
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) as total, "
                "  SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as done, "
                "  SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed "
                "FROM videos WHERE channel_id=?", (channel_id,)).fetchone()
            return dict(row) if row else {"total": 0, "done": 0, "failed": 0}

    def close(self):
        self.conn.close()


# ============================================================
# Cookie auto-extraction from browser
# ============================================================

def detect_browsers():
    browsers = []
    if os.name == "nt":
        appdata = os.environ.get("APPDATA", "")
        localappdata = os.environ.get("LOCALAPPDATA", "")
        if Path(appdata, "Mozilla/Firefox/Profiles").exists():
            browsers.append("Firefox")
        if Path(localappdata, "Google/Chrome/User Data").exists():
            browsers.append("Chrome")
        if Path(localappdata, "Microsoft/Edge/User Data").exists():
            browsers.append("Edge")
        if Path(localappdata, "BraveSoftware/Brave-Browser/User Data").exists():
            browsers.append("Brave")
    else:
        home = Path.home()
        if (home / ".mozilla/firefox").exists() or \
           (home / "Library/Application Support/Firefox/Profiles").exists():
            browsers.append("Firefox")
        if (home / ".config/google-chrome").exists() or \
           (home / "Library/Application Support/Google/Chrome").exists():
            browsers.append("Chrome")
        if (home / "Library/Application Support/Microsoft Edge").exists() or \
           (home / ".config/microsoft-edge").exists():
            browsers.append("Edge")
    return browsers


def get_cookie_opts(cookie_mode, cookie_file_path=None):
    mode_map = {
        "Auto (Firefox)": ("firefox",),
        "Firefox": ("firefox",),
        "Chrome": ("chrome",),
        "Edge": ("edge",),
        "Brave": ("brave",),
    }
    if cookie_mode in mode_map:
        return {"cookiesfrombrowser": mode_map[cookie_mode]}
    if cookie_mode == "Cookie File" and cookie_file_path:
        return {"cookiefile": cookie_file_path}
    return {}


def format_speed(speed_bps):
    if not speed_bps:
        return ""
    if speed_bps > 1_000_000:
        return f"{speed_bps / 1_000_000:.1f}MB/s"
    if speed_bps > 1_000:
        return f"{speed_bps / 1_000:.0f}KB/s"
    return f"{speed_bps:.0f}B/s"


# ============================================================
# Video row widget
# ============================================================

class VideoRow(ctk.CTkFrame):
    def __init__(self, master, index, video_id, title, url, initial_status="pending"):
        super().__init__(master, fg_color="transparent")
        self.video_id = video_id
        self.url = url

        self.grid_columnconfigure(2, weight=1)

        self.selected = ctk.BooleanVar(value=(initial_status != "completed"))
        self.checkbox = ctk.CTkCheckBox(self, text="", variable=self.selected,
                                        width=24, checkbox_width=18, checkbox_height=18)
        self.checkbox.grid(row=0, column=0, padx=(2, 2))

        self.num_label = ctk.CTkLabel(self, text=f"{index}.", width=30, anchor="e",
                                      font=ctk.CTkFont(size=12))
        self.num_label.grid(row=0, column=1, padx=(0, 4), sticky="w")

        self.title_label = ctk.CTkLabel(self, text=title, anchor="w", wraplength=440,
                                        font=ctk.CTkFont(size=12))
        self.title_label.grid(row=0, column=2, sticky="ew")

        self.status_label = ctk.CTkLabel(self, text="", width=130, anchor="e",
                                         font=ctk.CTkFont(size=12), text_color="gray")
        self.status_label.grid(row=0, column=3, padx=(6, 0))

        self.progress = ctk.CTkProgressBar(self, height=6)
        self.progress.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(2, 4))
        self.progress.set(0)

        self._apply_status(initial_status)

    def _apply_status(self, status):
        status_map = {
            "pending":     ("Queued",          "gray",    0),
            "completed":   ("Done ✓",          "green",   1.0),
            "failed":      ("Error ✗",         "red",     0),
            "skipped":     ("Skipped ⏭",       "#888888", 1.0),
            "downloading": ("Queued",          "gray",    0),
        }
        text, color, prog = status_map.get(status, ("Queued", "gray", 0))
        self.status_label.configure(text=text, text_color=color)
        self.progress.set(prog)

    def set_status(self, text, color="gray"):
        self.status_label.configure(text=text, text_color=color)

    def set_progress(self, value):
        self.progress.set(value)


# ============================================================
# Main App
# ============================================================

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("YouTube Channel Downloader")
        self.geometry("780x700")
        self.minsize(650, 500)

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        db_dir = os.path.join(os.path.expanduser("~"), ".yt-channel-dl")
        self.db = DownloadDB(os.path.join(db_dir, "history.db"))

        self.update_queue = queue.Queue()
        self.video_rows = {}
        self.is_downloading = False
        self.should_stop = False
        self.current_channel_id = None
        self.cookie_file_path = None
        self._fetch_in_progress = False

        self._build_ui()
        self._poll_queue()
        self._load_channel_history()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)

        # --- Row 0: Channel history ---
        hist_frame = ctk.CTkFrame(self)
        hist_frame.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 4))
        hist_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(hist_frame, text="History:", font=ctk.CTkFont(size=12, weight="bold")
                     ).grid(row=0, column=0, padx=(8, 6), pady=6)

        self.history_var = ctk.StringVar(value="-- New channel --")
        self.history_menu = ctk.CTkOptionMenu(hist_frame, variable=self.history_var,
                                              values=["-- New channel --"],
                                              command=self._on_history_select, width=400)
        self.history_menu.grid(row=0, column=1, sticky="ew", padx=4, pady=6)

        # --- Row 1: URL input ---
        top = ctk.CTkFrame(self)
        top.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 4))
        top.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(top, text="Channel URL:", font=ctk.CTkFont(size=13, weight="bold")
                     ).grid(row=0, column=0, padx=(8, 6), pady=8)

        self.url_entry = ctk.CTkEntry(top, placeholder_text="https://www.youtube.com/@channel",
                                      font=ctk.CTkFont(size=13))
        self.url_entry.grid(row=0, column=1, sticky="ew", pady=8)

        self.fetch_btn = ctk.CTkButton(top, text="Fetch Videos", width=120,
                                       command=self._on_fetch)
        self.fetch_btn.grid(row=0, column=2, padx=(6, 8), pady=8)

        # --- Row 2: Options ---
        opts = ctk.CTkFrame(self)
        opts.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 4))

        ctk.CTkLabel(opts, text="Save to:").grid(row=0, column=0, padx=(8, 4), pady=6)
        self.dir_entry = ctk.CTkEntry(opts, width=250, font=ctk.CTkFont(size=12))
        self.dir_entry.insert(0, os.path.join(os.path.expanduser("~"), "YouTube"))
        self.dir_entry.grid(row=0, column=1, pady=6)

        self.browse_btn = ctk.CTkButton(opts, text="Browse", width=60,
                                        command=self._browse_dir)
        self.browse_btn.grid(row=0, column=2, padx=4, pady=6)

        self.open_folder_btn = ctk.CTkButton(opts, text="Open", width=50,
                                             command=self._open_output_dir)
        self.open_folder_btn.grid(row=0, column=3, padx=(0, 4), pady=6)

        ctk.CTkLabel(opts, text="Quality:").grid(row=0, column=3, padx=(8, 4), pady=6)
        self.quality_var = ctk.StringVar(value="1080p")
        self.quality_menu = ctk.CTkOptionMenu(opts, values=["360p", "480p", "720p", "1080p", "Best"],
                                              variable=self.quality_var, width=80)
        self.quality_menu.grid(row=0, column=4, pady=6)

        # --- Row 3: Cookie + Actions ---
        opts2 = ctk.CTkFrame(self)
        opts2.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 6))
        opts2.grid_columnconfigure(2, weight=1)

        ctk.CTkLabel(opts2, text="Cookie:").grid(row=0, column=0, padx=(8, 4), pady=6)

        browsers = detect_browsers()
        cookie_options = ["None"]
        for b in browsers:
            if b == "Firefox":
                cookie_options.insert(0, "Auto (Firefox)")
            else:
                cookie_options.append(b)
        cookie_options.append("Cookie File")

        self.cookie_var = ctk.StringVar(value=cookie_options[0])
        self.cookie_menu = ctk.CTkOptionMenu(opts2, values=cookie_options,
                                              variable=self.cookie_var, width=140,
                                              command=self._on_cookie_change)
        self.cookie_menu.grid(row=0, column=1, pady=6)

        self.cookie_info = ctk.CTkLabel(opts2, text="↻ Fresh cookie per video" if cookie_options[0] != "None" else "",
                                        font=ctk.CTkFont(size=11), text_color="#888888")
        self.cookie_info.grid(row=0, column=2, padx=8, pady=6, sticky="w")

        self.download_btn = ctk.CTkButton(opts2, text="Download All", width=120,
                                          fg_color="green", hover_color="#228B22",
                                          command=self._on_download, state="disabled")
        self.download_btn.grid(row=0, column=3, padx=(4, 4), pady=6)

        self.stop_btn = ctk.CTkButton(opts2, text="Stop", width=60,
                                      fg_color="red", hover_color="#8B0000",
                                      command=self._on_stop, state="disabled")
        self.stop_btn.grid(row=0, column=4, padx=(0, 8), pady=6)

        # --- Row 4: Select controls + Video list ---
        list_header = ctk.CTkFrame(self, fg_color="transparent")
        list_header.grid(row=4, column=0, sticky="ew", padx=12, pady=(0, 2))

        self.select_all_btn = ctk.CTkButton(list_header, text="Select All", width=80,
                                            height=26, font=ctk.CTkFont(size=11),
                                            command=self._select_all)
        self.select_all_btn.grid(row=0, column=0, padx=(0, 4))

        self.select_none_btn = ctk.CTkButton(list_header, text="Select None", width=80,
                                             height=26, font=ctk.CTkFont(size=11),
                                             command=self._select_none)
        self.select_none_btn.grid(row=0, column=1, padx=(0, 4))

        self.select_failed_btn = ctk.CTkButton(list_header, text="Select Failed", width=90,
                                               height=26, font=ctk.CTkFont(size=11),
                                               command=self._select_failed)
        self.select_failed_btn.grid(row=0, column=2)

        self.scroll_frame = ctk.CTkScrollableFrame(self, label_text="Videos")
        self.scroll_frame.grid(row=5, column=0, sticky="nsew", padx=12, pady=(0, 6))
        self.scroll_frame.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)

        # --- Row 6: Status bar ---
        self.status_bar = ctk.CTkLabel(self, text="Select a channel from history or paste a new URL",
                                       font=ctk.CTkFont(size=12), anchor="w")
        self.status_bar.grid(row=6, column=0, sticky="ew", padx=16, pady=(0, 10))

    # --- Selection ---
    def _select_all(self):
        for row in self.video_rows.values():
            row.selected.set(True)

    def _select_none(self):
        for row in self.video_rows.values():
            row.selected.set(False)

    def _select_failed(self):
        for row in self.video_rows.values():
            status_text = row.status_label.cget("text") or ""
            row.selected.set("Error" in status_text or "Queued" in status_text)

    # --- Cookie ---
    def _on_cookie_change(self, value):
        if value == "Cookie File":
            from tkinter import filedialog
            f = filedialog.askopenfilename(
                title="Select cookies.txt",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
            if f:
                self.cookie_file_path = f
                self.cookie_info.configure(text=os.path.basename(f))
            else:
                self.cookie_var.set("None")
                self.cookie_file_path = None
                self.cookie_info.configure(text="")
        elif value == "None":
            self.cookie_file_path = None
            self.cookie_info.configure(text="No cookie (guest mode)")
        else:
            self.cookie_file_path = None
            self.cookie_info.configure(text="↻ Fresh cookie per video")

    # --- Channel history ---
    def _load_channel_history(self):
        channels = self.db.get_all_channels()
        values = ["-- New channel --"]
        self._channel_map = {}
        for ch in channels:
            label = f"{ch['name']} ({ch['done']}/{ch['total']})"
            values.append(label)
            self._channel_map[label] = dict(ch)
        self.history_menu.configure(values=values)

    def _on_history_select(self, value):
        if self.is_downloading:
            return

        if value == "-- New channel --":
            self.url_entry.delete(0, "end")
            self._clear_video_list()
            self.current_channel_id = None
            self.status_bar.configure(text="Paste a new channel URL")
            self.download_btn.configure(state="disabled")
            return

        ch = self._channel_map.get(value)
        if not ch:
            return

        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, ch["url"])
        self.current_channel_id = ch["id"]

        self._clear_video_list()
        videos = self.db.get_channel_videos(ch["id"])
        for i, v in enumerate(videos, 1):
            row = VideoRow(self.scroll_frame, i, v["id"], v["title"], v["url"], v["status"])
            row.grid(row=i - 1, column=0, sticky="ew", pady=1)
            self.video_rows[v["id"]] = row

        stats = self.db.get_channel_stats(ch["id"])
        self.download_btn.configure(state="normal")
        self.status_bar.configure(
            text=f"Channel: {ch['name']} — Done: {stats['done']}/{stats['total']} | "
                 f"Failed: {stats['failed']}")

    # --- Helpers ---
    def _browse_dir(self):
        from tkinter import filedialog
        d = filedialog.askdirectory()
        if d:
            self.dir_entry.delete(0, "end")
            self.dir_entry.insert(0, d)

    def _open_output_dir(self):
        d = self.dir_entry.get().strip() or os.path.join(os.path.expanduser("~"), "YouTube")
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        import subprocess
        if sys.platform == "win32":
            os.startfile(d)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", d])
        else:
            subprocess.Popen(["xdg-open", d])

    def _normalize_channel_url(self, url):
        url = url.strip().rstrip("/")
        if url.startswith("http"):
            parsed = urlparse(url)
            url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
        elif url.startswith("@"):
            url = "https://www.youtube.com/" + url
        else:
            url = "https://www.youtube.com/@" + url
        if not re.search(r"/(videos|streams|shorts)$", url):
            url += "/videos"
        return url

    def _quality_format(self):
        has_ffmpeg = is_ffmpeg_available()
        q = self.quality_var.get()
        if has_ffmpeg:
            if q == "Best":
                return ("bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]"
                        "/bestvideo[ext=mp4]+bestaudio[ext=m4a]"
                        "/bestvideo+bestaudio/best")
            h = q.replace("p", "")
            return (f"bestvideo[height<={h}][vcodec^=avc1]+bestaudio[ext=m4a]"
                    f"/bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
                    f"/bestvideo[height<={h}]+bestaudio/best")
        if q == "Best":
            return "best"
        height = q.replace("p", "")
        return f"best[height<={height}]/best"

    # --- Fetch ---
    def _on_fetch(self):
        if self._fetch_in_progress or self.is_downloading:
            return
        url = self.url_entry.get().strip()
        if not url:
            return

        self._fetch_in_progress = True
        self.fetch_btn.configure(state="disabled", text="Fetching...")
        self.download_btn.configure(state="disabled")
        self.history_menu.configure(state="disabled")
        self._clear_video_list()
        self.status_bar.configure(text="Scanning channel for videos...")

        cookie_opts = get_cookie_opts(self.cookie_var.get(), self.cookie_file_path)
        threading.Thread(target=self._fetch_worker, args=(url, cookie_opts), daemon=True).start()

    def _fetch_worker(self, url, cookie_opts):
        url = self._normalize_channel_url(url)
        opts = {
            "flat_playlist": True,
            "quiet": True,
            "extract_flat": "in_playlist",
            "ignoreerrors": True,
        }
        opts.update(cookie_opts)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                entries = list(info.get("entries") or []) if info else []

            channel_id = (info.get("channel_id") or info.get("uploader_id")
                          or info.get("id")
                          or hashlib.sha256(url.encode()).hexdigest()[:16])
            channel_name = info.get("channel") or info.get("uploader") or info.get("title") or "Unknown"
            channel_handle = None
            handle_match = re.search(r"@([\w.-]+)", url)
            if handle_match:
                channel_handle = "@" + handle_match.group(1)

            videos = []
            for e in entries:
                if e is None:
                    continue
                vid = e.get("id", "")
                if not vid:
                    continue
                title = e.get("title") or e.get("url") or vid
                video_url = e.get("url") or f"https://www.youtube.com/watch?v={vid}"
                videos.append((vid, title, video_url))

            self.update_queue.put(("fetch_done", {
                "channel_id": channel_id,
                "channel_name": channel_name,
                "channel_handle": channel_handle,
                "channel_url": url,
                "videos": videos,
            }))
        except Exception as ex:
            self.update_queue.put(("fetch_error", str(ex)))

    # --- Download ---
    def _on_download(self):
        if not self.video_rows or self.is_downloading:
            return

        self.is_downloading = True
        self.should_stop = False
        self.download_btn.configure(state="disabled")
        self.fetch_btn.configure(state="disabled")
        self.history_menu.configure(state="disabled")
        self.stop_btn.configure(state="normal")

        if not is_ffmpeg_available():
            self.status_bar.configure(
                text="WARNING: FFmpeg not found. Video quality limited. Install FFmpeg for best results.")

        output_dir = self.dir_entry.get().strip() or os.path.join(os.path.expanduser("~"), "YouTube")
        fmt = self._quality_format()
        cookie_mode = self.cookie_var.get()
        cookie_file = self.cookie_file_path
        channel_id = self.current_channel_id

        video_list = [(vid, row.url) for vid, row in self.video_rows.items() if row.selected.get()]
        if not video_list:
            self.status_bar.configure(text="No videos selected. Use checkboxes to select videos.")
            self.is_downloading = False
            self.download_btn.configure(state="normal")
            self.fetch_btn.configure(state="normal")
            self.history_menu.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            return

        threading.Thread(target=self._download_worker,
                         args=(video_list, output_dir, fmt, cookie_mode, cookie_file, channel_id),
                         daemon=True).start()

    def _build_download_opts(self, fmt, output_dir, progress_hook, cookie_opts):
        opts = {
            "format": fmt,
            "outtmpl": os.path.join(output_dir, "%(channel)s", "%(title)s [%(id)s].%(ext)s"),
            "windowsfilenames": True,
            "quiet": True,
            "no_warnings": True,
            "sleep_interval": 5,
            "max_sleep_interval": 10,
            "sleep_interval_requests": 1.5,
            "retries": 10,
            "fragment_retries": 15,
            "concurrent_fragment_downloads": 3,
            "progress_hooks": [progress_hook],
        }
        ffmpeg_dir = get_ffmpeg_location()
        if ffmpeg_dir:
            opts["ffmpeg_location"] = ffmpeg_dir
        if is_ffmpeg_available():
            opts["merge_output_format"] = "mp4"
            opts["postprocessor_args"] = {
                "merger": ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                           "-movflags", "+faststart"],
            }
        opts.update(cookie_opts)
        return opts

    def _try_download(self, url, opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.download([url])

    def _download_worker(self, video_list, output_dir, fmt, cookie_mode, cookie_file, channel_id):
        os.makedirs(output_dir, exist_ok=True)
        total = len(video_list)
        cookie_failed = False

        for i, (vid, url) in enumerate(video_list):
            if self.should_stop:
                self.update_queue.put(("download_stopped", None))
                return

            existing = self.db.get_video_status(vid)
            if existing == "completed":
                self.update_queue.put(("video_skipped", vid))
                continue

            self.update_queue.put(("video_start", (vid, i + 1, total)))
            self.db.set_video_status(vid, "downloading")

            def progress_hook(d, _vid=vid):
                if d["status"] == "downloading":
                    total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    downloaded = d.get("downloaded_bytes", 0)
                    speed = format_speed(d.get("speed"))
                    pct = downloaded / total_bytes if total_bytes > 0 else 0
                    self.update_queue.put(("video_progress", (_vid, pct, speed)))
                elif d["status"] == "finished":
                    self.update_queue.put(("video_merging", _vid))

            use_cookie = cookie_mode != "None" and not cookie_failed
            cookie_opts = get_cookie_opts(cookie_mode, cookie_file) if use_cookie else {}

            opts = self._build_download_opts(fmt, output_dir, progress_hook, cookie_opts)

            try:
                ret = self._try_download(url, opts)
                if ret != 0:
                    if use_cookie:
                        self.update_queue.put(("cookie_fallback", vid))
                        opts_no_cookie = self._build_download_opts(fmt, output_dir, progress_hook, {})
                        ret = self._try_download(url, opts_no_cookie)

                if ret != 0:
                    self.db.set_video_status(vid, "failed", error_msg="Download returned non-zero")
                    self.update_queue.put(("video_error", (vid, "Download failed")))
                else:
                    self.db.set_video_status(vid, "completed")
                    self.update_queue.put(("video_done", vid))
            except Exception as ex:
                err_msg = str(ex).lower()
                if use_cookie and ("cookie" in err_msg or "permission" in err_msg
                                   or "could not find" in err_msg or "decrypt" in err_msg
                                   or "locked" in err_msg):
                    cookie_failed = True
                    self.update_queue.put(("cookie_error", cookie_mode))
                    try:
                        opts_no_cookie = self._build_download_opts(fmt, output_dir, progress_hook, {})
                        ret = self._try_download(url, opts_no_cookie)
                        if ret == 0:
                            self.db.set_video_status(vid, "completed")
                            self.update_queue.put(("video_done", vid))
                            continue
                    except Exception:
                        pass
                self.db.set_video_status(vid, "failed", error_msg=str(ex))
                self.update_queue.put(("video_error", (vid, str(ex))))

        self.update_queue.put(("all_done", None))

    def _on_stop(self):
        self.should_stop = True
        self.stop_btn.configure(state="disabled")
        self.status_bar.configure(text="Stopping after current video...")

    def _on_close(self):
        self.should_stop = True
        if self.is_downloading:
            self.status_bar.configure(text="Closing after current video... (close terminal to force-quit)")
            self.after(500, self._on_close)
            return
        self.db.close()
        self.destroy()

    # --- Queue polling (C1 fix: exception-safe) ---
    def _poll_queue(self):
        try:
            while True:
                msg_type, data = self.update_queue.get_nowait()
                try:
                    self._handle_message(msg_type, data)
                except Exception:
                    pass
        except queue.Empty:
            pass
        finally:
            self.after(100, self._poll_queue)

    def _handle_message(self, msg_type, data):
        if msg_type == "fetch_done":
            self._fetch_in_progress = False
            ch = data
            self.fetch_btn.configure(state="normal", text="Fetch Videos")
            self.history_menu.configure(state="normal")
            videos = ch["videos"]
            if not videos:
                self.status_bar.configure(text="No videos found. Check the URL.")
                return

            self.current_channel_id = ch["channel_id"]
            self.db.upsert_channel(ch["channel_id"], ch["channel_name"],
                                   ch["channel_url"], ch["channel_handle"])

            videos_data = [(vid, ch["channel_id"], title, url) for vid, title, url in videos]
            self.db.bulk_upsert_videos(videos_data)
            statuses = self.db.bulk_get_statuses(ch["channel_id"])

            self._build_rows_chunked(videos, statuses, 0)

            self._load_channel_history()
            stats = self.db.get_channel_stats(ch["channel_id"])
            self.download_btn.configure(state="normal")
            self.status_bar.configure(
                text=f"{ch['channel_name']} — {len(videos)} videos "
                     f"(Done: {stats['done']} | Remaining: {stats['total'] - stats['done']})")

        elif msg_type == "fetch_error":
            self._fetch_in_progress = False
            self.fetch_btn.configure(state="normal", text="Fetch Videos")
            self.history_menu.configure(state="normal")
            self.status_bar.configure(text=f"Error: {data}")

        elif msg_type == "video_skipped":
            vid = data
            if vid in self.video_rows:
                self.video_rows[vid].set_progress(1.0)
                self.video_rows[vid].set_status("Done ✓", "green")

        elif msg_type == "cookie_error":
            browser = data
            self.cookie_var.set("None")
            self.cookie_info.configure(text=f"{browser} cookies failed, using guest mode")
            self.status_bar.configure(
                text=f"Cookie error: {browser} cookies cannot be read. Switched to guest mode. "
                     f"On Windows, only Firefox cookies work reliably.")

        elif msg_type == "cookie_fallback":
            vid = data
            if vid in self.video_rows:
                self.video_rows[vid].set_status("Retrying...", "orange")

        elif msg_type == "video_start":
            vid, num, total = data
            if vid in self.video_rows:
                self.video_rows[vid].set_status("Downloading...", "#3B8ED0")
                self.video_rows[vid].set_progress(0)
            self.status_bar.configure(text=f"Downloading {num}/{total}...")

        elif msg_type == "video_progress":
            vid, pct, speed = data
            if vid in self.video_rows:
                self.video_rows[vid].set_progress(min(pct, 1.0))
                pct_text = f"{pct * 100:.0f}%"
                extra = f" {speed}" if speed else ""
                self.video_rows[vid].set_status(f"{pct_text}{extra}", "#3B8ED0")

        elif msg_type == "video_merging":
            vid = data
            if vid in self.video_rows:
                self.video_rows[vid].set_status("Merging...", "orange")

        elif msg_type == "video_done":
            vid = data
            if vid in self.video_rows:
                self.video_rows[vid].set_progress(1.0)
                self.video_rows[vid].set_status("Done ✓", "green")

        elif msg_type == "video_error":
            vid, err = data
            if vid in self.video_rows:
                self.video_rows[vid].set_status("Error ✗", "red")

        elif msg_type == "all_done":
            self._finish_download("All downloads complete!")

        elif msg_type == "download_stopped":
            self._finish_download("Stopped by user.")

    def _build_rows_chunked(self, videos, statuses, start, chunk_size=50):
        end = min(start + chunk_size, len(videos))
        for i in range(start, end):
            vid, title, url = videos[i]
            status = statuses.get(vid, "pending")
            row = VideoRow(self.scroll_frame, i + 1, vid, title, url, status)
            row.grid(row=i, column=0, sticky="ew", pady=1)
            self.video_rows[vid] = row
        if end < len(videos):
            self.after(1, lambda: self._build_rows_chunked(videos, statuses, end, chunk_size))

    def _finish_download(self, message):
        self.is_downloading = False
        self.download_btn.configure(state="normal")
        self.fetch_btn.configure(state="normal")
        self.history_menu.configure(state="normal")
        self.stop_btn.configure(state="disabled")

        if self.current_channel_id:
            stats = self.db.get_channel_stats(self.current_channel_id)
            self.status_bar.configure(
                text=f"{message} (Done: {stats['done']} | Failed: {stats['failed']} | Total: {stats['total']})")
            self._load_channel_history()
        else:
            self.status_bar.configure(text=message)

    def _clear_video_list(self):
        for w in self.scroll_frame.winfo_children():
            w.destroy()
        self.video_rows.clear()


if __name__ == "__main__":
    app = App()
    app.mainloop()
