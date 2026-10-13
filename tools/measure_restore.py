#!/usr/bin/env python3
# Author: Maurizio Verde — LastReload (measurement protocol after LastReload)
"""Measure the automatic restore of the engine's saved state, without restarting the engine.

The proxy restores a saved state when the engine loses the conversation. The triggers are
( ctxproxy/autosave.py, Tracker ):

  1. the engine restarted          -> "started" changed in /v1/status
  2. another conversation used it  -> "requests" changed in /v1/status
  3. returning to a sealed segment

Trigger 2 is not destructive: it works on the live engine. This script exploits it:

  conversation A  grows to --tokens, goes idle -> autosave saves <conv>-seg0-auto-<fp>.bin
  conversation B  uses the engine              -> A's state leaves memory
  conversation A  next request                 -> Tracker sees a different requests -> restore

It measures BOTH sides on the SAME request, using the timings the proxy records in
the journal (prompt_ms / restore_ms), instead of extrapolating from wall-clock time.
Default method ("same request"): one conversation A, then the SAME request is sent
twice, each time after B has evicted A from the engine:

  A grows  -> autosave saves A.bin  -> freeze msgs_a
  B fills the engine  -> request = msgs_a, autorestore OFF  -> control (real re-read)
  B fills the engine  -> request = msgs_a, autorestore ON   -> restore + few new tokens

The reported speed-up is control_ms / (restore_ms + new_tokens_ms).
The older method, "--two-phases", runs a whole A/B cycle per phase instead.

It starts a private proxy (--port, default 8196) with its own data dir in front of the
SAME engine. It does not touch the running proxy and does not restart the engine.

SAFETY: this benchmark evicts other sessions from the engine (conversation B fills the
conversation state), so it requires an explicit --allow-shared-engine. It removes only
the .bin files it created itself.

Exit code: 0 when a restore was measured, 1 when nothing was restored (so it can be used
in scripts), 2 on a usage/config error.

Usage:
    .venv314/bin/python tools/measure_restore.py --allow-shared-engine
    .venv314/bin/python tools/measure_restore.py --dry-run               # plan only
    .venv314/bin/python tools/measure_restore.py --keep-data

Requires an OpenAI-compatible engine with saved state already running (Strata >= 0.1.40
with slot_save_path in the config, or llama-server with --slot-save-path).

Note: Strata-only for now (the journal events it reads are Strata's).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.abspath(os.path.dirname(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))

# Test config. mask_trigger high: what is measured here is the cost of re-reading the
# prompt, so the test conversation must not be masked ahead of time.
TEST_CONFIG = {
    "window": 131072,
    "mask_trigger": 60000,
    "mask_target": 45000,
    "mask_reasoning": False,
    "mask_tool_args": False,
    "mask_anchor": True,
    "slot_save": True,
    "seal_experimental": True,
    "notes_max_tokens": 12288,
    "response_floor": 16384,
    "autosave": True,
    "autosave_idle_s": 20.0,
    "autosave_keep": 4,
    "autosave_max_gb": 12.0,
    "autosave_min_free_gb": 8.0,
    "autorestore": True,
    "autorestore_min_gain": 4096,
    "inject_recall": False,
    "tools_paging": False,
    "live_dump": False,
}


def free_port(base: int) -> int:
    for p in range(base, base + 40):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise SystemExit("no free port from %d" % base)


def get_json(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def chat(base: str, model: str, messages: list, max_tokens: int, timeout: float = 900.0):
    """One chat completions request. -> (total ms, usage, response text)."""
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens,
               "stream": False, "temperature": 0.2}
    req = urllib.request.Request(base.rstrip("/") + "/v1/chat/completions",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read().decode())
    ms = (time.time() - t0) * 1000.0
    usage = body.get("usage") or {}
    txt = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return ms, usage, txt


def filler(turn: int, target_tokens: int) -> str:
    """Long, deterministic text: it grows the prompt without becoming an instruction."""
    unit = ("line %03d: context test record, field a=1 b=2 c=3, "
            "stable value, nothing to do, nothing to answer. " % (turn % 100))
    return (unit * max(1, int(target_tokens / 22)))[: target_tokens * 4]


def journal_events(path: str, since: float | None = None) -> list:
    """Journal events, optionally only those with a timestamp >= `since`.

    Filtering by start time is what makes a second run reliable: events from an earlier
    run in the same data dir are ignored, so an old restore cannot be reported as new."""
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since is not None:
                ts = e.get("t") or e.get("ts") or e.get("time")
                if ts is not None and float(ts) < since:
                    continue
            out.append(e)
    return out
def start_proxy(proxy_bin, upstream, port, data, cfgpath, tok):
    """Start the private test proxy and wait until /health is ok. -> (proc, base, log)."""
    base = "http://127.0.0.1:%d" % port
    cmd = [proxy_bin, "--upstream", upstream, "--host", "127.0.0.1", "--port", str(port),
           "--data", data, "--config", cfgpath]
    if tok:
        cmd += ["--tokenizer", tok]
    log = open(os.path.join(data, "proxy.log"), "w")
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    for _ in range(90):
        time.sleep(1.0)
        if proc.poll() is not None:
            break
        try:
            if get_json(base + "/health").get("status") == "ok":
                return proc, base, log
        except Exception:
            pass
    proc.terminate()
    return None, base, log


def stop_proxy(proc, log):
    if proc is None:
        return
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


def grow(base, model, msgs, target_tokens, tag):
    """Send turns until the prompt reaches target_tokens; returns the grown messages."""
    grown, turn = 0, 0
    while grown < target_tokens:
        turn += 1
        seed = turn if tag == "A" else 500 + turn
        msgs.append({"role": "user", "content": filler(seed, max(1500, target_tokens // 2))})
        ms, usage, txt = chat(base, model, msgs, 48 if tag == "A" else 32)
        grown = usage.get("prompt_tokens") or grown
        print("  %s turn %2d: prompt=%s tokens, %.0f ms" % (tag, turn, usage.get("prompt_tokens"), ms))
        msgs.append({"role": "assistant", "content": (txt or "ok")[:200]})
    return msgs


def list_slot_files(slot_dir):
    """Names of the .bin files in the slot dir (the tool cleans up only what it wrote)."""
    out = []
    if slot_dir and os.path.isdir(slot_dir):
        for f in sorted(os.listdir(slot_dir)):
            if os.path.isfile(os.path.join(slot_dir, f)):
                out.append(f)
    return out


def journal_path(data):
    return os.path.join(data, "journal.jsonl")


def write_cfg(data, slot_dir, autorestore, idle):
    """Write the test proxy config and return its path."""
    cfg = dict(TEST_CONFIG)
    cfg["autosave_idle_s"] = idle
    cfg["slot_dir"] = slot_dir
    cfg["autorestore"] = autorestore
    os.makedirs(data, exist_ok=True)
    path = os.path.join(data, "config.test.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return path


def prepare_a(a, base, model, data, t0):
    """Grow conversation A and wait for its autosave. Returns (msgs_a, autosave event).

    msgs_a is frozen here: the same list is sent for both measurements."""
    msgs_a = [{"role": "system", "content": "Answer in one line. Do not use tools."}]
    grow(base, model, msgs_a, a.tokens, "A")
    print("\nidle: waiting for autosave (autosave_idle_s=%ss) ..." % a.idle)
    deadline = time.time() + a.idle + 60
    saved = None
    while time.time() < deadline:
        time.sleep(3.0)
        for e in journal_events(journal_path(data), since=t0):
            if e.get("event") == "autosave":
                saved = e
        if saved:
            break
    if saved:
        print("  autosave: file=%s tokens=%s n_saved=%s bytes=%s in %s ms (idle %ss)"
              % (saved.get("file"), saved.get("tokens"), saved.get("n_saved"),
                 saved.get("bytes"), saved.get("ms"), saved.get("idle_s")))
    else:
        print("  no autosave recorded: see", os.path.join(data, "proxy.log"))
    return msgs_a, saved


def evict(base, model, probe_tokens):
    """Fill the engine with conversation B so A leaves its memory (the proxy sees it)."""
    msgs_b = [{"role": "system", "content": "Answer in one line. Do not use tools."}]
    grow(base, model, msgs_b, probe_tokens, "B")
    return msgs_b


def measure_request(a, model, msgs_a, autorestore, slot_dir, proxy_bin, tok, data, tag):
    """Send msgs_a through a private proxy with the given autorestore setting and read
    the timings from the journal. Returns (dict, exit code).

    A fresh proxy is used because autorestore is read from the config at start-up: it
    is how the flag changes between the two measurements. The engine is not touched.
    The journal is filtered by the proxy's start time, so the two measurements in the
    same data dir cannot pick up each other's events."""
    cfgpath = write_cfg(data, slot_dir, autorestore, a.idle)
    port = free_port(a.port)
    t0 = time.time()
    proc, base, log = start_proxy(proxy_bin, a.upstream, port, data, cfgpath, tok)
    if proc is None:
        print("\n[%s] test proxy did not come up; output in %s" % (tag, os.path.join(data, "proxy.log")))
        return None, 2
    out = {}
    try:
        print("\n[%s] request (autorestore %s) ..." % (tag, "ON" if autorestore else "OFF"))
        ms, usage_r, _ = chat(base, model, msgs_a, 32)
        evs = journal_events(journal_path(data), since=t0)
        reqs = [e for e in evs if e.get("event") == "request"]
        out["request"] = reqs[-1] if reqs else {}
        restores = [e for e in evs if e.get("event") == "autorestore"]
        out["autorestore"] = restores[-1] if restores else None
        out["raw_request_ms"] = ms
    finally:
        stop_proxy(proc, log)
    return out, 0

def run_same_request(a, model, slot_dir, proxy_bin, tok):
    """Default method: one conversation A and the SAME request twice, each time after B
    evicted A from the engine. Returns (results, exit code).

    control : autorestore OFF -> the engine must re-read A (prompt_ms = whole prompt)
    restore : autorestore ON  -> restore_ms + prompt_ms of the few new tokens

    Guards: it aborts if there was no autosave (nothing to restore) or if the control
    did not really re-read (reused > 0: the engine kept the prefix, so the comparison
    would be against a cached prompt, not a real re-read)."""
    data = a.data
    cfgpath = write_cfg(data, slot_dir, True, a.idle)
    port = free_port(a.port)
    t0 = time.time()
    proc, base, log = start_proxy(proxy_bin, a.upstream, port, data, cfgpath, tok)
    if proc is None:
        print("\ntest proxy did not come up; output in %s" % os.path.join(data, "proxy.log"))
        return None, 2
    before = set(list_slot_files(slot_dir))
    msgs_a = saved = None
    try:
        msgs_a, saved = prepare_a(a, base, model, data, t0)
    finally:
        stop_proxy(proc, log)
    own = sorted(set(list_slot_files(slot_dir)) - before)

    if not saved:
        print("\nABORT: no autosave, there is nothing to restore (raise --idle or --tokens).")
        return {"own_files": own}, 1

    # control: the same request, autorestore OFF
    _evict_via_proxy(a, model, proxy_bin, tok, slot_dir)
    ctl, rc = measure_request(a, model, msgs_a, False, slot_dir, proxy_bin, tok, data, "control")
    if rc != 0:
        return {"own_files": own}, rc
    ctl_req = ctl.get("request") or {}
    if (ctl_req.get("reused") or 0) > 0:
        print("\nABORT: the control did not re-read (reused=%s > 0): the engine kept the prefix,"
              % ctl_req.get("reused"))
        print("so the comparison would not be against a real re-read. Raise --probe-tokens so B")
        print("really fills the engine.")
        return {"control": ctl, "own_files": own}, 1

    # restore: the same request, autorestore ON
    _evict_via_proxy(a, model, proxy_bin, tok, slot_dir)
    rst, rc = measure_request(a, model, msgs_a, True, slot_dir, proxy_bin, tok, data, "restore")
    if rc != 0:
        return {"control": ctl, "own_files": own}, rc

    return {"control": ctl, "restore": rst, "method": "same-request", "own_files": own}, 0


def _evict_via_proxy(a, model, proxy_bin, tok, slot_dir):
    """Run conversation B through a private proxy so A leaves the engine memory."""
    data = a.data
    cfgpath = write_cfg(data, slot_dir, False, a.idle)
    port = free_port(a.port)
    proc, base, log = start_proxy(proxy_bin, a.upstream, port, data, cfgpath, tok)
    if proc is None:
        print("\n[evict] test proxy did not come up (%s)" % os.path.join(data, "proxy.log"))
        return
    try:
        evict(base, model, a.probe_tokens)
    finally:
        stop_proxy(proc, log)


def run_two_phases(a, model, slot_dir, proxy_bin, tok):
    """Older method (--two-phases): a whole A/B cycle per phase. Kept for comparison."""
    results = {"method": "two-phases", "own_files": []}
    own = set()
    for autorestore, key, tag in ((False, "control", "control"), (True, "restore", "restore")):
        print("\n=== %s (autorestore %s) ===" % (tag.upper(), "ON" if autorestore else "OFF"))
        out, rc = run_one_cycle(a, model, slot_dir, proxy_bin, tok, autorestore, tag)
        if rc != 0:
            results["own_files"] = sorted(own)
            return results, rc
        results[key] = out
        own.update(out.get("own_files", []) if out else [])
    results["own_files"] = sorted(own)
    return results, 0


def run_one_cycle(a, model, slot_dir, proxy_bin, tok, autorestore, tag):
    """One A/B cycle ending in A's request, with the given autorestore setting."""
    data = a.data
    cfgpath = write_cfg(data, slot_dir, autorestore, a.idle)
    port = free_port(a.port)
    t0 = time.time()
    proc, base, log = start_proxy(proxy_bin, a.upstream, port, data, cfgpath, tok)
    if proc is None:
        print("\n[%s] test proxy did not come up; output in %s" % (tag, os.path.join(data, "proxy.log")))
        return None, 2
    before = set(list_slot_files(slot_dir))
    out = {}
    try:
        msgs_a, saved = prepare_a(a, base, model, data, t0)
        out["autosave"] = saved
        evict(base, model, a.probe_tokens)
        msgs_a.append({"role": "user", "content": filler(900, 400)})
        ms, usage_r, _ = chat(base, model, msgs_a, 32)
        evs = journal_events(journal_path(data), since=t0)
        reqs = [e for e in evs if e.get("event") == "request"]
        out["request"] = reqs[-1] if reqs else {}
        restores = [e for e in evs if e.get("event") == "autorestore"]
        out["autorestore"] = restores[-1] if restores else None
        out["raw_request_ms"] = ms
    finally:
        stop_proxy(proc, log)
    out["own_files"] = sorted(set(list_slot_files(slot_dir)) - before)
    return out, 0


def print_report(results):
    ctl, rst = results.get("control") or {}, results.get("restore") or {}
    ctl_req = ctl.get("request") or {}
    rst_req = rst.get("request") or {}
    rs = rst.get("autorestore") or {}
    method = results.get("method") or "same-request"

    if method == "same-request":
        print("method: the SAME request, autorestore OFF then ON")
    else:
        print("method: two phases, a whole A/B cycle each")
    print("control (autorestore OFF):")
    print("  request: prompt=%s reused=%s read=%s, prompt_ms=%s  wall=%.0f ms"
          % (ctl_req.get("prompt_tokens"), ctl_req.get("reused"), ctl_req.get("prompt_read"),
             ctl_req.get("prompt_ms"), ctl.get("raw_request_ms") or 0))
    print("restore (autorestore ON):")
    print("  autorestore: file=%s tokens=%s restore_ms=%s"
          % (rs.get("file"), rs.get("tokens"), rs.get("restore_ms") or rs.get("ms")))
    print("  request: prompt=%s reused=%s read=%s, prompt_ms=%s  wall=%.0f ms"
          % (rst_req.get("prompt_tokens"), rst_req.get("reused"), rst_req.get("prompt_read"),
             rst_req.get("prompt_ms"), rst.get("raw_request_ms") or 0))

    ctl_ms = ctl_req.get("prompt_ms") or 0
    new_ms = rst_req.get("prompt_ms") or 0
    restore_ms = rs.get("restore_ms") or rs.get("ms") or 0
    if ctl_ms and (restore_ms or new_ms):
        total = restore_ms + new_ms
        print("\nreal re-read (control):      %.0f ms (prompt_ms, whole prompt)" % ctl_ms)
        print("restore + new tokens:        %.0f ms (restore_ms %s + prompt_ms %s)"
              % (total, restore_ms, new_ms))
        print("speed-up (journal-based):    %.1fx" % (ctl_ms / max(total, 1)))
    else:
        print("\nnot enough data for a speed-up (missing prompt_ms/restore_ms in the journal)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="measure the automatic restore of the saved state")
    ap.add_argument("--upstream", default="http://127.0.0.1:8080", help="engine (default: Strata on 8080)")
    ap.add_argument("--port", type=int, default=8196, help="test proxy port (default 8196)")
    ap.add_argument("--data", default="", help="test data dir (default: a fresh ./data-restore-test-<pid>)")
    ap.add_argument("--slot-dir", default="", help="saved-state dir, as the proxy uses it (default: config.json)")
    ap.add_argument("--tokens", type=int, default=24000, help="target prompt tokens for conversation A")
    ap.add_argument("--turn-tokens", type=int, default=4000, help="tokens per A turn")
    ap.add_argument("--probe-tokens", type=int, default=6000, help="tokens of conversation B (evicts A)")
    ap.add_argument("--idle", type=float, default=20.0, help="autosave_idle_s of the test (default 20)")
    ap.add_argument("--venv", default="", help="venv to use (default: .venv314 if present, else .venv)")
    ap.add_argument("--allow-shared-engine", action="store_true",
                    help="required: this benchmark evicts other sessions from the engine")
    ap.add_argument("--two-phases", action="store_true", dest="two_phases",
                    help="older method: a whole A/B cycle per phase (default: the same request twice)")
    ap.add_argument("--keep-data", action="store_true", help="do not delete the test data dir")
    ap.add_argument("--dry-run", action="store_true", help="print the plan only")
    a = ap.parse_args(argv)
    if not a.data:
        a.data = os.path.join(ROOT, "data-restore-test-%d" % os.getpid())

    # slot_dir is the proxy's own concept (Config.slot_dir, resolved like the proxy does):
    # the flag wins, otherwise the config.json in the repo root.
    slot_dir = a.slot_dir or read_config_slot_dir()
    if slot_dir:
        slot_dir = os.path.abspath(os.path.expanduser(slot_dir))

    venv = a.venv
    if not venv:
        venv = ".venv314" if os.path.exists(os.path.join(ROOT, ".venv314/bin/mnemonic-proxy")) else ".venv"
    proxy_bin = os.path.join(ROOT, venv, "bin/mnemonic-proxy")
    tok = os.environ.get("TOKENIZER", "")
    method = "two-phases" if a.two_phases else "same-request"

    print("=== plan ===")
    print("engine           :", a.upstream, "(the SAME one already running: nothing is restarted)")
    print("test proxy       :", proxy_bin, "data:", a.data)
    print("method           :", method)
    print("saved-state dir  :", slot_dir or "(none: saved state unavailable)")
    print("                   (only the files created by this run are removed at the end)")
    print("conversation A   : grows to ~%d tokens, idle %ss -> autosave" % (a.tokens, a.idle))
    print("conversation B   : ~%d tokens, fills the engine -> A's state leaves memory" % a.probe_tokens)
    if method == "same-request":
        print("measurement      : the SAME request twice (autorestore OFF, then ON), from the journal")
    else:
        print("measurement      : two phases, a whole A/B cycle each (autorestore OFF, then ON)")
    print("shared engine    :", "allowed" if a.allow_shared_engine else "NOT allowed (need --allow-shared-engine)")
    if a.dry_run:
        return 0
    if not slot_dir:
        print("\nMISSING: --slot-dir (the saved-state dir, the same the proxy gets as slot_dir).")
        return 2
    if not a.allow_shared_engine:
        print("\nREFUSING: this benchmark evicts other sessions from the engine.")
        print("  Conversation B fills the conversation state of %s, kicking out whoever is using it" % a.upstream)
        print("  (your running proxy included). Run it on an idle engine and pass --allow-shared-engine.")
        return 2

    try:
        st = get_json(a.upstream.rstrip("/") + "/v1/status")
    except Exception as e:
        print("engine does not answer at %s (%s)" % (a.upstream, e))
        return 2
    model = st.get("model") or "local-model"
    print("\nengine: model=%s started=%s requests=%s"
          % (model, st.get("started"), ((st.get("activity") or {}).get("requests")) or 0))

    # a reused data dir from a previous run: start clean, so old events cannot be reused
    if os.path.exists(a.data):
        print("data dir %s already exists: removing it (start from scratch)" % a.data)
        shutil.rmtree(a.data, ignore_errors=True)

    results = {}
    rc = 0
    try:
        if method == "same-request":
            results, rc = run_same_request(a, model, slot_dir, proxy_bin, tok)
        else:
            results, rc = run_two_phases(a, model, slot_dir, proxy_bin, tok)
    finally:
        cleanup_slots(slot_dir, (results or {}).get("own_files") or [])
        results = results or {}
        print("\n=== result ===")
        print_report(results)
        try:                                   # write the result before the data dir may go
            with open(os.path.join(a.data, "measure_result.json"), "w", encoding="utf-8") as f:
                json.dump(results, f, indent=2, ensure_ascii=False)
            print("\nresults in", os.path.join(a.data, "measure_result.json"))
        except OSError:
            pass
        if a.keep_data:
            print("(data kept in %s)" % a.data)
        else:
            shutil.rmtree(a.data, ignore_errors=True)

    if rc != 0:
        return rc
    if not (results.get("restore") or {}).get("autorestore"):
        print("\nNOTHING restored: no autorestore event in the restore measurement.")
        print("If 'reused' is high in the request, the engine kept the prefix (engine checkpoint)")
        print("and the restore was not triggered: raise --probe-tokens so B really fills the engine.")
        return 1
    return 0


def read_config_slot_dir():
    """slot_dir from the repo-root config.json, the same value the proxy reads."""
    cfgp = os.path.join(ROOT, "config.json")
    if not os.path.exists(cfgp):
        return ""
    try:
        with open(cfgp, encoding="utf-8") as f:
            return json.load(f).get("slot_dir", "")
    except Exception:
        return ""


def cleanup_slots(slot_dir, own_files):
    """Remove only the files this run created (the diff taken before/after), and say so."""
    removed = []
    for f in sorted(own_files):
        try:
            os.remove(os.path.join(slot_dir, f))
            removed.append(f)
        except OSError:
            pass
    if removed:
        print("\ncleaned up %d saved-state file(s) created by this run: %s" % (len(removed), ", ".join(removed)))
    elif own_files:
        print("\nnote: could not remove:", ", ".join(sorted(own_files)))

if __name__ == "__main__":
    sys.exit(main())
