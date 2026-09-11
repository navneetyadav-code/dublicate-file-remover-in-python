# Safe Duplicate Finder

A fast, lightweight Windows desktop app for finding duplicate files across selected folders. It is designed for large videos, movies, photos, phone backups, DCIM folders, and mixed file archives.

The app never deletes files automatically. When you choose files to remove, it moves them to the Windows Recycle Bin by default.

## Features

- Exact duplicate detection by content hash, even when filenames are different.
- Same-name conflict warnings when matching filenames have different content.
- Low RAM scanning with chunked reads for huge files.
- Background scan thread so the GUI stays responsive.
- SQLite hash cache at `%LOCALAPPDATA%\SafeDuplicateFinder\scan_cache.sqlite3`.
- Separate tabs for:
  - Exact Duplicates
  - Large Files
  - Videos
  - Images
  - Same Name Conflicts
- Progress, scan speed, bytes read, and recoverable space display.
- Manual keep-copy selection for duplicate groups.
- Open file location from the result list.
- Recycle Bin deletion using `send2trash`.

## Supported File Types

The scanner works with all file types. It also highlights common media extensions, including:

- Videos: `.mkv`, `.mp4`, `.avi`, `.mov`, `.wmv`, `.flv`, `.webm`, `.m4v`, `.3gp`, `.mpeg`, `.mpg`
- Images: `.jpg`, `.jpeg`, `.png`, `.heic`, `.heif`, `.gif`, `.bmp`, `.tif`, `.tiff`, `.webp`, `.raw`, `.cr2`, `.nef`, `.arw`

## Install And Run

Use Python 3.11 or newer on Windows.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run.py
```

If `python` opens the Microsoft Store or fails from `WindowsApps`, install Python from [python.org](https://www.python.org/downloads/windows/) and check **Add python.exe to PATH** during setup.

## Usage

1. Click **Add Folder** and select folders such as `my phone`, `hide wala sab`, `NEW PHONE AFTER RESET`, `DCIM`, or any other folder.
2. Click **Start Scan**.
3. Review **Exact Duplicates** first. Each group has one file marked `KEEP`.
4. Right-click a duplicate file and choose **Keep This Copy** if you want to keep a different one.
5. Select only files marked `Duplicate`.
6. Click **Move Selected Duplicates to Recycle Bin**.

Double-click any file result to open its location in Windows Explorer.

## How It Stays Fast And Safe

The scanner first groups files by size. Files with unique sizes cannot be exact duplicates, so they are not fully hashed.

For possible duplicates, it computes a quick sample hash from parts of the file. Only files with matching size and matching quick hash are fully hashed. Full hashes read files in 4 MB chunks, so large movies are never loaded fully into RAM.

SQLite caches hashes using path, size, and modification time. Unchanged files can reuse previous hashes on later scans.

## Build A Windows EXE

From the project folder:

```powershell
.\.venv\Scripts\Activate.ps1
pyinstaller --noconfirm --windowed --name SafeDuplicateFinder run.py
```

The executable will be created at:

```text
dist\SafeDuplicateFinder\SafeDuplicateFinder.exe
```

For a single-file executable:

```powershell
pyinstaller --noconfirm --onefile --windowed --name SafeDuplicateFinder run.py
```

The single EXE will be created at:

```text
dist\SafeDuplicateFinder.exe
```

## Safety Notes

- The app does not permanently delete files.
- The app does not auto-select files for deletion outside duplicate groups.
- Same-name conflicts are warnings only, not deletion suggestions.
- Always review duplicate groups before moving files to the Recycle Bin.
