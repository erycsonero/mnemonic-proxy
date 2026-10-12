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


if __name__ == "__main__":
    unittest.main()
