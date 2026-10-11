# © 2026 PocketKode · SPDX-License-Identifier: GPL-3.0-only
"""config.ini and the folders the transfer page offers, shared by the app and its background process (no screen)."""
import configparser
import os

from .files import ROOT_CANDIDATES, Roots


def load_config(app_dir):
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(os.path.join(app_dir, "config.ini"))
    cfg = {"port": "8080", "idle_minutes": "15", "roms_path": "/mnt/mmc/ROMS", "swap_ab": "no"}
    for section in cp.sections():
        cfg.update(dict(cp.items(section)))
    return cfg


def make_roots(cfg):
    test_root = os.environ.get("WT_TEST_ROOT")  # run on a PC against a fake SD card
    if test_root:
        fake = {"roms": "ROMS", "archive": "ARCHIVE", "bios": "MUOS/bios", "saves": "MUOS/save",
                "screenshots": "MUOS/screenshot", "music": "MUOS/music", "ports": "ports", "sd1": "."}
        cands = [(rid, label, [os.path.join(test_root, fake[rid])], desc)
                 for rid, label, _, desc in ROOT_CANDIDATES if rid in fake]
        if os.environ.get("WT_TEST_SD2"):  # a fake second SD card
            sd2 = os.environ["WT_TEST_SD2"]
            sub = {"roms2": "ROMS", "archive2": "ARCHIVE", "sd2": ""}
            cands += [(rid, label, [os.path.join(sd2, sub[rid]).rstrip("/")], desc)
                      for rid, label, _, desc in ROOT_CANDIDATES if rid in sub]
        return Roots(os.path.join(test_root, "ROMS"), os.path.join(test_root, "share"), cands)
    return Roots(roms_path=cfg.get("roms_path") or "/mnt/mmc/ROMS")
