from __future__ import annotations

import os
import subprocess
from pathlib import Path

from send2trash import send2trash


VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".m4v",
    ".3gp",
    ".mpeg",
    ".mpg",
}

IMAGE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".heic",
    ".heif",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
    ".raw",
    ".cr2",
    ".nef",
    ".arw",
}


def human_size(num_bytes: int | float) -> str:
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    size = float(num_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


def is_video(path: str | Path) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


def is_image(path: str | Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def open_location(path: str | Path) -> None:
    file_path = Path(path)
    if os.name == "nt":
        if file_path.exists() and file_path.is_file():
            subprocess.Popen(["explorer.exe", f"/select,{file_path}"])
        else:
            target = file_path if file_path.exists() else file_path.parent
            os.startfile(str(target))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(file_path.parent)])


def play_file(path: str | Path) -> None:
    file_path = Path(path)
    if os.name == "nt":
        os.startfile(str(file_path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(file_path)])


def move_to_recycle_bin(paths: list[str]) -> None:
    for path in paths:
        send2trash(path)
