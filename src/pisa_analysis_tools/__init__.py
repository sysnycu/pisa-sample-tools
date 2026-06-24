"""Compatibility namespace for the renamed PISA analysis distribution."""

from pisa_sample_tools.evidence import (
    AnalysisSpec,
    DatasetSpec,
    DerivedParameter,
    EvidenceError,
    EvidenceResult,
    MetricBinding,
    RunRecord,
    SelectedCase,
    build_evidence,
    load_analysis_spec,
    validate_evidence_inputs,
)

__all__ = [
    "AnalysisSpec",
    "DatasetSpec",
    "DerivedParameter",
    "EvidenceError",
    "EvidenceResult",
    "MetricBinding",
    "RunRecord",
    "SelectedCase",
    "build_evidence",
    "load_analysis_spec",
    "validate_evidence_inputs",
]
