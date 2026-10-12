#!/bin/sh
# setup.sh — prepare the mnemonic-proxy environment, like Strata's setup.sh:
# git clone, ./setup.sh, and the proxy is up. No manual steps.
#
#   ./setup.sh                create the venv and install the package
#   ./setup.sh --check        check only and report; exit 0 if ready, 1 if something is missing
#   ./setup.sh --no-kv        do not touch kv_archive (the Python >= 3.14 venv) even if missing
#   ./setup.sh --install-deps opt-in: run the system package install commands for you
#   ./setup.sh --no-sudo      deprecated alias: not calling sudo is the default now
#
# What the proxy really needs: Python >= 3.10 with venv+pip. The package has no
# dependencies (standard library only). Two things are optional, and this script
# prepares both when it can:
#   * tokenizers (extra "exact")        -> exact token counting
#   * Python >= 3.14 (compression.zstd) -> kv_archive, the cold archive of session files
# kv_archive is opt-in: without Python 3.14 the proxy works the same, just without
# the cold archive.
#
# Privileges: nothing here needs root except installing a system Python, and that is
# NEVER done by default — the script prints the command for your package manager and
# stops. --install-deps runs it for you (directly when already root, via sudo otherwise).

set -u
cd "$(dirname "$0")" || exit 1

VENV_MAIN="${VENV_MAIN:-.venv}"
VENV_KV="${VENV_KV:-.venv314}"
KV_PYTHON="${KV_PYTHON:-}"   # a manually installed Python >= 3.14 to use for VENV_KV
CHECK=0; WANT_KV=1; INSTALL_DEPS=0
for a in "$@"; do
  case "$a" in
    --check)        CHECK=1 ;;
    --no-kv)        WANT_KV=0 ;;
    --install-deps) INSTALL_DEPS=1 ;;
    --no-sudo)      : ;;   # deprecated: printing the command and stopping is the default
    -h|--help)      sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "setup.sh: unknown option: $a (--help lists the options)" >&2; exit 2 ;;
  esac
done
UV=$(command -v uv 2>/dev/null || true)
IS_ROOT=0; [ "$(id -u 2>/dev/null || echo x)" = "0" ] && IS_ROOT=1

# ---- paths: no rm -rf on an arbitrary path -------------------------------
# VENV_MAIN / VENV_KV come from the environment: a wrong value must not delete
# anything outside the project. Accepted: a path inside the project folder, with no
# ".." component, not the project folder itself, not a symlink (neither itself nor
# any ancestor), and not a system path (/usr, /etc, /tmp, /bin, HOME, ...). The
# comparison uses the PHYSICAL path (pwd -P): a parent that is a symlink pointing
# outside the project is rejected even if the written path starts with $ROOT.
ROOT=$(pwd -P)
home=${HOME:-}
# no path component (root -> leaf) may be a symlink
no_symlink_chain()
{
  rel=$1
  cur=$ROOT
  while [ -n "$rel" ]; do
    comp=${rel%%/*}
    if [ "$comp" = "$rel" ]; then rel=""; else rel=${rel#*/}; fi
    [ "$comp" = "." ] && continue
    cur="$cur/$comp"
    [ -L "$cur" ] && return 1
  done
  return 0
}
safe_venv()
{
  v=$1
  [ -n "$v" ] || return 1
  case "$v" in
    .|/|..|/usr|/usr/*|/etc|/etc/*|/var|/var/*|/bin|/bin/*|/sbin|/sbin/*|/lib|/lib/*|/root|/boot|/dev|/dev/*|/home|/home/*|"$home"|"$home"/*) return 1 ;;
    ../*|*/../*|*/..|..) return 1 ;;
  esac
  case "$v" in
    /*) abs=$v ;;
    *)  abs="$ROOT/$v" ;;
  esac
  case "$abs" in
    "$ROOT") return 1 ;;
    "$ROOT"/*) : ;;
    *) return 1 ;;
  esac
  [ -L "$v" ] && return 1
  # o caminho escrito comeca com $ROOT, mas o FISICO e' o que o rm -rf vai seguir:
  # resolve o pai (ou a propria pasta, se existe) e exige que esteja dentro de $ROOT
  if [ -d "$v" ]; then
    phys=$(cd "$v" 2>/dev/null && pwd -P) || return 1
  else
    # the parent may not exist yet (venvs/kv314): walk up to the first one that
    # does, the only one that can be a symlink; an option like "-rf" makes dirname
    # fail and the path is refused, without spinning in place
    parent=$(dirname "$v" 2>/dev/null) || return 1
    [ -n "$parent" ] || return 1
    while [ "$parent" != "/" ] && [ "$parent" != "." ] && [ ! -d "$parent" ]; do
      parent=$(dirname "$parent" 2>/dev/null) || return 1
      [ -n "$parent" ] || return 1
    done
    phys=$(cd "$parent" 2>/dev/null && pwd -P) || return 1
  fi
  case "$phys" in
    "$ROOT"|"$ROOT"/*) : ;;
    *) return 1 ;;
  esac
  if [ "$phys" = "$ROOT" ]; then rel=""; else rel=${phys#"$ROOT"/}; fi
  case "$rel" in
    /*|..|../*|*/../*|*/..) return 1 ;;
  esac
  no_symlink_chain "$rel" || return 1
  return 0
}
reject_venv()
{
  printf 'setup.sh: unsafe %s: %s\n' "$1" "'$2'" >&2
  printf '  it must be a folder inside %s, with no %s, no symlink in any\n' "$ROOT" "'..'" >&2
  printf '  path component, and not a system path (/usr, /etc, /tmp, /bin,\n' >&2
  printf '  /home, HOME). Nothing was removed.\n' >&2
  exit 2
}
safe_venv "$VENV_MAIN" || reject_venv VENV_MAIN "$VENV_MAIN"
safe_venv "$VENV_KV"   || reject_venv VENV_KV "$VENV_KV"
# once validated, the path becomes the physical one: rm -rf follows no symlink
if [ -d "$VENV_MAIN" ]; then VENV_MAIN=$(cd "$VENV_MAIN" && pwd -P); fi
if [ -d "$VENV_KV" ];   then VENV_KV=$(cd "$VENV_KV" && pwd -P);   fi
# rm -rf only on something that really is a venv
is_venv() { [ -d "$1" ] && [ -f "$1/pyvenv.cfg" ]; }

# a python is good if it is >= 3.10 and can create a venv WITH pip: Debian/Ubuntu
# ship venv without ensurepip (separate python3-venv package), and a venv without
# ensurepip has no pip
ok_py() { "$1" -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; }
py_ver() { "$1" -c 'import sys; print(".".join(str(x) for x in sys.version_info[:3]))' 2>/dev/null; }
venv_has_pip() { [ -x "$1/bin/python" ] && "$1/bin/python" -m pip --version >/dev/null 2>&1; }
# the import is tested outside the project: inside the folder Python sees the
# ctxproxy/ source package and would say "already installed" even with an empty venv
venv_has_pkg()
{
  [ -x "$1/bin/python" ] || return 1
  vp=$(cd "$1/bin" && pwd)/python
  (cd / && "$vp" -c 'import ctxproxy' >/dev/null 2>&1)
}
venv_zstd() { [ -x "$1/bin/python" ] && "$1/bin/python" -c 'import compression.zstd' >/dev/null 2>&1; }
venv_tok() { [ -x "$1/bin/python" ] && "$1/bin/python" -c 'import tokenizers' >/dev/null 2>&1; }
say() { printf '%s\n' "$*"; }

# ---- 1) a good python ------------------------------------------------------
pick_py()
{
  for c in python3 python python3.14 python3.13 python3.12 python3.11 python3.10; do
    p=$(command -v "$c" 2>/dev/null) || continue
    if ok_py "$p"; then printf '%s\n' "$p"; return 0; fi
  done
  return 1
}

# ---- privileges: system packages are never installed by default ------------
# run a command with the privileges it needs: directly when already root
# (containers, servers, WSL), via sudo otherwise
as_root()
{
  if [ "$IS_ROOT" = 1 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    say "setup.sh: this step needs root and sudo is not installed:"
    say "  $*"
    return 1
  fi
}

# the detected package manager: apt, dnf, pacman, zypper or ""
pm()
{
  if command -v apt-get >/dev/null 2>&1; then echo apt
  elif command -v dnf >/dev/null 2>&1; then echo dnf
  elif command -v pacman >/dev/null 2>&1; then echo pacman
  elif command -v zypper >/dev/null 2>&1; then echo zypper
  else echo ""
  fi
}

# the command that installs Python 3.10+ with venv+pip on this distro, with the
# right privilege prefix for the current user (sudo only when not root)
python_install_hint()
{
  pre=""; [ "$IS_ROOT" = 1 ] || pre="sudo "
  case "$(pm)" in
    apt)    say "${pre}apt-get update && ${pre}apt-get install -y python3 python3-venv python3-pip" ;;
    dnf)    say "${pre}dnf install -y python3 python3-pip" ;;
    pacman) say "${pre}pacman -S --noconfirm python python-pip" ;;
    zypper) say "${pre}zypper install -y python3 python3-pip" ;;
    *)      say "" ;;
  esac
}

# actually install Python: only runs with --install-deps
python_install_run()
{
  case "$(pm)" in
    apt)    as_root apt-get update && as_root apt-get install -y python3 python3-venv python3-pip ;;
    dnf)    as_root dnf install -y python3 python3-pip ;;
    pacman) as_root pacman -S --noconfirm python python-pip ;;
    zypper) as_root zypper install -y python3 python3-pip ;;
    *)      return 1 ;;
  esac
}

PY="$(pick_py)"
if [ -z "$PY" ]; then
  hint="$(python_install_hint)"
  if [ "$CHECK" = 1 ]; then
    say "MISSING: Python 3.10+ with venv+pip"
    [ -n "$hint" ] && say "  install it with: $hint"
    exit 1
  fi
  if [ "$INSTALL_DEPS" != 1 ]; then
    say "MISSING: Python 3.10+ with venv+pip."
    if [ -n "$hint" ]; then
      say "Installing it needs system packages, so setup.sh does not do it for you."
      say "Run this yourself, then re-run ./setup.sh:"
      say "  $hint"
      if [ "$IS_ROOT" = 1 ]; then
        say "(or re-run as ./setup.sh --install-deps and setup.sh runs it for you)"
      else
        say "(or re-run as ./setup.sh --install-deps and setup.sh runs it for you, via sudo)"
      fi
    else
      say "No supported package manager found (apt-get, dnf, pacman, zypper)."
      say "Install Python 3.10+ with venv+pip using your distro tools, then re-run ./setup.sh."
    fi
    exit 1
  fi
  say "installing Python 3.10+ with the system package manager (--install-deps) ..."
  if ! python_install_run; then
    say "setup.sh: the install failed."
    [ -n "$hint" ] && say "  try it by hand: $hint"
    exit 1
  fi
  PY="$(pick_py)"
  [ -n "$PY" ] || { say "setup.sh: still no Python 3.10+ with venv+pip after the install."; exit 1; }
fi

# ---- 2) the proxy venv ------------------------------------------------------
# a .venv from an interrupted run has a python without pip: start over
if [ -x "$VENV_MAIN/bin/python" ] && ! venv_has_pip "$VENV_MAIN"; then
  if ! is_venv "$VENV_MAIN"; then
    printf 'setup.sh: %s exists without pip and is not a venv (no pyvenv.cfg): not removing it.\n' "$VENV_MAIN" >&2
    printf '  point VENV_MAIN to a free folder inside the project.\n' >&2
    exit 2
  fi
  say "$VENV_MAIN exists without pip (interrupted creation): recreating."
  rm -rf "$VENV_MAIN"
fi

if [ ! -x "$VENV_MAIN/bin/python" ] && [ "$CHECK" = 1 ]; then
  say "MISSING: $VENV_MAIN (run ./setup.sh)"
  exit 1
fi

if [ ! -x "$VENV_MAIN/bin/python" ]; then
  say "creating $VENV_MAIN with $(py_ver "$PY") ..."
  if [ -n "$UV" ]; then
    # --seed: uv makes the venv WITHOUT pip; the seed puts pip inside (the proxy installs with pip)
    "$UV" venv --seed "$VENV_MAIN" --python "$PY" || { say "setup.sh: uv venv failed."; exit 1; }
  else
    "$PY" -m venv "$VENV_MAIN" || { say "setup.sh: python -m venv failed (Ubuntu/Debian: sudo apt install python3-venv)."; exit 1; }
  fi
fi

if ! venv_has_pkg "$VENV_MAIN"; then
  say "installing the mnemonic-proxy package in $VENV_MAIN ..."
  if [ -x "$VENV_MAIN/bin/pip" ]; then
    "$VENV_MAIN/bin/pip" install -q --upgrade pip >/dev/null 2>&1
    "$VENV_MAIN/bin/pip" install -q -e ".[exact]" \
      || "$VENV_MAIN/bin/pip" install -q -e . \
      || { say "setup.sh: pip install failed."; exit 1; }
  elif [ -n "$UV" ]; then
    "$UV" pip install --python "$VENV_MAIN/bin/python" -e ".[exact]" \
      || "$UV" pip install --python "$VENV_MAIN/bin/python" -e . \
      || { say "setup.sh: uv pip install failed."; exit 1; }
  else
    say "setup.sh: neither pip nor uv available to install the package."
    exit 1
  fi
fi

# ---- 3) kv_archive: needs Python >= 3.14 (compression.zstd) ----------------
# if the .venv python already is 3.14, kv_archive works in .venv itself: nothing to do
KV_VENV=""
if venv_zstd "$VENV_MAIN"; then
  KV_VENV="$VENV_MAIN"
elif venv_zstd "$VENV_KV" && venv_has_pkg "$VENV_KV"; then
  KV_VENV="$VENV_KV"
fi

# a python good for the cold archive: >= 3.14, with zstd, venv and ensurepip.
# KV_PYTHON (or a python3.1x in PATH) wins over downloading one with uv: whoever
# installed 3.14 by hand sees it picked up on the next run, with no download.
ok_py_kv() { "$1" -c 'import sys, venv, ensurepip, compression.zstd; sys.exit(0 if sys.version_info >= (3, 14) else 1)' 2>/dev/null; }
pick_py_kv()
{
  for c in $KV_PYTHON python3.16 python3.15 python3.14; do
    [ -n "$c" ] || continue
    p=$(command -v "$c" 2>/dev/null) || continue
    if ok_py_kv "$p"; then printf '%s\n' "$p"; return 0; fi
  done
  return 1
}

build_kv_venv()
{
  if [ -e "$VENV_KV" ] && ! is_venv "$VENV_KV"; then
    printf 'setup.sh: %s exists and is not a venv (no pyvenv.cfg): not removing it.\n' "$VENV_KV" >&2
    printf '  point VENV_KV to a free folder inside the project, or use --no-kv.\n' >&2
    exit 2
  fi
  if is_venv "$VENV_KV"; then rm -rf "$VENV_KV"; fi
  if [ -n "$UV" ]; then
    "$UV" venv --seed "$VENV_KV" --python "$1" >/dev/null 2>&1 || return 1
  else
    "$1" -m venv "$VENV_KV" >/dev/null 2>&1 || return 1
  fi
  if [ -x "$VENV_KV/bin/pip" ]; then
    "$VENV_KV/bin/pip" install -q --upgrade pip >/dev/null 2>&1
    "$VENV_KV/bin/pip" install -q -e ".[exact]" >/dev/null 2>&1 \
      || "$VENV_KV/bin/pip" install -q -e . >/dev/null 2>&1 || return 1
  elif [ -n "$UV" ]; then
    "$UV" pip install --python "$VENV_KV/bin/python" -q -e ".[exact]" >/dev/null 2>&1 \
      || "$UV" pip install --python "$VENV_KV/bin/python" -q -e . >/dev/null 2>&1 || return 1
  else
    return 1
  fi
  venv_zstd "$VENV_KV" || return 1
  KV_VENV="$VENV_KV"
}

if [ -z "$KV_VENV" ] && [ "$WANT_KV" = 1 ] && [ "$CHECK" != 1 ]; then
  PY314=$(pick_py_kv || true)
  if [ -n "$PY314" ]; then
    say "kv_archive needs Python >= 3.14: using $PY314 ($(py_ver "$PY314")) for $VENV_KV ..."
    build_kv_venv "$PY314"
  elif [ -n "$UV" ]; then
    say "kv_archive needs Python >= 3.14: creating $VENV_KV with uv (downloads 3.14 if needed) ..."
    build_kv_venv 3.14
  fi
  if [ -z "$KV_VENV" ]; then
    say "warning: kv_archive needs Python >= 3.14 (compression.zstd), not found on this machine."
    say "         the proxy works the same without it (kv_archive is opt-in). To enable it later,"
    say "         install one of these and re-run ./setup.sh:"
    say "           - uv (no root):  curl -LsSf https://astral.sh/uv/install.sh | sh"
    say "           - Python 3.14 from your distro, or any manually built 3.14 pointed by KV_PYTHON=/path/python3.14"
  fi
fi

# ---- 4) report --------------------------------------------------------------
TOK=no; venv_tok "$VENV_MAIN" && TOK=yes
ZSTD=no; [ -n "$KV_VENV" ] && ZSTD=yes
say "system python used  : $(py_ver "$PY") ($PY)"
say "$VENV_MAIN             : $(py_ver "$VENV_MAIN/bin/python") | package $(venv_has_pkg "$VENV_MAIN" && echo yes || echo no) | tokenizers $TOK"
say "kv_archive (zstd)   : $ZSTD$( [ -n "$KV_VENV" ] && printf ' in %s' "$KV_VENV" )"
if [ "$ZSTD" = no ]; then
  say "                    (opt-in: without Python >= 3.14 the proxy runs the same, without the cold"
  say "                     archive; to enable it later install uv or Python 3.14 and re-run ./setup.sh)"
fi
if [ "$CHECK" = 1 ]; then
  if venv_has_pkg "$VENV_MAIN"; then
    [ -n "$KV_VENV" ] || say "check: ok (without kv_archive: opt-in, the proxy runs anyway)"
    exit 0
  fi
  say "check: MISSING the package installed in $VENV_MAIN (run ./setup.sh)"
  exit 1
fi

say ""
say "done. To start the proxy in front of Strata:"
say "  ./run-strata-proxy.sh"
say "to check the environment without installing anything:"
say "  ./setup.sh --check"
