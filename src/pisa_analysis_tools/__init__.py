"""Compatibility namespace for the renamed PISA analysis distribution."""

from pisa_sample_tools.evidence import (
    AnalysisSpec,
    DatasetSpec,
    EvidenceError,
    EvidenceResult,
    MetricBinding,
    RunRecord,
    SelectedCase,
    build_evidence,
    load_analysis_spec,
)

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
