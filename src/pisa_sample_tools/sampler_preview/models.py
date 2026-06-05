from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SamplerTestError(ValueError):
    """Raised for user-facing sampler preview failures."""


@dataclass(frozen=True)
class SamplePreview:
    index: int
    id: str | None
    params: dict[str, Any]
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class SamplerPreviewResult:
    source_file: Path
    source_type: str
    sampler_name: str
    total_samples: int | None
    samples: list[SamplePreview]

    @property
    def generated_samples(self) -> int:
        return len(self.samples)

