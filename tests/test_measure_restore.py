"""tools/measure_restore.py: the pure logic that turns the journal into a number.

The tool runs a live engine, so the harness here tests the parts that do not touch
it: journal filtering by start time (review point 2), the speed-up computed from
prompt_ms / restore_ms instead of an extrapolated wall time (review point 1),
own-file listing (review point 4) and the CLI guards (review points 3 and 4, via
main with --allow-shared-engine and a missing engine).

The numbers reused below are the ones measured live on a Strata engine
(prompt 31968, read 31968 -> control 30575 ms; restore 2355 + 2357 ms -> 6.5x).
"""
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "measure_restore.py")


def load_tool():
    spec = importlib.util.spec_from_file_location("measure_restore", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class JournalFilterTests(unittest.TestCase):
    """Review point 2: a second run must not reuse events from the first run."""

    def setUp(self):
        self.mod = load_tool()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "journal.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rows):
        with open(self.path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_since_filters_out_older_events(self):
        self.write([
            {"ts": 100.0, "event": "autorestore", "file": "old.bin"},
            {"ts": 200.0, "event": "request", "prompt_tokens": 10},
            {"ts": 300.0, "event": "autorestore", "file": "new.bin"},
        ])
        got = self.mod.journal_events(self.path, since=250.0)
        self.assertEqual([e["event"] for e in got], ["autorestore"])
        self.assertEqual(got[0]["file"], "new.bin")

    def test_without_since_returns_everything(self):
        self.write([{"ts": 1.0, "event": "a"}, {"ts": 2.0, "event": "b"}])
        self.assertEqual(len(self.mod.journal_events(self.path)), 2)

    def test_missing_file_is_empty(self):
        self.assertEqual(self.mod.journal_events(os.path.join(self.tmp.name, "nope")), [])

    def test_broken_lines_are_skipped(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"ts": 1.0, "event": "ok"}\n')
            f.write("{ this is not json\n")
            f.write('{"ts": 2.0, "event": "ok2"}\n')
        self.assertEqual([e["event"] for e in self.mod.journal_events(self.path)], ["ok", "ok2"])


class SlotFileTests(unittest.TestCase):
    """Review point 4: the tool cleans up only the files it created itself."""

    def setUp(self):
        self.mod = load_tool()
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_lists_only_files_sorted(self):
        for n in ("b.bin", "a.bin"):
            open(os.path.join(self.d, n), "wb").close()
        os.makedirs(os.path.join(self.d, "sub"))
        self.assertEqual(self.mod.list_slot_files(self.d), ["a.bin", "b.bin"])

    def test_missing_dir_is_empty(self):
        self.assertEqual(self.mod.list_slot_files(os.path.join(self.d, "nope")), [])
        self.assertEqual(self.mod.list_slot_files(""), [])


class ReportTests(unittest.TestCase):
    """Review point 1: the speed-up comes from prompt_ms / restore_ms, not from an
    extrapolated wall time. The live numbers from the Strata run must give ~6.5x."""

    def setUp(self):
        self.mod = load_tool()

    def report(self, results):
        out = io.StringIO()
        with redirect_stdout(out):
            self.mod.print_report(results)
        return out.getvalue()

    def test_speedup_uses_journal_timings(self):
        results = {
            "control": {"request": {"prompt_tokens": 31968, "reused": 0, "prompt_read": 31968,
                                    "prompt_ms": 30575.1}, "raw_request_ms": 32079},
            "restore": {"request": {"prompt_tokens": 31968, "reused": 31425, "prompt_read": 543,
                                    "prompt_ms": 2357.0},
                        "autorestore": {"file": "x-seg0-auto-y.bin", "tokens": 31430,
                                        "restore_ms": 2355.1}, "raw_request_ms": 5844},
        }
        out = self.report(results)
        self.assertIn("6.5x", out)
        self.assertIn("4712 ms", out)      # restore_ms + prompt_ms of the new tokens
        self.assertIn("30575 ms", out)

    def test_restore_ms_falls_back_to_ms(self):
        results = {
            "control": {"request": {"prompt_ms": 1000.0}},
            "restore": {"request": {"prompt_ms": 100.0},
                        "autorestore": {"tokens": 5, "ms": 300}},
        }
        out = self.report(results)
        self.assertIn("speed-up (journal-based):    2.5x", out)   # 1000 / (300 + 100)

    def test_without_timings_it_says_so(self):
        out = self.report({"control": {"request": {}}, "restore": {"request": {}}})
        self.assertIn("not enough data for a speed-up", out)
        self.assertNotIn("speed-up (journal-based):", out)


class CliGuardTests(unittest.TestCase):
    """Review points 3 and 4 via main(): no --allow-shared-engine refuses (exit 2);
    with it but no engine, the tool stops with a clear error (exit 2)."""

    def setUp(self):
        self.mod = load_tool()
        self.tmp = tempfile.TemporaryDirectory()
        self.slot = os.path.join(self.tmp.name, "sessions")
        os.makedirs(self.slot)

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, args):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.mod.main(args)
        return rc, out.getvalue()

    def test_refuses_without_allow_shared_engine(self):
        rc, out = self.run_tool(["--slot-dir", self.slot, "--data", os.path.join(self.tmp.name, "d"),
                                 "--upstream", "http://127.0.0.1:1"])
        self.assertEqual(rc, 2, out)
        self.assertIn("REFUSING", out)
        self.assertIn("--allow-shared-engine", out)

    def test_missing_slot_dir_returns_usage_error(self):
        """No --slot-dir and no config.json in the repo root: exit 2 with a clear
        message, before touching the engine."""
        cfg = os.path.join(ROOT, "config.json")
        saved = None
        if os.path.exists(cfg):                 # keep the repo clean: this tool reads it
            with open(cfg, "rb") as f:
                saved = f.read()
            os.remove(cfg)
        try:
            rc, out = self.run_tool(["--data", os.path.join(self.tmp.name, "d"),
                                     "--allow-shared-engine"])
        finally:
            if saved is not None:
                with open(cfg, "wb") as f:
                    f.write(saved)
        self.assertEqual(rc, 2, out)
        self.assertIn("MISSING: --slot-dir", out)

    def test_dry_run_returns_zero(self):
        rc, out = self.run_tool(["--slot-dir", self.slot, "--dry-run", "--allow-shared-engine"])
        self.assertEqual(rc, 0, out)
        self.assertIn("=== plan ===", out)

    def test_engine_down_returns_usage_error(self):
        rc, out = self.run_tool(["--slot-dir", self.slot, "--allow-shared-engine",
                                 "--data", os.path.join(self.tmp.name, "d"),
                                 "--upstream", "http://127.0.0.1:1"])
        self.assertEqual(rc, 2, out)
        self.assertIn("engine does not answer", out)


class DataDirGuardTests(unittest.TestCase):
    """Blocking review point: an existing --data without the tool's marker must never be
    removed. A typo like "--data data/" points at the proxy's real data dir, the one thing
    the proxy promises never to lose."""

    def setUp(self):
        self.mod = load_tool()
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_existing_dir_without_marker_is_refused_and_kept(self):
        victim = os.path.join(self.d, "data")          # e.g. the proxy's real data dir
        os.makedirs(os.path.join(victim, "sessions"))
        with open(os.path.join(victim, "journal.jsonl"), "w") as f:
            f.write('{"event": "request"}\n')
        err = self.mod.prepare_data_dir(victim)
        self.assertIsNotNone(err, "an existing dir without the marker must be refused")
        self.assertTrue(os.path.exists(os.path.join(victim, "journal.jsonl")),
                        "nothing may be removed from a dir we did not create")

    def test_dir_with_marker_is_cleaned_and_reused(self):
        mine = os.path.join(self.d, "data")
        os.makedirs(mine)
        with open(os.path.join(mine, self.mod.DATA_MARKER), "w") as f:
            f.write("")
        with open(os.path.join(mine, "old.txt"), "w") as f:
            f.write("old")
        err = self.mod.prepare_data_dir(mine)
        self.assertIsNone(err)
        self.assertFalse(os.path.exists(os.path.join(mine, "old.txt")))
        self.assertTrue(os.path.isfile(os.path.join(mine, self.mod.DATA_MARKER)))

    def test_fresh_dir_is_created_with_marker(self):
        fresh = os.path.join(self.d, "new")
        self.assertIsNone(self.mod.prepare_data_dir(fresh))
        self.assertTrue(os.path.isfile(os.path.join(fresh, self.mod.DATA_MARKER)))

    def test_main_refuses_existing_dir_without_marker(self):
        """End to end through main(): exit 2 and the dir is left untouched."""
        victim = os.path.join(self.d, "precious")
        os.makedirs(victim)
        with open(os.path.join(victim, "keep.txt"), "w") as f:
            f.write("important")
        slot = os.path.join(self.d, "slot")
        os.makedirs(slot)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = self.mod.main(["--slot-dir", slot, "--data", victim,
                                "--allow-shared-engine", "--upstream", "http://127.0.0.1:1"])
        self.assertEqual(rc, 2, out.getvalue())
        self.assertIn("REFUSING", out.getvalue())
        self.assertTrue(os.path.exists(os.path.join(victim, "keep.txt")),
                        "the existing data dir must not be removed")


class SameRequestTests(unittest.TestCase):
    """The default method sends the SAME request twice. These tests use monkeypatch
    so nothing touches a real engine or proxy."""

    def setUp(self):
        self.mod = load_tool()
        self.tmp = tempfile.TemporaryDirectory()
        self.slot = os.path.join(self.tmp.name, "sessions")
        os.makedirs(self.slot)

    def tearDown(self):
        self.tmp.cleanup()

    def fake_args(self, **kw):
        a = type("A", (), {})()
        a.data = os.path.join(self.tmp.name, "data")
        a.upstream = "http://x"
        a.port = 8196
        a.tokens = 100
        a.probe_tokens = 50
        a.idle = 0.0
        for k, v in kw.items():
            setattr(a, k, v)
        return a

    def test_measure_request_sends_the_messages_it_is_given(self):
        """The request is NOT rebuilt: measure_request sends exactly the list passed in,
        so both measurements use byte-identical messages."""
        mod = self.mod
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        seen = []
        mod.start_proxy = lambda *a, **k: (object(), "http://p", open(os.devnull, "w"))
        mod.stop_proxy = lambda *a, **k: None
        mod.write_cfg = lambda *a, **k: "/dev/null"
        mod.free_port = lambda p: p
        mod.journal_events = lambda *a, **k: [{"event": "request", "prompt_ms": 1.0}]
        def fake_chat(base, model, messages, mx, **kw):
            seen.append(list(messages))
            return 1.0, {}, "ok"
        mod.chat = fake_chat
        a = self.fake_args()
        out, rc = mod.measure_request(a, "m", msgs, False, self.slot, "bin", "", a.data, "control")
        self.assertEqual(rc, 0)
        self.assertEqual(seen[0], msgs, "the measured request must be the list it was given")

    def test_same_request_aborts_when_control_is_not_a_real_reread(self):
        """Guard B: if the control request has reused>0, the engine kept the prefix and
        the comparison would be against a cached prompt: abort with exit 1."""
        mod = self.mod
        state = {"calls": 0}
        mod.free_port = lambda p: p
        mod.write_cfg = lambda *a, **k: "/dev/null"
        mod.start_proxy = lambda *a, **k: (object(), "http://p", open(os.devnull, "w"))
        mod.stop_proxy = lambda *a, **k: None
        mod.list_slot_files = lambda d: []
        mod.prepare_a = lambda *a, **k: ([{"role": "user", "content": "u"}], {"event": "autosave"})
        mod._evict_via_proxy = lambda *a, **k: None
        def fake_measure(a, model, msgs_a, autorestore, slot_dir, proxy_bin, tok, data, tag):
            state["calls"] += 1
            return {"request": {"reused": 5, "prompt_ms": 10.0}, "autorestore": None}, 0
        mod.measure_request = fake_measure
        a = self.fake_args()
        out = io.StringIO()
        with redirect_stdout(out):
            results, rc = mod.run_same_request(a, "m", self.slot, "bin", "")
        self.assertEqual(rc, 1, out.getvalue())
        self.assertIn("did not re-read", out.getvalue())
        self.assertEqual(state["calls"], 1, "it must abort before the restore measurement")

    def test_same_request_aborts_without_autosave(self):
        """Guard A: without an autosave there is nothing to restore: abort with exit 1."""
        mod = self.mod
        mod.free_port = lambda p: p
        mod.write_cfg = lambda *a, **k: "/dev/null"
        mod.start_proxy = lambda *a, **k: (object(), "http://p", open(os.devnull, "w"))
        mod.stop_proxy = lambda *a, **k: None
        mod.list_slot_files = lambda d: []
        mod.prepare_a = lambda *a, **k: ([{"role": "user", "content": "u"}], None)
        a = self.fake_args()
        out = io.StringIO()
        with redirect_stdout(out):
            results, rc = mod.run_same_request(a, "m", self.slot, "bin", "")
        self.assertEqual(rc, 1, out.getvalue())
        self.assertIn("no autosave", out.getvalue())

    def test_same_request_happy_path_returns_both_sides(self):
        """With an autosave and a clean control, the two measurements are reported."""
        mod = self.mod
        seq = [
            ({"request": {"reused": 0, "prompt_ms": 30575.1}, "autorestore": None}, 0),      # control
            ({"request": {"reused": 31425, "prompt_ms": 2357.0},
              "autorestore": {"restore_ms": 2355.1, "tokens": 31430}}, 0),                     # restore
        ]
        it = iter(seq)
        mod.free_port = lambda p: p
        mod.write_cfg = lambda *a, **k: "/dev/null"
        mod.start_proxy = lambda *a, **k: (object(), "http://p", open(os.devnull, "w"))
        mod.stop_proxy = lambda *a, **k: None
        mod.list_slot_files = lambda d: []
        mod.prepare_a = lambda *a, **k: ([{"role": "user", "content": "u"}], {"event": "autosave"})
        mod._evict_via_proxy = lambda *a, **k: None
        mod.measure_request = lambda *a, **k: next(it)
        a = self.fake_args()
        out = io.StringIO()
        with redirect_stdout(out):
            results, rc = mod.run_same_request(a, "m", self.slot, "bin", "")
        self.assertEqual(rc, 0, out.getvalue())
        self.assertEqual(results["method"], "same-request")
        self.assertTrue(results["control"] and results["restore"])


if __name__ == "__main__":
    unittest.main()
