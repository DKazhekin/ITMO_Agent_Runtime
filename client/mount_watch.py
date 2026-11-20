"""Bounded, best-effort save hints for tracked workspace paths."""

from collections import OrderedDict
import os
from pathlib import Path
from threading import Lock

from client.mount_files import path_parts


class WatchHints:
    def __init__(self, root, limit=256):
        self.root = root
        self.limit = limit
        self.paths = OrderedDict()
        self.lock = Lock()

    def notify(self, *paths):
        with self.lock:
            for value in paths:
                if not value:
                    continue
                try:
                    path = Path(os.fsdecode(value)).relative_to(self.root).as_posix()
                    path_parts(path)
                except (PermissionError, ValueError):
                    continue
                self.paths[path] = None
                self.paths.move_to_end(path)
                if len(self.paths) > self.limit:
                    self.paths.popitem(last=False)

    def prioritize(self, baselines):
        with self.lock:
            dirty = set(self.paths)
            self.paths.clear()
        return sorted(baselines, key=lambda item: item[0][1] not in dirty)


def start_watcher(root, hints):
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    class Saves(FileSystemEventHandler):
        def on_any_event(self, event):
            if not event.is_directory and event.event_type in ("created", "modified", "deleted", "moved"):
                hints.notify(event.src_path, event.dest_path)

    observer = Observer(timeout=0.2)
    observer.schedule(Saves(), str(root), recursive=True)
    observer.start()
    return observer
