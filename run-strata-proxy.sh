#!/bin/sh
# Start mnemonic-proxy in front of the local Strata server.
#
# Strata must ALREADY be running (./setup.sh in the Strata folder).
# Then point the client (OpenAI base URL) at http://127.0.0.1:8096/v1
#
# Everything is overridable by environment variable, e.g.:
#   STRATA_URL=http://127.0.0.1:8080 PORT=8096 ./run-strata-proxy.sh
#
# kv_archive is opt-in: the proxy runs without Python 3.14 (no cold archive). The
# venv is chosen by who has compression.zstd; a config CREATED here is born with
# kv_archive off — enable it explicitly with KV_ARCHIVE=1 (experimental). A config
# of yours is never modified: if it asks for kv_archive without support, this stops
# with an explanation.
#
# Engine paths: STRATA_DIR is the Strata folder (default ~/Strata). TOKENIZER and
# SLOT_DIR are optional: when missing they are read from the config file of the
# engine in use; without a tokenizer the proxy counts chars / 3.5, without a slot
# dir sessions go to $STRATA_DIR/sessions.
STRATA_DIR="${STRATA_DIR:-$HOME/Strata}"
TOKENIZER="${TOKENIZER:-}"
STRATA_URL="${STRATA_URL:-http://127.0.0.1:8080}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8096}"
DATA="${DATA:-./data}"
CONFIG="${CONFIG:-./config.json}"
PROBE_TIMEOUT="${PROBE_TIMEOUT:-3}"
KV_ARCHIVE="${KV_ARCHIVE:-0}"   # 1: enable kv_archive in the generated config (experimental, needs Python >= 3.14)
# a number: without curl the python fallback does float(), and a non-numeric value
# would give an empty HTTP code read as "the port answered"
case "$PROBE_TIMEOUT" in ''|*[!0-9]*) PROBE_TIMEOUT=3 ;; esac

cd "$(dirname "$0")" || exit 1

# ---- helpers -------------------------------------------------------------
venv_has() { [ -x "$1/bin/mnemonic-proxy" ]; }
# kv_archive needs compression.zstd (Python >= 3.14): look at the support, not the
# folder name — .venv314 can be a 3.12 and .venv can be a 3.14.
venv_zstd() { [ -x "$1/bin/python" ] && "$1/bin/python" -c 'import compression.zstd' >/dev/null 2>&1; }
# Does the config ask for kv_archive? 1 yes, 0 no, empty if the JSON is unreadable
cfg_wants_kv()
{
  [ -x "$VENV/bin/python" ] || return 1
  "$VENV/bin/python" - "$CONFIG" <<'PY' 2>/dev/null
import json, sys
try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(1)
print("1" if cfg.get("kv_archive") else "0")
PY
}
# HTTP code of /v1/status: "conn" for a failed connection or timeout, "nocurl" when
# there is no way to probe. A 404/503 is NOT the engine: it is a service answering no.
probe_status()
{
  if command -v curl >/dev/null 2>&1; then
    code=$(curl -s -o /dev/null -w '%{http_code}' -m "$PROBE_TIMEOUT" "$STRATA_URL/v1/status" 2>/dev/null)
    case "$code" in ""|000) printf 'conn\n' ;; *) printf '%s\n' "$code" ;; esac
    return 0
  fi
  if [ -x "$VENV/bin/python" ]; then
    "$VENV/bin/python" - "$STRATA_URL" "$PROBE_TIMEOUT" <<'PY' 2>/dev/null
import sys, socket, urllib.error, urllib.request
url, to = sys.argv[1] + "/v1/status", float(sys.argv[2])
try:
    with urllib.request.urlopen(url, timeout=to) as r:
        print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except (socket.timeout, OSError):
    print("conn")
PY
    return 0
  fi
  printf 'nocurl\n'
}

# Environment: the same philosophy as Strata — if something is missing, setup.sh
# prepares it on the spot. SKIP_SETUP=1 skips the check (for whoever knows the
# environment is ready).
if [ "${SKIP_SETUP:-0}" != 1 ] && [ -x ./setup.sh ]; then
  if ! ./setup.sh --check >/dev/null 2>&1; then
    echo "[run-strata-proxy] incomplete environment: running ./setup.sh ..."
    ./setup.sh || exit 1
  fi
fi

# The venv is chosen by who has compression.zstd (kv_archive), not by name: without
# 3.14 the proxy runs the same, without the cold archive.
if [ -z "$VENV" ]; then
  if venv_has .venv314 && venv_zstd .venv314; then VENV=.venv314
  elif venv_has .venv && venv_zstd .venv; then VENV=.venv
  elif venv_has .venv314; then VENV=.venv314
  else VENV=.venv; fi
fi
venv_has "$VENV" || { echo "mnemonic-proxy not installed in $VENV (./setup.sh installs it)" >&2; exit 1; }
KV_CAP=0; venv_zstd "$VENV" && KV_CAP=1

# tokenizer and slot_dir: when not given, they are deduced from the engine config
# (the "tokenizer" and "slot_save_path" keys in the JSON files of the Strata folder).
# With more than one config in the folder — a multi-pack install — the file is taken
# only if the engine in use is identified (the "model" from /v1/status, or the port
# in the URL): otherwise no --tokenizer and no deduced slot dir. A file picked at
# random would give the numbers and sessions of a pack that is not the one in use.
# TOKENIZER and SLOT_DIR given by the caller take precedence and skip the discovery.
SLOT="${SLOT_DIR:-}"
if [ -x "$VENV/bin/python" ] && { [ -z "$TOKENIZER" ] || [ -z "$SLOT" ]; }; then
  DISC=$("$VENV/bin/python" - "$STRATA_DIR" "$STRATA_URL" "$PROBE_TIMEOUT" <<'PY' 2>/dev/null
import glob, json, os, sys
base, url, to = sys.argv[1], sys.argv[2], float(sys.argv[3])
cands = []
for p in sorted(glob.glob(os.path.join(base, "*.json"))):
    try:
        cfg = json.load(open(p, encoding="utf-8"))
    except Exception:
        continue
    if isinstance(cfg, dict):
        cands.append(cfg)
if not cands:
    print("NONE")
    sys.exit(0)
if len(cands) > 1:
    model, port = None, None
    try:
        from urllib.parse import urlparse
        u = urlparse(url)
        port = u.port or (443 if u.scheme == "https" else 80)
    except Exception:
        pass
    try:
        import urllib.request
        with urllib.request.urlopen(url.rstrip("/") + "/v1/status", timeout=to) as r:
            model = json.loads(r.read().decode("utf-8", "replace")).get("model")
    except Exception:
        pass
    hits = [c for c in cands
            if (model and c.get("model_name") == model) or (port is not None and c.get("port") == port)]
    if len({json.dumps(c, sort_keys=True) for c in hits}) != 1:
        print("AMBIGUOUS %d" % len(cands))
        sys.exit(0)
    cands = hits
tok = cands[0].get("tokenizer")
slot = cands[0].get("slot_save_path")
print("OK")
print(tok if isinstance(tok, str) and os.path.isdir(tok) else "")
print(slot if isinstance(slot, str) and slot else "")
PY
)
  case "$DISC" in
    OK*)
      [ -n "$TOKENIZER" ] || TOKENIZER=$(printf '%s\n' "$DISC" | sed -n '2p')
      [ -n "$SLOT" ] || SLOT=$(printf '%s\n' "$DISC" | sed -n '3p')
      ;;
    AMBIGUOUS*)
      echo "[run-strata-proxy] ${DISC#AMBIGUOUS } engine configs in $STRATA_DIR, engine in use not identified (/v1/status reports no model and the URL port matches none)." >&2
      echo "No file chosen: --tokenizer not passed and slot_dir not deduced - the proxy counts chars / chars_per_token and sessions go to $STRATA_DIR/sessions." >&2
      ;;
  esac
fi

# config.json is local (gitignored): when missing, it is born from the example. The
# slot_dir comes from the engine file identified above (slot_save_path); without one
# it falls back to STRATA_DIR/sessions, the Strata convention. kv_archive stays off
# unless explicitly asked with KV_ARCHIVE=1 (experimental, needs Python >= 3.14).
if [ ! -f "$CONFIG" ] && [ -f examples/config.example.json ]; then
  [ -n "$SLOT" ] || SLOT="$STRATA_DIR/sessions"
  cp examples/config.example.json "$CONFIG"
  KV_ON=0; [ "$KV_ARCHIVE" = 1 ] && [ "$KV_CAP" = 1 ] && KV_ON=1
  if [ -x "$VENV/bin/python" ]; then
    "$VENV/bin/python" - "$CONFIG" "$SLOT" "$KV_ON" <<'PY'
import json, sys
p, slot, kv = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
cfg = json.load(open(p, encoding="utf-8"))
cfg["slot_dir"] = slot
cfg["kv_archive"] = kv
json.dump(cfg, open(p, "w", encoding="utf-8"), indent=2)
print("[run-strata-proxy] config created at %s: slot_dir=%s, kv_archive=%s" % (p, slot, "true" if kv else "false"))
PY
  else
    echo "[run-strata-proxy] config created at $CONFIG: set slot_dir to the Strata sessions folder"
  fi
  if [ "$KV_ARCHIVE" = 1 ] && [ "$KV_CAP" != 1 ]; then
    echo "[run-strata-proxy] KV_ARCHIVE=1 ignored: $VENV has no compression.zstd (needs Python >= 3.14). Run ./setup.sh to build .venv314." >&2
  elif [ "$KV_ON" != 1 ] && [ "$KV_CAP" = 1 ]; then
    echo "[run-strata-proxy] kv_archive is available (Python >= 3.14 in $VENV) but OFF in the new config: enable it with KV_ARCHIVE=1 $0"
  fi
fi

# slot_dir: the proxy reads it from the config. Print the effective value and check
# the folder exists — saved state (autosave/restore) writes there through the engine.
EFF_SLOT=""
if [ -x "$VENV/bin/python" ] && [ -f "$CONFIG" ]; then
  EFF_SLOT=$("$VENV/bin/python" - "$CONFIG" <<'PY' 2>/dev/null
import json, sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("slot_dir") or "")
except Exception:
    pass
PY
)
fi
if [ -n "$EFF_SLOT" ] && [ ! -d "$EFF_SLOT" ]; then
  echo "[run-strata-proxy] WARNING: slot_dir=$EFF_SLOT does not exist" >&2
  echo "  saved state (autosave/restore) has nowhere to write until the folder exists:" >&2
  echo "  mkdir -p \"$EFF_SLOT\"" >&2
fi

# ---- kv_archive: the gate, before the proxy stops at startup --------------
WANT=$(cfg_wants_kv)
if [ -z "$WANT" ]; then
  echo "could not read $CONFIG (invalid JSON?)" >&2
  exit 1
fi
if [ "$WANT" = 1 ] && [ "$KV_CAP" != 1 ] && [ "${SKIP_SETUP:-0}" != 1 ] && [ -x ./setup.sh ]; then
  echo "[run-strata-proxy] the config asks for kv_archive and $VENV has no compression.zstd: trying ./setup.sh ..."
  ./setup.sh >/dev/null 2>&1 || true
  if venv_has .venv314 && venv_zstd .venv314; then VENV=.venv314; KV_CAP=1; fi
fi
if [ "$WANT" = 1 ] && [ "$KV_CAP" != 1 ]; then
  echo "the config $CONFIG asks for \"kv_archive\": true, but the Python of $VENV has no compression.zstd (needs Python >= 3.14)." >&2
  echo "  the proxy would stop at startup. Options:" >&2
  echo "    ./setup.sh                            build .venv314 (a Python >= 3.14 in PATH or KV_PYTHON is used first, uv downloads one otherwise)" >&2
  echo "    \"kv_archive\": false                   in $CONFIG: the proxy runs the same, without the cold archive" >&2
  echo "    VENV=/path/to/venv314 ./run-strata-proxy.sh" >&2
  echo "  $CONFIG was not modified." >&2
  exit 1
fi

# ---- the engine: warn first, with the Strata start command -----------------
# Only 2xx is the engine. Connection/timeout -> "conn"; 4xx/5xx -> the port answers,
# but it is not the engine.
CODE=$(probe_status)
case "$CODE" in
  2*) : ;;
  conn)
    echo "Strata does not answer at $STRATA_URL/v1/status (connection failed or timeout) - start it first: cd $STRATA_DIR && ./setup.sh" >&2
    exit 1 ;;
  nocurl)
    echo "no curl and no python in the venv: could not check $STRATA_URL/v1/status (install curl, or run ./setup.sh)" >&2
    exit 1 ;;
  *)
    echo "Strata answered HTTP $CODE at $STRATA_URL/v1/status: the port answers, but it is not the engine (some other service, or the engine is down)." >&2
    exit 1 ;;
esac

echo "[run-strata-proxy] venv=$VENV kv_archive=$([ "$WANT" = 1 ] && [ "$KV_CAP" = 1 ] && echo on || echo off) upstream=$STRATA_URL listen=$HOST:$PORT data=$DATA config=$CONFIG slot_dir=${EFF_SLOT:-?} tokenizer=$([ -n "$TOKENIZER" ] && echo "$TOKENIZER" || echo 'chars/3.5')"
set --
[ -n "$TOKENIZER" ] && set -- --tokenizer "$TOKENIZER"
exec "$VENV/bin/mnemonic-proxy" \
  --upstream "$STRATA_URL" \
  --host "$HOST" --port "$PORT" \
  --data "$DATA" \
  --config "$CONFIG" "$@"
