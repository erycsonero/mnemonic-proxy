"""Tests for setup.sh and run-strata-proxy.sh (the one-click layer, without touching the engine).

Covers the review points:
  1. the flow works without Python 3.14: a created config is born with kv_archive OFF
     (opt-in via KV_ARCHIVE=1), and a personal config asking for kv_archive without
     support makes the script fail with a clear message without being overwritten;
  2. the engine check accepts only 2xx and tells connection/timeout apart from HTTP 4xx/5xx;
  3. no rm -rf on an arbitrary path: VENV_MAIN / VENV_KV outside the project, with "..",
     a symlink, or a folder that is not a venv, are refused before any removal;
  4. privileges: without Python the default is to print the command of the detected
     package manager and stop (no sudo); --install-deps runs it — directly as root,
     via sudo otherwise;
  5. the tests do not depend on the host Python version: the stubs intercept the
     `import compression.zstd` probe, and real venvs hide it via sitecustomize.

The tests use real venvs created by `python -m venv` (fast) and a stub
`mnemonic-proxy` that does not start a server: what is tested is the shell, not the proxy.
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(cmd, cwd, env=None, timeout=180):
    e = dict(os.environ)
    e.update(env or {})
    p = subprocess.run(cmd, cwd=cwd, env=e, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def make_tree():
    """copy the tracked baseline (pyproject, ctxproxy, examples) + the current scripts."""
    tmp = tempfile.mkdtemp(prefix="mp-shell-")
    r = subprocess.run(["git", "archive", "HEAD"], cwd=ROOT, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("git archive falhou: " + r.stderr.decode()[:200])
    subprocess.run(["tar", "-x", "-C", tmp], input=r.stdout, check=True)
    for name in ("setup.sh", "run-strata-proxy.sh"):
        shutil.copy(os.path.join(ROOT, name), os.path.join(tmp, name))
    return tmp


def stub_venv(tree, name, kv=False):
    """venv with bin/python and bin/mnemonic-proxy (a stub that starts no server).

    The python is a wrapper around the real one that intercepts ONLY the `import
    compression.zstd` probe: kv=True passes, kv=False fails — so the test does not
    depend on the host Python version (a 3.14 host does not turn kv on "for free").
    """
    d = os.path.join(tree, name)
    binp = os.path.join(d, "bin")
    os.makedirs(binp, exist_ok=True)
    real = sys.executable
    verdict = "0" if kv else "1"
    wrap = os.path.join(binp, "python")
    with open(wrap, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n"
                 "if [ \"$1\" = \"-c\" ]; then case \"$2\" in *compression.zstd*) exit " + verdict + ";; esac; fi\n"
                 "exec " + real + " \"$@\"\n")
    os.chmod(wrap, 0o755)
    stub = os.path.join(binp, "mnemonic-proxy")
    with open(stub, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\necho \"[stub mnemonic-proxy] $*\"\n")
    os.chmod(stub, 0o755)
    return name


def hide_zstd(venv):
    """Block `import compression.zstd` inside a REAL venv via sitecustomize.

    The venv's bin/python stays untouched (pip and venv keep working correctly);
    only the kv_archive probe sees a pre-3.14 Python, on any machine.
    """
    sp = glob.glob(os.path.join(venv, "lib", "python*", "site-packages"))
    with open(os.path.join(sp[0], "sitecustomize.py"), "w", encoding="utf-8") as fh:
        fh.write("import sys\n"
                 "class _BlockZstd:\n"
                 "    def find_spec(self, name, path=None, target=None):\n"
                 "        if name == 'compression.zstd':\n"
                 "            raise ImportError('blocked by test')\n"
                 "        return None\n"
                 "sys.meta_path.insert(0, _BlockZstd())\n")


def tmp_abs(tree, name):
    return os.path.join(tree, name)


def strata_dir(tree, slot_path):
    """a 'Strata' folder with one model JSON declaring slot_save_path."""
    d = os.path.join(tree, "strata")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "modelo.json"), "w", encoding="utf-8") as fh:
        json.dump({"slot_save_path": slot_path}, fh)
    return d


def strata_configs(tree, name, items):
    """a 'Strata' folder with several engine JSONs: items = [(model, tokenizer, port, slot)].

    Simulates a multi-pack install: each config points to its own pack's tokenizer
    and slot dir, and the launcher must pick the file of the engine in use, not
    the first one in alphabetical order.
    """
    d = os.path.join(tree, name)
    os.makedirs(d, exist_ok=True)
    for model, tok, port, slot in items:
        os.makedirs(tok, exist_ok=True)
        with open(os.path.join(d, "strata-%s.json" % model), "w", encoding="utf-8") as fh:
            json.dump({"model_name": model, "port": port,
                       "slot_save_path": slot, "tokenizer": tok}, fh)
    return d


class _Handler(BaseHTTPRequestHandler):
    code, sleep = 200, 0.0
    body = b'{"status":"ok","context":{"native":131072}}'

    def do_GET(self):
        if self.sleep:
            import time
            time.sleep(self.sleep)
        body = self.body
        self.send_response(self.code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass                      # the client already hung up (timeout): expected noise

    def log_message(self, *a):
        pass


def serve(code=200, sleep=0.0, body=None):
    h = type("H", (_Handler,),
             {"code": code, "sleep": sleep, "body": body or _Handler.body})
    srv = HTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % srv.server_address[1], srv



class RunScriptTests(unittest.TestCase):
    """run-strata-proxy.sh: venv chosen by support, coherent created config, engine check."""

    def setUp(self):
        self.tree = make_tree()
        self.strata = strata_dir(self.tree, os.path.join(self.tree, "sessions"))
        self.cfg = os.path.join(self.tree, "config.json")

    def tearDown(self):
        shutil.rmtree(self.tree, ignore_errors=True)

    def write_config(self, cfg):
        with open(self.cfg, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)

    def read_config(self):
        with open(self.cfg, encoding="utf-8") as fh:
            return json.load(fh)

    def env(self, url, extra=None):
        e = {"SKIP_SETUP": "1", "STRATA_URL": url, "STRATA_DIR": self.strata,
             "TOKENIZER": os.path.join(self.tree, "tok"), "CONFIG": "./config.json",
             "DATA": "./data-test", "PROBE_TIMEOUT": "2"}
        e.update(extra or {})
        return e

    def boot(self, code=200, sleep=0.0, extra=None, body=None):
        url, srv = serve(code, sleep, body)
        rc, out = run(["./run-strata-proxy.sh"], cwd=self.tree, env=self.env(url, extra))
        srv.shutdown()
        srv.server_close()
        return rc, out

    def test_tokenizer_deduzido_do_config_do_motor(self):
        """TOKENIZER is not mandatory: the launcher reads it from the "tokenizer" key
        of the engine JSON, as it already does with slot_save_path. No personal path
        in the launcher: whoever has the engine in any folder finds it automatically."""
        stub_venv(self.tree, ".venv")
        tok = os.path.join(self.tree, "tok_auto")
        os.makedirs(tok, exist_ok=True)
        with open(os.path.join(self.strata, "modelo.json"), "w", encoding="utf-8") as fh:
            json.dump({"slot_save_path": os.path.join(self.tree, "sessions"),
                       "tokenizer": tok}, fh)
        rc, out = self.boot(extra={"TOKENIZER": ""})
        self.assertEqual(rc, 0, out)
        self.assertIn("tokenizer=" + tok, out)
        self.assertIn("--tokenizer " + tok, out)

    def test_sem_tokenizer_o_proxy_sobe_sem_o_argumento(self):
        """No tokenizer in the engine config: the launcher must not pass --tokenizer.
        The proxy counts chars / chars_per_token, which is the documented estimate —
        not an error, and no empty path is passed."""
        stub_venv(self.tree, ".venv")
        rc, out = self.boot(extra={"TOKENIZER": ""})
        self.assertEqual(rc, 0, out)
        self.assertNotIn("--tokenizer", out)
        self.assertIn("chars/3.5", out)

    def test_dois_configs_sem_identificacao_nao_escolhe_nada(self):
        """Two packs in the engine folder, no identifier of the engine in use: the
        launcher passes no --tokenizer and deduces no slot_dir. A file from another
        pack would give wrong numbers and sessions in the wrong place; the chars/3.5
        estimate and the Strata folder's sessions are more honest."""
        stub_venv(self.tree, ".venv")
        d = strata_configs(self.tree, "multi",
                           [("coder", os.path.join(self.tree, "tokA"), 8081,
                             os.path.join(self.tree, "sessA")),
                            ("swift", os.path.join(self.tree, "tokB"), 8080,
                             os.path.join(self.tree, "sessB"))])
        rc, out = self.boot(extra={"TOKENIZER": "", "STRATA_DIR": d})
        self.assertEqual(rc, 0, out)
        # the proxy line is the one that counts: the warning mentions --tokenizer,
        # the call does not.
        stub = [l for l in out.splitlines() if l.startswith("[stub mnemonic-proxy]")]
        self.assertEqual(len(stub), 1, out)
        self.assertNotIn("--tokenizer", stub[0])
        self.assertNotIn("tokA", out)
        self.assertNotIn("tokB", out)
        self.assertNotIn("sessA", out)
        self.assertNotIn("sessB", out)
        self.assertIn("chars/3.5", out)
        self.assertIn("not identified", out)
        self.assertEqual(self.read_config()["slot_dir"], os.path.join(d, "sessions"))

    def test_dois_configs_com_mesmo_model_name_nao_escolhe_nada(self):
        """Two files declaring the SAME model_name, each with its own tokenizer and
        slot: the engine's 'model' does not break the tie (it matches both) and the
        URL port matches none. Nothing is chosen — two candidates, an honest launcher."""
        stub_venv(self.tree, ".venv")
        d = strata_configs(self.tree, "multi",
                           [("swift", os.path.join(self.tree, "tokB"), 8080,
                             os.path.join(self.tree, "sessB")),
                            ("coder", os.path.join(self.tree, "tokA"), 8081,
                             os.path.join(self.tree, "sessA"))])
        with open(os.path.join(d, "strata-swift-copia.json"), "w", encoding="utf-8") as fh:
            json.dump({"model_name": "swift", "port": 9999,
                       "tokenizer": os.path.join(self.tree, "tokC"),
                       "slot_save_path": os.path.join(self.tree, "sessC")}, fh)
        body = b'{"status":"ok","model":"swift","context":{"native":131072}}'
        rc, out = self.boot(body=body, extra={"TOKENIZER": "", "STRATA_DIR": d})
        self.assertEqual(rc, 0, out)
        stub = [l for l in out.splitlines() if l.startswith("[stub mnemonic-proxy]")]
        self.assertEqual(len(stub), 1, out)
        self.assertNotIn("--tokenizer", stub[0])
        self.assertNotIn("tokB", out)
        self.assertNotIn("tokC", out)
        self.assertIn("not identified", out)
        self.assertEqual(self.read_config()["slot_dir"], os.path.join(d, "sessions"))

    def test_model_do_motor_escolhe_tokenizer_e_slot_do_mesmo_pack(self):
        """With two configs, the 'model' from /v1/status decides the file: tokenizer and
        slot_dir come from the SAME pack (the one of the engine in use), not from the
        first in alphabetical order (coder sorts before swift, exactly what we don't want)."""
        stub_venv(self.tree, ".venv")
        d = strata_configs(self.tree, "multi",
                           [("coder", os.path.join(self.tree, "tokA"), 8081,
                             os.path.join(self.tree, "sessA")),
                            ("swift", os.path.join(self.tree, "tokB"), 8080,
                             os.path.join(self.tree, "sessB"))])
        body = b'{"status":"ok","model":"swift","context":{"native":131072}}'
        rc, out = self.boot(body=body, extra={"TOKENIZER": "", "STRATA_DIR": d})
        self.assertEqual(rc, 0, out)
        self.assertIn("--tokenizer " + os.path.join(self.tree, "tokB"), out)
        self.assertNotIn("tokA", out)
        self.assertEqual(self.read_config()["slot_dir"], os.path.join(self.tree, "sessB"))

    def test_porta_da_url_escolhe_tokenizer_e_slot_do_mesmo_pack(self):
        """Without 'model' in /v1/status, the URL port identifies the file: tokenizer
        and slot_dir come from the SAME pack, the one whose port matches STRATA_URL."""
        stub_venv(self.tree, ".venv")
        url, srv = serve(200)
        try:
            port = int(url.rsplit(":", 1)[1])
            d = strata_configs(self.tree, "multi",
                               [("coder", os.path.join(self.tree, "tokA"), 8081,
                                 os.path.join(self.tree, "sessA")),
                                ("swift", os.path.join(self.tree, "tokB"), port,
                                 os.path.join(self.tree, "sessB"))])
            rc, out = run(["./run-strata-proxy.sh"], cwd=self.tree,
                          env=self.env(url, {"TOKENIZER": "", "STRATA_DIR": d}))
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(rc, 0, out)
        self.assertIn("--tokenizer " + os.path.join(self.tree, "tokB"), out)
        self.assertNotIn("tokA", out)
        self.assertEqual(self.read_config()["slot_dir"], os.path.join(self.tree, "sessB"))

    def test_arquivo_unico_da_pasta_preserva_o_comportamento(self):
        """A single JSON in the engine folder: tokenizer and slot_dir from that file,
        exactly as before the identification rule."""
        stub_venv(self.tree, ".venv")
        tok = os.path.join(self.tree, "tok_uno")
        slot = os.path.join(self.tree, "sess_uno")
        os.makedirs(tok, exist_ok=True)
        with open(os.path.join(self.strata, "modelo.json"), "w", encoding="utf-8") as fh:
            json.dump({"model_name": "uno", "port": 8080, "tokenizer": tok,
                       "slot_save_path": slot}, fh)
        rc, out = self.boot(extra={"TOKENIZER": ""})
        self.assertEqual(rc, 0, out)
        self.assertIn("--tokenizer " + tok, out)
        self.assertEqual(self.read_config()["slot_dir"], slot)

    def test_sem_slot_save_path_as_sessoes_vao_para_a_pasta_do_strata(self):
        """A JSON without slot_save_path: nothing is invented. The slot falls back to
        the $STRATA_DIR/sessions convention and the tokenizer still comes from the same file."""
        stub_venv(self.tree, ".venv")
        tok = os.path.join(self.tree, "tok_noslot")
        os.makedirs(tok, exist_ok=True)
        with open(os.path.join(self.strata, "modelo.json"), "w", encoding="utf-8") as fh:
            json.dump({"model_name": "noslot", "tokenizer": tok}, fh)
        rc, out = self.boot(extra={"TOKENIZER": ""})
        self.assertEqual(rc, 0, out)
        self.assertIn("--tokenizer " + tok, out)
        self.assertEqual(self.read_config()["slot_dir"], os.path.join(self.strata, "sessions"))

    def test_tokenizer_e_slot_dir_explicitos_tem_precedencia(self):
        """TOKENIZER and SLOT_DIR given by the caller win, even with two configs and
        an engine that does not identify itself: the discovery does not overwrite them."""
        stub_venv(self.tree, ".venv")
        d = strata_configs(self.tree, "multi",
                           [("coder", os.path.join(self.tree, "tokA"), 8081,
                             os.path.join(self.tree, "sessA")),
                            ("swift", os.path.join(self.tree, "tokB"), 8080,
                             os.path.join(self.tree, "sessB"))])
        rc, out = self.boot(extra={"TOKENIZER": os.path.join(self.tree, "tok_escolhido"),
                                   "SLOT_DIR": os.path.join(self.tree, "sess_escolhida"),
                                   "STRATA_DIR": d})
        self.assertEqual(rc, 0, out)
        self.assertIn("--tokenizer " + os.path.join(self.tree, "tok_escolhido"), out)
        self.assertNotIn("tokA", out)
        self.assertNotIn("tokB", out)
        self.assertNotIn("sessA", out)
        self.assertNotIn("sessB", out)
        self.assertEqual(self.read_config()["slot_dir"],
                         os.path.join(self.tree, "sess_escolhida"))

    def test_sem_python_314_sobe_e_cria_config_sem_kv(self):
        stub_venv(self.tree, ".venv")                      # python without compression.zstd
        rc, out = self.boot()
        self.assertEqual(rc, 0, out)
        cfg = self.read_config()
        self.assertIs(cfg["kv_archive"], False, "a created config must not ask for kv without support")
        self.assertEqual(cfg["slot_dir"], os.path.join(self.tree, "sessions"))
        self.assertIn("kv_archive=off", out)

    def test_com_suporte_o_config_nasce_com_kv_off_e_avisa_como_ligar(self):
        """kv_archive is opt-in (experimental): even with Python 3.14 available the
        created config is born with kv_archive=false, and the output shows how to enable it."""
        stub_venv(self.tree, ".venv314", kv=True)
        rc, out = self.boot()
        self.assertEqual(rc, 0, out)
        self.assertIs(self.read_config()["kv_archive"], False)
        self.assertIn("kv_archive=off", out)
        self.assertIn("KV_ARCHIVE=1", out)

    def test_kv_opt_in_com_suporte_cria_config_com_kv(self):
        stub_venv(self.tree, ".venv314", kv=True)
        rc, out = self.boot(extra={"KV_ARCHIVE": "1"})
        self.assertEqual(rc, 0, out)
        self.assertIs(self.read_config()["kv_archive"], True)
        self.assertIn("kv_archive=on", out)

    def test_kv_opt_in_sem_suporte_cria_config_sem_kv_e_avisa(self):
        """KV_ARCHIVE=1 without Python 3.14: the config is born without kv and the reason is printed."""
        stub_venv(self.tree, ".venv")                        # zstd probe blocked by the stub
        rc, out = self.boot(extra={"KV_ARCHIVE": "1"})
        self.assertEqual(rc, 0, out)
        self.assertIs(self.read_config()["kv_archive"], False)
        self.assertIn("KV_ARCHIVE=1 ignored", out)
        self.assertIn("kv_archive=off", out)

    def test_escolhe_o_venv_que_tem_suporte(self):
        stub_venv(self.tree, ".venv")
        stub_venv(self.tree, ".venv314", kv=True)
        rc, out = self.boot(extra={"KV_ARCHIVE": "1"})
        self.assertEqual(rc, 0, out)
        self.assertIn("venv=.venv314", out)
        self.assertIn("kv_archive=on", out)

    def test_slot_dir_ausente_avisa_sem_falhar(self):
        """The effective slot_dir is printed; if the folder does not exist, a warning
        with the mkdir command (saved state has nowhere to write), but the proxy starts."""
        stub_venv(self.tree, ".venv")
        rc, out = self.boot()
        self.assertEqual(rc, 0, out)
        slot = os.path.join(self.tree, "sessions")
        self.assertIn("slot_dir=%s does not exist" % slot, out)
        self.assertIn('mkdir -p "%s"' % slot, out)
        self.assertIn("slot_dir=" + slot, out)

    def test_slot_dir_presente_nao_avisa(self):
        stub_venv(self.tree, ".venv")
        os.makedirs(os.path.join(self.tree, "sessions"))
        rc, out = self.boot()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("does not exist", out)

    def test_config_pessoal_com_kv_sem_suporte_falha_sem_tocar_no_config(self):
        stub_venv(self.tree, ".venv")
        self.write_config({"slot_dir": "/meu/slot", "kv_archive": True, "response_floor": 16384})
        antes = open(self.cfg, "rb").read()
        rc, out = self.boot()
        self.assertNotEqual(rc, 0)
        self.assertIn("compression.zstd", out)
        self.assertIn("was not modified", out)
        self.assertEqual(open(self.cfg, "rb").read(), antes, "a personal config must not be modified")
        self.assertEqual(self.read_config()["slot_dir"], "/meu/slot")

    def test_config_pessoal_sem_kv_sobe_sem_314(self):
        stub_venv(self.tree, ".venv")
        self.write_config({"slot_dir": "/meu/slot", "kv_archive": False})
        rc, out = self.boot()
        self.assertEqual(rc, 0, out)
        self.assertIn("kv_archive=off", out)
        self.assertIn("--config ./config.json", out)

    def test_config_pessoal_sem_chave_kv_e_tratado_como_sem_kv(self):
        stub_venv(self.tree, ".venv")
        self.write_config({"slot_dir": "/meu/slot"})
        rc, out = self.boot()
        self.assertEqual(rc, 0, out)
        self.assertIn("kv_archive=off", out)

    def test_json_do_config_invalido_falha(self):
        stub_venv(self.tree, ".venv")
        with open(self.cfg, "w", encoding="utf-8") as fh:
            fh.write("{ isto nao e json")
        rc, out = self.boot()
        self.assertNotEqual(rc, 0)
        self.assertIn("invalid JSON", out)

    def test_http_503_do_motor_e_erro(self):
        stub_venv(self.tree, ".venv")
        rc, out = self.boot(code=503)
        self.assertNotEqual(rc, 0)
        self.assertIn("HTTP 503", out)

    def test_http_404_de_porta_de_outra_coisa_e_erro(self):
        stub_venv(self.tree, ".venv")
        rc, out = self.boot(code=404)
        self.assertNotEqual(rc, 0)
        self.assertIn("HTTP 404", out)

    def test_conn_recusada_e_erro(self):
        stub_venv(self.tree, ".venv")
        rc, out = self.boot(extra={"STRATA_URL": "http://127.0.0.1:1"})
        self.assertNotEqual(rc, 0)
        self.assertIn("connection", out)

    def test_timeout_e_erro(self):
        stub_venv(self.tree, ".venv")
        rc, out = self.boot(sleep=3.0, extra={"PROBE_TIMEOUT": "1"})
        self.assertNotEqual(rc, 0)
        self.assertIn("connection", out)

    def test_timeout_invalido_sem_curl_nao_inventa_http(self):
        """Without curl the probe falls back to python: a non-numeric PROBE_TIMEOUT must
        not produce an empty HTTP code, which would be read as 'the port answered'."""
        stub_venv(self.tree, ".venv")
        self.write_config({"slot_dir": "/meu/slot", "kv_archive": False})
        nobin = os.path.join(self.tree, "nobin")
        os.makedirs(nobin, exist_ok=True)
        os.symlink(shutil.which("dirname"), os.path.join(nobin, "dirname"))
        rc, out = self.boot(extra={"STRATA_URL": "http://127.0.0.1:1",
                                   "PROBE_TIMEOUT": "abc", "PATH": nobin})
        self.assertNotEqual(rc, 0, out)
        self.assertIn("connection", out)
        self.assertNotIn("answered HTTP", out)

    def test_sem_venv_nao_sobe(self):
        rc, out = self.boot()
        self.assertNotEqual(rc, 0)


def python_sem_pip(path):
    """a python that fails on `-m pip`: what an interrupted venv looks like to setup.sh."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n"
                 "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"pip\" ]; then exit 1; fi\n"
                 "exec " + sys.executable + " \"$@\"\n")
    os.chmod(path, 0o755)


class SetupScriptTests(unittest.TestCase):
    """setup.sh: checking without Python 3.14 and watched paths before any rm -rf."""

    def setUp(self):
        self.tree = make_tree()

    def tearDown(self):
        shutil.rmtree(self.tree, ignore_errors=True)

    def real_venv(self, name, with_bin=True):
        subprocess.run([sys.executable, "-m", "venv", name], cwd=self.tree,
                       capture_output=True, text=True, timeout=300, check=True)
        if with_bin:
            stub = os.path.join(self.tree, name, "bin", "mnemonic-proxy")
            with open(stub, "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\nprintf 'stub\\n'\n")
            os.chmod(stub, 0o755)
        return os.path.join(self.tree, name)

    def setup(self, args=(), env=None):
        return run(["./setup.sh"] + list(args), cwd=self.tree, env=env or {}, timeout=600)

    def test_sintaxe_posix_e_bash(self):
        for name in ("setup.sh", "run-strata-proxy.sh"):
            for shell in ("sh", "bash"):
                if shutil.which(shell) is None:
                    continue
                rc, out = run([shell, "-n", name], cwd=self.tree)
                self.assertEqual(rc, 0, "%s -n %s: %s" % (shell, name, out))

    def test_check_passa_sem_python_314(self):
        venv = self.real_venv(".venv")
        hide_zstd(venv)          # independent of the host Python (review point 5)
        rc, out = self.setup(["--check"])
        self.assertEqual(rc, 0, out)
        self.assertIn("kv_archive (zstd)", out)
        self.assertIn(": no", out)

    def test_check_falha_sem_venv(self):
        rc, out = self.setup(["--check"])
        self.assertEqual(rc, 1, out)
        self.assertIn("MISSING", out)

    def test_no_kv_nao_tenta_uv_e_sai_zero(self):
        venv = self.real_venv(".venv")
        hide_zstd(venv)
        rc, out = self.setup(["--no-sudo", "--no-kv"])
        self.assertEqual(rc, 0, out)
        self.assertIn("kv_archive (zstd)", out)
        self.assertIn(": no", out)
        self.assertNotIn("creating %s" % os.path.join(self.tree, ".venv314"), out)
        self.assertNotIn("building .venv314", out)
    # ---- privileges (review points 1, 2 and 6) -------------------------------
    # A minimal PATH with NO python: the coreutils setup.sh uses before the Python
    # step (dirname, id), plus logging stubs for the package manager and for sudo.
    # This proves WHAT would be run, without actually installing anything.
    def shim_bin(self, pm="apt", root=False):
        d = os.path.join(self.tree, "shimbin")
        os.makedirs(d, exist_ok=True)
        self.shimlog = os.path.join(self.tree, "shim.log")
        for tool in ("dirname",):
            src = shutil.which(tool)
            os.symlink(src, os.path.join(d, tool))
        if root:
            real_id = shutil.which("id")
            with open(os.path.join(d, "id"), "w", encoding="utf-8") as fh:
                fh.write("#!/bin/sh\n"
                         "if [ \"$1\" = \"-u\" ]; then echo 0; else exec %s \"$@\"; fi\n" % real_id)
                os.chmod(os.path.join(d, "id"), 0o755)
        else:
            os.symlink(shutil.which("id"), os.path.join(d, "id"))
        pm_bin = {"apt": "apt-get", "dnf": "dnf", "pacman": "pacman", "zypper": "zypper"}[pm]
        with open(os.path.join(d, pm_bin), "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\necho \"%s $*\" >> \"%s\"\n" % (pm_bin, self.shimlog))
            os.chmod(os.path.join(d, pm_bin), 0o755)
        with open(os.path.join(d, "sudo"), "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\necho \"sudo $*\" >> \"%s\"\nexec \"$@\"\n" % self.shimlog)
            os.chmod(os.path.join(d, "sudo"), 0o755)
        return d

    def shim_log(self):
        if not os.path.exists(self.shimlog):
            return ""
        with open(self.shimlog, encoding="utf-8") as fh:
            return fh.read()

    def test_sem_python_o_padrao_e_mostrar_o_comando_e_parar(self):
        """Without Python and without --install-deps: the script prints the command of
        the detected package manager and exits 1 — calling neither sudo nor the
        package manager (review point 6)."""
        d = self.shim_bin(pm="apt")
        rc, out = self.setup(env={"PATH": d})
        self.assertEqual(rc, 1, out)
        self.assertIn("MISSING: Python 3.10+", out)
        self.assertIn("sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip", out)
        self.assertIn("--install-deps", out)
        self.assertEqual(self.shim_log(), "", "default mode must not run sudo or the package manager")

    def test_hint_segue_o_gerenciador_detectado(self):
        """On a Fedora machine (dnf, no apt-get) the hint shows the dnf command,
        not the apt one (review point 2)."""
        d = self.shim_bin(pm="dnf")
        rc, out = self.setup(env={"PATH": d})
        self.assertEqual(rc, 1, out)
        self.assertIn("sudo dnf install -y python3 python3-pip", out)
        self.assertNotIn("apt-get install", out)

    def test_install_deps_roda_via_sudo_fora_de_root(self):
        """--install-deps as a non-root user: the package manager runs via sudo (review point 6)."""
        d = self.shim_bin(pm="apt")
        rc, out = self.setup(["--install-deps"], {"PATH": d})
        self.assertEqual(rc, 1, out)   # instala, mas continua sem python -> para com explicacao
        self.assertIn("still no Python 3.10+", out)
        log = self.shim_log()
        self.assertIn("sudo apt-get update", log)
        self.assertIn("sudo apt-get install -y python3 python3-venv python3-pip", log)

    def test_install_deps_como_root_chama_direto_sem_sudo(self):
        """--install-deps as root (containers, WSL): the package manager runs DIRECTLY,
        without sudo — the case that failed on a clean Ubuntu 24.04 (review point 1)."""
        d = self.shim_bin(pm="apt", root=True)
        rc, out = self.setup(["--install-deps"], {"PATH": d})
        self.assertEqual(rc, 1, out)
        log = self.shim_log()
        self.assertNotIn("sudo", log)
        self.assertIn("apt-get update", log)
        self.assertIn("apt-get install -y python3 python3-venv python3-pip", log)

    def test_root_sem_sudo_o_hint_nao_tem_prefixo(self):
        """As root (id -u = 0) the printed command has no sudo prefix: it is the
        command the root user should run."""
        d = self.shim_bin(pm="apt", root=True)
        rc, out = self.setup(env={"PATH": d})
        self.assertEqual(rc, 1, out)
        self.assertIn("  apt-get update && apt-get install -y python3 python3-venv python3-pip", out)
        self.assertNotIn("sudo apt-get", out)



    def test_venv_main_fora_do_projeto_nada_e_removido(self):
        fora = tempfile.mkdtemp(prefix="mp-fora-")
        marcador = os.path.join(fora, "marcador.txt")
        with open(marcador, "w", encoding="utf-8") as fh:
            fh.write("intact")
        rc, out = self.setup(["--no-sudo"], {"VENV_MAIN": fora})
        self.assertEqual(rc, 2, out)
        self.assertIn("unsafe", out)
        self.assertIn("Nothing was removed", out)
        self.assertTrue(os.path.exists(marcador), "nothing may be deleted outside the project")
        shutil.rmtree(fora, ignore_errors=True)

    def test_venv_main_com_dotdot_e_recusado(self):
        rc, out = self.setup(["--no-sudo"], {"VENV_MAIN": "../fora"})
        self.assertEqual(rc, 2, out)
        self.assertIn("unsafe", out)

    def test_venv_main_raiz_do_projeto_e_recusada(self):
        rc, out = self.setup(["--no-sudo"], {"VENV_MAIN": "."})
        self.assertEqual(rc, 2, out)
        self.assertIn("unsafe", out)

    def test_venv_kv_caminho_da_home_e_recusado(self):
        rc, out = self.setup(["--no-sudo"], {"HOME": self.tree, "VENV_KV": self.tree + "/.venv314"})
        self.assertEqual(rc, 2, out)
        self.assertIn("unsafe", out)

    def test_venv_main_symlink_e_recusado(self):
        alvo = os.path.join(self.tree, "alvo")
        os.makedirs(alvo)
        os.symlink(alvo, os.path.join(self.tree, ".venv"))
        rc, out = self.setup(["--no-sudo"], {"VENV_MAIN": ".venv"})
        self.assertEqual(rc, 2, out)
        self.assertIn("unsafe", out)

    def test_nao_remove_pasta_que_nao_e_venv(self):
        binp = os.path.join(self.tree, ".venv", "bin")
        os.makedirs(binp)
        python_sem_pip(os.path.join(binp, "python"))
        marcador = os.path.join(self.tree, ".venv", "marcador.txt")
        with open(marcador, "w", encoding="utf-8") as fh:
            fh.write("intact")
        rc, out = self.setup(["--no-sudo", "--no-kv"])
        self.assertEqual(rc, 2, out)
        self.assertIn("not a venv", out)
        self.assertTrue(os.path.exists(marcador), "without pyvenv.cfg nothing is removed")

    def test_venv_em_subpasta_inexistente_e_aceito(self):
        """A legitimate path inside the project whose parent does not exist yet
        (VENV_MAIN=venvs/kv314): the normal case of someone choosing where to
        create the venv, and it must not be read as 'unsafe'."""
        rc, out = self.setup(env={"VENV_MAIN": "venvs/kv314"})
        self.assertNotIn("unsafe", out)
        self.assertNotEqual(rc, 2, out)

    def test_pai_symlink_para_fora_nada_e_removido(self):
        """shortcut -> a folder outside; shortcut/.venv is a legitimate venv out there.
        rm -rf would follow the symlink: the validation must catch the ancestor,
        not just the leaf."""
        fora = tempfile.mkdtemp(prefix="mp-fora-")
        ext = os.path.join(fora, ".venv")
        os.makedirs(os.path.join(ext, "bin"))
        with open(os.path.join(ext, "pyvenv.cfg"), "w", encoding="utf-8") as fh:
            fh.write("home = %s\n" % os.path.dirname(sys.executable))
        python_sem_pip(os.path.join(ext, "bin", "python"))
        marcador = os.path.join(ext, "marcador.txt")
        with open(marcador, "w", encoding="utf-8") as fh:
            fh.write("intact")
        os.symlink(fora, os.path.join(self.tree, "atalho"))
        rc, out = self.setup(["--no-sudo", "--no-kv"], {"VENV_MAIN": "atalho/.venv"})
        self.assertEqual(rc, 2, out)
        self.assertIn("unsafe", out)
        self.assertIn("Nothing was removed", out)
        self.assertTrue(os.path.isdir(ext), "the external venv must not be removed")
        self.assertTrue(os.path.exists(marcador), "the external content must not be touched")
        shutil.rmtree(fora, ignore_errors=True)

    def test_ancestral_symlink_em_caminho_absoluto_tambem_e_recusado(self):
        fora = tempfile.mkdtemp(prefix="mp-fora-")
        ext = os.path.join(fora, ".venv")
        os.makedirs(os.path.join(ext, "bin"))
        with open(os.path.join(ext, "pyvenv.cfg"), "w", encoding="utf-8") as fh:
            fh.write("home = x\n")
        python_sem_pip(os.path.join(ext, "bin", "python"))
        marcador = os.path.join(ext, "marcador.txt")
        with open(marcador, "w", encoding="utf-8") as fh:
            fh.write("intact")
        os.symlink(fora, os.path.join(self.tree, "atalho"))
        rc, out = self.setup(["--no-sudo", "--no-kv"],
                             {"VENV_MAIN": os.path.join(self.tree, "atalho", ".venv")})
        self.assertEqual(rc, 2, out)
        self.assertIn("unsafe", out)
        self.assertTrue(os.path.exists(marcador), "the external content must not be touched")
        shutil.rmtree(fora, ignore_errors=True)

    def test_venv_em_subpasta_do_projeto_continua_aceito(self):
        """The validation must not restrict beyond what the interface documents: a
        legitimate VENV_MAIN in a subfolder must pass (and the setup carries on)."""
        os.makedirs(os.path.join(self.tree, "sub"))
        self.real_venv(os.path.join("sub", ".venv"))
        rc, out = self.setup(["--no-sudo", "--no-kv"], {"VENV_MAIN": "sub/.venv"})
        self.assertNotEqual(rc, 2, out)
        self.assertNotIn("unsafe", out)
        self.assertEqual(rc, 0, out)
    def test_sem_home_definido_o_script_nao_quebra(self):
        """HOME may be missing (cron, systemd, env -i). With `set -u` the script must
        not die with 'parameter not set', nor exit with the 2 of the safety rejection."""
        env = dict(os.environ)
        env.pop("HOME", None)
        p = subprocess.run(["./setup.sh", "--check"], cwd=self.tree, env=env,
                           capture_output=True, text=True, timeout=300)
        out = p.stdout + p.stderr
        self.assertNotIn("parameter not set", out)
        self.assertNotEqual(p.returncode, 2, out)
        self.assertIn("MISSING", out)
