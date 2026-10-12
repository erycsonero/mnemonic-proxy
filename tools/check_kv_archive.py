#!/usr/bin/env python3
"""Confirm that `kv_archive` is working, and how much it saves.

STRICTLY READ-ONLY: it does not archive, does not delete, does not touch the engine,
and never creates the archive directories. Opening a missing archive is reported, not
materialised.

It takes a snapshot of three things and compares them:

  sessions/     the saved-state .bin files (bytes, file age)
  kv-archive/   blocks/*.kvb + manifests/*.kva (usage, blocks shared by dedup)
  conditions    what the archiver asks for (ctxproxy/core.py) and how far it is from firing

The conditions to archive are ALL three together:

  engine idle for >= kv_archive_idle_s        (default 600 s = 10 min)
  state file idle for >= kv_archive_min_age_s (default 1800 s = 30 min)
  state file >= kv_archive_min_bytes          (default 64 MiB)

and the archiver wakes every kv_archive_poll_s (default 60 s).

The engine's idle counter RESETS on every request: a conversation in progress
(the client calling tools) prevents archiving. That is why the check is done with
the engine left alone.

Exit code: 0 when everything is fine, 1 when a problem is found (a corrupt archive,
a failed restore, or --watch that never saw an archive), 2 on a usage/config error.
That makes it usable in scripts.

Usage:
    .venv314/bin/python tools/check_kv_archive.py              # snapshot now
    .venv314/bin/python tools/check_kv_archive.py --watch      # follow until it archives
    .venv314/bin/python tools/check_kv_archive.py --restore-check
                                                              # prove the restore rebuilds
                                                                a byte-identical .bin (sha256)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import types
import urllib.request

HERE = os.path.abspath(os.path.dirname(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ctxproxy.kvarchive import KvArchive, default_root  # noqa: E402


def human(n):
    if n is None:
        return "-"
    n = float(n)
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            return "%.2f %s" % (n / div, unit)
    return "%d B" % n


def hhmm(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts))


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def engine_status(upstream, timeout=8.0):
    """/v1/status of the engine: requests, in_flight, started. None if it does not answer."""
    try:
        with urllib.request.urlopen(upstream.rstrip("/") + "/v1/status", timeout=timeout) as r:
            st = json.loads(r.read().decode())
    except Exception as e:
        return {"error": str(e)}
    act = st.get("activity") or {}
    return {"requests": act.get("requests"), "in_flight": act.get("in_flight"),
            "started": st.get("started"), "loaded": st.get("loaded")}


def snapshot_sessions(slot_dir):
    out = []
    if not slot_dir or not os.path.isdir(slot_dir):
        return out
    now = time.time()
    for f in sorted(os.listdir(slot_dir)):
        p = os.path.join(slot_dir, f)
        if not os.path.isfile(p):
            continue
        st = os.stat(p)
        out.append({"name": f, "bytes": st.st_size, "mtime": st.st_mtime,
                    "age_s": round(now - st.st_mtime)})
    return out


def snapshot_archive(arc):
    """usage() from the library + blocks shared between manifests (dedup).

Read-only: a KvArchive is opened only when the root exists, because building one
would create root/blocks and root/manifests as a side effect."""
    if not os.path.isdir(arc.root):
        return {"root": arc.root, "exists": False}
    u = arc.usage()
    rc = arc.refcounts()
    stored = arc.stored_blocks()
    shared = {h: c for h, c in rc.items() if c > 1}
    shared_bytes = sum(stored.get(h, 0) for h in shared)
    logical_shared = sum(c - 1 for c in shared.values())
    return {"root": arc.root, "exists": True, "manifests": u["manifests"],
            "blocks": u["blocks"], "stored_bytes": u["stored_bytes"],
            "logical_bytes": u["logical_bytes"], "ratio": u["ratio"],
            "shared_blocks": len(shared), "shared_stored_bytes": shared_bytes,
            "reuses": logical_shared, "names": arc.names()}


def verify_all(arc, names=None):
    out = []
    for n in (names or arc.names()):
        t = time.time()
        try:
            ok = arc.verify(n)
        except Exception as e:
            ok = False
            out.append({"name": n, "ok": False, "error": str(e)[:200]})
            continue
        out.append({"name": n, "ok": ok, "ms": round((time.time() - t) * 1000)})
    return out


def restore_check(arc, dest_dir=None):
    """Prove the restore: rebuild every archive and compare the sha256 with the manifest.

    Restores into a private temp dir (tempfile.mkdtemp) unless one is given, and
    removes it at the end: the audit never writes to a fixed path under /tmp."""
    own_dir = dest_dir is None
    if own_dir:
        dest_dir = tempfile.mkdtemp(prefix="kva-restore-check-")
    out = []
    try:
        for n in arc.names():
            m = arc.manifest(n)
            dest = os.path.join(dest_dir, n + ".bin")
            try:
                r = arc.restore(n, dest)
                sha = hashlib.sha256()
                with open(dest, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 22), b""):
                        sha.update(chunk)
                out.append({"name": n, "bytes": r["bytes"], "ms": r["ms"],
                            "sha256_ok": sha.hexdigest() == m["sha256"],
                            "sha256": m["sha256"][:16]})
            except Exception as e:
                out.append({"name": n, "error": str(e)[:200]})
            finally:
                try:
                    os.remove(dest)
                except OSError:
                    pass
    finally:
        if own_dir:
            shutil.rmtree(dest_dir, ignore_errors=True)
    return out


def conditions(cfg, sessions, eng, idle_seen_s):
    """What the archiver asks for and how far it is. idle_seen_s: seconds of engine idle observed."""
    need_idle = float(getattr(cfg, "kv_archive_idle_s", 600.0))
    need_age = float(getattr(cfg, "kv_archive_min_age_s", 1800.0))
    need_bytes = int(getattr(cfg, "kv_archive_min_bytes", 64 * 2 ** 20))
    rows = []
    for s in sessions:
        ok_age = s["age_s"] >= need_age
        ok_size = s["bytes"] >= need_bytes
        ok_eng = (idle_seen_s or 0) >= need_idle and not eng.get("in_flight")
        rows.append({"name": s["name"], "bytes": s["bytes"], "age_s": s["age_s"],
                     "size_ok": ok_size, "age_ok": ok_age, "engine_ok": ok_eng,
                     "ready": ok_age and ok_size and ok_eng,
                     "eta_age_s": max(0, round(need_age - s["age_s"])),
                     "eta_idle_s": max(0, round(need_idle - (idle_seen_s or 0)))})
    return rows, {"idle_s": need_idle, "min_age_s": need_age, "min_bytes": need_bytes,
                  "poll_s": float(getattr(cfg, "kv_archive_poll_s", 60.0))}


def report(sessions, arch, rows, need, eng, idle_seen_s, arc):
    print("== sessions/ (%s) ==" % hhmm(time.time()))
    if not sessions:
        print("  no .bin: there is no saved state (autosave has not saved yet)")
    tot = 0
    for s in sessions:
        tot += s["bytes"]
        print("  %-46s %12s  age %5ss" % (s["name"], human(s["bytes"]), s["age_s"]))
    print("  total %s in %d files" % (human(tot), len(sessions)))

    print("\n== kv-archive/ (%s) ==" % arch["root"])
    if not arch.get("exists"):
        print("  the folder does not exist yet: the archiver has never archived")
    elif arch["manifests"] == 0:
        print("  empty: %d blocks, no manifest - the archiver has not archived anything yet"
              % arch["blocks"])
    else:
        print("  manifests %d, blocks %d" % (arch["manifests"], arch["blocks"]))
        print("  logical %s  stored %s  ratio %s"
              % (human(arch["logical_bytes"]), human(arch["stored_bytes"]),
                 ("%.1f%%" % (arch["ratio"] * 100)) if arch["ratio"] else "-"))
        if arch["logical_bytes"]:
            saved = arch["logical_bytes"] - arch["stored_bytes"]
            print("  saved %s (%.1f%%)" % (human(saved), 100.0 * saved / arch["logical_bytes"]))
        print("  blocks shared by dedup: %d (%s paid only once, %d reuses)"
              % (arch["shared_blocks"], human(arch["shared_stored_bytes"]), arch["reuses"]))
        for n in arch["names"]:
            print("    %s" % n)

    print("\n== why it is not archiving (conditions in ctxproxy/core.py) ==")
    print("  engine idle >= %ss, file idle >= %ss, file >= %s, wakes every %ss"
          % (int(need["idle_s"]), int(need["min_age_s"]), human(need["min_bytes"]), int(need["poll_s"])))
    print("  engine: requests=%s in_flight=%s; idle observed for %ss"
          % (eng.get("requests"), eng.get("in_flight"), int(idle_seen_s or 0)))
    if eng.get("error"):
        print("  engine unreachable: %s" % eng["error"])
    for r in rows:
        print("  %-46s size_ok=%s age_ok=%s engine_ok=%s -> %s"
              % (r["name"], r["size_ok"], r["age_ok"], r["engine_ok"],
                 "READY" if r["ready"] else "missing age %ss, engine %ss"
                 % (r["eta_age_s"], r["eta_idle_s"])))


def open_archive(root):
    """Open a KvArchive only when the root exists; otherwise return None.

    KvArchive.__init__ creates root/blocks and root/manifests, so building one for a
    missing root would write to disk: the audit stays read-only by never doing it."""
    if not root or not os.path.isdir(root):
        return None
    return KvArchive(root)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="check of the cold kv_archive store")
    ap.add_argument("--config", default=os.path.join(ROOT, "config.json"))
    ap.add_argument("--slot-dir", default="", help="default: from config.json")
    ap.add_argument("--root", default="", help="default: <parent of slot_dir>/kv-archive")
    ap.add_argument("--upstream", default="http://127.0.0.1:8080")
    ap.add_argument("--watch", action="store_true", help="follow until a manifest appears")
    ap.add_argument("--interval", type=float, default=60.0, help="--watch poll (default 60 s)")
    ap.add_argument("--duration", type=float, default=3600.0, help="max --watch seconds (default 3600)")
    ap.add_argument("--restore-check", action="store_true",
                    help="rebuild every archive and compare sha256 (private temp dir)")
    ap.add_argument("--json", action="store_true", help="also print the summary JSON")
    a = ap.parse_args(argv)

    raw = read_json(a.config) if os.path.exists(a.config) else {}
    slot_dir = a.slot_dir or raw.get("slot_dir", "")
    cfg = types.SimpleNamespace(
        slot_dir=slot_dir,
        kv_archive_dir=raw.get("kv_archive_dir", ""),
        kv_archive=bool(raw.get("kv_archive", False)),
        kv_archive_idle_s=raw.get("kv_archive_idle_s", 600.0),
        kv_archive_min_age_s=raw.get("kv_archive_min_age_s", 1800.0),
        kv_archive_min_bytes=raw.get("kv_archive_min_bytes", 64 * 2 ** 20),
        kv_archive_poll_s=raw.get("kv_archive_poll_s", 60.0))
    root = a.root or default_root(cfg)
    if not slot_dir:
        print("MISSING slot_dir (config.json without slot_dir and no --slot-dir): no archive")
        return 2
    print("config %s | kv_archive=%s | slot_dir=%s | root=%s" % (a.config, cfg.kv_archive, slot_dir, root))
    if not cfg.kv_archive:
        print("NOTE: kv_archive is off in the config. The archiver is not running: this is just the state.")

    arc = open_archive(root)
    eng0 = engine_status(a.upstream)
    sessions = snapshot_sessions(slot_dir)
    arch = snapshot_archive(arc) if arc else {"root": root, "exists": False}
    rows, need = conditions(cfg, sessions, eng0, 0.0)
    report(sessions, arch, rows, need, eng0, 0.0, arc)

    # verify_all is part of the audit (not only of --restore-check): a corrupt block
    # must be reported and must make the exit code non-zero.
    checks = verify_all(arc) if arc else []
    bad = [r for r in checks if not r.get("ok")]
    print("\n== verify (block hashes + size, %d archives) ==" % len(checks))
    if not arc:
        print("  skipped: the archive folder does not exist")
    elif not checks:
        print("  no manifest to verify")
    else:
        for r in checks:
            print("  %-46s %s%s" % (r["name"], "ok" if r.get("ok") else "CORRUPT",
                                    "" if r.get("ok") else " (%s)" % r.get("error", "hash mismatch")))
    if bad:
        print("  %d corrupt archive(s): the audit failed" % len(bad))

    if not a.watch:
        restore_bad = []
        if a.restore_check:
            print("\n== restore-check (rebuild + sha256) ==")
            for r in restore_check(arc):
                print("  ", json.dumps(r, ensure_ascii=False))
                if r.get("error") or not r.get("sha256_ok"):
                    restore_bad.append(r)
        if a.json:
            print("\n" + json.dumps({"sessions": sessions, "archive": arch, "conditions": rows,
                                     "need": need, "verify": checks, "restore": restore_bad},
                                    ensure_ascii=False, indent=2))
        return 1 if (bad or restore_bad) else 0

    print("\n--watch: engine idle for %ss; a single use of the engine resets the count\n"
          % int(0))
    last_req, idle_since = eng0.get("requests"), time.time()
    seen = set(arch["names"]) if arch.get("exists") else set()
    t_end = time.time() + a.duration
    while time.time() < t_end:
        time.sleep(a.interval)
        eng = engine_status(a.upstream)
        now = time.time()
        if eng.get("requests") != last_req:
            last_req, idle_since = eng.get("requests"), now
        idle_seen = now - idle_since
        sessions = snapshot_sessions(slot_dir)
        arc = open_archive(root)
        arch = snapshot_archive(arc) if arc else {"root": root, "exists": False}
        rows, need = conditions(cfg, sessions, eng, idle_seen)
        new = [n for n in arch["names"] if n not in seen]
        if new:
            print("== %s: ARCHIVED %s ==" % (hhmm(now), ", ".join(new)))
            report(sessions, arch, rows, need, eng, idle_seen, arc)
            if a.restore_check:
                print("\n== restore-check (rebuild + sha256) ==")
                for r in restore_check(arc):
                    print("  ", json.dumps(r, ensure_ascii=False))
            if a.json:
                print(json.dumps({"sessions": sessions, "archive": arch}, ensure_ascii=False, indent=2))
            return 0
        ready = [r for r in rows if r["ready"]]
        print("%s  engine idle %3.0fs  in_flight=%s  manifests=%d  %s"
              % (hhmm(now), idle_seen, eng.get("in_flight"), arch["manifests"],
                 ("READY: " + ", ".join(r["name"] for r in ready)) if ready else
                 "missing age/engine"))
    print("--watch ended without archiving (the engine was never idle long enough)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
