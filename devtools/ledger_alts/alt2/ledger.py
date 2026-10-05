"""Alternative correct ledger #2 (verifier fairness check, not the reference).

Design choices that differ from the reference:
- segments are created lazily, on the first record after open or compaction
  (the directory is synced before that record is acknowledged);
- records go through a buffered file object: write, flush, fdatasync;
- recovery repairs a torn segment by rewriting its valid prefix to a .tmp
  file and renaming it over the original;
- the snapshot is written straight to its final name (it only becomes live
  once CURRENT names it).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import zlib


class LedgerError(Exception):
    pass


def encode(op, key, data):
    body = json.dumps({"op": op, "key": key, "data": base64.b64encode(data).decode()}, separators=(",", ":")).encode()
    return b"%08x %s\n" % (zlib.crc32(body), body)


def valid_prefix(blob):
    recs, pos = [], 0
    for line in blob.split(b"\n")[:-1]:
        try:
            crc, body = line.split(b" ", 1)
            if len(crc) != 8 or crc != crc.lower() or zlib.crc32(body) != int(crc, 16):
                break
            o = json.loads(body)
            recs.append((o["op"], o["key"], base64.b64decode(o["data"], validate=True)))
        except (ValueError, KeyError, binascii.Error):
            break
        pos += len(line) + 1
    return recs, pos


class Ledger:
    def __init__(self, path):
        self.path = path
        self.intents, self.results = {}, {}
        self.fh = None
        self.dead = False
        try:
            self.number = self._recover()
        except OSError as exc:
            raise LedgerError(exc) from exc

    def _f(self, name):
        return os.path.join(self.path, name)

    def _sync_dir(self):
        fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _put(self, name, data):
        with open(self._f(name), "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fdatasync(fh.fileno())

    def _recover(self):
        names = set(os.listdir(self.path))
        live = 0
        if "CURRENT" in names:
            with open(self._f("CURRENT"), "rb") as fh:
                live = int(re.fullmatch(rb"snap-(\d{6})\.log\n", fh.read()).group(1))
            with open(self._f(f"snap-{live:06d}.log"), "rb") as fh:
                self._take(valid_prefix(fh.read())[0])
        top = live
        changed = False
        for name in sorted(names):
            m = re.fullmatch(r"seg-(\d{6})\.log", name)
            if not m or int(m.group(1)) <= live:
                continue
            top = max(top, int(m.group(1)))
            with open(self._f(name), "rb") as fh:
                blob = fh.read()
            recs, used = valid_prefix(blob)
            self._take(recs)
            if used != len(blob):
                self._put(name + ".tmp", blob[:used])
                os.rename(self._f(name + ".tmp"), self._f(name))
                changed = True
        for name in names:
            seg = re.fullmatch(r"seg-(\d{6})\.log", name)
            snap = re.fullmatch(r"snap-(\d{6})\.log", name)
            if (seg and int(seg.group(1)) <= live) or (snap and int(snap.group(1)) != live) or name.endswith(".tmp"):
                if os.path.exists(self._f(name)):
                    os.unlink(self._f(name))
                    changed = True
        if changed:
            self._sync_dir()
        return top + 1

    def _take(self, recs):
        for op, key, data in recs:
            if op == "begin":
                self.intents.setdefault(key, data)
            else:
                self.intents.setdefault(key, b"")
                self.results.setdefault(key, data)

    def _record(self, line):
        if self.dead:
            raise LedgerError("closed or failed")
        try:
            if self.fh is None:
                self.fh = open(self._f(f"seg-{self.number:06d}.log"), "ab")
                self._sync_dir()
            self.fh.write(line)
            self.fh.flush()
            os.fdatasync(self.fh.fileno())
        except OSError as exc:
            self.dead = True
            raise LedgerError(exc) from exc

    def status(self, key):
        if key in self.results:
            return "done", self.results[key]
        return ("in_doubt", None) if key in self.intents else ("new", None)

    def begin(self, key, request):
        state = self.status(key)
        if state[0] != "new":
            return state
        self._record(encode("begin", key, request))
        self.intents[key] = bytes(request)
        return state

    def complete(self, key, result):
        if key in self.results:
            return
        if key not in self.intents:
            raise ValueError(key)
        self._record(encode("done", key, result))
        self.results[key] = bytes(result)

    def compact(self):
        if self.dead:
            raise LedgerError("closed or failed")
        try:
            covered = self.number
            body = b"".join(encode("begin", k, v) + (encode("done", k, self.results[k]) if k in self.results else b"")
                            for k, v in self.intents.items())
            if self.fh is not None:
                self.fh.close()
                self.fh = None
            self._put(f"snap-{covered:06d}.log", body)
            self._put("CURRENT.tmp", f"snap-{covered:06d}.log\n".encode())
            os.rename(self._f("CURRENT.tmp"), self._f("CURRENT"))
            self._sync_dir()
            self.number = covered + 1
            for name in os.listdir(self.path):
                seg = re.fullmatch(r"seg-(\d{6})\.log", name)
                snap = re.fullmatch(r"snap-(\d{6})\.log", name)
                if (seg and int(seg.group(1)) <= covered) or (snap and int(snap.group(1)) != covered):
                    os.unlink(self._f(name))
        except OSError as exc:
            self.dead = True
            raise LedgerError(exc) from exc

    def close(self):
        if self.fh is not None:
            self.fh.close()
            self.fh = None
        self.dead = True
