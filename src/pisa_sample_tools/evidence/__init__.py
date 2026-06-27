import os

os.environ.setdefault("MPLCONFIGDIR", "/tmp/pisa-matplotlib")
os.environ.setdefault("MPLBACKEND", "Agg")

from .concrete_compare import align_numeric_series, build_concrete_comparison_groups
from .models import (
    AnalysisSpec,
    AxisRule,
    ComparisonDetailSpec,
    ConcreteComparisonGroup,
    DatasetSpec,
    DerivedParameter,
    EvidenceError,
    EvidenceResult,
    MetricBinding,
    RunRecord,
    SelectedCase,
)
from .service import build_evidence, validate_evidence_inputs
from .spec import load_analysis_spec

__all__ = [
    "AnalysisSpec",
    "AxisRule",
    "ComparisonDetailSpec",
    "ConcreteComparisonGroup",
    "DatasetSpec",
    "DerivedParameter",
    "EvidenceError",
    "EvidenceResult",
    "MetricBinding",
    "RunRecord",
    "SelectedCase",
    "build_evidence",
    "align_numeric_series",
    "build_concrete_comparison_groups",
    "validate_evidence_inputs",
    "load_analysis_spec",
]
