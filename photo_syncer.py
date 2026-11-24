#!/usr/bin/env python3
"""
Incremental Google Photos uploader for Windows.

First run:
  - asks for explicit BASELINE confirmation;
  - indexes files already present in the source folders without uploading them.

Later runs:
  - finds new or changed image/video files;
  - shows the complete upload list and asks for YES;
  - uploads only that list to the Google Photos general library via rclone.

Set source_dirs and rclone_exe in photo_syncer.ini, and edit REMOTE below.
"""

import configparser
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "photo_syncer.ini"


def load_paths():
    config = configparser.ConfigParser(interpolation=None)
    with CONFIG_PATH.open(encoding="utf-8") as config_file:
        config.read_file(config_file)
    source_dirs = [
        Path(line.strip())
        for line in config.get("paths", "source_dirs").splitlines()
        if line.strip()
    ]
    rclone_exe = config.get("paths", "rclone_exe").strip()
    if not source_dirs or not rclone_exe:
        raise ValueError(f"source_dirs and rclone_exe must not be empty in {CONFIG_PATH}")
    return source_dirs, rclone_exe


# ---------------- USER SETTINGS ----------------

REMOTE = "GooglePhoto"                       # Name from: rclone listremotes
TRANSFERS = "4"

# Common image/video formats. Add extensions if needed.
MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
    ".heic", ".heif", ".tif", ".tiff",
    ".mp4", ".mov", ".avi", ".mpg", ".mpeg", ".m4v",
    ".3gp", ".wmv", ".mkv", ".webm", ".mts", ".m2ts",
}

# ------------------------------------------------

DB_PATH = BASE_DIR / "google_photos_index.sqlite3"
LOG_PATH = BASE_DIR / "rclone_upload.log"


def human_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def connect_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""
        CREATE TABLE IF NOT EXISTS files (
            relpath TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            sha256 TEXT NOT NULL
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256)")
    db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    db.commit()
    return db


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_media_files(roots: list[Path]):
    def raise_walk_error(error):
        raise error

    for root in roots:
        for directory, _, filenames in os.walk(root, onerror=raise_walk_error):
            for name in filenames:
                path = Path(directory) / name
                if path.suffix.lower() not in MEDIA_EXTENSIONS:
                    continue
                st = path.stat()
                if stat.S_ISREG(st.st_mode):
                    yield path, st


def root_key(root: Path) -> str:
    return str(root).casefold()


def file_key(root: Path, rel: str) -> str:
    return json.dumps([root_key(root), rel.casefold()], ensure_ascii=False)


def check_settings(db, roots: list[Path]):
    current = sorted(root_key(root) for root in roots)
    stored = db.execute(
        "SELECT value FROM settings WHERE key='source_dirs'"
    ).fetchone()
    legacy = db.execute(
        "SELECT value FROM settings WHERE key='source_dir'"
    ).fetchone()

    if stored:
        if json.loads(stored[0]) != current:
            raise ValueError("The configured source folders differ from the folders bound to the index.")
        return

    if legacy:
        if len(current) != 1 or legacy[0] != current[0]:
            raise ValueError("The existing index belongs to a different source folder.")
        rows = db.execute("SELECT relpath, size, mtime_ns, sha256 FROM files").fetchall()
        with db:
            db.execute("DELETE FROM files")
            db.executemany(
                "INSERT INTO files(relpath, size, mtime_ns, sha256) VALUES (?, ?, ?, ?)",
                [(file_key(roots[0], row[0]), *row[1:]) for row in rows],
            )
            db.execute("DELETE FROM settings WHERE key='source_dir'")
            db.execute(
                "INSERT INTO settings(key, value) VALUES('source_dirs', ?)",
                (json.dumps(current, ensure_ascii=False),),
            )
            if rows:
                db.execute("INSERT INTO settings(key, value) VALUES('baseline_complete', '1')")
        return

    if db.execute("SELECT 1 FROM files LIMIT 1").fetchone():
        raise ValueError("The existing index has files but no source folder settings.")


def checked_hash(path: Path, st) -> str:
    digest = sha256_file(path)
    current = path.stat()
    if (current.st_size, current.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
        raise ValueError(f"File changed while being read: {path}")
    return digest


def make_initial_baseline(db, roots: list[Path]):
    files = [(root, path, st) for root in roots for path, st in iter_media_files([root])]
    print(f"\nNo existing baseline was found. Found {len(files)} supported files in:")
    for root in roots:
        print(f"  {root}")
    print("\nThis will NOT upload anything. Every current file will be marked")
    print("as already present in Google Photos. Only confirm if that is true.")
    if input("\nTo confirm, type BASELINE: ").strip() != "BASELINE":
        print("Cancelled. No baseline was created.")
        return

    with db:
        for index, (root, path, st) in enumerate(files, 1):
            digest = checked_hash(path, st)
            rel = path.relative_to(root).as_posix()
            db.execute(
                "INSERT OR REPLACE INTO files(relpath, size, mtime_ns, sha256) VALUES (?, ?, ?, ?)",
                (file_key(root, rel), st.st_size, st.st_mtime_ns, digest),
            )
            if index % 100 == 0 or index == len(files):
                print(f"Indexed {index}/{len(files)} files...", end="\r", flush=True)
        db.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES('source_dirs', ?)",
            (json.dumps(sorted(root_key(root) for root in roots), ensure_ascii=False),),
        )
        db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('baseline_complete', '1')")
    print(f"\nBaseline complete. Indexed {len(files)} files. Nothing was uploaded.")


def find_candidates(db, roots: list[Path]):
    candidates = []
    known_count = 0
    files = [(root, path, st) for root in roots for path, st in iter_media_files([root])]

    with db:
        for index, (root, path, st) in enumerate(files, 1):
            rel = path.relative_to(root).as_posix()
            key = file_key(root, rel)
            old = db.execute(
                "SELECT size, mtime_ns FROM files WHERE relpath=?", (key,)
            ).fetchone()
            if old and old == (st.st_size, st.st_mtime_ns):
                known_count += 1
                continue

            digest = checked_hash(path, st)
            same_content = db.execute(
                "SELECT 1 FROM files WHERE sha256=? LIMIT 1", (digest,)
            ).fetchone()
            if same_content:
                db.execute(
                    "INSERT OR REPLACE INTO files(relpath, size, mtime_ns, sha256) VALUES (?, ?, ?, ?)",
                    (key, st.st_size, st.st_mtime_ns, digest),
                )
                known_count += 1
            else:
                candidates.append({
                    "root": root, "path": path, "rel": rel, "key": key,
                    "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                    "sha256": digest, "changed": old is not None,
                })
            if index % 100 == 0 or index == len(files):
                print(f"Scanning {index}/{len(files)} files...", end="\r", flush=True)
    print()
    return candidates, known_count, len(files)


def upload_candidates(db, roots: list[Path], candidates, rclone_exe: str):
    print("\nFiles proposed for upload:")
    for number, item in enumerate(candidates, 1):
        tag = " [CHANGED FILE]" if item["changed"] else ""
        print(f"{number:4}. {item['path']}  ({human_size(item['size'])}){tag}")
    print(f"\nFiles: {len(candidates)}")
    print(f"Total size: {human_size(sum(item['size'] for item in candidates))}")
    print(f"Destination: {REMOTE}:upload  (Google Photos main library)")
    print("\nNothing has been uploaded yet.")
    if input("\nUpload this list? Type YES: ").strip() != "YES":
        print("Cancelled. No files were uploaded.")
        return

    for root in roots:
        group = [item for item in candidates if item["root"] == root]
        if not group:
            continue
        for item in group:
            st = item["path"].stat()
            if (st.st_size, st.st_mtime_ns) != (item["size"], item["mtime_ns"]):
                raise ValueError(f"File changed after the upload list was prepared: {item['path']}")
            if checked_hash(item["path"], st) != item["sha256"]:
                raise ValueError(f"File changed after the upload list was prepared: {item['path']}")

        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", suffix=".txt", prefix="rclone_files_", delete=False) as file_list:
                temp_name = file_list.name
                for item in group:
                    file_list.write(item["rel"].encode("utf-8") + b"\0")
            command = [
                rclone_exe, "copy", str(root), f"{REMOTE}:upload",
                "--files-from0", temp_name, "--transfers", TRANSFERS,
                "--gphotos-batch-mode", "sync", "--log-file", str(LOG_PATH),
                "--log-level", "INFO", "--progress",
            ]
            print(f"\nStarting rclone for {root}...\n")
            result = subprocess.run(command, check=False)
            if result.returncode != 0:
                print(f"Rclone returned error code {result.returncode}.")
                print("This folder's files were not indexed; check the log before retrying.")
                print("Some files may already have uploaded and could be submitted again.")
                raise SystemExit(result.returncode)
            with db:
                db.executemany(
                    "INSERT OR REPLACE INTO files(relpath, size, mtime_ns, sha256) VALUES (?, ?, ?, ?)",
                    [(item["key"], item["size"], item["mtime_ns"], item["sha256"]) for item in group],
                )
            print(f"Uploaded files from {root} were added to the index.")
        finally:
            if temp_name is not None:
                os.remove(temp_name)


def main():
    source_dirs, rclone_exe = load_paths()
    roots = [root.expanduser().resolve() for root in source_dirs]
    for root in roots:
        if not root.is_dir():
            raise ValueError(f"Source folder does not exist or is not a directory: {root}")
    for index, root in enumerate(roots):
        for other in roots[:index]:
            if root == other or root in other.parents or other in root.parents:
                raise ValueError(f"Source folders must not overlap: {root} and {other}")
    if not shutil.which(rclone_exe):
        raise ValueError(f"rclone executable not found: {rclone_exe}")

    db = connect_db()
    try:
        check_settings(db, roots)
        baseline = db.execute(
            "SELECT value FROM settings WHERE key='baseline_complete'"
        ).fetchone()
        if not baseline:
            make_initial_baseline(db, roots)
            return
        candidates, known_count, total = find_candidates(db, roots)
        print(f"Checked {total} files; {known_count} already known; {len(candidates)} new/changed.")
        if candidates:
            upload_candidates(db, roots, candidates, rclone_exe)
        else:
            print("No new files to upload.")
    finally:
        db.close()


if __name__ == "__main__":
    try:
        main()
    except (OSError, sqlite3.Error, configparser.Error, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)