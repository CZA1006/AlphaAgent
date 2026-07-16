"""Artifact storage boundary with a byte-compatible local implementation."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal, Protocol

ArtifactKind = Literal[
    "validations",
    "promoted",
    "trails",
    "autonomous_runs",
    "research_tasks",
    "combinations",
]


class ArtifactStore(Protocol):
    """Storage contract for typed research artifacts."""

    def write(self, kind: ArtifactKind, artifact_id: str, payload: dict[str, Any]) -> Path: ...

    def read(self, kind: ArtifactKind, artifact_id: str) -> dict[str, Any] | None: ...

    def list(self, kind: ArtifactKind) -> list[dict[str, Any]]: ...


class LocalArtifactStore:
    """Persist artifacts under today's ``artifacts/<kind>`` layout."""

    def __init__(
        self,
        root: Path | str = "artifacts",
        *,
        directories: dict[ArtifactKind, Path | str] | None = None,
    ) -> None:
        self.root = Path(root)
        self._directories = {
            kind: Path(directory) for kind, directory in (directories or {}).items()
        }

    @classmethod
    def for_directory(
        cls,
        kind: ArtifactKind,
        directory: Path | str,
    ) -> LocalArtifactStore:
        path = Path(directory)
        return cls(path.parent, directories={kind: path})

    def directory(self, kind: ArtifactKind) -> Path:
        return self._directories.get(kind, self.root / kind)

    def path(self, kind: ArtifactKind, artifact_id: str) -> Path:
        if not artifact_id or Path(artifact_id).name != artifact_id:
            raise ValueError(f"invalid artifact id: {artifact_id!r}")
        return self.directory(kind) / f"{artifact_id}.json"

    def write(self, kind: ArtifactKind, artifact_id: str, payload: dict[str, Any]) -> Path:
        path = self.path(kind, artifact_id)
        self._atomic_write_json(path, payload)
        return path

    def read(self, kind: ArtifactKind, artifact_id: str) -> dict[str, Any] | None:
        path = self.path(kind, artifact_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def list(self, kind: ArtifactKind) -> list[dict[str, Any]]:
        directory = self.directory(kind)
        index = directory / "_index.jsonl"
        if index.is_file():
            rows: list[dict[str, Any]] = []
            for line in index.read_text(encoding="utf-8").splitlines():
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    rows.append(payload)
            return rows
        if kind != "autonomous_runs":
            return []
        rows = []
        for path in sorted(directory.glob("*.json")):
            payload = self.read(kind, path.stem)
            if payload is not None:
                rows.append(payload)
        return rows

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=True, default=str)
                fh.write("\n")
            os.replace(tmp_name, path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
