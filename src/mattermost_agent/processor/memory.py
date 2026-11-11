"""A conversation's memory on disk: written only by our code, read by the agent.

conversations/<channel_id>/memory.md is the current memory; history/<unix-ms>.md every earlier
version, named by the time it was written. The agent proposes the complete new text; save_memory
writes it atomically and keeps the old one. Nothing else is ever created in a conversation folder.
forget_memory overwrites each file with zeros before unlinking it, and removes the folder.
"""

import os
from pathlib import Path

MEMORY_FILE = "memory.md"
HISTORY_DIR = "history"
TEMP_FILE = "memory.md.tmp"


def read_memory(conversation_dir: Path) -> str | None:
    """Return the current memory, or None if there is none yet."""
    try:
        return (conversation_dir / MEMORY_FILE).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def memory_versions(conversation_dir: Path) -> int:
    """Return how many earlier versions the history holds."""
    history = conversation_dir / HISTORY_DIR
    return sum(1 for entry in history.iterdir() if entry.is_file()) if history.is_dir() else 0


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _archive(history: Path, current: Path) -> None:
    """Hard-link the current file into history/ as <unix-ms>.md, named by when it was written.

    A crash between this link and the rename leaves the link in place; the rerun finds it (same
    inode) and adds nothing. Another file of that name is a collision, and gets a suffix.
    """
    stem = str(current.stat().st_mtime_ns // 1_000_000)
    candidate = history / f"{stem}.md"
    counter = 1
    while candidate.exists():
        if candidate.samefile(current):
            return
        candidate = history / f"{stem}-{counter}.md"
        counter += 1
    os.link(current, candidate)
    _fsync_dir(history)


def save_memory(conversation_dir: Path, text: str) -> int | None:
    """Make `text` the current memory and return its version number, or None if it already is.

    Idempotent: a crash and a rerun with the same text adds no history entry. The new text is
    written to a temp file and fsynced; the old file is hard-linked into history/; then the temp
    file is renamed over memory.md, which is therefore present and complete at every instant.
    """
    current = conversation_dir / MEMORY_FILE
    if read_memory(conversation_dir) == text:
        return None
    temp = conversation_dir / TEMP_FILE
    with open(temp, "w", encoding="utf-8", opener=lambda p, f: os.open(p, f, 0o600)) as file:
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    history = conversation_dir / HISTORY_DIR
    if current.exists():
        history.mkdir(mode=0o700, exist_ok=True)
        _archive(history, current)
    os.replace(temp, current)
    _fsync_dir(conversation_dir)
    return memory_versions(conversation_dir) + 1


def _shred(path: Path) -> None:
    """Overwrite a regular file with zeros, then remove it. A symlink is only unlinked."""
    if not path.is_symlink():
        size = path.stat().st_size
        descriptor = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)
        try:
            os.write(descriptor, b"\0" * size)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    path.unlink()


def forget_memory(conversation_dir: Path) -> int:
    """Remove the conversation folder with everything in it. Return how many files were shredded."""
    count = 0
    history = conversation_dir / HISTORY_DIR
    if history.is_dir():
        for entry in history.iterdir():
            _shred(entry)
            count += 1
        history.rmdir()
    for entry in conversation_dir.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            raise IsADirectoryError(str(entry))
        _shred(entry)
        count += 1
    conversation_dir.rmdir()
    return count
