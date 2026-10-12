#!/usr/bin/env python3
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

It measures BOTH sides of the comparison in the same run, using the timings the proxy
itself records in the journal (prompt_ms / restore_ms), instead of extrapolating from
wall-clock time:

  control : A's request after B, with autorestore OFF -> real re-read (prompt_ms of the
            whole re-read, from the journal)
  restore : A's request after B, with autorestore ON  -> restore_ms + prompt_ms of the
            few new tokens

The reported speed-up is control_ms / (restore_ms + new_tokens_ms).

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
"""
from __future__ import annotations

import argparse
import json
import os
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


def run_phase(a, model, slot_dir, proxy_bin, tok, autorestore, phase_tag):
    """One full phase: A grows -> autosave -> B evicts -> A's request, with the given
    autorestore setting. Returns (dict with the journal-derived timings, exit code).

    The journal is read only for events with a timestamp >= the phase start, so a
    second run in the same data dir cannot pick up an event from the first one."""
    phase_cfg = dict(TEST_CONFIG)
    phase_cfg["autosave_idle_s"] = a.idle
    phase_cfg["slot_dir"] = slot_dir
    phase_cfg["autorestore"] = autorestore
    data = a.data
    os.makedirs(data, exist_ok=True)
    cfgpath = os.path.join(data, "config.test.json")
    with open(cfgpath, "w", encoding="utf-8") as f:
        json.dump(phase_cfg, f, indent=2)

    port = free_port(a.port)
    t0 = time.time()
    proc, base, log = start_proxy(proxy_bin, a.upstream, port, data, cfgpath, tok)
    if proc is None:
        print("\n[%s] test proxy did not come up; output in %s" % (phase_tag, os.path.join(data, "proxy.log")))
        return None, 2

    before = set(list_slot_files(slot_dir))
    out = {}
    try:
        msgs_a = [{"role": "system", "content": "Answer in one line. Do not use tools."}]
        grow(base, model, msgs_a, a.tokens, "A")

        print("\n[%s] idle: waiting for autosave (autosave_idle_s=%ss) ..." % (phase_tag, a.idle))
        deadline = time.time() + a.idle + 60
        saved = None
        while time.time() < deadline:
            time.sleep(3.0)
            for e in journal_events(journal_path(data), since=t0):
                if e.get("event") == "autosave":
                    saved = e
            if saved:
                break
        out["autosave"] = saved
        if saved:
            print("  autosave: file=%s tokens=%s n_saved=%s bytes=%s in %s ms (idle %ss)"
                  % (saved.get("file"), saved.get("tokens"), saved.get("n_saved"),
                     saved.get("bytes"), saved.get("ms"), saved.get("idle_s")))
        else:
            print("  no autosave recorded: see", os.path.join(data, "proxy.log"))

        msgs_b = [{"role": "system", "content": "Answer in one line. Do not use tools."}]
        grow(base, model, msgs_b, a.probe_tokens, "B")

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
    ap.add_argument("--slot-dir", default="", help="session-files dir (default: from config.json)")
    ap.add_argument("--tokens", type=int, default=24000, help="target prompt tokens for conversation A")
    ap.add_argument("--turn-tokens", type=int, default=4000, help="tokens per A turn")
    ap.add_argument("--probe-tokens", type=int, default=6000, help="tokens of conversation B (evicts A)")
    ap.add_argument("--idle", type=float, default=20.0, help="autosave_idle_s of the test (default 20)")
    ap.add_argument("--venv", default="", help="venv to use (default: .venv314 if present, else .venv)")
    ap.add_argument("--allow-shared-engine", action="store_true",
                    help="required: this benchmark evicts other sessions from the engine")
    ap.add_argument("--keep-data", action="store_true", help="do not delete the test data dir")
    ap.add_argument("--dry-run", action="store_true", help="print the plan only")
    a = ap.parse_args(argv)
    if not a.data:
        a.data = os.path.join(ROOT, "data-restore-test-%d" % os.getpid())

    slot_dir = a.slot_dir
    if not slot_dir:
        cfgp = os.path.join(ROOT, "config.json")
        if os.path.exists(cfgp):
            try:
                with open(cfgp, encoding="utf-8") as f:
                    slot_dir = json.load(f).get("slot_dir", "")
            except Exception:
                slot_dir = ""

    venv = a.venv
    if not venv:
        venv = ".venv314" if os.path.exists(os.path.join(ROOT, ".venv314/bin/mnemonic-proxy")) else ".venv"
    proxy_bin = os.path.join(ROOT, venv, "bin/mnemonic-proxy")
    tok = os.environ.get("TOKENIZER", "")

    print("=== plan ===")
    print("engine           :", a.upstream, "(the SAME one already running: nothing is restarted)")
    print("test proxy       :", proxy_bin, "data:", a.data)
    print("session files    :", slot_dir or "(none: saved state unavailable)")
    print("conversation A   : grows to ~%d tokens, idle %ss -> autosave" % (a.tokens, a.idle))
    print("conversation B   : ~%d tokens, fills the engine -> A's state leaves memory" % a.probe_tokens)
    print("measurement      : control (autorestore OFF) vs restore (ON), both from the journal")
    print("                   (prompt_ms / restore_ms, not an extrapolated wall time)")
    print("shared engine    :", "allowed" if a.allow_shared_engine else "NOT allowed (need --allow-shared-engine)")
    if a.dry_run:
        return 0
    if not slot_dir:
        print("\nMISSING: --slot-dir (the session-files dir, the same as the engine's --slot-save-path).")
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

    results = {"control": None, "restore": None, "engine": {"model": model, "upstream": a.upstream}}
    own_files = set()
    try:
        print("\n=== phase 1/2: control (autorestore OFF) ===")
        ctl, rc = run_phase(a, model, slot_dir, proxy_bin, tok, False, "control")
        if rc != 0:
            return rc
        results["control"] = ctl
        own_files.update(ctl.get("own_files", []) if ctl else [])

        print("\n=== phase 2/2: restore (autorestore ON) ===")
        rst, rc = run_phase(a, model, slot_dir, proxy_bin, tok, True, "restore")
        if rc != 0:
            return rc
        results["restore"] = rst
        own_files.update(rst.get("own_files", []) if rst else [])
    finally:
        # cleanup: remove ONLY the .bin files this run created
        removed = []
        for f in sorted(own_files):
            try:
                os.remove(os.path.join(slot_dir, f))
                removed.append(f)
            except OSError:
                pass
        if removed:
            print("\ncleaned up %d .bin file(s) created by this run: %s" % (len(removed), ", ".join(removed)))
        elif own_files:
            print("\nnote: could not remove:", ", ".join(sorted(own_files)))
        if a.keep_data:
            print("(data kept in %s)" % a.data)
        else:
            shutil.rmtree(a.data, ignore_errors=True)

    print("\n=== result ===")
    print_report(results)
    with open(os.path.join(a.data, "measure_result.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print("\nresults in", os.path.join(a.data, "measure_result.json"))

    if not (results.get("restore") or {}).get("autorestore"):
        print("\nNOTHING restored: no autorestore event in phase 2.")
        print("If 'reused' is high in the request, the engine kept the prefix (engine checkpoint)")
        print("and the restore was not triggered: raise --probe-tokens so B really fills the engine.")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
