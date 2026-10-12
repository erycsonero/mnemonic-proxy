"""tools/check_kv_archive.py: the audit must be strictly read-only and must fail
(non-zero) when the archive is corrupt.

Review points covered:
  5. a corrupted block was not detected: the audit exited 0 and prescribed the
     audit to run verify_all() and exit non-zero on any error;
  6. not fully read-only: opening a missing archive created its directories, and
     --restore-check wrote full-size copies to a fixed folder under /tmp.

The tests build a real archive with the same helper the proxy tests use
(make_session from tests.test_kvarchive), so no Python >= 3.14 engine is needed
beyond compression.zstd.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO

from ctxproxy import kvarchive as K
from tests.test_kvarchive import make_session

if K.zstd is None:  # kv_archive needs Python >= 3.14 (compression.zstd)
    raise unittest.SkipTest("kv_archive needs Python >= 3.14 (compression.zstd)")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "check_kv_archive.py")


def load_tool():
    """Import tools/check_kv_archive.py as a module (it is a script, not a package)."""
    spec = importlib.util.spec_from_file_location("check_kv_archive", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_archive(root, names=("c1-seg0-A.bin",), size=8000):
    """A real archive with the given files, built with the proxy test helper."""
    arc = K.KvArchive(root)
    tmp = tempfile.mkdtemp(prefix="kva-src-")
    for i, n in enumerate(names):
        p = os.path.join(tmp, n)
        with open(p, "wb") as f:
            f.write(make_session(size, i + 1))
        arc.archive(p, n)
    return arc


class ReadOnlyTests(unittest.TestCase):
    """--check must not create, remove or modify anything (review point 6)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.mod = load_tool()

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_archive_is_not_created(self):
        """Pointing --root at a non-existent folder must NOT create it: the audit is
        read-only, so a missing archive is reported, not materialised."""
        missing = os.path.join(self.d, "no-such-archive")
        rc = self.run_tool(["--root", missing, "--slot-dir", self.d])
        self.assertFalse(os.path.exists(missing),
                         "the audit must not create the archive directories")

    def test_existing_archive_is_untouched(self):
        """An existing archive is left byte-for-byte identical (no block/manifest
        written, no file removed)."""
        root = os.path.join(self.d, "kv-archive")
        build_archive(root)
        before = tree_snapshot(root)
        self.run_tool(["--root", root, "--slot-dir", self.d])
        self.assertEqual(tree_snapshot(root), before,
                         "the audit must not modify the archive")

    def test_restore_check_uses_a_private_temp_dir(self):
        """--restore-check must restore into a private temp dir (tempfile.mkdtemp),
        not a fixed folder under /tmp, and must leave nothing behind."""
        root = os.path.join(self.d, "kv-archive")
        build_archive(root)
        fixed = "/tmp/kva-restore-check"
        before_fixed = tree_snapshot(fixed)
        rc = self.run_tool(["--root", root, "--slot-dir", self.d, "--restore-check"])
        self.assertEqual(tree_snapshot(fixed), before_fixed,
                         "restore-check must not write to the fixed /tmp/kva-restore-check")

    # ---- helper used by the tests -------------------------------------------
    def run_tool(self, args):
        out = StringIO()
        with redirect_stdout(out):
            rc = self.mod.main(args)
        self.out = out.getvalue()
        return rc


def tree_snapshot(root):
    """{relpath: (size, mtime_ns)} for every file under root (empty if missing)."""
    snap = {}
    if not os.path.isdir(root):
        return snap
    for base, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(base, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            snap[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns)
    return snap


class DetectionTests(unittest.TestCase):
    """A corrupt archive must be detected and must make the audit exit non-zero
    (review point 5)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.mod = load_tool()

    def tearDown(self):
        self.tmp.cleanup()

    def corrupt_one_block(self, root):
        """Flip a byte inside one stored block, keeping every file in place."""
        arc = K.KvArchive(root)
        name = arc.names()[0]
        h = arc.manifest(name)["blocks"][0][0]
        p = arc.block_path(h)
        with open(p, "r+b") as f:
            f.seek(max(0, os.path.getsize(p) - 4))
            b = f.read(1)
            f.seek(-1, os.SEEK_CUR)
            f.write(bytes([b[0] ^ 0xFF]))

    def run_tool(self, args):
        out = StringIO()
        with redirect_stdout(out):
            rc = self.mod.main(args)
        self.out = out.getvalue()
        return rc

    def test_corrupt_block_is_detected(self):
        """The plain audit (no --restore-check) must call verify_all and exit non-zero
        when a block is corrupt, instead of reporting everything fine."""
        root = os.path.join(self.d, "kv-archive")
        build_archive(root)
        self.corrupt_one_block(root)
        rc = self.run_tool(["--root", root, "--slot-dir", self.d])
        self.assertNotEqual(rc, 0, self.out)
        self.assertIn("CORRUPT", self.out)

    def test_healthy_archive_exits_zero(self):
        root = os.path.join(self.d, "kv-archive")
        build_archive(root)
        rc = self.run_tool(["--root", root, "--slot-dir", self.d])
        self.assertEqual(rc, 0, self.out)

    def test_restore_check_on_corrupt_exits_non_zero(self):
        """--restore-check on a corrupt archive already detected the error but exited
        0; now it must exit non-zero."""
        root = os.path.join(self.d, "kv-archive")
        build_archive(root)
        self.corrupt_one_block(root)
        rc = self.run_tool(["--root", root, "--slot-dir", self.d, "--restore-check"])
        self.assertNotEqual(rc, 0, self.out)

    def test_restore_check_on_healthy_exits_zero(self):
        root = os.path.join(self.d, "kv-archive")
        build_archive(root)
        rc = self.run_tool(["--root", root, "--slot-dir", self.d, "--restore-check"])
        self.assertEqual(rc, 0, self.out)

    def test_missing_slot_dir_returns_usage_error(self):
        """No slot_dir anywhere: exit 2 (usage/config error), not a crash."""
        rc = self.run_tool(["--config", os.path.join(self.d, "nope.json")])
        self.assertEqual(rc, 2, self.out)
