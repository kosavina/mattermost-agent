import os
import re
from pathlib import Path

import pytest

from mattermost_agent.processor.memory import (
    forget_memory,
    memory_versions,
    read_memory,
    save_memory,
)


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    path = tmp_path / "k3d7w1q9m2n4p6r8t0v2x4z6a8"
    path.mkdir(mode=0o700)
    return path


def test_no_memory_yet(folder: Path) -> None:
    assert read_memory(folder) is None
    assert memory_versions(folder) == 0


def test_the_first_save(folder: Path) -> None:
    assert save_memory(folder, "one") == 1
    assert read_memory(folder) == "one"
    assert memory_versions(folder) == 0
    assert sorted(entry.name for entry in folder.iterdir()) == ["memory.md"]
    assert ((folder / "memory.md").stat().st_mode & 0o777) == 0o600


def test_later_saves_keep_every_earlier_version(folder: Path) -> None:
    save_memory(folder, "one")
    assert save_memory(folder, "two") == 2
    assert save_memory(folder, "three") == 3
    assert read_memory(folder) == "three"
    assert memory_versions(folder) == 2
    history = folder / "history"
    assert ((history.stat().st_mode) & 0o777) == 0o700
    archived = list(history.iterdir())
    assert sorted(entry.read_text() for entry in archived) == ["one", "two"]
    assert all(re.fullmatch(r"[0-9]{13}(-[0-9]+)?\.md", entry.name) for entry in archived)
    assert sorted(entry.name for entry in folder.iterdir()) == ["history", "memory.md"]


def test_saving_the_same_text_again_changes_nothing(folder: Path) -> None:
    save_memory(folder, "one")
    assert save_memory(folder, "one") is None
    assert memory_versions(folder) == 0
    assert not (folder / "history").exists()


def test_a_crash_before_the_rename_leaves_the_old_memory_and_no_duplicate(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old file is archived first, then replaced: it never disappears, even for an instant."""
    save_memory(folder, "one")
    real_replace = os.replace

    def failing(source: object, target: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", failing)
    with pytest.raises(OSError):
        save_memory(folder, "two")
    monkeypatch.setattr(os, "replace", real_replace)
    assert read_memory(folder) == "one"
    assert memory_versions(folder) == 1  # the archive link was made before the failure
    assert save_memory(folder, "two") == 2  # the rerun reuses it: no duplicate entry
    assert read_memory(folder) == "two"
    assert memory_versions(folder) == 1
    assert sorted(p.read_text() for p in (folder / "history").iterdir()) == ["one"]


def test_forget_shreds_and_removes_everything(folder: Path) -> None:
    save_memory(folder, "secret-one")
    save_memory(folder, "secret-two")
    assert forget_memory(folder) == 2
    assert not folder.exists()


def test_forget_zeroes_a_file_before_unlinking(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_memory(folder, "secret-one")
    seen: list[bytes] = []
    real_unlink = Path.unlink

    def spying_unlink(self: Path, missing_ok: bool = False) -> None:
        seen.append(self.read_bytes())
        real_unlink(self, missing_ok)

    monkeypatch.setattr(Path, "unlink", spying_unlink)
    forget_memory(folder)
    assert seen == [b"\0" * len("secret-one")]


def test_forget_does_not_follow_a_symlink(folder: Path, tmp_path: Path) -> None:
    target = tmp_path / "precious.txt"
    target.write_text("keep me")
    (folder / "memory.md").symlink_to(target)
    forget_memory(folder)
    assert target.read_text() == "keep me"
    assert not folder.exists()


def test_forget_refuses_an_unexpected_folder(folder: Path) -> None:
    (folder / "unexpected").mkdir()
    with pytest.raises(IsADirectoryError):
        forget_memory(folder)
