"""Windows PC save-game library. Standard-library only; Python 3.10+ and Tk."""
from __future__ import annotations

import json
import configparser
import io
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import unicodedata
import urllib.parse
import urllib.request
import ctypes
from ctypes import wintypes
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_NAME = "存档管家"
APP_VERSION = "1.0.0"
BG, PANEL, CARD, TEXT, MUTED, ACCENT = "#0b1020", "#121a2d", "#172238", "#eef3ff", "#94a3bd", "#8b9cff"
GREEN, LINE = "#56d6a1", "#26324a"
HOME = Path.home()
DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(HOME))) / "SaveLibrary"
DATA_FILE = DATA_DIR / "library.json"
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
ICON_DIR = APP_DIR / "icon"
UI_FILE = APP_DIR / "存档管家.ini"
DEFAULT_UI_STATE = {"geometry": "1798x1056+500+200", "sash_fraction": "0.39003322259136214",
                    "window_state": "normal", "font_size": "15", "split_user_adjusted": "true",
                    "split_version": "6"}
SKIP = {"cache", "caches", "crashdumps", "logs", "log", "shadercache", "temp", "tmp", "webcache",
        "config", "settings", "screenshots", "mods", "workshop", "achievements"}
SOFTWARE_DIRS = {"feishu", "jianying", "jianyingpro", "capcut", "xwechat_files", "wechat files",
                 "wechat", "weixin", "wecom", "wxwork", "discord", "telegram", "slack",
                 "msteams", "teams", "chrome", "chromium", "edge", "firefox", "opera",
                 "bravesoftware", "spotify", "adobe", "everything", "voidtools"}
GENERIC = {"save", "saves", "saved", "savegames", "saved games", "savedata", "game saves", "gse saves",
           "rune", "steam", "userdata", "remote", "remotecache", "profile", "profiles", "slot", "slots", "games"}
SAVE_WORDS = re.compile(r"save|profile|slot|world|checkpoint|campaign", re.I)
SAVE_EXTENSIONS = {".sav", ".save", ".sav2", ".slot", ".profile", ".world", ".checkpoint", ".dsv", ".ess", ".fos"}
LIKELY_DATA_EXTENSIONS = {".dat", ".db", ".sqlite", ".bin", ".json", ".xml"}


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().replace("&", "and")
    return "".join(char for char in text if char.isalnum())


def _steam_names() -> dict[str, str]:
    result = {}
    for root in known_roots():
        if root.name.casefold() != "userdata":
            continue
        for manifest in (root.parent / "steamapps").glob("appmanifest_*.acf"):
            try:
                data = manifest.read_text(errors="ignore")
                aid = re.search(r'"appid"\s+"(\d+)"', data)
                name = re.search(r'"name"\s+"([^"]+)"', data)
                if aid and name:
                    result[aid.group(1)] = name.group(1)
            except OSError:
                continue
    return result


def _steam_search(query: str) -> dict | None:
    if len(query.strip()) < 3:
        return None
    is_cjk = any("\u3400" <= char <= "\u9fff" for char in query)
    url = "https://store.steampowered.com/api/storesearch/?" + urllib.parse.urlencode(
        {"term": query, "l": "schinese" if is_cjk else "english", "cc": "CN" if is_cjk else "US"})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SaveLibrary/1.0"})
        with urllib.request.urlopen(req, timeout=4) as response:
            items = json.loads(response.read().decode("utf-8", "replace")).get("items", [])
        candidates = [x for x in items if x.get("type") == "app" and x.get("id")]
        q = _normalize(query)
        ranked = sorted(candidates, key=lambda x: SequenceMatcher(None, q, _normalize(x.get("name", ""))).ratio(), reverse=True)
        if not ranked:
            return None
        best = ranked[0]
        name = best.get("name", "")
        score = SequenceMatcher(None, q, _normalize(name)).ratio()
        normalized_name = _normalize(name)
        if q != normalized_name and score < .92 and not (len(q) >= 8 and q in normalized_name):
            return None
        appid = str(best["id"])
        return {"appid": appid, "title": name,
                "icon_url": f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/library_600x900.jpg",
                "provider": "Steam", "confirmed": True}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _steam_details(appid: str) -> dict | None:
    """Resolve a numbered Steam/RUNE save folder using its Steam AppID."""
    url = "https://store.steampowered.com/api/appdetails?" + urllib.parse.urlencode({
        "appids": appid, "l": "schinese", "cc": "CN"})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SaveLibrary/1.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            entry = json.loads(response.read().decode("utf-8", "replace")).get(appid, {})
        data = entry.get("data", {})
        if entry.get("success") and data.get("name"):
            return {"appid": appid, "title": data["name"],
                    "icon_url": data.get("header_image") or f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg",
                    "provider": "Steam", "confirmed": True}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _wikipedia_search(query: str) -> dict | None:
    """Use a tightly matched Wikipedia page image when Steam has no confident result."""
    is_cjk = any("\u3400" <= char <= "\u9fff" for char in query)
    hosts = ["zh.wikipedia.org", "en.wikipedia.org"] if is_cjk else ["en.wikipedia.org", "zh.wikipedia.org"]
    headers = {"User-Agent": "SaveLibrary/1.0 (desktop game library)"}
    for host in hosts:
        try:
            search_url = f"https://{host}/w/api.php?" + urllib.parse.urlencode({
                "action": "query", "list": "search", "srsearch": query, "format": "json", "utf8": 1, "srlimit": 5})
            with urllib.request.urlopen(urllib.request.Request(search_url, headers=headers), timeout=4) as response:
                hits = json.loads(response.read().decode("utf-8", "replace")).get("query", {}).get("search", [])
            q = _normalize(query)
            ranked = sorted(hits, key=lambda x: SequenceMatcher(None, q, _normalize(x.get("title", ""))).ratio(), reverse=True)
            if not ranked:
                continue
            best = ranked[0]
            if SequenceMatcher(None, q, _normalize(best.get("title", ""))).ratio() < .92:
                continue
            image_url = f"https://{host}/w/api.php?" + urllib.parse.urlencode({
                "action": "query", "prop": "pageimages", "piprop": "thumbnail", "pithumbsize": 300,
                "titles": best["title"], "format": "json"})
            with urllib.request.urlopen(urllib.request.Request(image_url, headers=headers), timeout=4) as response:
                pages = json.loads(response.read().decode("utf-8", "replace")).get("query", {}).get("pages", {})
            page = next(iter(pages.values()), {})
            thumb = page.get("thumbnail", {}).get("source")
            if thumb:
                return {"title": best["title"], "icon_url": thumb, "provider": "Wikipedia", "confirmed": True}
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def _search_game(query: str) -> dict | None:
    return _steam_search(query) or _wikipedia_search(query)


def _steam_local_icon(appid: str) -> str | None:
    for root in known_roots():
        if root.name.casefold() != "userdata":
            continue
        cache = root.parent / "appcache" / "librarycache"
        for suffix in ("_icon.jpg", "_icon.png", "_icon.jpeg"):
            candidate = cache / f"{appid}{suffix}"
            if candidate.exists():
                return str(candidate)
    return None


def _cache_icon(cache_key: str, url: str, local_path: str | None = None) -> None:
    """Cache a square game icon, preferring Steam's own local library icon when available."""
    cache = ICON_DIR
    target = cache / f"{cache_key}.png"
    if target.exists():
        return
    try:
        cache.mkdir(parents=True, exist_ok=True)
        source_path = Path(local_path) if local_path and Path(local_path).exists() else cache / f"{cache_key}.image"
        if source_path == cache / f"{cache_key}.image":
            urls = [url]
            if cache_key.startswith("v2-") and cache_key[3:].isdigit() and "library_600x900.jpg" in url:
                urls.append(url.replace("library_600x900.jpg", "header.jpg"))
            for source_url in urls:
                try:
                    req = urllib.request.Request(source_url, headers={"User-Agent": "SaveLibrary/1.0"})
                    with urllib.request.urlopen(req, timeout=6) as response:
                        payload = response.read(2_000_000)
                    if payload.startswith((b"\xff\xd8", b"\x89PNG", b"GIF8", b"BM")):
                        source_path.write_bytes(payload)
                        break
                except OSError:
                    continue
            if not source_path.exists():
                return
        ps = shutil_which("powershell.exe") or shutil_which("powershell")
        if not ps:
            return
        source = str(source_path).replace("'", "''")
        dest = str(target).replace("'", "''")
        command = ("Add-Type -AssemblyName System.Drawing; "
                   f"$i=[System.Drawing.Image]::FromFile('{source}'); "
                   "$s=[Math]::Min($i.Width,$i.Height); "
                   "$x=[int](($i.Width-$s)/2); $y=[int](($i.Height-$s)/2); "
                   "$b=[System.Drawing.Bitmap]::new(48,48); "
                   "$g=[System.Drawing.Graphics]::FromImage($b); "
                   "$g.InterpolationMode=[System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic; "
                   "$src=[System.Drawing.Rectangle]::new($x,$y,$s,$s); "
                   "$dst=[System.Drawing.Rectangle]::new(0,0,48,48); "
                   "$g.DrawImage($i,$dst,$src,[System.Drawing.GraphicsUnit]::Pixel); "
                   f"$b.Save('{dest}',[System.Drawing.Imaging.ImageFormat]::Png); "
                   "$g.Dispose(); $i.Dispose(); $b.Dispose()")
        subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", command],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12, check=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return


def shutil_which(name: str) -> str | None:
    # Avoid an extra import in the scanner's hot path.
    import shutil
    return shutil.which(name)


def known_roots() -> list[Path]:
    appdata = Path(os.environ.get("APPDATA", HOME / "AppData/Roaming"))
    local = Path(os.environ.get("LOCALAPPDATA", HOME / "AppData/Local"))
    public = Path(os.environ.get("PUBLIC", r"C:\Users\Public"))
    docs = HOME / "Documents"
    roots = [appdata, local, HOME / "AppData/LocalLow", docs / "My Games",
             docs, HOME / "Saved Games", public / "Documents/Steam/RUNE",
             public / "Documents", public / "Documents/Steam", HOME / "Games"]
    # Steam may be installed on any library drive. Include every discovered userdata directory.
    steam_bases = [Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam",
                   Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Steam",
                   HOME / "AppData/Local/Programs/Steam"]
    for base in steam_bases:
        if base.exists():
            roots.append(base / "userdata")
    # Read libraryfolders.vdf paths to find Steam libraries on other drives.
    for vf in [base / "steamapps/libraryfolders.vdf" for base in steam_bases]:
        try:
            for line in vf.read_text(errors="ignore").splitlines():
                m = re.search(r'"path"\s+"([^"]+)"', line)
                if m:
                    library = Path(m.group(1).replace("\\\\", "\\"))
                    roots.append(library / "steamapps" / "userdata")
        except OSError:
            pass
    unique = []
    seen = set()
    for p in roots:
        try:
            key = str(p.resolve()).casefold()
            if key not in seen and p.is_dir():
                seen.add(key); unique.append(p)
        except OSError:
            continue
    return unique


def scan_saves(extra: list[str], identity_cache: dict) -> list[dict]:
    roots = known_roots() + [Path(p) for p in extra]
    custom_roots = set()
    for custom in extra:
        try:
            custom_roots.add(str(Path(custom).resolve()).casefold())
        except OSError:
            pass
    steam = _steam_names()
    candidates: dict[str, dict] = {}
    for root in roots:
        if not root.is_dir():
            continue
        try:
            stack = [(root, 0)]
            while stack:
                folder, depth = stack.pop()
                if any(part.casefold() in SKIP for part in folder.parts):
                    continue
                if any(part.casefold() in SOFTWARE_DIRS for part in folder.parts):
                    continue
                try:
                    entries = list(folder.iterdir())
                except (OSError, PermissionError):
                    continue
                files = [p for p in entries if p.is_file()]
                dirs = [p for p in entries if p.is_dir()]
                is_custom_root = str(folder).casefold() in custom_roots
                if (folder != root or is_custom_root) and files:
                    parts = [p.casefold() for p in folder.parts]
                    has_save_path = any(SAVE_WORDS.search(p) or p in {"saved games", "savegames", "savedata", "gse saves", "game saves"} for p in parts)
                    steam_cloud_path = "userdata" in parts and any(p in {"remote", "remotecache"} for p in parts)
                    pirate_group = any(p in {"rune", "gse saves"} or _normalize(p) == "gsesaves" for p in parts)
                    numbered_game_folder = folder.name.isdigit() and len(folder.parts) > 1 and (
                        folder.parts[-2].casefold() in {"rune", "gse saves"} or _normalize(folder.parts[-2]) == "gsesaves")
                    has_save_file = any(SAVE_WORDS.search(p.name) or p.suffix.casefold() in SAVE_EXTENSIONS for p in files)
                    weak_data = any(p.suffix.casefold() in LIKELY_DATA_EXTENSIONS for p in files)
                    # Ignore folders whose contents look exclusively like app preferences or logs.
                    config_only = all(p.suffix.casefold() in {".ini", ".cfg", ".log", ".txt", ".xml"} for p in files[:50])
                    if (has_save_path or has_save_file or steam_cloud_path or weak_data or numbered_game_folder) and not (
                            config_only and not has_save_path and not has_save_file and not steam_cloud_path):
                        title = next((p for p in reversed(folder.parts)
                                      if p.casefold() not in GENERIC and not p.isdigit() and not SAVE_WORDS.search(p)), folder.name)
                        title = re.sub(r"[_-]+", " ", title).strip()
                        appid = None
                        if "userdata" in parts:
                            i = parts.index("userdata")
                            if len(parts) > i + 2 and parts[i + 2].isdigit():
                                appid = parts[i + 2]
                                title = steam.get(appid, f"Steam 游戏 {appid}")
                        elif pirate_group:
                            # RUNE and GSE Saves commonly name each save folder by Steam AppID.
                            for i, part in enumerate(parts):
                                if part in {"rune", "gse saves"} or _normalize(part) == "gsesaves":
                                    appid = parts[i + 1] if i + 1 < len(parts) and parts[i + 1].isdigit() else None
                                    if appid:
                                        title = steam.get(appid, f"Steam 游戏 {appid}")
                                        break
                        key = str(folder.resolve()).casefold()
                        try:
                            modified = max([folder.stat().st_mtime] + [p.stat().st_mtime for p in files])
                        except OSError:
                            modified = 0
                        candidates[key] = {"guess": title, "path": str(folder), "files": len(files),
                                           "strong": bool(has_save_path or has_save_file or steam_cloud_path or numbered_game_folder),
                                           "appid": appid, "modified": modified}
                if depth < 4:
                    for child in dirs[:160]:
                        if child.name.casefold() not in SKIP and child.name.casefold() not in SOFTWARE_DIRS:
                            stack.append((child, depth + 1))
        except OSError:
            continue

    # Resolve candidate titles online in parallel. Only confident exact title matches receive
    # downloaded artwork; uncertain names retain a question-mark badge.
    guesses = {x["guess"] for x in candidates.values() if not x.get("appid")}
    now = int(time.time())
    fresh = {}
    to_search = []
    for guess in guesses:
        cached = identity_cache.get(_normalize(guess))
        if cached and now - cached.get("checked", 0) < (86400 if not cached.get("appid") else 30 * 86400):
            if cached.get("appid"):
                cached.setdefault("confirmed", True)
                cached.setdefault("provider", "Steam")
            fresh[_normalize(guess)] = cached
        else:
            to_search.append(guess)
    if to_search:
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(_search_game, to_search))
        for guess, result in zip(to_search, results):
            cache = {**(result or {}), "checked": now}
            identity_cache[_normalize(guess)] = cache
            fresh[_normalize(guess)] = cache

    # AppIDs beneath Steam userdata, RUNE, or GSE Saves are direct identifiers. Query those
    # IDs instead of trying to treat the number as a game title.
    appids = {x["appid"] for x in candidates.values() if x.get("appid") and x["appid"] not in steam}
    app_info = {}
    appid_search = []
    for appid in appids:
        cache_key = f"appid:{appid}"
        cached = identity_cache.get(cache_key)
        age_limit = 30 * 86400 if cached and cached.get("confirmed") else 86400
        if cached and now - cached.get("checked", 0) < age_limit:
            app_info[appid] = cached
        else:
            appid_search.append(appid)
    if appid_search:
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(_steam_details, appid_search))
        for appid, result in zip(appid_search, results):
            cache = {**(result or {}), "checked": now}
            identity_cache[f"appid:{appid}"] = cache
            app_info[appid] = cache

    merged: dict[str, dict] = {}
    for item in candidates.values():
        guess = item["guess"]
        identity = None
        if item.get("appid"):
            appid = item["appid"]
            if appid in steam:
                identity = {"appid": appid, "title": steam[appid],
                            "icon_url": f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/library_600x900.jpg",
                            "provider": "Steam", "confirmed": True}
            elif app_info.get(appid, {}).get("confirmed"):
                identity = app_info[appid]
        else:
            cached = fresh.get(_normalize(guess), {})
            if cached.get("confirmed"):
                identity = cached
        if not item["strong"] and not identity:
            continue
        # Strong save evidence may be shown as unconfirmed, but ordinary data folders without
        # a save-like path/file are discarded above.
        title = identity.get("title") if identity else guess
        group_key = f"steam:{identity['appid']}" if identity and identity.get("appid") else f"name:{_normalize(title if identity else guess)}"
        appid = identity.get("appid") if identity else None
        icon_key = f"v2-{appid}" if appid else f"v2-wiki-{_normalize(title)}" if identity else None
        row = merged.setdefault(group_key, {"name": title, "paths": [], "files": 0, "appid": appid,
                                              "icon_key": icon_key,
                                              "icon_url": identity.get("icon_url") if identity else None,
                                              "local_icon": _steam_local_icon(appid) if appid else None,
                                              "provider": identity.get("provider") if identity else None,
                                              "confirmed": bool(identity), "modified": 0})
        row["modified"] = max(row["modified"], item.get("modified", 0))
        if item["path"] not in row["paths"]:
            row["paths"].append(item["path"])
            row["files"] += item["files"]
    results = list(merged.values())
    # Fetch confirmed cover art in background scanner; Tk only loads the resulting cached PNG.
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_cache_icon, item["icon_key"], item["icon_url"], item.get("local_icon"))
                   for item in results if item.get("icon_key") and (item.get("icon_url") or item.get("local_icon"))]
        for future in futures:
            try:
                future.result(timeout=15)
            except Exception:
                pass
    for item in results:
        item["path"] = item["paths"][0]
    return sorted(results, key=lambda x: x["name"].casefold())


def _safe_stat(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


class SaveLibrary:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(f"{APP_NAME} v{APP_VERSION}  ·  PC 游戏存档")
        self.ui_state = self._read_ui_state()
        split_version = self.ui_state.get("split_version", "0")
        self.split_user_adjusted = (split_version == "6" and
                                    self.ui_state.get("split_user_adjusted", "false").casefold() == "true")
        self.scanning = False
        self.root.geometry(self.ui_state.get("geometry", "1120x760"))
        self.root.minsize(900, 620)
        self.root.configure(bg=BG)
        try:
            self.font_size = min(32, max(12, int(self.ui_state.get("font_size", 20))))
        except ValueError:
            self.font_size = 20
        self.font_objects: dict[tuple, tkfont.Font] = {}
        # Tk parses a space-delimited font string as family + size; quote the
        # family as a tuple because the Chinese UI font family itself has spaces.
        self.root.option_add("*Font", self._font(20))
        self.items: list[dict] = []
        self.extra: list[str] = []
        self.game_cache: dict = {}
        self.icon_images: dict[str, tk.PhotoImage] = {}
        self.visible_items: dict[str, dict] = {}
        self.checked: set[str] = set()
        self.elapsed: dict[str, int] = {}
        self.active_started: dict[str, float] = {}
        self.running: set[str] = set()
        self.query = tk.StringVar()
        self.status = tk.StringVar(value="就绪 · 仅扫描常见用户存档位置")
        self._load()
        self._style()
        self._build()
        self.refresh()
        self._tick()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Configure>", self._schedule_ui_save, add="+")

    def _set_busy(self, busy, text="正在检索游戏名称和图标，请稍候…"):
        if busy:
            self.busy_text.set(text)
            self.root.update_idletasks()
            width, height = 600, 190
            x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
            y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - height) // 2)
            self.busy_window.geometry(f"{width}x{height}+{x}+{y}")
            self.busy_window.deiconify()
            self.busy_window.lift()
            try:
                self.busy_window.grab_set()
            except tk.TclError:
                pass
            self.progress.start(12)
        else:
            self.progress.stop()
            try:
                self.busy_window.grab_release()
            except tk.TclError:
                pass
            self.busy_window.withdraw()
        self.scan_btn.configure(state="disabled" if busy else "normal",
                                text="扫描中…" if busy else "⟳  扫描存档")
        self.add_folder_btn.configure(state="disabled" if busy else "normal")
        self.settings_btn.configure(state="disabled" if busy else "normal")
        for button in getattr(self, "quick_buttons", []):
            button.configure(state="disabled" if busy else "normal")
        self.tree.state(["disabled"] if busy else ["!disabled"])
        self.scanning = busy
        self._update_delete_button()

    def _load(self):
        try:
            obj = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            self.extra = obj.get("extra", [])
            self.elapsed = obj.get("elapsed", {})
            self.items = obj.get("items", [])
            self.game_cache = obj.get("game_cache", {})
        except (OSError, ValueError):
            pass

    def _read_ui_state(self):
        config = configparser.ConfigParser()
        state = dict(DEFAULT_UI_STATE)
        try:
            config.read(UI_FILE, encoding="utf-8")
            if config.has_section("ui"):
                state.update(dict(config["ui"]))
        except (OSError, configparser.Error):
            pass
        return state

    def _font(self, size, family="Microsoft YaHei UI", bold=False):
        key = (family, int(size), bool(bold))
        if key not in self.font_objects:
            scaled = max(8, round(size * self.font_size / 20))
            self.font_objects[key] = tkfont.Font(root=self.root, family=family, size=scaled,
                                                 weight="bold" if bold else "normal")
        return self.font_objects[key]

    def set_font_size(self, value):
        self.font_size = min(32, max(12, int(float(value))))
        for (family, base_size, bold), font in self.font_objects.items():
            font.configure(size=max(8, round(base_size * self.font_size / 20)))
        self.root.option_add("*Font", self._font(20))
        self.tree.configure(height=10)
        ttk.Style().configure("Treeview", rowheight=round(124 * self.font_size / 20),
                              font=self._font(22))
        self.tree.column("check", width=round(64 * self.font_size / 20),
                         minwidth=round(64 * self.font_size / 20))
        self.side_panel.configure(width=round(300 * self.font_size / 20))
        self._save_ui_state()

    def open_settings(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("设置 · 字号")
        dialog.configure(bg=BG)
        dialog.geometry("680x360")
        dialog.transient(self.root)
        tk.Label(dialog, text="界面字号", fg=TEXT, bg=BG, font=self._font(24, bold=True)).pack(anchor="w", padx=24, pady=(22, 4))
        tk.Label(dialog, text="调整后立即生效，并在下次启动时保留。", fg=MUTED, bg=BG,
                 font=self._font(16)).pack(anchor="w", padx=24)
        value = tk.IntVar(value=self.font_size)
        tk.Scale(dialog, from_=12, to=32, orient="horizontal", variable=value, command=self.set_font_size,
                 bg=BG, fg=TEXT, troughcolor=PANEL, highlightthickness=0, showvalue=True,
                 font=self._font(16), length=600).pack(padx=20, pady=(12, 10))
        self._button(dialog, "完成", dialog.destroy).pack(anchor="e", padx=24, pady=(0, 18))

    def _schedule_ui_save(self, _event=None):
        if hasattr(self, "_ui_save_job"):
            self.root.after_cancel(self._ui_save_job)
        self._ui_save_job = self.root.after(500, self._save_ui_state)

    def _save_ui_state(self):
        if not hasattr(self, "panes"):
            return
        if self.root.state() == "iconic":
            return
        config = configparser.ConfigParser()
        config["ui"] = {"geometry": self.root.geometry(),
                        "sash_fraction": str(self.panes.sash_coord(0)[0] / max(1, self.panes.winfo_width())),
                        "window_state": self.root.state(), "font_size": str(self.font_size),
                        "split_user_adjusted": str(self.split_user_adjusted).lower(), "split_version": "6"}
        try:
            output = io.StringIO()
            config.write(output)
            UI_FILE.write_text("; 界面状态由存档管家自动保存\n" + output.getvalue(), encoding="utf-8")
        except OSError:
            pass

    def _save(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        DATA_FILE.write_text(json.dumps({"extra": self.extra, "elapsed": self.elapsed, "game_cache": self.game_cache,
                                         "items": self.items}, ensure_ascii=False, indent=2), encoding="utf-8")

    def _style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=round(124 * self.font_size / 20), borderwidth=0, font=self._font(22, "Microsoft YaHei UI"))
        style.configure("Treeview.Heading", background=BG, foreground=MUTED, relief="flat",
                        font=self._font(18, "Microsoft YaHei UI", True))
        style.map("Treeview", background=[("selected", "#29395b")], foreground=[("selected", TEXT)])
        style.configure("Vertical.TScrollbar", background=PANEL, troughcolor=BG, bordercolor=BG, arrowsize=12)

    def _build(self):
        shell = tk.Frame(self.root, bg=BG); shell.pack(fill="both", expand=True)
        side = tk.Frame(shell, bg="#0e1526", width=round(300 * self.font_size / 20)); side.pack(side="left", fill="y"); side.pack_propagate(False)
        self.side_panel = side
        logo = tk.Frame(side, bg="#0e1526"); logo.pack(fill="x", padx=23, pady=(26, 30))
        try:
            self.brand_icon = tk.PhotoImage(file=str(APP_DIR / "存档管家图标.png")).subsample(6, 6)
            tk.Label(logo, image=self.brand_icon, bg="#0e1526", bd=0).pack(side="left")
        except tk.TclError:
            tk.Label(logo, text="▣", fg=ACCENT, bg="#0e1526", font=self._font(46, "Segoe UI Symbol")).pack(side="left")
        tk.Label(logo, text="  存档管家", fg=TEXT, bg="#0e1526", font=self._font(30, "Microsoft YaHei UI", True)).pack(side="left")
        tk.Label(side, text="游戏存档", fg="#62718e", bg="#0e1526", font=self._font(18, "Microsoft YaHei UI", True)).pack(anchor="w", padx=24, pady=(0, 9))
        self.nav = tk.Label(side, text="▦   游戏存档", fg=TEXT, bg="#202b44", anchor="w", padx=18, pady=12)
        self.nav.pack(fill="x", padx=12)
        tk.Label(side, text="快速入口", fg="#62718e", bg="#0e1526", font=self._font(18, "Microsoft YaHei UI", True)).pack(anchor="w", padx=24, pady=(29, 9))
        self.quick_buttons = []
        for text, path in [("◷   Roaming 存档", Path(os.environ.get("APPDATA", HOME / "AppData/Roaming"))),
                           ("◷   Steam / RUNE", Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Documents/Steam/RUNE"),
                           ("◷   Saved Games", HOME / "Saved Games")]:
            b = tk.Button(side, text=text, command=lambda p=path: self.open_path(p), fg="#afbbd2", bg="#0e1526",
                          activebackground="#202b44", activeforeground=TEXT, relief="flat", anchor="w", padx=18, pady=10, cursor="hand2")
            b.pack(fill="x", padx=12)
            self.quick_buttons.append(b)
        bottom = tk.Frame(side, bg="#0e1526"); bottom.pack(side="bottom", fill="x", padx=20, pady=19)
        tk.Label(bottom, text="本地管理 · 数据保存在此电脑", fg="#65728b", bg="#0e1526", font=self._font(16, "Microsoft YaHei UI")).pack(anchor="w")

        main = tk.Frame(shell, bg=BG); main.pack(side="left", fill="both", expand=True, padx=34, pady=27)
        head = tk.Frame(main, bg=BG); head.pack(fill="x")
        left = tk.Frame(head, bg=BG); left.pack(side="left")
        tk.Label(left, text="游戏存档", fg=TEXT, bg=BG, font=self._font(46, "Microsoft YaHei UI", True)).pack(anchor="w")
        tk.Label(left, text="管理游戏存档位置与游玩时长", fg=MUTED, bg=BG, font=self._font(20, "Microsoft YaHei UI")).pack(anchor="w", pady=(5, 0))
        self.scan_btn = tk.Button(head, text="⟳  扫描存档", command=self.refresh, fg="#fff", bg="#5969dd", activebackground="#7482ef",
                                  relief="flat", padx=17, pady=10, cursor="hand2", font=self._font(20, "Microsoft YaHei UI", True))
        self.scan_btn.pack(side="right", pady=7)
        self.delete_btn = tk.Button(head, text="删除所选", command=self.delete_selected, state="disabled",
                                    fg="#aab2c1", bg="#454a54", activebackground="#b83242", activeforeground="white",
                                    disabledforeground="#aab2c1", relief="flat", padx=15, pady=10,
                                    cursor="hand2", font=self._font(20, "Microsoft YaHei UI", True))
        self.delete_btn.pack(side="right", padx=(0, 8), pady=7)
        self.settings_btn = tk.Button(head, text="⚙ 设置", command=self.open_settings, fg="#cbd6ec", bg=PANEL,
                                      activebackground="#293856", activeforeground=TEXT, relief="flat",
                                      padx=14, pady=10, cursor="hand2", font=self._font(20, bold=True))
        self.settings_btn.pack(side="right", padx=(0, 8), pady=7)

        bar = tk.Frame(main, bg=BG); bar.pack(fill="x", pady=(22, 13))
        search = tk.Entry(bar, textvariable=self.query, bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat", font=self._font(20, "Microsoft YaHei UI"))
        search.pack(side="left", fill="x", expand=True, ipady=11, padx=(0, 10)); search.insert(0, "")
        search.bind("<KeyRelease>", lambda _e: self.render())
        self.add_folder_btn = self._button(bar, "+ 添加存档目录", self.add_folder, subtle=True)
        self.add_folder_btn.pack(side="right")

        content = tk.Frame(main, bg=BG); content.pack(fill="both", expand=True)
        self.panes = tk.PanedWindow(content, orient="horizontal", sashwidth=8, sashrelief="raised",
                                    sashpad=1, showhandle=True, handlepad=5, bg=BG, bd=0, relief="flat")
        self.panes.pack(fill="both", expand=True)
        list_panel = tk.Frame(self.panes, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        tk.Label(list_panel, text="  游戏存档", fg=MUTED, bg=PANEL, anchor="w",
                 font=self._font(18, "Microsoft YaHei UI", True)).pack(fill="x", pady=(12, 8))
        self.tree = ttk.Treeview(list_panel, columns=("check",), show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="游戏名称", anchor="w")
        self.tree.heading("check", text="选择", anchor="center")
        self.tree.tag_configure("group", foreground=MUTED, font=self._font(18, bold=True))
        self.tree.column("#0", width=330, minwidth=260, stretch=True, anchor="w")
        check_width = round(64 * self.font_size / 20)
        self.tree.column("check", width=check_width, minwidth=check_width, stretch=False, anchor="center")
        self.tree.pack(side="left", fill="both", expand=True, padx=(5, 0), pady=(0, 6))
        scroll = tk.Scrollbar(list_panel, orient="vertical", command=self.tree.yview,
                              width=18, bg="#46546f", troughcolor="#0b1020",
                              activebackground=ACCENT, relief="flat", bd=0, highlightthickness=0)
        scroll.pack(side="right", fill="y", padx=(3, 6), pady=6)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Double-1>", self.open_selected)
        self.tree.bind("<<TreeviewSelect>>", self.selection_changed)
        self.tree.bind("<Button-1>", self._toggle_checkbox, add="+")
        self.panes.bind("<ButtonRelease-1>", self._sash_released, add="+")
        empty = tk.Frame(list_panel, bg=PANEL); self.empty = empty
        tk.Label(empty, text="⌕", fg="#556481", bg=PANEL, font=self._font(62, "Segoe UI Symbol")).pack(pady=(30, 5))
        self.empty_label = tk.Label(empty, text="正在扫描你的游戏存档…", fg=MUTED, bg=PANEL, font=self._font(22, "Microsoft YaHei UI")); self.empty_label.pack()
        self.empty_hint = tk.Label(empty, text="AppData · Documents · Steam · RUNE", fg="#65728b", bg=PANEL, font=self._font(18, "Microsoft YaHei UI")); self.empty_hint.pack(pady=(7, 25))
        self.detail_panel = tk.Frame(self.panes, bg="#121a2d", highlightbackground=LINE, highlightthickness=1)
        self.panes.add(list_panel, minsize=360, width=500)
        self.panes.add(self.detail_panel, minsize=230)
        self.root.after(180, self._set_default_split)
        self._empty_state()
        self._show_detail(None)
        foot = tk.Frame(main, bg=BG); foot.pack(fill="x", pady=(11, 0))
        tk.Label(foot, textvariable=self.status, fg="#74829c", bg=BG, font=self._font(18, "Microsoft YaHei UI")).pack(side="left")
        tk.Label(foot, text="运行时间从本软件启动后开始累计", fg="#65728b", bg=BG, font=self._font(18, "Microsoft YaHei UI")).pack(side="right")
        self.busy_window = tk.Toplevel(self.root)
        self.busy_window.withdraw()
        self.busy_window.transient(self.root)
        self.busy_window.overrideredirect(True)
        self.busy_window.configure(bg=LINE)
        card = tk.Frame(self.busy_window, bg=PANEL, highlightbackground=ACCENT, highlightthickness=1)
        card.pack(fill="both", expand=True, padx=1, pady=1)
        self.busy_text = tk.StringVar(value="正在检索游戏名称和图标，请稍候…")
        tk.Label(card, textvariable=self.busy_text, fg=TEXT, bg=PANEL,
                 font=self._font(22, bold=True)).pack(pady=(32, 20))
        self.progress = ttk.Progressbar(card, mode="indeterminate", length=500)
        self.progress.pack(pady=(0, 30))

    def _set_default_split(self):
        try:
            if self.ui_state.get("window_state") == "zoomed":
                self.root.state("zoomed")
                self.root.update_idletasks()
            self.root.update_idletasks()
            width = self.panes.winfo_width()
            if width > 0:
                fraction = float(self.ui_state.get("sash_fraction", .58)) if self.split_user_adjusted else .58
                self.panes.sash_place(0, int(width * min(.8, max(.2, fraction))), 0)
        except tk.TclError:
            pass

    def _sash_released(self, _event=None):
        self.split_user_adjusted = True
        self._schedule_ui_save()

    def _stat_card(self, parent, title, value, hint, color):
        box = tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        box.pack(side="left", fill="x", expand=True, padx=(0, 11))
        tk.Frame(box, bg=color, width=3).pack(side="left", fill="y")
        inner = tk.Frame(box, bg=PANEL); inner.pack(anchor="w", padx=15, pady=12)
        tk.Label(inner, text=title, fg=MUTED, bg=PANEL, font=self._font(18, "Microsoft YaHei UI")).pack(anchor="w")
        value_label = tk.Label(inner, text=value, fg=TEXT, bg=PANEL, font=self._font(34, "Microsoft YaHei UI", True)); value_label.pack(anchor="w", pady=(2, 0))
        tk.Label(inner, text=hint, fg="#74829c", bg=PANEL, font=self._font(16, "Microsoft YaHei UI")).pack(anchor="w", pady=(1, 0))
        return value_label

    def _button(self, parent, text, command, subtle=False):
        return tk.Button(parent, text=text, command=command, fg="#cbd6ec" if subtle else "white", bg=PANEL if subtle else ACCENT,
                         activebackground="#293856", activeforeground=TEXT, relief="flat", padx=13, pady=9, cursor="hand2")

    def refresh(self):
        if self.scanning:
            return
        self.status.set("扫描并整理存档目录中…")
        self._set_busy(True)
        def worker():
            cache = dict(self.game_cache)
            try:
                results = scan_saves(self.extra, cache)
                error = None
            except Exception as exc:
                results, error = [], exc
            self.root.after(0, lambda: self._finish_scan(results, cache, error))
        threading.Thread(target=worker, daemon=True).start()

    def _finish_scan(self, results, cache, error=None):
        if error:
            self._set_busy(False)
            self.status.set("扫描失败")
            messagebox.showerror("扫描失败", str(error))
            return
        self.items = results
        self.game_cache = cache
        valid_keys = {self._item_key(item) for item in results}
        self.checked.intersection_update(valid_keys)
        self._update_delete_button()
        self.status.set(f"扫描完成 · {len(results)} 款游戏 · 已合并重复目录 · {time.strftime('%H:%M:%S')}")
        if results:
            self.empty_label.configure(text="没有匹配的游戏")
        else:
            self.empty_label.configure(text="没有发现可信的游戏存档目录")
        self._load_icons()
        self._save(); self.render()
        self._set_busy(False)

    def _load_icons(self):
        self.icon_images.clear()
        for item in self.items:
            key = item.get("icon_key")
            if not key:
                continue
            image_path = ICON_DIR / f"{key}.png"
            try:
                if image_path.exists():
                    self.icon_images[key] = tk.PhotoImage(file=str(image_path))
            except tk.TclError:
                continue

    def render(self):
        query = self.query.get().strip().casefold()
        if self.tree.exists("group-unknown"):
            self.unknown_expanded = bool(self.tree.item("group-unknown", "open"))
        self.items.sort(key=lambda item: (not bool(item.get("icon_key") and self.icon_images.get(item.get("icon_key"))),
                                          -float(item.get("modified", 0))))
        previous = self.visible_items.get(self.tree.selection()[0]) if self.tree.selection() else None
        previous_key = self._item_key(previous) if previous else None
        for iid in self.tree.get_children(): self.tree.delete(iid)
        filtered = [x for x in self.items if query in x["name"].casefold() or
                    any(query in p.casefold() for p in x.get("paths", [x.get("path", "")]))]
        self.visible_items.clear()
        known = [item for item in filtered if item.get("icon_key") and self.icon_images.get(item.get("icon_key"))]
        unknown = [item for item in filtered if item not in known]
        if known:
            self.tree.insert("", "end", iid="group-icon", text=f"有图标  ·  {len(known)} 款", open=True, tags=("group",))
        if unknown:
            self.tree.insert("", "end", iid="group-unknown", text=f"未知  ·  {len(unknown)} 款",
                             open=getattr(self, "unknown_expanded", False), tags=("group",))
        for index, item in enumerate(filtered):
            iid = str(index)
            self.visible_items[iid] = item
            icon = self.icon_images.get(item.get("icon_key"))
            check = "☑" if self._item_key(item) in self.checked else "☐"
            modified = item.get("modified", 0)
            modified_text = time.strftime("%m-%d %H:%M", time.localtime(modified)) if modified else "时间未知"
            parent = "group-icon" if icon else "group-unknown"
            self.tree.insert(parent, "end", iid=iid, image=icon or "",
                             text=f"{item['name']}  ·  {modified_text}", values=(check,))
        self._empty_state(len(filtered))
        selectable = known + (unknown if getattr(self, "unknown_expanded", False) else [])
        if selectable:
            selected = next((iid for iid, item in self.visible_items.items()
                             if self._item_key(item) == previous_key and item in selectable), None)
            selected = selected or next(iid for iid, item in self.visible_items.items() if item in selectable)
            self.tree.selection_set(selected)
            self.tree.focus(selected)
            self._show_detail(self.visible_items[selected])
        else:
            self.tree.selection_remove(self.tree.selection())
            self._show_detail(None)

    def _item_key(self, item):
        return f"steam:{item.get('appid')}" if item and item.get("appid") else _normalize(item.get("name", "")) if item else None

    def _toggle_checkbox(self, event):
        if self.tree.identify_column(event.x) != "#1":
            return
        iid = self.tree.identify_row(event.y)
        item = self.visible_items.get(iid)
        if not item:
            return "break"
        key = self._item_key(item)
        if key in self.checked:
            self.checked.remove(key)
        else:
            self.checked.add(key)
        self.render()
        self._update_delete_button()
        return "break"

    def _update_delete_button(self):
        enabled = bool(self.checked) and not self.scanning
        self.delete_btn.configure(state="normal" if enabled else "disabled",
                                  fg="white" if enabled else "#aab2c1",
                                  bg="#c43d4d" if enabled else "#454a54",
                                  activebackground="#a92f40" if enabled else "#454a54")

    def delete_selected(self):
        if self.scanning:
            return
        chosen = [item for item in self.items if self._item_key(item) in self.checked]
        paths = []
        for item in chosen:
            paths.extend(item.get("paths") or ([item["path"]] if item.get("path") else []))
        # Remove duplicate and nested paths so the Shell receives each physical directory once.
        unique = []
        for raw in sorted(set(paths), key=lambda p: (len(Path(p).parts), p.casefold())):
            candidate = os.path.normcase(os.path.abspath(raw))
            nested = False
            for parent in unique:
                try:
                    if os.path.commonpath([candidate, parent]) == parent:
                        nested = True
                        break
                except ValueError:  # Different Windows drives
                    continue
            if nested:
                continue
            if os.path.exists(raw):
                unique.append(candidate)
        if not unique:
            messagebox.showinfo("没有可删除的存档目录", "所选游戏的存档目录已不存在。")
            self.checked.clear(); self._update_delete_button(); self.refresh()
            return
        if not messagebox.askyesno("移到回收站", f"将把 {len(unique)} 个存档目录移到回收站。\n游戏安装目录不会删除。\n\n继续吗？"):
            return
        source = "\0".join(unique) + "\0\0"
        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                        ("pTo", wintypes.LPCWSTR), ("fFlags", wintypes.WORD),
                        ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", wintypes.LPVOID),
                        ("lpszProgressTitle", wintypes.LPCWSTR)]
        op = SHFILEOPSTRUCTW(None, 3, source, None, 0x0004 | 0x0010 | 0x0040 | 0x0400, False, None, None)
        try:
            result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        except (AttributeError, OSError) as exc:
            messagebox.showerror("删除失败", f"无法调用 Windows 回收站操作：\n{exc}")
            return
        if result != 0 or op.fAnyOperationsAborted:
            messagebox.showerror("删除失败", f"Windows 未能完成回收站操作（错误码 {result}）。")
            return
        self.checked.clear()
        self._update_delete_button()
        self.status.set(f"已将 {len(unique)} 个存档目录移到回收站")
        self.refresh()

    def selection_changed(self, _event=None):
        selection = self.tree.selection()
        item = self.visible_items.get(selection[0]) if selection else None
        if item:
            self._show_detail(item)

    def _show_detail(self, item):
        for child in self.detail_panel.winfo_children():
            child.destroy()
        self.detail_image = None
        if not item:
            tk.Label(self.detail_panel, text="选择一个游戏查看存档位置", fg=MUTED, bg="#121a2d",
                     font=self._font(24, "Microsoft YaHei UI")).pack(expand=True)
            return
        header = tk.Frame(self.detail_panel, bg="#121a2d"); header.pack(fill="x", padx=22, pady=(23, 19))
        icon = self.icon_images.get(item.get("icon_key"))
        if icon:
            self.detail_image = icon.zoom(2, 2)
            tk.Label(header, image=self.detail_image, bg="#121a2d").pack(side="left", padx=(0, 18))
        title_box = tk.Frame(header, bg="#121a2d"); title_box.pack(side="left", fill="x", expand=True)
        tk.Label(title_box, text=item["name"], fg=TEXT, bg="#121a2d", anchor="w",
                 font=self._font(34, "Microsoft YaHei UI", True), wraplength=370, justify="left").pack(anchor="w")
        if item.get("confirmed"):
            source = item.get("provider", "已匹配") + (" · 图标暂不可用" if not icon else "")
        else:
            source = "未能确认游戏身份 · 显示问号"
        tk.Label(title_box, text=source, fg=GREEN if item.get("confirmed") and icon else "#e5b86c",
                 bg="#121a2d", font=self._font(18, "Microsoft YaHei UI")).pack(anchor="w", pady=(6, 0))

        runtime = self.elapsed.get(item["name"].casefold(), 0)
        if item["name"].casefold() in self.running:
            runtime += int(time.time() - self.active_started.get(item["name"].casefold(), time.time()))
        tk.Label(self.detail_panel, text="游玩时间", fg=MUTED, bg="#121a2d",
                 font=self._font(18, "Microsoft YaHei UI")).pack(anchor="w", padx=24)
        tk.Label(self.detail_panel, text=self._duration(runtime), fg=TEXT, bg="#121a2d",
                 font=self._font(30, "Microsoft YaHei UI", True)).pack(anchor="w", padx=24, pady=(3, 20))

        paths = item.get("paths", [item.get("path", "")])
        tk.Label(self.detail_panel, text=f"存档位置  ·  {len(paths)} 个目录", fg=MUTED, bg="#121a2d",
                 font=self._font(18, "Microsoft YaHei UI", True)).pack(anchor="w", padx=24, pady=(0, 8))
        listing = tk.Frame(self.detail_panel, bg="#121a2d"); listing.pack(fill="both", expand=True, padx=20, pady=(0, 18))
        for path in paths:
            card = tk.Frame(listing, bg="#1a2539", highlightbackground=LINE, highlightthickness=1)
            card.pack(fill="x", pady=4)
            tk.Label(card, text=path, fg="#c6d0e4", bg="#1a2539", anchor="w", justify="left",
                     wraplength=350, font=self._font(18, "Microsoft YaHei UI")).pack(side="left", fill="x", expand=True, padx=12, pady=10)
            self._button(card, "打开", lambda p=path: self.open_path(p), subtle=True).pack(side="right", padx=9, pady=7)

    def _empty_state(self, count=None):
        count = len(self.tree.get_children()) if count is None and hasattr(self, "tree") else count
        if count == 0:
            self.empty.place(relx=.5, rely=.5, anchor="center")
        else:
            self.empty.place_forget()

    def _duration(self, seconds):
        hours, rem = divmod(max(0, seconds), 3600); minutes = rem // 60
        return f"{hours} 小时 {minutes:02d} 分" if hours else f"{minutes} 分钟"

    def _tick(self):
        self._update_processes()
        self.render()
        self.root.after(5000, self._tick)

    def close(self):
        self._save_ui_state()
        now = time.time()
        for game in tuple(self.running):
            self.elapsed[game] = self.elapsed.get(game, 0) + int(now - self.active_started.get(game, now))
        self.running.clear()
        self.active_started.clear()
        self._save()
        self.root.destroy()

    def _update_processes(self):
        try:
            output = subprocess.check_output(["tasklist", "/fo", "csv", "/nh"], text=True, errors="ignore", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            names = set()
            for line in output.splitlines():
                m = re.match(r'"([^\"]+\.exe)"', line, re.I)
                if m: names.add(Path(m.group(1)).stem.casefold())
            current = set()
            for item in self.items:
                game = item["name"].casefold()
                # Count only an exact executable/title match; never guess across unrelated games.
                if game.replace(" ", "") in {n.replace(" ", "") for n in names}:
                    current.add(game)
            now = time.time()
            for game in current - self.running: self.active_started[game] = now
            for game in self.running - current:
                self.elapsed[game] = self.elapsed.get(game, 0) + int(now - self.active_started.pop(game, now))
            self.running = current
            if current: self._save()
        except (OSError, subprocess.SubprocessError):
            pass

    def add_folder(self):
        if self.scanning:
            return
        folder = filedialog.askdirectory(title="选择游戏存档文件夹")
        if folder and folder not in self.extra:
            self.extra.append(folder); self.refresh()

    def open_path(self, path: Path | str):
        try:
            p = Path(path)
            if p.exists(): os.startfile(str(p))
            else: messagebox.showinfo("目录尚不存在", f"此目录当前不存在：\n{p}")
        except OSError as exc:
            messagebox.showerror("无法打开目录", str(exc))

    def open_selected(self, _event=None):
        sel = self.tree.selection()
        item = self.visible_items.get(sel[0]) if sel else None
        if item:
            self.open_item_paths(item)

    def open_item_paths(self, item):
        paths = item.get("paths") or ([item.get("path")] if item.get("path") else [])
        if not paths:
            return
        if len(paths) == 1:
            self.open_path(paths[0]); return
        dialog = tk.Toplevel(self.root)
        dialog.title(f"{item.get('name', '游戏')} · 存档目录")
        dialog.configure(bg=BG); dialog.geometry("680x300")
        tk.Label(dialog, text="选择要打开的存档目录", fg=TEXT, bg=BG,
                 font=self._font(26, "Microsoft YaHei UI", True)).pack(anchor="w", padx=20, pady=(18, 10))
        listing = tk.Listbox(dialog, bg=PANEL, fg=TEXT, selectbackground="#29395b", relief="flat",
                             font=self._font(18, "Microsoft YaHei UI"), activestyle="none")
        listing.pack(fill="both", expand=True, padx=20, pady=(0, 12))
        for path in paths: listing.insert("end", path)
        def open_selected_path(_event=None):
            sel = listing.curselection()
            if sel:
                self.open_path(paths[sel[0]])
        listing.bind("<Double-Button-1>", open_selected_path)
        self._button(dialog, "打开选中目录", open_selected_path).pack(anchor="e", padx=20, pady=(0, 16))

    def _click_path(self, event):
        row = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if row and col == "#2":
            self.open_item_paths(self.visible_items.get(row, {}))


if __name__ == "__main__":
    root = tk.Tk()
    app = SaveLibrary(root)
    root.mainloop()

