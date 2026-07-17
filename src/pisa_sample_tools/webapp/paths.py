from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from .errors import APIError


class PathPolicy:
    """Resolve user-provided paths without allowing them to escape configured roots."""

    def __init__(self, roots: Iterable[Path]) -> None:
        resolved = []
        for root in roots:
            candidate = Path(root).expanduser().resolve()
            if candidate not in resolved:
                resolved.append(candidate)
        if not resolved:
            resolved.append(Path.cwd().resolve())
        self.roots = tuple(resolved)

    def resolve(
        self,
        value: str | Path,
        *,
        field: str = "path",
        must_exist: bool = True,
        kind: str = "any",
        suffixes: set[str] | None = None,
    ) -> Path:
        raw = Path(value).expanduser()
        candidate = (Path.cwd() / raw).resolve() if not raw.is_absolute() else raw.resolve()
        if not any(candidate == root or candidate.is_relative_to(root) for root in self.roots):
            raise APIError(
                403,
                "path_not_allowed",
                f"{field} is outside the configured roots",
                field=field,
                details={"path": str(candidate), "roots": [str(root) for root in self.roots]},
            )
        if must_exist and not candidate.exists():
            raise APIError(
                404,
                "path_not_found",
                f"{field} does not exist",
                field=field,
                details={"path": str(candidate)},
            )
        if candidate.exists():
            if kind == "file" and not candidate.is_file():
                raise APIError(400, "path_not_file", f"{field} must be a file", field=field)
            if kind == "directory" and not candidate.is_dir():
                raise APIError(
                    400, "path_not_directory", f"{field} must be a directory", field=field
                )
        if suffixes is not None and candidate.suffix.lower() not in suffixes:
            raise APIError(
                400,
                "file_type_not_allowed",
                f"{field} has an unsupported file type",
                field=field,
                details={"allowed": sorted(suffixes), "suffix": candidate.suffix.lower()},
            )
        return candidate

    def relative_asset(self, root: Path, value: str, *, suffixes: set[str] | None = None) -> Path:
        if not value or Path(value).is_absolute():
            raise APIError(
                400,
                "invalid_artifact_path",
                "artifact_path must be a non-empty relative path",
                field="artifact_path",
            )
        target = (root / value).resolve()
        resolved_root = root.resolve()
        if not target.is_relative_to(resolved_root):
            raise APIError(
                403,
                "path_not_allowed",
                "artifact_path escapes the report directory",
                field="artifact_path",
            )
        if not target.is_file():
            raise APIError(
                404,
                "artifact_not_found",
                "report artifact does not exist",
                field="artifact_path",
            )
        if suffixes is not None and target.suffix.lower() not in suffixes:
            raise APIError(
                400,
                "file_type_not_allowed",
                "artifact has an unsupported file type",
                field="artifact_path",
                details={"allowed": sorted(suffixes), "suffix": target.suffix.lower()},
            )
        return target

