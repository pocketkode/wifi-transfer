# © 2026 PocketKode · SPDX-License-Identifier: GPL-3.0-only
"""The game console's screen: QR code, address and PIN while the transfer server runs.
Free: everything, including the full SD card backup, works without an account."""
import os
import shutil
import time

import segno

from . import background, lang, updater
from .input import Input
from .keepawake import KeepAwake
from .lang import _, tr
from .server import TransferServer, fmt_size, local_ip
from .settings import load_config, make_roots  # noqa: F401 (load_config: also used by older callers)
from .ui import C, H, UI, W

APP_VERSION = "2.0.2"
# Files of earlier Wi-Fi Transfer builds (loose modules, the PRO module wtpro/, its licence file). Installing over
# one only adds and replaces files, so once this version has started, the old ones go.
OLD_FILES = ("activation", "app", "backup", "ed25519", "files", "input", "keepawake", "lang", "lang_common",
             "server", "ui", "updater") + tuple("texts_" + c for c in ("ja", "es", "fr", "de", "nl", "ru", "zh", "ko"))
OLD_PATHS = ("wtransfer/pk.py", "data/license.json")
OLD_DIRS = ("wtpro",)


def remove_old_files(app_dir):
    paths = [name + ext for name in OLD_FILES for ext in (".so", ".py")] + list(OLD_PATHS)
    for rel in paths:
        path = os.path.join(app_dir, rel)
        if os.path.isfile(path):
            try:
                os.remove(path)
            except OSError:
                pass
    for rel in OLD_DIRS:
        shutil.rmtree(os.path.join(app_dir, rel), ignore_errors=True)
HEADER_H, FOOTER_H = 44, 34


def draw_header(ui, title, right=""):
    ui.fill(0, 0, W, HEADER_H, C["bar"])
    ui.logo(12, 8)
    ui.text(54, 10, title, 21, C["text"])
    if right:
        ui.text(W - 12, 14, right, 16, C["dim"], align="right")


def draw_footer(ui, hints):
    y = H - FOOTER_H
    ui.fill(0, y, W, FOOTER_H, C["bar"])
    x = 12
    for btn, label in hints:
        bw = max(24, ui.measure(btn, 14)[0] + 12)
        ui.rounded(x, y + 6, bw, 22, 11, C["text"])
        ui.text(x + bw // 2, y + 8, btn, 14, C["black"], align="center")
        x += bw + 6
        x += ui.text(x, y + 7, label, 16, C["dim"]) + 14


class App:
    def __init__(self, app_dir):
        self.app_dir = app_dir
        self.cfg = load_config(app_dir)
        lang.setup(app_dir)  # the app's choice, else muOS's language (see lang.py); the web page has its own
        self.ui = UI(app_dir)
        self.input = Input(swap_ab=self.cfg.get("swap_ab", "no").lower() in ("1", "yes", "true", "on"))
        self.roots = make_roots(self.cfg)
        # Back from the background: stop that process and take its PIN and signed-in browsers over
        self.handover = background.load_state(app_dir) if background.stop_running() else None
        self.to_background = False  # X: the transfer goes on in the background and the app closes
        self.awake = KeepAwake()
        self.server = None
        self.ip = None
        self.qr = None
        self.mode = "starting"      # starting | nowifi | running | stopped | error
        self.message = ""
        self.notice, self.notice_until = "", 0.0
        self.restart = False  # "Restart" after an update (from the page): main.py returns 42, mux_launch.sh starts again
        # Updates from GitHub releases (checked at start and daily when online; nothing installs by itself)
        self.updater = updater.Updater(app_dir, "wifi-transfer", APP_VERSION)
        self._confirmed = False
        self._update_seen = None
        if self.updater.just_updated:
            self.notice = _("✓ Updated to version {v}. Your settings were kept.", v=APP_VERSION)
            self.notice_until = time.monotonic() + 15
        elif self.updater.rolled_back:
            self.notice = _("Version {bad} didn't start, so Wi-Fi Transfer went back to {v}.",
                            bad=self.updater.rolled_back.get('version'), v=APP_VERSION)
            self.notice_until = time.monotonic() + 20
        self.confirm_quit_until = 0
        self.next_wifi_check = 0
        self.running = True

    # --- server control
    def start_server(self):
        self.ip = local_ip()
        if not self.ip:
            self.mode = "nowifi"
            self.next_wifi_check = time.monotonic() + 2
            return
        try:
            port = self.handover["port"] if self.handover else int(self.cfg.get("port") or 8080)
            self.server = TransferServer(self.roots, os.path.join(self.app_dir, "web"), port=port)
            self.server.updater = self.updater        # the page shows and starts updates
            self.server.on_restart = self.request_restart
            self.server.start()
            if self.handover:
                background.apply_state(self.server, self.handover)
                self.handover = None
                self.notice = _("✓ Back from the background: same PIN, and signed-in browsers stay signed in.")
                self.notice_until = time.monotonic() + 8
        except OSError as e:
            self.mode, self.message = "error", str(e)
            return
        self.qr = segno.make_qr(self.url, error="m").matrix
        self.mode = "running"

    def stop_server(self, reason=""):
        if self.server:
            self.server.stop()
            self.server = None
        self.awake.stop()
        if hasattr(os, "sync"):
            os.sync()  # make sure received files are on the SD card
        self.mode, self.message = "stopped", reason

    @property
    def url(self):
        port = self.server.port if self.server else self.cfg.get("port")
        return f"http://{self.ip}:{port}"

    # --- loop
    def handle(self, b):
        now = time.monotonic()
        if self.mode == "running":
            if b == "B":
                if self.server.busy() and now > self.confirm_quit_until:
                    self.confirm_quit_until = now + 3  # press B again within 3 s to really stop
                    return
                self.running = False
            elif b == "Y":
                self.server.new_pin()
            elif b == "X":
                if self.server.busy():
                    self.notice, self.notice_until = _("Wait until the transfer finishes, then try again."), now + 4
                else:
                    self.to_background = True
                    self.running = False
            elif b == "SELECT":
                self.change_language()
        elif self.mode in ("stopped", "error", "nowifi"):
            if b == "A" and self.mode != "nowifi":
                self.mode = "starting"
                self.start_server()
            elif b == "B":
                self.running = False
            elif b == "SELECT":
                self.change_language()

    def change_language(self):
        lang.cycle()
        self.ui.language_changed()
        self.notice, self.notice_until = _("Language: {name}", name=lang.label()), time.monotonic() + 3

    def request_restart(self):
        self.restart = True  # the run loop ends; main.py returns 42; mux_launch.sh starts the app again
        self.running = False

    def update(self):
        now = time.monotonic()
        self.updater.maybe_check()
        u = self.updater
        if u.state == "available" and u.info and self._update_seen != u.info.get("version"):
            self._update_seen = u.info.get("version")
            self.notice = _("Update available: version {v}. Update it from the transfer page.", v=u.info['version'])
            self.notice_until = now + 20
        if self.mode == "starting":
            self.start_server()
        elif self.mode == "nowifi" and now >= self.next_wifi_check:
            self.start_server()
        elif self.mode == "running":
            srv = self.server
            if srv.busy() or srv.connected():
                self.awake.start()
                self.awake.pulse()
            else:
                self.awake.stop()
            idle = float(self.cfg.get("idle_minutes") or 15) * 60
            if not srv.busy() and now - srv.last_activity > idle:
                self.stop_server(_("Stopped after {n} minutes without activity.", n=int(idle // 60)))

    def run(self):
        try:
            while self.running and not self.input.quit:
                for b in self.input.poll():
                    self.handle(b)
                self.update()
                self.ui.clear()
                self.draw()
                self.ui.present()
                if not self._confirmed:  # the first screen is up: an update that was just installed works
                    self._confirmed = True
                    updater.confirm(self.app_dir)
                    remove_old_files(self.app_dir)
                time.sleep(1 / 20)
        finally:
            if self.to_background and self.server:
                self.draw_background_note()
                background.save_state(self.app_dir, self.server)
                self.stop_server()
                background.start(self.app_dir)
            else:
                self.stop_server()
            self.ui.close()

    def draw_background_note(self):
        ui = self.ui
        ui.clear()
        draw_header(ui, "Wi-Fi Transfer", f"v{APP_VERSION}")
        ui.text(W // 2, 170, _("Running in the background"), 26, C["text"], align="center")
        body = _("The transfer page keeps working with the same address and PIN. Open Wi-Fi Transfer again to stop it.")
        for i, line in enumerate(ui.wrap(body, 17, W - 120, 4)):
            ui.text(W // 2, 216 + i * 26, line, 17, C["dim"], align="center")
        ui.present()
        time.sleep(1.5)

    # --- drawing
    def draw(self):
        ui = self.ui
        draw_header(ui, "Wi-Fi Transfer", f"v{APP_VERSION}")
        if self.mode == "running":
            self.draw_running()
            if time.monotonic() < self.notice_until:
                ui.fill(0, H - FOOTER_H - 30, W, 30, C["bar"])
                ui.text(W // 2, H - FOOTER_H - 25, self.notice, 14, C["ok"] if self.notice.startswith("✓") else C["warn"],
                        align="center", max_w=W - 24)
            if time.monotonic() < self.confirm_quit_until:
                draw_footer(ui, [("B", _("Press again to stop the transfer"))])
            else:
                draw_footer(ui, [("B", _("Quit")), ("X", _("Background")), ("Y", _("New PIN")), ("SELECT", _("Language"))])
            return
        titles = {
            "starting": (_("Starting…"), ""),
            "nowifi": (_("Wi-Fi is off"), _("Turn on Wi-Fi in muOS (Configuration → Network). "
                                            "Transfer starts automatically once connected.")),
            "stopped": (_("Transfer stopped"), tr(self.message) or _("The transfer page is closed.")),
            "error": (_("Couldn't start"), tr(self.message)),
        }
        title, body = titles.get(self.mode, ("", ""))
        ui.text(W // 2, 170, title, 26, C["text"], align="center")
        for i, line in enumerate(ui.wrap(body, 17, W - 120, 4)):
            ui.text(W // 2, 216 + i * 26, line, 17, C["dim"], align="center")
        if self.mode == "nowifi":
            ui.spinner(W // 2, 340, 12)
            draw_footer(ui, [("B", _("Quit")), ("SELECT", _("Language"))])
        else:
            draw_footer(ui, [("A", _("Start again")), ("B", _("Quit")), ("SELECT", _("Language"))])
        if time.monotonic() < self.notice_until:
            ui.text(W // 2, H - FOOTER_H - 26, self.notice, 14, C["ok"] if self.notice.startswith("✓") else C["warn"],
                    align="center", max_w=W - 24)

    def draw_qr(self, x, y, size, matrix=None):
        qr = matrix if matrix is not None else self.qr
        n = len(qr)
        quiet = 3
        scale = max(1, size // (n + 2 * quiet))
        side = scale * (n + 2 * quiet)
        ui = self.ui
        ui.fill(x, y, side, side, (255, 255, 255))
        for r, row in enumerate(qr):
            c = 0
            while c < n:
                if row[c] & 1:
                    start = c
                    while c < n and row[c] & 1:
                        c += 1
                    ui.fill(x + (quiet + start) * scale, y + (quiet + r) * scale, (c - start) * scale, scale, (0, 0, 0))
                else:
                    c += 1
        return side

    def draw_running(self):
        ui, srv = self.ui, self.server
        side = self.draw_qr(24, HEADER_H + 20, 200)
        ui.text(24 + side // 2, HEADER_H + 26 + side, _("Scan with your phone"), 14, C["dim"], align="center", max_w=side + 20)

        x = 24 + side + 28
        cw = W - x - 20
        y = HEADER_H + 18
        ui.text(x, y, _("Or open in a browser:"), 15, C["dim"], max_w=cw)
        ui.text(x, y + 22, self.url.replace("http://", ""), 23, C["text"], max_w=cw)
        ui.text(x, y + 62, "PIN", 15, C["dim"])
        ui.text(x, y + 80, "  ".join(srv.pin), 42, C["accent"])

        n = srv.connected()
        status = (_("Connected · 1 device") if n == 1 else _("Connected · {n} devices", n=n)) if n \
            else _("Waiting for your phone or PC…")
        ui.text(x, y + 142, status, 16, C["ok"] if n else C["dim"], max_w=cw)

        # uploads in progress
        yy = HEADER_H + 20 + side + 36
        with srv.lock:
            uploads = list(srv.uploads.values())
            events = list(srv.events)
        bk = dict(srv.backup) if srv.backup else None
        if bk:
            pct = bk["done"] / bk["total"] if bk["total"] else 0
            ui.text(24, yy, _("Full SD card backup: don't turn off the game console"), 15, C["text"], max_w=W - 170)
            ui.text(W - 24, yy, _("{done} of {total}", done=fmt_size(bk['done']), total=fmt_size(bk['total'])), 14, C["dim"],
                    align="right")
            ui.fill(24, yy + 22, W - 48, 6, C["row"])
            ui.fill(24, yy + 22, int((W - 48) * pct), 6, C["accent"])
            yy += 38
        for up in uploads[:2]:
            pct = up["done"] / up["total"] if up["total"] else 1
            ui.text(24, yy, f"↓ {up['name']}", 15, C["text"], max_w=W - 140)
            ui.text(W - 24, yy, _("{pct}% of {total}", pct=int(pct * 100), total=fmt_size(up['total'])), 14, C["dim"], align="right")
            ui.fill(24, yy + 22, W - 48, 6, C["row"])
            ui.fill(24, yy + 22, int((W - 48) * pct), 6, C["accent"])
            yy += 38
        room = max(0, (H - FOOTER_H - 34 - yy) // 22)
        for _ts, text in events[:room]:
            ui.text(24, yy, tr(text), 14, C["dim"], max_w=W - 48)
            yy += 22
        ui.text(W // 2, H - FOOTER_H - 26, _("Anyone on this Wi-Fi with the PIN can manage your files."), 13,
                C["faint"], align="center", max_w=W - 24)
