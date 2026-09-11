from __future__ import annotations

import hashlib
import os
import queue
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .db import ScanDatabase
from .file_utils import is_image, is_video


CHUNK_SIZE = 4 * 1024 * 1024
QUICK_SAMPLE_SIZE = 1024 * 1024
LARGE_FILE_BYTES = 500 * 1024 * 1024


@dataclass(slots=True)
class FileRecord:
    path: str
    name: str
    size: int
    mtime_ns: int
    extension: str
    quick_hash: str | None = None
    full_hash: str | None = None
    error: str | None = None


@dataclass(slots=True)
class ScanProgress:
    phase: str
    files_seen: int = 0
    files_hashed: int = 0
    bytes_read: int = 0
    total_bytes: int = 0
    current_file: str = ""
    speed_bps: float = 0.0
    message: str = ""


@dataclass(slots=True)
class ScanResults:
    exact_duplicates: list[list[FileRecord]] = field(default_factory=list)
    same_name_conflicts: list[list[FileRecord]] = field(default_factory=list)
    large_files: list[FileRecord] = field(default_factory=list)
    videos: list[FileRecord] = field(default_factory=list)
    images: list[FileRecord] = field(default_factory=list)
    errors: list[FileRecord] = field(default_factory=list)
    total_files: int = 0
    total_bytes: int = 0
    bytes_read: int = 0

    @property
    def recoverable_bytes(self) -> int:
        total = 0
        for group in self.exact_duplicates:
            sizes = sorted((file.size for file in group), reverse=True)
            total += sum(sizes[1:])
        return total


ProgressCallback = Callable[[ScanProgress], None]


class DuplicateScanner:
    def __init__(
        self,
        db: ScanDatabase,
        progress_callback: ProgressCallback,
        *,
        workers: int = 2,
        stop_event: threading.Event | None = None,
    ):
        self.db = db
        self.progress_callback = progress_callback
        self.workers = max(1, min(workers, 4))
        self.stop_event = stop_event or threading.Event()
        self.scan_id = time.time_ns()
        self._bytes_read = 0
        self._bytes_lock = threading.Lock()
        self._start_time = time.monotonic()
        self._last_emit_time = 0.0

    def scan(self, folders: list[str]) -> ScanResults:
        self._start_time = time.monotonic()
        records = self._collect_files(folders)
        if self.stop_event.is_set():
            return ScanResults(total_files=len(records))

        total_bytes = sum(record.size for record in records)
        self._emit("Grouping", files_seen=len(records), total_bytes=total_bytes, force=True)

        by_size: dict[int, list[FileRecord]] = defaultdict(list)
        by_name: dict[str, list[FileRecord]] = defaultdict(list)
        for record in records:
            by_size[record.size].append(record)
            by_name[record.name.lower()].append(record)

        candidates = [record for group in by_size.values() if len(group) > 1 for record in group]
        self._hash_candidates(candidates, total_bytes)

        exact_groups: list[list[FileRecord]] = []
        by_full_hash: dict[tuple[int, str], list[FileRecord]] = defaultdict(list)
        for record in candidates:
            if record.full_hash and not record.error:
                by_full_hash[(record.size, record.full_hash)].append(record)
        for group in by_full_hash.values():
            if len(group) > 1:
                exact_groups.append(sorted(group, key=lambda file: file.path.lower()))

        conflicts: list[list[FileRecord]] = []
        for same_name_group in by_name.values():
            content_signatures = {
                record.full_hash or record.quick_hash for record in same_name_group if record.full_hash or record.quick_hash
            }
            sizes = {record.size for record in same_name_group}
            if len(same_name_group) > 1 and (len(sizes) > 1 or len(content_signatures) > 1):
                conflicts.append(sorted(same_name_group, key=lambda file: file.path.lower()))

        seen_paths = [record.path for record in records]
        self.db.remove_missing(seen_paths)

        return ScanResults(
            exact_duplicates=sorted(exact_groups, key=lambda g: g[0].size, reverse=True),
            same_name_conflicts=sorted(conflicts, key=lambda g: g[0].name.lower()),
            large_files=sorted(
                [record for record in records if record.size >= LARGE_FILE_BYTES],
                key=lambda file: file.size,
                reverse=True,
            ),
            videos=sorted([record for record in records if is_video(record.path)], key=lambda file: file.size, reverse=True),
            images=sorted([record for record in records if is_image(record.path)], key=lambda file: file.size, reverse=True),
            errors=[record for record in records if record.error],
            total_files=len(records),
            total_bytes=total_bytes,
            bytes_read=self._bytes_read,
        )

    def _collect_files(self, folders: list[str]) -> list[FileRecord]:
        records: list[FileRecord] = []
        seen_paths: set[str] = set()
        files_seen = 0
        total_bytes = 0

        for folder in folders:
            if self.stop_event.is_set():
                break
            root = Path(folder)
            if not root.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                if self.stop_event.is_set():
                    break
                dirnames[:] = [name for name in dirnames if not self._is_system_skip_dir(name)]
                for filename in filenames:
                    if self.stop_event.is_set():
                        break
                    path = Path(dirpath) / filename
                    try:
                        resolved = str(path.resolve())
                        if resolved in seen_paths:
                            continue
                        stat = path.stat()
                        if not path.is_file() or stat.st_size <= 0:
                            continue
                        record = FileRecord(
                            path=resolved,
                            name=path.name,
                            size=stat.st_size,
                            mtime_ns=stat.st_mtime_ns,
                            extension=path.suffix.lower(),
                        )
                        quick_hash, full_hash = self.db.get_cached_hashes(resolved, record.size, record.mtime_ns)
                        record.quick_hash = quick_hash
                        record.full_hash = full_hash
                        self._save_record(record)
                        records.append(record)
                        seen_paths.add(resolved)
                        files_seen += 1
                        total_bytes += record.size
                        if files_seen % 200 == 0:
                            self._emit(
                                "Scanning folders",
                                files_seen=files_seen,
                                total_bytes=total_bytes,
                                current_file=resolved,
                            )
                    except OSError as exc:
                        records.append(
                            FileRecord(
                                path=str(path),
                                name=path.name,
                                size=0,
                                mtime_ns=0,
                                extension=path.suffix.lower(),
                                error=str(exc),
                            )
                        )
        self._emit("Scanning folders", files_seen=files_seen, total_bytes=total_bytes, force=True)
        return records

    def _hash_candidates(self, candidates: list[FileRecord], total_bytes: int) -> None:
        if not candidates:
            return

        for record in candidates:
            if self.stop_event.is_set():
                return
            if not record.quick_hash:
                record.quick_hash = self._quick_hash(record)
                self._save_record(record)

        by_quick: dict[tuple[int, str], list[FileRecord]] = defaultdict(list)
        for record in candidates:
            if record.quick_hash:
                by_quick[(record.size, record.quick_hash)].append(record)

        full_hash_candidates = [
            record for group in by_quick.values() if len(group) > 1 for record in group
        ]
        work_queue: queue.Queue[FileRecord | None] = queue.Queue()
        for record in full_hash_candidates:
            if not record.full_hash:
                work_queue.put(record)

        files_hashed = sum(1 for record in full_hash_candidates if record.full_hash)
        total_to_hash = len(full_hash_candidates)

        def worker() -> None:
            nonlocal files_hashed
            while not self.stop_event.is_set():
                record = work_queue.get()
                if record is None:
                    work_queue.task_done()
                    return
                try:
                    record.full_hash = self._full_hash(record)
                    self._save_record(record)
                finally:
                    files_hashed += 1
                    self._emit(
                        "Hashing duplicates",
                        files_seen=total_to_hash,
                        files_hashed=files_hashed,
                        total_bytes=total_bytes,
                        current_file=record.path,
                    )
                    work_queue.task_done()

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(self.workers)]
        for thread in threads:
            thread.start()
        for _ in threads:
            work_queue.put(None)
        work_queue.join()

    def _quick_hash(self, record: FileRecord) -> str | None:
        hasher = hashlib.blake2b(digest_size=16)
        try:
            with open(record.path, "rb") as file:
                head = file.read(QUICK_SAMPLE_SIZE)
                hasher.update(head)
                if record.size > QUICK_SAMPLE_SIZE * 2:
                    file.seek(max(0, record.size // 2 - QUICK_SAMPLE_SIZE // 2))
                    hasher.update(file.read(QUICK_SAMPLE_SIZE))
                    file.seek(max(0, record.size - QUICK_SAMPLE_SIZE))
                    hasher.update(file.read(QUICK_SAMPLE_SIZE))
            self._add_bytes_read(min(record.size, QUICK_SAMPLE_SIZE * 3))
            return hasher.hexdigest()
        except OSError as exc:
            record.error = str(exc)
            return None

    def _full_hash(self, record: FileRecord) -> str | None:
        hasher = hashlib.blake2b(digest_size=32)
        try:
            with open(record.path, "rb") as file:
                while not self.stop_event.is_set():
                    chunk = file.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    self._add_bytes_read(len(chunk))
                    self._emit(
                        "Hashing current file",
                        files_seen=1,
                        total_bytes=record.size,
                        current_file=record.path,
                    )
                    time.sleep(0.001)
            if self.stop_event.is_set():
                return None
            return hasher.hexdigest()
        except OSError as exc:
            record.error = str(exc)
            return None

    def _save_record(self, record: FileRecord) -> None:
        self.db.upsert_file(
            path=record.path,
            name_lower=record.name.lower(),
            size=record.size,
            mtime_ns=record.mtime_ns,
            quick_hash=record.quick_hash,
            full_hash=record.full_hash,
            extension=record.extension,
            last_seen=self.scan_id,
        )

    def _add_bytes_read(self, amount: int) -> None:
        with self._bytes_lock:
            self._bytes_read += amount

    def _emit(
        self,
        phase: str,
        *,
        files_seen: int = 0,
        files_hashed: int = 0,
        total_bytes: int = 0,
        current_file: str = "",
        message: str = "",
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        if not force and now - self._last_emit_time < 0.25:
            return
        self._last_emit_time = now
        elapsed = max(0.001, time.monotonic() - self._start_time)
        self.progress_callback(
            ScanProgress(
                phase=phase,
                files_seen=files_seen,
                files_hashed=files_hashed,
                bytes_read=self._bytes_read,
                total_bytes=total_bytes,
                current_file=current_file,
                speed_bps=self._bytes_read / elapsed,
                message=message,
            )
        )

    @staticmethod
    def _is_system_skip_dir(name: str) -> bool:
        return name.lower() in {
            "$recycle.bin",
            "system volume information",
            ".git",
            "node_modules",
            "__pycache__",
        }
