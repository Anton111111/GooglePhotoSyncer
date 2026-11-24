import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import photo_syncer as syncer


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.roots = [base / "photos", base / "videos"]
        for root in self.roots:
            root.mkdir()
        with patch.object(syncer, "DB_PATH", base / "index.sqlite3"):
            self.db = syncer.connect_db()
        self.addCleanup(self.db.close)

    def baseline(self):
        with patch("builtins.input", return_value="BASELINE"):
            syncer.make_initial_baseline(self.db, self.roots)

    def test_baseline_cancel_and_empty_collection(self):
        with patch("builtins.input", return_value="NO"):
            syncer.make_initial_baseline(self.db, self.roots)
        self.assertIsNone(self.db.execute(
            "SELECT value FROM settings WHERE key='baseline_complete'"
        ).fetchone())
        self.baseline()
        self.assertEqual(self.db.execute(
            "SELECT value FROM settings WHERE key='baseline_complete'"
        ).fetchone(), ("1",))
        syncer.check_settings(self.db, self.roots)
        with self.assertRaises(ValueError):
            syncer.check_settings(self.db, self.roots[:1])

    def test_baseline_failure_rolls_back(self):
        (self.roots[0] / "one.jpg").write_bytes(b"one")
        (self.roots[1] / "two.jpg").write_bytes(b"two")
        with patch("builtins.input", return_value="BASELINE"), \
             patch.object(syncer, "checked_hash", side_effect=["hash", OSError("read failed")]):
            with self.assertRaises(OSError):
                syncer.make_initial_baseline(self.db, self.roots)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)
        self.assertIsNone(self.db.execute(
            "SELECT value FROM settings WHERE key='baseline_complete'"
        ).fetchone())

    def test_multiple_roots_upload_and_partial_failure(self):
        for root, content in zip(self.roots, (b"old photo", b"old video")):
            (root / "same.jpg").write_bytes(content)
        self.baseline()
        for root, content in zip(self.roots, (b"new photo", b"new video")):
            (root / "new.jpg").write_bytes(content)
        candidates, known, total = syncer.find_candidates(self.db, self.roots)
        self.assertEqual((len(candidates), known, total), (2, 2, 4))

        calls = []

        def fake_run(command, check):
            file_list = Path(command[command.index("--files-from0") + 1])
            calls.append((Path(command[2]), file_list.read_bytes()))
            return subprocess.CompletedProcess(command, 0 if len(calls) == 1 else 1)

        with patch("builtins.input", return_value="YES"), \
             patch.object(syncer.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(SystemExit):
                syncer.upload_candidates(self.db, self.roots, candidates, "rclone")
        self.assertEqual(calls, [(self.roots[0], b"new.jpg\0"), (self.roots[1], b"new.jpg\0")])
        remaining, known, total = syncer.find_candidates(self.db, self.roots)
        self.assertEqual((len(remaining), known, total), (1, 3, 4))
        self.assertEqual(remaining[0]["root"], self.roots[1])

    def test_changed_file_after_confirmation_is_not_uploaded(self):
        self.baseline()
        path = self.roots[0] / "new.jpg"
        path.write_bytes(b"before")
        candidates, _, _ = syncer.find_candidates(self.db, self.roots)
        path.write_bytes(b"after")
        with patch("builtins.input", return_value="YES"), \
             patch.object(syncer.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                syncer.upload_candidates(self.db, self.roots, candidates, "rclone")
            run.assert_not_called()

    def test_duplicates_changed_files_and_cancelled_upload(self):
        original = self.roots[0] / "old.jpg"
        original.write_bytes(b"already indexed")
        self.baseline()
        (self.roots[1] / "copy.jpg").write_bytes(b"already indexed")
        original.write_bytes(b"changed content")
        candidates, known, total = syncer.find_candidates(self.db, self.roots)
        self.assertEqual((len(candidates), known, total), (1, 1, 2))
        self.assertTrue(candidates[0]["changed"])
        with patch("builtins.input", return_value="NO"), \
             patch.object(syncer.subprocess, "run") as run:
            syncer.upload_candidates(self.db, self.roots, candidates, "rclone")
            run.assert_not_called()
        remaining, _, _ = syncer.find_candidates(self.db, self.roots)
        self.assertEqual(len(remaining), 1)

    def test_load_multiple_paths_and_reject_empty(self):
        config = Path(self.temp.name) / "photo_syncer.ini"
        config.write_text(
            "[paths]\nsource_dirs =\n    D:\\100% photos\n    D:\\Videos\n"
            "rclone_exe = rclone\n", encoding="utf-8"
        )
        with patch.object(syncer, "CONFIG_PATH", config):
            roots, executable = syncer.load_paths()
            self.assertEqual([str(root) for root in roots], [r"D:\100% photos", r"D:\Videos"])
            self.assertEqual(executable, "rclone")
            config.write_text("[paths]\nsource_dirs =\nrclone_exe = rclone\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                syncer.load_paths()

    def test_migrate_single_root_index(self):
        root = self.roots[0]
        self.db.execute("INSERT INTO settings VALUES ('source_dir', ?)", (str(root).casefold(),))
        self.db.execute("INSERT INTO files VALUES ('old.jpg', 1, 2, 'hash')")
        self.db.commit()
        syncer.check_settings(self.db, [root])
        self.assertEqual(self.db.execute("SELECT relpath FROM files").fetchone()[0],
                         syncer.file_key(root, "old.jpg"))
        self.assertEqual(self.db.execute(
            "SELECT value FROM settings WHERE key='baseline_complete'"
        ).fetchone(), ("1",))


if __name__ == "__main__":
    unittest.main()
