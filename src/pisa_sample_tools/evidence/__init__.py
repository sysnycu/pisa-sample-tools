import os

os.environ.setdefault("MPLCONFIGDIR", "/tmp/pisa-matplotlib")
os.environ.setdefault("MPLBACKEND", "Agg")

from .models import (
    AnalysisSpec,
    DatasetSpec,
    EvidenceError,
    EvidenceResult,
    MetricBinding,
    RunRecord,
    SelectedCase,
)
from .service import build_evidence
from .spec import load_analysis_spec

__all__ = [
    "AnalysisSpec",
    "DatasetSpec",
    "EvidenceError",
    "EvidenceResult",
    "MetricBinding",
    "RunRecord",
    "SelectedCase",
    "build_evidence",
    "load_analysis_spec",
]
