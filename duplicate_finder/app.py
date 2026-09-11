from __future__ import annotations

import os
import queue
import threading
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from .db import ScanDatabase
from .file_utils import human_size, move_to_recycle_bin, open_location, play_file
from .scanner import DuplicateScanner, FileRecord, ScanProgress, ScanResults

try:
    from PIL import Image, ImageDraw, ImageOps

    PIL_AVAILABLE = True
except ImportError:
    Image = ImageDraw = ImageOps = None
    PIL_AVAILABLE = False


APP_DIR = Path(os.getenv("LOCALAPPDATA", Path.home())) / "SafeDuplicateFinder"
DB_PATH = APP_DIR / "scan_cache.sqlite3"


class DuplicateFinderApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Safe Duplicate Finder")
        self.geometry("1280x780")
        self.minsize(1000, 650)

        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.db = ScanDatabase(DB_PATH)
        self.folders: list[str] = []
        self.results = ScanResults()
        self.progress_queue: queue.Queue[ScanProgress | tuple[str, ScanResults] | tuple[str, Exception]] = queue.Queue()
        self.scan_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.keep_by_group: dict[int, str] = {}
        self.duplicate_item_paths: dict[str, tuple[int, str]] = {}
        self.tree_file_paths: dict[str, str] = {}
        self.media_tabs: dict[str, dict[str, object]] = {}
        self.media_buttons: list[ctk.CTkButton] = []
        self.media_images: list[ctk.CTkImage] = []
        self.preview_image: ctk.CTkImage | None = None
        self.current_media_path: str | None = None

        self._build_ui()
        self.after(150, self._poll_progress)

    def _build_ui(self) -> None:
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar = ctk.CTkFrame(self, width=270, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_rowconfigure(3, weight=1)

        ctk.CTkLabel(
            self.sidebar,
            text="Safe Duplicate Finder",
            font=ctk.CTkFont(size=22, weight="bold"),
        ).grid(row=0, column=0, padx=18, pady=(18, 8), sticky="w")

        ctk.CTkLabel(
            self.sidebar,
            text="Exact duplicates are verified by content hash. Nothing is deleted automatically.",
            wraplength=230,
            justify="left",
        ).grid(row=1, column=0, padx=18, pady=(0, 14), sticky="w")

        folder_buttons = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        folder_buttons.grid(row=2, column=0, padx=14, pady=4, sticky="ew")
        folder_buttons.grid_columnconfigure((0, 1), weight=1)

        ctk.CTkButton(folder_buttons, text="Add Folder", command=self._add_folder).grid(
            row=0, column=0, padx=4, pady=4, sticky="ew"
        )
        ctk.CTkButton(folder_buttons, text="Remove", command=self._remove_selected_folder).grid(
            row=0, column=1, padx=4, pady=4, sticky="ew"
        )

        self.folder_list = ctk.CTkTextbox(self.sidebar, height=250, wrap="none")
        self.folder_list.grid(row=3, column=0, padx=18, pady=8, sticky="nsew")
        self.folder_list.configure(state="disabled")

        self.scan_button = ctk.CTkButton(self.sidebar, text="Start Scan", command=self._start_scan)
        self.scan_button.grid(row=4, column=0, padx=18, pady=(14, 6), sticky="ew")

        self.stop_button = ctk.CTkButton(
            self.sidebar, text="Stop Scan", command=self._stop_scan, state="disabled", fg_color="#8a2424"
        )
        self.stop_button.grid(row=5, column=0, padx=18, pady=6, sticky="ew")

        ctk.CTkButton(
            self.sidebar,
            text="Move Selected to Recycle Bin",
            command=self._delete_selected_duplicates,
            fg_color="#a43f21",
        ).grid(row=6, column=0, padx=18, pady=(18, 6), sticky="ew")

        ctk.CTkButton(
            self.sidebar,
            text="Move All Duplicates to Recycle Bin",
            command=self._delete_all_duplicates,
            fg_color="#7f1d1d",
        ).grid(row=7, column=0, padx=18, pady=(6, 8), sticky="ew")

        self.main = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.main.grid(row=0, column=1, sticky="nsew")
        self.main.grid_columnconfigure(0, weight=1)
        self.main.grid_rowconfigure(2, weight=1)

        self.stats_frame = ctk.CTkFrame(self.main, corner_radius=8)
        self.stats_frame.grid(row=0, column=0, padx=16, pady=14, sticky="ew")
        self.stats_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)

        self.files_stat = self._stat_label("Files", "0", 0)
        self.read_stat = self._stat_label("Read", "0 B", 1)
        self.recovery_stat = self._stat_label("Recoverable", "0 B", 2)
        self.speed_stat = self._stat_label("Speed", "0 B/s", 3)

        progress_frame = ctk.CTkFrame(self.main, corner_radius=8)
        progress_frame.grid(row=1, column=0, padx=16, pady=(0, 12), sticky="ew")
        progress_frame.grid_columnconfigure(0, weight=1)
        self.phase_label = ctk.CTkLabel(progress_frame, text="Ready", anchor="w")
        self.phase_label.grid(row=0, column=0, padx=12, pady=(8, 0), sticky="ew")
        self.progress_bar = ctk.CTkProgressBar(progress_frame)
        self.progress_bar.grid(row=1, column=0, padx=12, pady=(6, 10), sticky="ew")
        self.progress_bar.set(0)

        self.tabs = ctk.CTkTabview(self.main)
        self.tabs.grid(row=2, column=0, padx=16, pady=(0, 16), sticky="nsew")
        self.tab_trees: dict[str, ttk.Treeview] = {}
        for tab_name in ("Exact Duplicates", "Large Files", "Videos", "Images", "Same Name Conflicts"):
            tab = self.tabs.add(tab_name)
            tab.grid_columnconfigure(0, weight=1)
            tab.grid_rowconfigure(0, weight=1)
            if tab_name in {"Videos", "Images"}:
                self.media_tabs[tab_name] = self._make_media_tab(tab, tab_name)
            else:
                self.tab_trees[tab_name] = self._make_tree(tab, tab_name)

        self.details = ctk.CTkTextbox(self.main, height=120)
        self.details.grid(row=3, column=0, padx=16, pady=(0, 16), sticky="ew")
        self.details.insert("1.0", "Select a result to preview file details.")
        self.details.configure(state="disabled")

    def _stat_label(self, title: str, value: str, column: int) -> ctk.CTkLabel:
        frame = ctk.CTkFrame(self.stats_frame, fg_color="transparent")
        frame.grid(row=0, column=column, padx=12, pady=10, sticky="ew")
        ctk.CTkLabel(frame, text=title, anchor="w").pack(anchor="w")
        label = ctk.CTkLabel(frame, text=value, font=ctk.CTkFont(size=20, weight="bold"), anchor="w")
        label.pack(anchor="w")
        return label

    def _make_tree(self, parent: ctk.CTkFrame, tab_name: str) -> ttk.Treeview:
        style = ttk.Style()
        style.theme_use("default")
        style.configure("Treeview", rowheight=28, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

        columns = ("size", "type", "hash", "path")
        tree = ttk.Treeview(parent, columns=columns, show="tree headings", selectmode="extended")
        tree.heading("#0", text="Name / Group")
        tree.heading("size", text="Size")
        tree.heading("type", text="Type")
        tree.heading("hash", text="Content Hash")
        tree.heading("path", text="Path")
        tree.column("#0", width=260, minwidth=180)
        tree.column("size", width=110, minwidth=90, anchor="e")
        tree.column("type", width=90, minwidth=80)
        tree.column("hash", width=170, minwidth=120)
        tree.column("path", width=540, minwidth=240)
        tree.grid(row=0, column=0, sticky="nsew")
        tree.bind("<<TreeviewSelect>>", lambda _event, selected_tree=tree: self._show_details(selected_tree))
        tree.bind("<Double-1>", lambda event, selected_tree=tree: self._open_tree_event_location(event, selected_tree))

        menu = self._make_context_menu(tree)
        tree.bind("<Button-3>", lambda event, m=menu: m.tk_popup(event.x_root, event.y_root))
        return tree

    def _make_media_tab(self, parent: ctk.CTkFrame, tab_name: str) -> dict[str, object]:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_columnconfigure(1, weight=0)
        parent.grid_rowconfigure(0, weight=1)

        grid = ctk.CTkScrollableFrame(parent, corner_radius=8)
        grid.grid(row=0, column=0, padx=(0, 12), pady=0, sticky="nsew")
        grid.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform=f"{tab_name}-media")

        preview = ctk.CTkFrame(parent, width=330, corner_radius=8)
        preview.grid(row=0, column=1, sticky="nsew")
        preview.grid_propagate(False)
        preview.grid_columnconfigure(0, weight=1)
        preview.grid_rowconfigure(1, weight=1)

        title = ctk.CTkLabel(preview, text=f"{tab_name} Preview", font=ctk.CTkFont(size=17, weight="bold"))
        title.grid(row=0, column=0, padx=14, pady=(14, 8), sticky="w")

        image_label = ctk.CTkLabel(preview, text="Select a tile", width=300, height=260, fg_color="#1f2937", corner_radius=8)
        image_label.grid(row=1, column=0, padx=14, pady=8, sticky="nsew")

        info = ctk.CTkTextbox(preview, height=130, wrap="word")
        info.grid(row=2, column=0, padx=14, pady=8, sticky="ew")
        info.insert("1.0", "Media files from all selected folders will appear here after scan.")
        info.configure(state="disabled")

        actions = ctk.CTkFrame(preview, fg_color="transparent")
        actions.grid(row=3, column=0, padx=10, pady=(4, 14), sticky="ew")
        actions.grid_columnconfigure((0, 1), weight=1)
        play_button = ctk.CTkButton(actions, text="Play / Open", command=self._play_current_media, state="disabled")
        play_button.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        location_button = ctk.CTkButton(actions, text="Location", command=self._open_current_media_location, state="disabled")
        location_button.grid(row=0, column=1, padx=4, pady=4, sticky="ew")

        return {
            "grid": grid,
            "preview": image_label,
            "info": info,
            "play_button": play_button,
            "location_button": location_button,
            "tab_name": tab_name,
        }

    def _make_context_menu(self, tree: ttk.Treeview):
        import tkinter as tk

        menu = tk.Menu(tree, tearoff=0)
        menu.add_command(label="Open File Location", command=lambda: self._open_selected_location(tree))
        menu.add_command(label="Keep This Copy", command=lambda: self._mark_keep_copy(tree))
        return menu

    def _add_folder(self) -> None:
        folder = filedialog.askdirectory(title="Select a folder to scan")
        if folder and folder not in self.folders:
            self.folders.append(folder)
            self._refresh_folder_list()

    def _remove_selected_folder(self) -> None:
        if self.folders:
            self.folders.pop()
            self._refresh_folder_list()

    def _refresh_folder_list(self) -> None:
        self.folder_list.configure(state="normal")
        self.folder_list.delete("1.0", "end")
        if self.folders:
            folder_text = f"{len(self.folders)} folder(s) selected. The scan reads all of them:\n\n"
            folder_text += "\n".join(self.folders)
        else:
            folder_text = "No folders selected."
        self.folder_list.insert("1.0", folder_text)
        self.folder_list.configure(state="disabled")

    def _start_scan(self) -> None:
        if not self.folders:
            messagebox.showwarning("Select folders", "Add at least one folder before scanning.")
            return
        folders_to_scan = list(self.folders)
        self.stop_event.clear()
        self.scan_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.progress_bar.configure(mode="indeterminate")
        self.progress_bar.start()
        self.phase_label.configure(text=f"Starting scan across {len(folders_to_scan)} selected folder(s)...")
        self._clear_trees()

        def run() -> None:
            try:
                scanner = DuplicateScanner(
                    self.db,
                    progress_callback=self.progress_queue.put,
                    workers=1,
                    stop_event=self.stop_event,
                )
                results = scanner.scan(folders_to_scan)
                self.progress_queue.put(("done", results))
            except Exception as exc:
                self.progress_queue.put(("error", exc))

        self.scan_thread = threading.Thread(target=run, daemon=True)
        self.scan_thread.start()

    def _stop_scan(self) -> None:
        self.stop_event.set()
        self.phase_label.configure(text="Stopping after the current chunk...")

    def _poll_progress(self) -> None:
        latest_progress: ScanProgress | None = None
        handled_terminal_event = False
        try:
            for _ in range(100):
                item = self.progress_queue.get_nowait()
                if isinstance(item, ScanProgress):
                    latest_progress = item
                elif item[0] == "done":
                    if latest_progress:
                        self._update_progress(latest_progress)
                        latest_progress = None
                    self._scan_done(item[1])
                    handled_terminal_event = True
                    break
                elif item[0] == "error":
                    if latest_progress:
                        self._update_progress(latest_progress)
                        latest_progress = None
                    self._scan_error(item[1])
                    handled_terminal_event = True
                    break
        except queue.Empty:
            pass
        if latest_progress and not handled_terminal_event:
            self._update_progress(latest_progress)
        self.after(150, self._poll_progress)

    def _update_progress(self, progress: ScanProgress) -> None:
        self.files_stat.configure(text=f"{progress.files_seen:,}")
        self.read_stat.configure(text=human_size(progress.bytes_read))
        self.speed_stat.configure(text=f"{human_size(progress.speed_bps)}/s")
        display_path = self._short_display_path(progress.current_file)
        current = f" - {display_path}" if display_path else ""
        self.phase_label.configure(text=f"{progress.phase}: {progress.files_hashed:,} hashed{current}")

    def _scan_done(self, results: ScanResults) -> None:
        self.progress_bar.stop()
        self.progress_bar.configure(mode="determinate")
        self.progress_bar.set(1)
        self.scan_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.results = results
        self.recovery_stat.configure(text=human_size(results.recoverable_bytes))
        self.files_stat.configure(text=f"{results.total_files:,}")
        self.read_stat.configure(text=human_size(results.bytes_read))
        self.phase_label.configure(text="Scan complete")
        self._populate_results(results)

    def _scan_error(self, exc: Exception) -> None:
        self.progress_bar.stop()
        self.scan_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        messagebox.showerror("Scan failed", str(exc))

    def _clear_trees(self) -> None:
        self.keep_by_group.clear()
        self.duplicate_item_paths.clear()
        self.tree_file_paths.clear()
        self.media_buttons.clear()
        self.media_images.clear()
        self.preview_image = None
        self.current_media_path = None
        for tree in self.tab_trees.values():
            for item in tree.get_children():
                tree.delete(item)
        for media_tab in self.media_tabs.values():
            grid = media_tab["grid"]
            if isinstance(grid, ctk.CTkScrollableFrame):
                for child in grid.winfo_children():
                    child.destroy()
            preview = media_tab["preview"]
            if isinstance(preview, ctk.CTkLabel):
                preview.configure(image=None, text="Select a tile")
            info = media_tab["info"]
            if isinstance(info, ctk.CTkTextbox):
                self._set_textbox(info, "Media files from all selected folders will appear here after scan.")
            for key in ("play_button", "location_button"):
                button = media_tab[key]
                if isinstance(button, ctk.CTkButton):
                    button.configure(state="disabled")

    def _populate_results(self, results: ScanResults) -> None:
        self._clear_trees()
        duplicate_tree = self.tab_trees["Exact Duplicates"]
        for group_index, group in enumerate(results.exact_duplicates, start=1):
            recoverable = sum(file.size for file in group) - max(file.size for file in group)
            parent = duplicate_tree.insert(
                "",
                "end",
                text=f"Duplicate group {group_index} ({len(group)} files)",
                values=(human_size(recoverable), "Recoverable", "", ""),
                open=True,
            )
            self.keep_by_group[group_index] = group[0].path
            for file in group:
                item = duplicate_tree.insert(
                    parent,
                    "end",
                    text=file.name,
                    values=(human_size(file.size), "KEEP" if file.path == group[0].path else "Duplicate", self._short_hash(file), file.path),
                )
                self.duplicate_item_paths[item] = (group_index, file.path)
                self.tree_file_paths[item] = file.path

        self._populate_flat("Large Files", results.large_files)
        self._populate_media_grid("Videos", results.videos)
        self._populate_media_grid("Images", results.images)
        conflict_tree = self.tab_trees["Same Name Conflicts"]
        for group_index, group in enumerate(results.same_name_conflicts, start=1):
            parent = conflict_tree.insert("", "end", text=f"Name conflict {group_index}: {group[0].name}", values=("", "Warning", "", ""), open=True)
            for file in group:
                item = conflict_tree.insert(parent, "end", text=file.name, values=(human_size(file.size), "Different content", self._short_hash(file), file.path))
                self.tree_file_paths[item] = file.path

    def _populate_flat(self, tab_name: str, files: list[FileRecord]) -> None:
        tree = self.tab_trees[tab_name]
        for file in files:
            item = tree.insert("", "end", text=file.name, values=(human_size(file.size), file.extension or "file", self._short_hash(file), file.path))
            self.tree_file_paths[item] = file.path

    def _populate_media_grid(self, tab_name: str, files: list[FileRecord]) -> None:
        media_tab = self.media_tabs[tab_name]
        grid = media_tab["grid"]
        if not isinstance(grid, ctk.CTkScrollableFrame):
            return
        for child in grid.winfo_children():
            child.destroy()

        if not files:
            ctk.CTkLabel(grid, text=f"No {tab_name.lower()} found in the selected folders.").grid(
                row=0, column=0, padx=12, pady=12, sticky="w"
            )
            return

        visible_files = files[:800]
        if len(files) > len(visible_files):
            ctk.CTkLabel(
                grid,
                text=f"Showing first {len(visible_files):,} largest {tab_name.lower()} for smooth browsing. Total found: {len(files):,}.",
            ).grid(row=0, column=0, columnspan=4, padx=12, pady=(10, 4), sticky="w")

        for index, file in enumerate(visible_files):
            row = (index // 4) + (1 if len(files) > len(visible_files) else 0)
            column = index % 4
            image = self._make_media_thumbnail(file, tab_name, (178, 118))
            self.media_images.append(image)
            tile_text = f"{file.name}\n{human_size(file.size)}"
            button = ctk.CTkButton(
                grid,
                image=image,
                text=tile_text,
                compound="top",
                anchor="center",
                width=190,
                height=176,
                command=lambda selected=file, selected_tab=tab_name: self._select_media(selected_tab, selected),
            )
            button.grid(row=row, column=column, padx=8, pady=8, sticky="nsew")
            self.media_buttons.append(button)

    def _select_media(self, tab_name: str, file: FileRecord) -> None:
        media_tab = self.media_tabs[tab_name]
        self.current_media_path = file.path

        preview = media_tab["preview"]
        if isinstance(preview, ctk.CTkLabel):
            self.preview_image = self._make_media_thumbnail(file, tab_name, (300, 260), preview=True)
            preview.configure(image=self.preview_image, text="")

        info = media_tab["info"]
        if isinstance(info, ctk.CTkTextbox):
            self._set_textbox(
                info,
                "\n".join(
                    [
                        file.name,
                        f"Size: {human_size(file.size)}",
                        f"Type: {file.extension or 'file'}",
                        f"Path: {file.path}",
                    ]
                ),
            )

        for key in ("play_button", "location_button"):
            button = media_tab[key]
            if isinstance(button, ctk.CTkButton):
                button.configure(state="normal")

    def _make_media_thumbnail(
        self, file: FileRecord, tab_name: str, size: tuple[int, int], *, preview: bool = False
    ) -> ctk.CTkImage:
        if PIL_AVAILABLE and Image is not None and ImageDraw is not None and ImageOps is not None:
            try:
                if tab_name == "Images":
                    with Image.open(file.path) as original:
                        image = ImageOps.exif_transpose(original)
                        image.thumbnail(size, Image.Resampling.LANCZOS)
                        canvas = Image.new("RGB", size, "#111827")
                        x = (size[0] - image.width) // 2
                        y = (size[1] - image.height) // 2
                        canvas.paste(image.convert("RGB"), (x, y))
                        return ctk.CTkImage(light_image=canvas, dark_image=canvas, size=size)
            except Exception:
                pass
            canvas = Image.new("RGB", size, "#172033" if tab_name == "Videos" else "#2b2630")
            draw = ImageDraw.Draw(canvas)
            label = "PLAY" if tab_name == "Videos" else file.extension.upper().lstrip(".")
            draw.rounded_rectangle((size[0] // 2 - 35, size[1] // 2 - 22, size[0] // 2 + 35, size[1] // 2 + 22), radius=8, fill="#2563eb")
            if tab_name == "Videos":
                points = [
                    (size[0] // 2 - 10, size[1] // 2 - 14),
                    (size[0] // 2 - 10, size[1] // 2 + 14),
                    (size[0] // 2 + 16, size[1] // 2),
                ]
                draw.polygon(points, fill="white")
            else:
                draw.text((size[0] // 2 - 22, size[1] // 2 - 6), label[:6], fill="white")
            if preview:
                draw.text((16, size[1] - 28), "Open to preview with Windows", fill="#d1d5db")
            return ctk.CTkImage(light_image=canvas, dark_image=canvas, size=size)

        return ctk.CTkImage(size=size)

    def _show_details(self, tree: ttk.Treeview) -> None:
        selection = tree.selection()
        if not selection:
            return
        item = selection[0]
        values = tree.item(item, "values")
        text = [
            f"Name: {tree.item(item, 'text')}",
            f"Size: {values[0] if len(values) > 0 else ''}",
            f"Type: {values[1] if len(values) > 1 else ''}",
            f"Hash: {values[2] if len(values) > 2 else ''}",
            f"Path: {values[3] if len(values) > 3 else ''}",
            "",
            "Double-click a file to open its location. In duplicate groups, right-click a file and choose Keep This Copy.",
        ]
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", "\n".join(text))
        self.details.configure(state="disabled")

    def _open_selected_location(self, tree: ttk.Treeview) -> None:
        selection = tree.selection()
        if not selection:
            return
        path = self._path_for_tree_item(tree, selection[0])
        if path:
            open_location(path)

    def _open_tree_event_location(self, event, tree: ttk.Treeview) -> None:
        item = tree.identify_row(event.y)
        if item:
            tree.selection_set(item)
        else:
            selection = tree.selection()
            item = selection[0] if selection else ""
        path = self._path_for_tree_item(tree, item)
        if path:
            open_location(path)

    def _path_for_tree_item(self, tree: ttk.Treeview, item: str) -> str | None:
        if not item:
            return None
        path = self.tree_file_paths.get(item)
        if path:
            return path
        children = tree.get_children(item)
        for child in children:
            path = self.tree_file_paths.get(child)
            if path:
                return path
        return None

    def _play_current_media(self) -> None:
        if self.current_media_path:
            play_file(self.current_media_path)

    def _open_current_media_location(self) -> None:
        if self.current_media_path:
            open_location(self.current_media_path)

    def _mark_keep_copy(self, tree: ttk.Treeview) -> None:
        selection = tree.selection()
        if not selection:
            return
        item = selection[0]
        if item not in self.duplicate_item_paths:
            return
        group_index, path = self.duplicate_item_paths[item]
        self.keep_by_group[group_index] = path
        parent = tree.parent(item)
        for child in tree.get_children(parent):
            child_group, child_path = self.duplicate_item_paths[child]
            values = list(tree.item(child, "values"))
            values[1] = "KEEP" if child_path == path else "Duplicate"
            tree.item(child, values=values)

    def _delete_selected_duplicates(self) -> None:
        tree = self.tab_trees["Exact Duplicates"]
        paths_to_delete: list[str] = []
        for item in tree.selection():
            selected_items = tree.get_children(item) if item not in self.duplicate_item_paths else (item,)
            for selected_item in selected_items:
                if selected_item not in self.duplicate_item_paths:
                    continue
                group_index, path = self.duplicate_item_paths[selected_item]
                if self.keep_by_group.get(group_index) != path:
                    paths_to_delete.append(path)

        if not paths_to_delete:
            messagebox.showinfo(
                "Nothing selected",
                "Select duplicate files marked Duplicate. Files marked KEEP will never be moved.",
            )
            return
        self._move_duplicate_paths(sorted(set(paths_to_delete)), "selected duplicate")

    def _delete_all_duplicates(self) -> None:
        paths_to_delete: list[str] = []
        for _item, (group_index, path) in self.duplicate_item_paths.items():
            if self.keep_by_group.get(group_index) != path:
                paths_to_delete.append(path)

        if not paths_to_delete:
            messagebox.showinfo("No duplicates", "There are no duplicate files to move.")
            return
        self._move_duplicate_paths(sorted(set(paths_to_delete)), "duplicate")

    def _move_duplicate_paths(self, paths_to_delete: list[str], label: str) -> None:
        total = sum(Path(path).stat().st_size for path in paths_to_delete if Path(path).exists())
        confirmed = messagebox.askyesno(
            "Move to Recycle Bin",
            f"Move {len(paths_to_delete)} {label} file(s) to the Windows Recycle Bin?\n\n"
            f"Potential recovery: {human_size(total)}\n\nThis does not permanently delete files.",
        )
        if not confirmed:
            return
        try:
            move_to_recycle_bin(paths_to_delete)
            messagebox.showinfo("Done", "Selected duplicate files were moved to the Recycle Bin.")
            self._start_scan()
        except Exception as exc:
            messagebox.showerror("Could not move files", str(exc))

    @staticmethod
    def _short_hash(file: FileRecord) -> str:
        digest = file.full_hash or file.quick_hash or ""
        return digest[:18] if digest else ""

    @staticmethod
    def _set_textbox(textbox: ctk.CTkTextbox, text: str) -> None:
        textbox.configure(state="normal")
        textbox.delete("1.0", "end")
        textbox.insert("1.0", text)
        textbox.configure(state="disabled")

    @staticmethod
    def _short_display_path(path: str, limit: int = 130) -> str:
        if not path or len(path) <= limit:
            return path
        return "..." + path[-limit:]
