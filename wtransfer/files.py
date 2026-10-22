# © 2026 PocketKode · SPDX-License-Identifier: GPL-3.0-only
"""What the transfer page may see and change: a few named folders on the SD card(s), nothing else.

Every path from the browser is (root id, relative path, name). It's checked here so it can never
leave its root: no "..", no absolute paths, no hidden names, and the real path (after symlinks)
must stay inside the root folder.
"""
import json
import os
import time
import errno
import shutil

# (id, label, candidate paths — first existing one wins, description)
ROOT_CANDIDATES = [
    ("roms", "ROMS", ["{roms}"], "Games, one folder per system"),
    ("archive", "ARCHIVE", ["/mnt/mmc/ARCHIVE"],
     "Drop .muxapp apps here, then install them with Applications → Archive Manager"),
    ("bios", "BIOS", ["/run/muos/storage/bios", "/mnt/mmc/MUOS/bios"], "BIOS files that some emulators need"),
    ("saves", "Saves", ["/run/muos/storage/save", "/mnt/mmc/MUOS/save"], "Game saves and save states"),
    ("screenshots", "Screenshots", ["/run/muos/storage/screenshot", "/mnt/mmc/MUOS/screenshot"],
     "Screenshots taken on the game console"),
    ("music", "Music", ["/run/muos/storage/music", "/mnt/mmc/MUOS/music"], "Music for the muOS menus"),
    ("ports", "Ports", ["/mnt/mmc/ports"], "PortMaster game data, e.g. files a port asks you to add"),
    ("sd1", "SD card 1", ["/mnt/mmc"], "Everything on SD card 1, including muOS's own files"),
    ("roms2", "ROMS", ["/mnt/sdcard/ROMS"], "Games, one folder per system"),
    ("archive2", "ARCHIVE", ["/mnt/sdcard/ARCHIVE"],
     "Drop .muxapp apps here, then install them with Applications → Archive Manager"),
    ("sd2", "SD card 2", ["/mnt/sdcard"], "Everything on the second SD card"),
]

# File extension -> muOS system name. Only extensions that clearly belong to one system;
# ambiguous ones (.iso, .bin, .cue, .chd, .zip…) get no suggestion.
EXT_SYSTEM = {
    ".gb": "Nintendo Game Boy", ".gbc": "Nintendo Game Boy Color", ".gba": "Nintendo Game Boy Advance",
    ".nes": "Nintendo NES - Famicom", ".sfc": "Nintendo SNES - SFC", ".smc": "Nintendo SNES - SFC",
    ".n64": "Nintendo N64", ".z64": "Nintendo N64", ".v64": "Nintendo N64",
    ".md": "Sega Mega Drive - Genesis", ".gen": "Sega Mega Drive - Genesis", ".smd": "Sega Mega Drive - Genesis",
    ".sms": "Sega Master System", ".gg": "Sega Game Gear", ".32x": "Sega 32X",
    ".gdi": "Sega Dreamcast", ".cdi": "Sega Dreamcast", ".cso": "Sony PlayStation Portable",
    ".pce": "NEC PC Engine", ".ngp": "SNK Neo Geo Pocket - Color", ".ngc": "SNK Neo Geo Pocket - Color",
    ".a26": "Atari 2600", ".lnx": "Atari Lynx", ".p8.png": "PICO-8",
}
CARD2 = ("roms2", "archive2", "sd2")  # the folders on the second SD card
SPACE_MARGIN = 20 * 1024 * 1024  # keep this much space free after an upload

# Folder to create under ROMS when no existing folder is mapped to the system yet
DEFAULT_FOLDER = {
    "Nintendo Game Boy": "GB", "Nintendo Game Boy Color": "GBC", "Nintendo Game Boy Advance": "GBA",
    "Nintendo NES - Famicom": "NES", "Nintendo SNES - SFC": "SNES", "Nintendo N64": "N64",
    "Sega Mega Drive - Genesis": "MD", "Sega Master System": "SMS", "Sega Game Gear": "GG",
    "Sega 32X": "SEGA32X", "Sega Dreamcast": "DC", "Sony PlayStation Portable": "PSP",
    "NEC PC Engine": "PCE", "SNK Neo Geo Pocket - Color": "NGP", "Atari 2600": "ATARI2600",
    "Atari Lynx": "LYNX", "PICO-8": "PICO8",
}


# A folder containing one of these is a disc game (muOS keeps each such game in its own folder)
DISC_MAIN = (".m3u", ".gdi", ".cdi", ".cue", ".chd", ".pbp", ".iso", ".cso", ".ccd")


def game_folder_info(path, max_entries=200):
    """(files, total size) if `path` looks like a single disc game, else None."""
    files, total, main = 0, 0, False
    try:
        with os.scandir(path) as it:
            for i, e in enumerate(it):
                if i >= max_entries:
                    return None
                if e.name.startswith(".") or not e.is_file():
                    continue
                files += 1
                total += e.stat().st_size
                main = main or e.name.lower().endswith(DISC_MAIN)
    except OSError:
        return None
    return (files, total) if main else None


class PathError(Exception):
    """A path or name the transfer page isn't allowed to use (shown to the user)."""


def valid_name(name):
    name = (name or "").strip()
    if not name or name in (".", "..") or name.startswith("."):
        raise PathError("That name isn't allowed.")
    if any(c in name for c in '/\\\0') or len(name.encode("utf-8")) > 255:
        raise PathError("Names can't contain / or \\ and must be shorter than 255 bytes.")
    return name


def file_ext(name):
    low = name.lower()
    return ".p8.png" if low.endswith(".p8.png") else os.path.splitext(low)[1]


def card_size(path):
    """Size of the whole SD card holding `path`, as the card reports it (sysfs), or None when unknown. The
    mounted folder is often only one partition (on SD card 1, the system has its own)."""
    try:
        dev = os.stat(path).st_dev
        blk = os.path.realpath(f"/sys/dev/block/{os.major(dev)}:{os.minor(dev)}")
        if os.path.exists(os.path.join(blk, "partition")):
            blk = os.path.dirname(blk)
        with open(os.path.join(blk, "size")) as f:
            return int(f.read()) * 512 or None
    except (OSError, ValueError):
        return None


class Roots:
    def __init__(self, roms_path="/mnt/mmc/ROMS", share_dir="/opt/muos/share", candidates=None):
        self.roots = {}
        for rid, label, paths, desc in candidates or ROOT_CANDIDATES:
            for p in paths:
                p = p.format(roms=roms_path)
                if rid in CARD2 and candidates is None and not os.path.ismount("/mnt/sdcard"):
                    continue  # /mnt/sdcard stays as an empty folder when there's no second card
                if os.path.isdir(p):
                    real = os.path.realpath(p)
                    card = 2 if rid in CARD2 or real.startswith("/mnt/sdcard/") else 1
                    self.roots[rid] = {"id": rid, "label": label, "path": real, "desc": desc, "card": card}
                    break
        self.assign = {}
        self._lost = {}
        try:
            with open(os.path.join(share_dir, "info", "assign", "assign.json")) as f:
                self.assign = {k.lower(): v for k, v in json.load(f).items() if isinstance(v, str)}
        except (OSError, ValueError):
            pass

    # --- resolving paths safely
    def root(self, rid):
        r = self.roots.get(rid)
        if not r:
            raise PathError("Unknown folder.")
        return r["path"]

    def resolve(self, rid, rel="", name=None):
        """Absolute path for root + relative folder (+ optional name), guaranteed inside the root."""
        base = self.root(rid)
        parts = [p for p in (rel or "").replace("\\", "/").split("/") if p]
        for p in parts:
            valid_name(p)
        if name is not None:
            parts.append(valid_name(name))
        full = os.path.join(base, *parts)
        real = os.path.realpath(full)
        if real != base and not real.startswith(base + os.sep):
            raise PathError("That location is outside the allowed folders.")
        return full

    # --- operations
    def usage(self, rid):
        path = self.root(rid)
        u = shutil.disk_usage(path)
        return {"free": u.free, "total": card_size(path) or u.total}

    def storage(self):
        """Free space and whole-card size of each SD card holding one of the folders: [{card, free, total}], card 1
        first.
        Folders on the same card share it (same device); card 2 is the one mounted at /mnt/sdcard (folder "sd2")."""
        cards = {}
        for r in self.roots.values():
            try:
                dev = os.stat(r["path"]).st_dev
            except OSError:
                continue
            two = r["id"] == "sd2" or r["path"].startswith("/mnt/sdcard/")
            cards.setdefault((dev, two), {"card": 2 if two else 1, "path": r["path"]})
        out = []
        for c in cards.values():
            u = shutil.disk_usage(c["path"])
            out.append({"card": c["card"], "free": u.free, "total": card_size(c["path"]) or u.total})
        return sorted(out, key=lambda c: c["card"])

    def lost_space(self, rid, time_limit=20.0):
        """Bytes the card marks as used that no file or folder holds (e.g. left by a copy that was cut off), measured
        on the whole card `rid` ("sd1"/"sd2"); None if the walk takes too long. Kept for 10 minutes."""
        cached = self._lost.get(rid)
        if cached and time.monotonic() - cached[0] < 600:
            return cached[1]
        base = self.root(rid)
        u = shutil.disk_usage(base)
        dev, held, end, stack = os.stat(base).st_dev, 0, time.monotonic() + time_limit, [base]
        while stack:
            if time.monotonic() > end:
                return None
            try:
                it = os.scandir(stack.pop())
            except OSError:
                continue
            with it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if st.st_dev != dev:
                        continue  # something else mounted inside
                    held += st.st_blocks * 512  # the space it really takes on the card, blocks included
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
        lost = max(0, (u.total - u.free) - held)
        self._lost[rid] = (time.monotonic(), lost)
        return lost

    def list(self, rid, rel=""):
        folder = self.resolve(rid, rel)
        if not os.path.isdir(folder):
            raise PathError("That folder doesn't exist any more.")
        out = []
        with os.scandir(folder) as it:
            for e in it:
                if e.name.startswith(".") or e.name.endswith(".part"):
                    continue  # hidden files, macOS "._" leftovers, uploads still in progress
                try:
                    st = e.stat()
                    is_dir = e.is_dir()
                except OSError:
                    continue
                item = {"name": e.name, "dir": is_dir, "size": 0 if is_dir else st.st_size,
                        "mtime": int(st.st_mtime)}
                parts = rel.split("/") if rel else []
                in_system = (rid in ("roms", "roms2") and parts) or (rid == "sd2" and len(parts) > 1 and parts[0].upper() == "ROMS")
                if is_dir and in_system:  # inside a system folder
                    info = game_folder_info(os.path.join(folder, e.name))
                    if info:
                        item.update(game=True, files=info[0], size=info[1])
                out.append(item)
        out.sort(key=lambda x: (not x["dir"], x["name"].lower()))
        return out

    def dirsize(self, rid, rel, name, limit=10.0):
        """(total bytes, files, complete) of a folder and everything in it, for the transfer page. Links aren't
        followed; after `limit` seconds the sum so far is returned with complete=False."""
        top = self.resolve(rid, rel, name or None)  # no name: the folder `rel` itself (a main folder when rel is "")
        if not os.path.isdir(top):
            raise PathError("That folder doesn't exist any more.")
        total, files, end = 0, 0, time.monotonic() + limit
        stack = [top]
        while stack:
            if time.monotonic() > end:
                return total, files, False
            try:
                with os.scandir(stack.pop()) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                stack.append(e.path)
                            elif e.is_file(follow_symlinks=False):
                                total += e.stat(follow_symlinks=False).st_size
                                files += 1
                        except OSError:
                            continue
            except OSError:
                continue
        return total, files, True

    def search(self, text, limit=300, time_limit=8.0):
        """Files and folders on all the main folders whose name has every word of `text` (any case).
        Returns (results, complete): at most `limit` results; after `time_limit` seconds the ones found so far."""
        words = [w for w in (text or "").lower().split() if w]
        if not words:
            return [], True
        out, seen, end = [], set(), time.monotonic() + time_limit
        reals = {os.path.realpath(r["path"]) for r in self.roots.values()}
        for rid in self.roots:
            try:
                base = self.root(rid)
            except PathError:
                continue
            real = os.path.realpath(base)
            if real in seen or any(real.startswith(o + os.sep) for o in reals):
                continue  # reached twice (a muOS storage link, or ROMS inside "SD card 2"): search it once
            seen.add(real)
            stack = [""]
            while stack:
                if time.monotonic() > end:
                    return out, False
                rel = stack.pop()
                try:
                    it = os.scandir(os.path.join(base, rel) if rel else base)
                except OSError:
                    continue
                with it:
                    for e in it:
                        if e.name.startswith(".") or e.name.endswith(".part"):
                            continue
                        try:
                            is_dir = e.is_dir(follow_symlinks=False)
                            low = e.name.lower()
                            if all(w in low for w in words):
                                st = e.stat(follow_symlinks=False)
                                out.append({"root": rid, "path": rel, "name": e.name, "dir": is_dir,
                                            "size": 0 if is_dir else st.st_size, "mtime": int(st.st_mtime)})
                                if len(out) >= limit:
                                    return out, False
                        except OSError:
                            continue
                        if is_dir:
                            stack.append(f"{rel}/{e.name}" if rel else e.name)
        return out, True

    def mkdir(self, rid, rel, name):
        path = self.resolve(rid, rel, name)
        if os.path.exists(path):
            raise PathError("Something with that name already exists.")
        os.makedirs(path)
        return path

    def rename(self, rid, rel, name, new):
        src = self.resolve(rid, rel, name)
        dst = self.resolve(rid, rel, new)
        if not os.path.exists(src):
            raise PathError("That item doesn't exist any more.")
        if os.path.exists(dst):
            raise PathError("Something with the new name already exists.")
        os.rename(src, dst)
        return dst

    def delete(self, rid, rel, name):
        path = self.resolve(rid, rel, name)
        if os.path.realpath(path) == self.root(rid):
            raise PathError("A main folder can't be deleted.")
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.remove(path)
        else:
            raise PathError("That item doesn't exist any more.")
        return path

    def transfer(self, rid, rel, name, to_rid, to_rel, keep=False):
        """Moves (or with keep=True copies) an item into the folder `to_rel` of `to_rid`, on either SD card.
        On the same card a move is a rename; across cards it's a copy, and the original goes only once the copy is complete."""
        src = self.resolve(rid, rel, name)
        if not os.path.lexists(src):
            raise PathError("That item doesn't exist any more.")
        if os.path.realpath(src) == self.root(rid):
            raise PathError("A main folder can't be moved or copied.")
        dest_dir = self.resolve(to_rid, to_rel)
        if not os.path.isdir(dest_dir):
            raise PathError("That folder doesn't exist any more.")
        real_src, real_dest = os.path.realpath(src), os.path.realpath(dest_dir)
        if real_dest == real_src or real_dest.startswith(real_src + os.sep):
            raise PathError("A folder can't go inside itself.")
        if real_dest == os.path.realpath(os.path.dirname(src)):
            raise PathError("It's already in that folder.")
        dst = os.path.join(dest_dir, os.path.basename(src))
        if os.path.lexists(dst):
            raise PathError(f"{os.path.basename(src)} already exists there.")
        if not keep:
            try:
                os.rename(src, dst)
                return dst
            except OSError as e:
                if e.errno != errno.EXDEV:  # anything but "another card"
                    raise
        need = self._size(src)
        if shutil.disk_usage(dest_dir).free < need + SPACE_MARGIN:
            raise PathError(f"Not enough free space there for {os.path.basename(src)}.")
        try:
            self._copy(src, dst)
        except BaseException:  # leave no half copy behind
            if os.path.isdir(dst):
                shutil.rmtree(dst, ignore_errors=True)
            elif os.path.exists(dst):
                os.remove(dst)
            raise
        if not keep:
            if os.path.isdir(src) and not os.path.islink(src):
                shutil.rmtree(src)
            else:
                os.remove(src)
        return dst

    @staticmethod
    def _size(path):
        if not os.path.isdir(path):
            return os.path.getsize(path)
        return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(path) for f in fs)

    @staticmethod
    def _copy(src, dst):
        """File contents only: SD cards (exFAT) don't keep Linux permissions, so copying them would fail."""
        if not os.path.isdir(src):
            shutil.copyfile(src, dst)
            return
        for d, dirs, files in os.walk(src):
            out = os.path.join(dst, os.path.relpath(d, src))
            os.makedirs(out, exist_ok=True)
            for f in files:
                shutil.copyfile(os.path.join(d, f), os.path.join(out, f))

    # --- where a file probably belongs
    def _system_folder(self, roms, system):
        """The folder under `roms` muOS uses for `system`, else the usual name for it."""
        try:
            for d in sorted(os.listdir(roms)):
                if os.path.isdir(os.path.join(roms, d)) and self.assign.get(d.lower()) == system:
                    return d
        except OSError:
            pass
        return DEFAULT_FOLDER.get(system)

    def _card2_roms(self):
        """The ROMS folder's name on SD card 2 (made by the upload if missing)."""
        try:
            for d in os.listdir(self.root("sd2")):
                if d.upper() == "ROMS" and os.path.isdir(os.path.join(self.root("sd2"), d)):
                    return d
        except OSError:
            pass
        return "ROMS"

    def suggest(self, filename, card=1):
        """{'root','path','label'} on SD card `card` for a file type we recognise, else None."""
        ext = file_ext(filename)
        system = EXT_SYSTEM.get(ext)
        if card == 2:
            if "sd2" not in self.roots or not (ext == ".muxapp" or system):
                return None
            if ext == ".muxapp":  # Archive Manager looks in ARCHIVE on either card
                return ({"root": "archive2", "path": "", "label": "ARCHIVE"} if "archive2" in self.roots
                        else {"root": "sd2", "path": "ARCHIVE", "label": "ARCHIVE"})
            if "roms2" in self.roots:
                folder = self._system_folder(self.root("roms2"), system)
                return {"root": "roms2", "path": folder, "label": f"ROMS/{folder}"} if folder else None
            top = self._card2_roms()
            folder = self._system_folder(os.path.join(self.root("sd2"), top), system)
            return {"root": "sd2", "path": f"{top}/{folder}", "label": f"{top}/{folder}"} if folder else None
        if ext == ".muxapp" and "archive" in self.roots:
            return {"root": "archive", "path": "", "label": "ARCHIVE (install with Archive Manager)"}
        if not system or "roms" not in self.roots:
            return None
        folder = self._system_folder(self.root("roms"), system)
        return {"root": "roms", "path": folder, "label": f"ROMS/{folder} ({system})"} if folder else None
