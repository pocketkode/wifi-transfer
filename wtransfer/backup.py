# SPDX-License-Identifier: MIT
# Copyright (c) 2026 PocketKode
"""Full SD card backup: the whole system card (muOS, settings, games, saves) as one .img.gz.

Written back with Rufus / balenaEtcher / Raspberry Pi Imager it gives an exact copy of the card,
e.g. after the card got corrupted. Streamed straight to the browser, so no space is needed on the
handheld.

Free space on the exFAT games partition isn't read: the partition's allocation bitmap says which
clusters are unused, and those go into the image as zeros. Zeros compress to almost nothing, so the
download is about as big as the data on the card, not the card itself.
"""
import os
import struct
import subprocess
import time
import zlib

CHUNK = 1 << 20
SECTOR = 512
MIN_SKIP = 1 << 20  # only skip free runs of at least 1 MiB (keeps reads large)


class BackupError(Exception):
    """Shown to the user."""


# --- finding the card
def mounts():
    out = {}
    try:
        with open("/proc/mounts") as f:
            for line in f:
                dev, mnt = line.split()[:2]
                out[mnt.replace("\\040", " ")] = dev
    except OSError:
        pass
    return out


def disk_of(partition_dev):
    """/dev/mmcblk0p6 -> /dev/mmcblk0 (via sysfs)."""
    name = os.path.basename(os.path.realpath(partition_dev))
    sys_path = f"/sys/class/block/{name}"
    if os.path.exists(os.path.join(sys_path, "partition")):
        name = os.path.basename(os.path.dirname(os.path.realpath(sys_path)))
    return f"/dev/{name}"


def device_size(dev):
    with open(dev, "rb") as f:
        return f.seek(0, os.SEEK_END)


class Card:
    """The system SD card (the one holding /mnt/mmc)."""

    def __init__(self, mount_point="/mnt/mmc", device=None):
        self.mount_point = mount_point
        self.test = bool(device)  # a disk image file instead of a real card (testing on a PC)
        if not device:
            part = mounts().get(mount_point)
            if not part or not part.startswith("/dev/"):
                raise BackupError("Couldn't find the SD card.")
            device = disk_of(part)
        self.device = device
        try:
            self.size = device_size(device)
        except PermissionError as e:
            raise BackupError("The app isn't allowed to read the SD card.") from e
        except OSError as e:
            raise BackupError(f"Couldn't read the SD card: {e.strerror or e}") from e

    # --- layout
    def partitions(self, f):
        """[(start, length)] in bytes, from the GPT or MBR. Empty if there's no table."""
        f.seek(SECTOR)
        hdr = f.read(92)
        if hdr[:8] == b"EFI PART":
            lba, count, esize = struct.unpack_from("<QII", hdr, 72)
            f.seek(lba * SECTOR)
            table = f.read(count * esize)
            parts = []
            for i in range(count):
                e = table[i * esize:(i + 1) * esize]
                if e[:16] == bytes(16):
                    continue
                first, last = struct.unpack_from("<QQ", e, 32)
                parts.append((first * SECTOR, (last - first + 1) * SECTOR))
            return parts
        f.seek(0)
        mbr = f.read(SECTOR)
        if mbr[510:512] != b"\x55\xaa":
            return []
        parts = []
        for i in range(4):
            ptype = mbr[446 + i * 16 + 4]
            first, n = struct.unpack_from("<II", mbr, 446 + i * 16 + 8)
            if ptype and n:
                parts.append((first * SECTOR, n * SECTOR))
        return parts

    def exfat_free(self, f, start, length):
        """Free byte ranges [(offset, length)] of an exFAT partition, or [] if it isn't exFAT."""
        f.seek(start)
        boot = f.read(512)
        if boot[3:11] != b"EXFAT   ":
            return []
        fat_off, _, heap_off, clusters, root = struct.unpack_from("<IIIII", boot, 80)
        bps = 1 << boot[108]
        cluster = bps << boot[109]
        heap = start + heap_off * bps
        if heap + clusters * cluster > start + length:
            return []  # doesn't add up; don't trust it

        def chain(first, limit=1 << 22):
            c, seen = first, 0
            while 2 <= c < clusters + 2 and seen < limit:
                yield c
                seen += 1
                f.seek(start + fat_off * bps + c * 4)
                c = struct.unpack("<I", f.read(4))[0]

        def read_clusters(first, nbytes, contiguous):
            out = bytearray()
            clist = (range(first, first + (nbytes + cluster - 1) // cluster) if contiguous
                     else chain(first))
            for c in clist:
                f.seek(heap + (c - 2) * cluster)
                out += f.read(cluster)
                if len(out) >= nbytes:
                    break
            return bytes(out[:nbytes])

        # find the allocation bitmap entry (type 0x81) in the root directory
        bitmap = None
        for c in chain(root, 64):
            f.seek(heap + (c - 2) * cluster)
            data = f.read(cluster)
            for i in range(0, len(data), 32):
                if data[i] == 0x81 and not data[i + 1] & 1:  # first FAT's bitmap
                    first, size = struct.unpack_from("<IQ", data, i + 20)
                    bitmap = read_clusters(first, size, contiguous=False)
                    break
                if data[i] == 0:
                    break
            if bitmap is not None:
                break
        if not bitmap or len(bitmap) * 8 < clusters:
            return []

        free, run_start = [], None
        min_clusters = max(1, MIN_SKIP // cluster)
        for byte_i, byte in enumerate(bitmap[:(clusters + 7) // 8]):
            if byte == 0 and run_start is None:
                run_start = byte_i * 8
                continue
            if byte == 0:
                continue
            for bit in range(8):
                idx = byte_i * 8 + bit
                if idx >= clusters:
                    break
                if byte >> bit & 1:
                    if run_start is not None:
                        if idx - run_start >= min_clusters:
                            free.append((heap + run_start * cluster, (idx - run_start) * cluster))
                        run_start = None
                elif run_start is None:
                    run_start = idx
        if run_start is not None and clusters - run_start >= min_clusters:
            free.append((heap + run_start * cluster, (clusters - run_start) * cluster))
        return free

    def plan(self):
        """[(offset, length, read?)] covering the whole card; unread parts become zeros."""
        with open(self.device, "rb") as f:
            free = []
            for start, length in self.partitions(f):
                free += self.exfat_free(f, start, length)
        free.sort()
        out, pos = [], 0
        for off, n in free:
            if off > pos:
                out.append((pos, off - pos, True))
            out.append((off, n, False))
            pos = off + n
        if pos < self.size:
            out.append((pos, self.size - pos, True))
        return out

    # --- keeping the card still while it's copied
    def freeze(self):
        """Flush everything, and make the games partition read-only if nothing is writing to it.
        Returns True if it's read-only now (then thaw() must be called)."""
        if hasattr(os, "sync"):
            os.sync()
        if self.test:
            return False
        r = subprocess.run(["mount", "-o", "remount,ro", self.mount_point],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0

    def thaw(self):
        for _ in range(5):
            r = subprocess.run(["mount", "-o", "remount,rw", self.mount_point],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0:
                return True
            time.sleep(1)
        return False


def stream(card, plan, write, progress):
    """Compress the card as gzip and pass each piece to write(). progress(done_bytes, sent_bytes)."""
    gz = zlib.compressobj(1, zlib.DEFLATED, 31)  # level 1: the handheld's CPU is slow
    zeros = bytes(CHUNK)
    done = sent = 0
    with open(card.device, "rb", buffering=0) as f:
        for off, length, read in plan:
            if read:
                f.seek(off)
            left = length
            while left:
                n = min(CHUNK, left)
                if read:
                    data = f.read(n)
                    if not data:
                        raise BackupError("The SD card ended early.")
                    n = len(data)
                else:
                    data = zeros if n == CHUNK else bytes(n)
                out = gz.compress(data)
                if out:
                    write(out)
                    sent += len(out)
                left -= n
                done += n
                progress(done, sent)
    out = gz.flush()
    write(out)
    sent += len(out)
    progress(done, sent)
    return sent
