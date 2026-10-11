# © 2026 PocketKode · SPDX-License-Identifier: GPL-3.0-only
"""The web server the phone or PC talks to. Runs in a background thread while the app is open.

Security: a new random PIN each time the app starts; after login the browser gets a session
cookie (HttpOnly, SameSite=Strict). Changing requests also need an X-Requested-With header, so
other websites can't trigger them. 5 wrong PINs from one address lock it out for a minute.
"""
import json
import os
import secrets
import socket
import threading
import time
import urllib.parse
import zipfile
from collections import deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import backup
from .files import PathError, SPACE_MARGIN

CHUNK = 256 * 1024
FREE_MARGIN = SPACE_MARGIN  # keep this much space free after an upload
MAX_TRIES, LOCKOUT = 5, 60


def local_ip():
    """The handheld's address on the Wi-Fi, or None. (Sends nothing: UDP connect only picks a route.)"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            ip = s.getsockname()[0]
            return None if ip.startswith("127.") else ip
    except OSError:
        return None


def fmt_size(n):
    for unit, div in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= div:
            return f"{n / div:.1f} {unit}"
    return f"{n} B"


class TransferServer:
    def __init__(self, roots, web_dir, port=8080, host="0.0.0.0"):
        self.roots = roots
        self.host = host
        self.web_dir = web_dir
        self.port = port
        self.pin = f"{secrets.randbelow(10000):04d}"
        self.tokens = set()
        self.failures = {}              # ip -> (count, locked_until)
        self.clients = {}               # ip -> last seen
        self.uploads = {}               # id -> {"name", "done", "total"}
        self.downloads = 0              # folder downloads (ZIPs) in progress
        self.backup = None              # full SD card backup in progress: {"done", "total", "sent", ...}
        self.cancel = False             # the app is quitting
        self.backup_cancel = False      # "Cancel backup" on the page
        self.backup_device = os.environ.get("WT_BACKUP_DEVICE")  # a disk image instead of the card (tests)
        self.events = deque(maxlen=8)   # recent activity shown on the handheld
        self.last_activity = time.monotonic()
        self.lock = threading.Lock()
        self.httpd = None
        self.updater = None          # the app's Updater: the page shows "Update available" and can start it
        self.on_restart = None       # the app restarts (to finish an update) when the page asks

    # --- lifecycle
    def start(self):
        handler = type("Handler", (Handler,), {"app": self})
        for port in range(self.port, self.port + 10):
            try:
                self.httpd = ThreadingHTTPServer((self.host, port), handler)
                self.port = port
                break
            except OSError:
                continue
        if not self.httpd:
            raise OSError("No free network port for the transfer page.")
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.cancel = True  # a running backup stops and makes the card writable again
        deadline = time.monotonic() + 10
        while self.backup is not None and time.monotonic() < deadline:
            time.sleep(0.1)
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    def new_pin(self):
        with self.lock:
            self.pin = f"{secrets.randbelow(10000):04d}"
            self.tokens.clear()  # everyone has to log in again
            self.clients.clear()

    # --- state for the handheld screen
    def event(self, text):
        with self.lock:
            self.events.appendleft((time.time(), text))
        self.last_activity = time.monotonic()

    def touch(self, ip):
        with self.lock:
            self.clients[ip] = time.monotonic()
        self.last_activity = time.monotonic()

    def connected(self, within=30):
        now = time.monotonic()
        with self.lock:
            return sum(1 for t in self.clients.values() if now - t < within)

    def busy(self):
        with self.lock:
            return bool(self.uploads) or self.downloads > 0 or self.backup is not None


class Handler(BaseHTTPRequestHandler):
    app = None  # set by TransferServer.start
    server_version = "WiFiTransfer/1.0"

    def log_message(self, fmt, *args):
        pass  # quiet; errors are shown on screen and in the JSON replies

    # --- helpers
    @property
    def ip(self):
        return self.client_address[0]

    def send_json(self, data, status=200, headers=None):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def error(self, message, status=400):
        self.send_json({"error": message}, status)

    def query(self):
        return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 64 * 1024:
            raise PathError("Request too large.")
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError as e:
            raise PathError("Bad request.") from e

    def logged_in(self):
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        token = cookie.get("wt")
        return bool(token) and token.value in self.app.tokens

    def allowed(self, changing=False):
        if not self.logged_in():
            self.error("Please enter the PIN shown on the game console.", 401)
            return False
        if changing and self.headers.get("X-Requested-With") != "WiFiTransfer":
            self.error("Missing request header.", 403)
            return False
        self.app.touch(self.ip)
        return True

    # --- routing
    def do_GET(self):
        route = urllib.parse.urlsplit(self.path).path
        try:
            if route in ("/", "/index.html"):
                return self.static("index.html", "text/html; charset=utf-8")
            if route == "/icon.svg":
                return self.static("icon.svg", "image/svg+xml")
            if route == "/api/session":
                return self.send_json({"loggedIn": self.logged_in()})
            if not self.allowed():
                return
            q = self.query()
            if route == "/api/roots":
                roots = []
                for r in self.app.roots.roots.values():
                    roots.append({**{k: r[k] for k in ("id", "label", "desc", "card")}, **self.app.roots.usage(r["id"])})
                return self.send_json({"roots": roots})
            if route == "/api/lost":  # space marked as used that no folder shows, for the note on a card's tab
                return self.send_json({"lost": self.app.roots.lost_space(q.get("root", ""))})
            if route == "/api/storage":  # free / total of each SD card, for the line under the page's title
                return self.send_json({"cards": self.app.roots.storage()})
            if route == "/api/list":
                rid, rel = q.get("root", ""), q.get("path", "")
                return self.send_json({"entries": self.app.roots.list(rid, rel), **self.app.roots.usage(rid)})
            if route == "/api/search":  # names on all the main folders (both SD cards)
                results, complete = self.app.roots.search(q.get("q", ""))
                return self.send_json({"results": results, "complete": complete})
            if route == "/api/dirsize":  # a folder's total size, for the list (asked one folder at a time)
                total, files, complete = self.app.roots.dirsize(q.get("root", ""), q.get("path", ""), q.get("name", ""))
                return self.send_json({"size": total, "files": files, "complete": complete})
            if route == "/api/backup":  # full SD card backup: the card's size, and the one running
                return self.backup_info()
            if route == "/api/backup/image":
                return self.backup_image()
            if route == "/api/update":  # in-app update: status for the page
                return self.update_status()
            if route == "/api/suggest":
                card = 2 if q.get("card") == "2" else 1
                return self.send_json({"suggestion": self.app.roots.suggest(q.get("name", ""), card)})
            if route == "/api/download":
                return self.download(q)
            if route == "/api/zip":
                return self.download_zip(q)
            return self.error("Not found.", 404)
        except PathError as e:
            return self.error(str(e))
        except ConnectionError:
            self.close_connection = True  # the browser cancelled or lost the connection
        except OSError as e:
            return self.error(f"The game console couldn't do that: {e.strerror or e}", 500)

    def do_POST(self):
        route = urllib.parse.urlsplit(self.path).path
        try:
            if route == "/api/login":
                return self.login()
            if route == "/api/backup/cancel":
                if not self.allowed(changing=True):
                    return
                return self.backup_cancel()
            if route in ("/api/update/start", "/api/update/check", "/api/update/restart", "/api/update/enabled"):
                if not self.allowed(changing=True):
                    return
                return self.update_action(route.rsplit("/", 1)[1])
            if not self.allowed(changing=True) or self.backup_blocks():
                return
            data = self.read_json()
            rid, rel, name = data.get("root", ""), data.get("path", ""), data.get("name", "")
            roots = self.app.roots
            if route == "/api/mkdir":
                roots.mkdir(rid, rel, name)
                self.app.event(f"New folder {name}")
            elif route == "/api/rename":
                roots.rename(rid, rel, name, data.get("newName", ""))
                self.app.event(f"Renamed {name} → {data.get('newName', '')}")
            elif route in ("/api/move", "/api/copy"):
                keep = route == "/api/copy"
                with self.app.lock:
                    self.app.downloads += 1  # counts as busy: the app waits for it before going to the background
                try:
                    roots.transfer(rid, rel, name, data.get("toRoot", ""), data.get("toPath", ""), keep=keep)
                finally:
                    with self.app.lock:
                        self.app.downloads -= 1
                self.app.event(f"{'Copied' if keep else 'Moved'} {name}")
            elif route == "/api/delete":
                roots.delete(rid, rel, name)
                self.app.event(f"Deleted {name}")
            else:
                return self.error("Not found.", 404)
            return self.send_json({"ok": True})
        except PathError as e:
            return self.error(str(e))
        except ConnectionError:
            self.close_connection = True  # the browser cancelled or lost the connection
        except OSError as e:
            return self.error(f"The game console couldn't do that: {e.strerror or e}", 500)

    def do_PUT(self):
        if urllib.parse.urlsplit(self.path).path != "/api/upload":
            return self.error("Not found.", 404)
        if not self.allowed(changing=True) or self.backup_blocks():
            return
        try:
            self.upload(self.query())
        except PathError as e:
            self.error(str(e))
        except ConnectionError:
            self.close_connection = True
        except OSError as e:
            self.error(f"The game console couldn't save the file: {e.strerror or e}", 500)

    # --- actions
    def update_status(self):
        u = self.app.updater
        if u is None:
            return self.send_json({"state": None})
        info = u.info or {}
        return self.send_json({"state": u.state, "current": u.version, "version": info.get("version"),
                               "notes": info.get("notes") or [], "size": info.get("size"), "progress": u.progress,
                               "error": u.error, "enabled": u.enabled})

    def update_action(self, what):
        """From the page: check, download (checked: SHA-256 and PocketKode's signature), restart to finish."""
        u = self.app.updater
        if u is None:
            return self.send_json({"ok": True})
        if what == "check":
            if u.state not in ("available", "downloading", "ready"):
                u.state, u.info = None, None
            u.maybe_check(force=True)
        elif what == "start":
            if u.state == "error" and u.info:
                u.state = "available"
            u.download()
            self.app.event("Update: download started from the page")
        elif what == "restart" and u.state == "ready" and self.app.on_restart:
            self.send_json({"ok": True})
            self.app.on_restart()
            return None
        elif what == "enabled":
            u.set_enabled(bool(self.read_json().get("on")))
        return self.send_json({"ok": True})

    def static(self, name, ctype):
        try:
            with open(os.path.join(self.app.web_dir, name), "rb") as f:
                body = f.read()
        except OSError:
            return self.error("Missing web page file.", 500)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def login(self):
        app, now = self.app, time.monotonic()
        count, until = app.failures.get(self.ip, (0, 0))
        if until > now:
            return self.error(f"Too many wrong PINs. Try again in {int(until - now) + 1} seconds.", 429)
        pin = str(self.read_json().get("pin", "")).strip()
        if not secrets.compare_digest(pin, app.pin):
            count += 1
            app.failures[self.ip] = (0, now + LOCKOUT) if count >= MAX_TRIES else (count, 0)
            return self.error("Wrong PIN. Check the number on the game console's screen.", 403)
        app.failures.pop(self.ip, None)
        token = secrets.token_urlsafe(24)
        app.tokens.add(token)
        app.touch(self.ip)
        app.event(f"Connected: {self.ip}")
        return self.send_json({"ok": True}, headers={
            "Set-Cookie": f"wt={token}; Path=/; HttpOnly; SameSite=Strict"})

    def upload(self, q):
        rid, rel, name = q.get("root", ""), q.get("path", ""), q.get("name", "")
        roots = self.app.roots
        target = roots.resolve(rid, rel, name)
        folder = os.path.dirname(target)
        length = int(self.headers.get("Content-Length") or -1)
        if length < 0:
            raise PathError("Upload without a size isn't supported by this browser.")
        if roots.usage(rid)["free"] < length + FREE_MARGIN:
            raise PathError(f"Not enough free space on the SD card for {name} ({fmt_size(length)}).")
        if os.path.exists(target) and q.get("overwrite") != "1":
            return self.error(f"{name} already exists.", 409)
        os.makedirs(folder, exist_ok=True)  # e.g. ROMS/GBA suggested but not created yet
        uid = secrets.token_hex(4)
        with self.app.lock:
            self.app.uploads[uid] = {"name": name, "done": 0, "total": length}
        part = target + ".part"
        try:
            with open(part, "wb") as f:
                left = length
                while left > 0:
                    chunk = self.rfile.read(min(CHUNK, left))
                    if not chunk:
                        raise PathError("The upload stopped before it finished. Try again.")
                    f.write(chunk)
                    left -= len(chunk)
                    self.app.uploads[uid]["done"] = length - left
                    self.app.last_activity = time.monotonic()
                f.flush()
                os.fsync(f.fileno())
            os.replace(part, target)
        finally:
            with self.app.lock:
                self.app.uploads.pop(uid, None)
            if os.path.exists(part):
                os.remove(part)
        where = "/".join(p for p in (roots.roots[rid]["label"], rel) if p)
        self.app.event(f"↓ {name} ({fmt_size(length)}) → {where}")
        self.send_json({"ok": True})

    def download_zip(self, q):
        """Stream a folder as a ZIP (stored, not compressed: games barely compress and the
        handheld's CPU is slow). Written straight to the connection, so no temporary space is used."""
        rid, rel, name = q.get("root", ""), q.get("path", ""), q.get("name") or None
        roots = self.app.roots
        base = os.path.realpath(roots.root(rid))
        # "names": only the items ticked in the folder `rel` ("/"-separated: a name can't hold a "/")
        picked = [n for n in (q.get("names") or "").split("/") if n]
        folder = roots.resolve(rid, rel, None if picked else name)
        if not os.path.isdir(folder):
            raise PathError("That folder doesn't exist any more.")
        label = os.path.basename(folder.rstrip("/")) if (name or rel) else roots.roots[rid]["label"]
        if picked:
            items = [(roots.resolve(rid, rel, n), n) for n in picked]  # each checked like any other path
        else:
            items = [(folder, label)]
        files = []
        for top, arc_top in items:
            if os.path.isfile(top):
                files.append((top, arc_top))
                continue
            for dirpath, dirnames, filenames in os.walk(top):  # doesn't follow symlinked folders
                dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
                for fn in sorted(filenames):
                    path = os.path.join(dirpath, fn)
                    real = os.path.realpath(path)
                    if fn.startswith(".") or fn.endswith(".part") or not real.startswith(base + os.sep):
                        continue
                    if os.path.isfile(real):
                        files.append((path, os.path.join(arc_top, os.path.relpath(path, top))))
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition",
                         f"attachment; filename=wifi-transfer-{time.strftime('%Y%m%d-%H%M%S')}.zip")
        self.started_cookie(q)
        self.end_headers()  # no Content-Length: the connection closes at the end (HTTP/1.0)
        with self.app.lock:
            self.app.downloads += 1
        total = 0
        try:
            with zipfile.ZipFile(self.wfile, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                for path, arc in files:
                    zf.write(path, arc)
                    total += os.path.getsize(path)
                    self.app.last_activity = time.monotonic()
        finally:
            with self.app.lock:
                self.app.downloads -= 1
        self.app.event(f"↑ Sent {label}.zip ({len(files)} files, {fmt_size(total)})")

    # --- full SD card backup
    def card(self):
        return backup.Card(device=self.app.backup_device)

    def backup_info(self):
        srv = self.app
        try:
            card = self.card()
            plan = card.plan()
        except backup.BackupError as e:
            return self.send_json({"available": False, "error": str(e)})
        with srv.lock:
            status = dict(srv.backup) if srv.backup else None
        used = sum(n for _, n, read in plan if read)
        return self.send_json({"available": True, "size": card.size, "used": used, "status": status,
                               "busy": bool(srv.uploads) or srv.downloads > 0})

    def backup_cancel(self):
        if self.app.backup is None:
            return self.error("No backup is running.", 409)
        self.app.backup_cancel = True
        return self.send_json({"ok": True})

    def backup_image(self):
        """The whole card as one .img.gz, streamed (see backup.py). Changes wait while it runs."""
        srv = self.app
        with srv.lock:
            if srv.backup is not None:
                return self.error("A backup is already running.", 409)
            if srv.uploads or srv.downloads:
                return self.error("Wait for the transfers to finish, then start the backup.", 409)
            srv.backup = {"done": 0, "total": 0, "sent": 0, "started": time.time(), "readonly": False}
            srv.backup_cancel = False
        frozen = False
        card = None
        try:
            try:
                card = self.card()
                frozen = card.freeze()
                plan = card.plan()
            except backup.BackupError as e:
                return self.error(str(e), 500)
            except OSError as e:
                return self.error(f"Couldn't read the SD card: {e.strerror or e}", 500)
            with srv.lock:
                srv.backup.update(total=card.size, readonly=frozen)
            name = f"muOS-SD-backup-{time.strftime('%Y-%m-%d')}.img.gz"
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Disposition", f"attachment; filename={name}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()  # size isn't known in advance; the connection closes at the end
            srv.event(f"Backup started ({fmt_size(card.size)} card)")

            def progress(done, sent):
                if srv.cancel:
                    raise backup.BackupError("stopped on the game console")
                if srv.backup_cancel:
                    raise backup.BackupError("cancelled")
                srv.backup["done"], srv.backup["sent"] = done, sent
                srv.last_activity = time.monotonic()

            try:
                sent = backup.stream(card, plan, self.wfile.write, progress)
                self.wfile.flush()
            except (OSError, backup.BackupError) as e:
                self.close_connection = True
                reason = getattr(e, "strerror", None) or str(e)
                srv.event("Backup cancelled from the page" if reason == "cancelled" else f"Backup stopped: {reason}")
                return None
            srv.event(f"✓ Backup sent: {name} ({fmt_size(sent)})")
        finally:
            if frozen and card is not None and not card.thaw():
                srv.event("Couldn't make the SD card writable again. Restart the game console.")
            with srv.lock:
                srv.backup = None
        return None

    def backup_blocks(self):
        """Changes wait while the card is being backed up (the copy must not change under it)."""
        if self.app.backup is None:
            return False
        self.error("A full SD card backup is running. Changes are paused until it finishes.", 409)
        return True

    def started_cookie(self, q):
        """Tells the page its download has started (it shows a spinner until then): a short-lived
        cookie named after the token the page sent along."""
        token = q.get("dl", "")
        if token.isalnum() and len(token) <= 24:
            self.send_header("Set-Cookie", f"wtdl{token}=1; Path=/; Max-Age=120; SameSite=Strict")

    def download(self, q):
        path = self.app.roots.resolve(q.get("root", ""), q.get("path", ""), q.get("name", ""))
        if not os.path.isfile(path):
            raise PathError("That file doesn't exist any more.")
        size = os.path.getsize(path)
        name = os.path.basename(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{urllib.parse.quote(name)}")
        self.started_cookie(q)
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.app.last_activity = time.monotonic()
        self.app.event(f"↑ Sent {name} ({fmt_size(size)})")
