# Google Photos Syncer

This script uploads photos and videos to Google Photos using rclone.

The script operates in one direction, from local folders to Google Photos.
Deleting local files does not delete them from Google Photos. Uploads require
confirmation; there is no automatic schedule.

## Requirements

- Windows and Python 3.9 or later.
- rclone configured to work with Google Photos.

## Configuration

1. Open `photo_syncer.ini` next to the script.
2. List the photo and video folders under `source_dirs`, one per indented line.
   Subfolders are also searched. The folders must exist and must not be
   duplicated or nested inside one another.
3. Set `rclone_exe` to the full path to `rclone.exe`, or to `rclone` if it is
   available on PATH.

Example:

```ini
[paths]
source_dirs =
    D:\Photos
    D:\Videos
rclone_exe = C:\rclone\rclone.exe
```

To view or change the rclone configuration, run in PowerShell:

```powershell
& "C:\rclone\rclone.exe" config
```

Replace the path in the command with your own. Create a Google Photos remote
and authorize it by following the rclone prompts. The remote name must match
`REMOTE` in `photo_syncer.py` (currently `GooglePhoto`). To list remote names:

```powershell
& "C:\rclone\rclone.exe" listremotes
```

Create your own OAuth client ID using the
[rclone instructions](https://rclone.org/googlephotos/#making-your-own-client-id):
the shared rclone client ID is being retired. The Google Photos API has
[limitations](https://rclone.org/googlephotos/#limitations): uploaded files
count against your account storage, and downloading originals through the API
is restricted.

Configuring a remote does not start an upload. Listing local folders does not
create corresponding albums in Google Photos.

## Running

Double-click `start_photo_syncer.bat`. When it finishes, press any key to close
the window.

Alternatively, open PowerShell, change to the script folder, and run:

```powershell
cd H:\GooglePhotosSyncer
python .\photo_syncer.py
```

Use your own folder path if the script is located elsewhere.

### First run

The script displays the number of media files found and asks you to enter
`BASELINE`. Once confirmed, it records them in the local index **without
uploading them**. Confirm only if all current files are already in Google
Photos: the script does not check the cloud. Otherwise, existing files will
not be uploaded. Declining confirmation cancels creation of the index.

### Subsequent runs

The script finds new and changed photos and videos, displays the complete list
and total size, and asks you to enter `YES`. Nothing is uploaded without this
confirmation. Files whose content is already in the index are not uploaded
again. Size and modification time are used to quickly identify unchanged
files; SHA-256 is calculated for the others.
If file content changes without a change in size or modification time, the
script will not detect the change.

Files are uploaded one folder at a time to `GooglePhoto:upload` (or to the remote
specified in `REMOTE`). After each folder succeeds, its files are recorded in
the index. If an error occurs, check `rclone_upload.log`: some files in the
current folder may have been uploaded but not indexed, and will be submitted
again on retry.

The index `google_photos_index.sqlite3` and log `rclone_upload.log` are stored
next to the script. Keep the index: it records what has already been accounted
for. After the initial baseline, the script refuses to work with a different
set of source folders so that existing files are not mistaken for new ones.
If you need a different set, use a separate copy of the script with a separate
index and carefully confirm its initial baseline.

A missing INI file or parameters, an empty folder list, or an inaccessible
folder or rclone executable will cause an error; check the configuration and
try again.
