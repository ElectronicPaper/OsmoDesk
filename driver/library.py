"""A shot library: named moves that survive the browser tab.

What separates a motion-control rig from a jog box is that you can come back
next week and run the same move again. That needs the move to outlive the page
it was authored on, to carry a name someone can find it by, and to travel with
the rigging notes -- because angles alone do not recreate a frame.

Everything here is deliberately boring: JSON files in one directory, written
atomically, with names the operator chose. The interesting part is that the
name comes from a text box and ends up in a path, which is the shape of a
directory-traversal bug. `safe_stem` is the whole defence and it is tested
against the usual attempts.
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from . import moves

SUFFIX = ".move.json"

# A name is a label, not a path. Everything outside this set is folded away.
_ALLOWED = re.compile(r"[^A-Za-z0-9 ._-]+")
_RUNS = re.compile(r"[ _]+")

MAX_STEM = 64

# Windows will not create these whatever the extension, and a save that fails
# silently on one machine and works on another is worse than a rename.
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


class LibraryError(ValueError):
    """A name or a file the library will not accept."""


def safe_stem(name: str) -> str:
    """Turn an operator's name into a filename that cannot escape the folder.

    Not a sanitiser that tries to spot bad input -- an allow-list that keeps
    only what is known good. `../../etc/passwd` keeps its letters and loses
    every separator, so it lands in the library as a file called `etcpasswd`
    rather than anywhere near /etc.
    """
    # Normalise first: a composed and a decomposed form of the same name would
    # otherwise be two different files that look identical in a list.
    cleaned = unicodedata.normalize("NFKC", name or "").strip()
    cleaned = _ALLOWED.sub("", cleaned)
    cleaned = _RUNS.sub(" ", cleaned).strip(" .")
    if not cleaned:
        raise LibraryError("that name has no usable characters in it")
    if len(cleaned) > MAX_STEM:
        cleaned = cleaned[:MAX_STEM].strip(" .")
    if cleaned.lower() in _RESERVED:
        raise LibraryError(f"{cleaned!r} is a reserved device name on Windows")
    return cleaned


@dataclass(frozen=True)
class Entry:
    """One shot in the library, as the picker needs it."""

    name: str
    stem: str
    waypoints: int
    duration: float
    setup: str
    saved_at: float

    def to_dict(self) -> dict:
        return {
            "name": self.name, "stem": self.stem,
            "waypoints": self.waypoints, "duration": round(self.duration, 2),
            "setup": self.setup, "saved_at": self.saved_at,
        }


class Library:
    """Named moves in one directory."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, name: str) -> Path:
        path = (self.root / (safe_stem(name) + SUFFIX)).resolve()
        # Belt and braces. safe_stem already makes traversal impossible, but
        # this is the check that stays correct if safe_stem is ever loosened.
        root = self.root.resolve()
        if root not in path.parents:
            raise LibraryError("that name does not resolve inside the library")
        return path

    def save(self, name: str, move: moves.Move) -> Entry:
        """Write a move under `name`, replacing any move already there.

        Written to a temporary file and renamed, so an interrupted save leaves
        the previous version intact rather than a truncated one. Losing last
        week's move to a half-written file is not recoverable.
        """
        if len(move.waypoints) < 2:
            raise LibraryError("a move needs at least two waypoints to be worth saving")
        stem = safe_stem(name)
        path = self._path(stem)
        self.root.mkdir(parents=True, exist_ok=True)

        payload = move.to_dict()
        payload["name"] = stem
        payload["saved_at"] = time.time()

        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return self._entry(path, payload)

    def load(self, name: str) -> moves.Move:
        path = self._path(name)
        if not path.exists():
            raise LibraryError(f"no move called {safe_stem(name)!r} in the library")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise LibraryError(f"{path.name} is not readable JSON: {exc}") from None
        return moves.Move.from_dict(payload)

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if not path.exists():
            return False
        path.unlink()
        return True

    def list(self) -> list[Entry]:
        """Every saved move, newest first.

        A file that will not parse is skipped rather than raising: one corrupt
        move must not make the whole library unopenable, which is exactly when
        an operator needs the other nine.
        """
        if not self.root.exists():
            return []
        out: list[Entry] = []
        for path in sorted(self.root.glob("*" + SUFFIX)):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                out.append(self._entry(path, payload))
            except (ValueError, OSError):
                continue
        out.sort(key=lambda e: e.saved_at, reverse=True)
        return out

    @staticmethod
    def _entry(path: Path, payload: dict) -> Entry:
        move = moves.Move.from_dict(payload)
        stem = path.name[: -len(SUFFIX)]
        return Entry(
            name=str(payload.get("name") or stem),
            stem=stem,
            waypoints=len(move.waypoints),
            duration=move.total_duration,
            setup=move.setup_summary(),
            saved_at=float(payload.get("saved_at") or path.stat().st_mtime),
        )
