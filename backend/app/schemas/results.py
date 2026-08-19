from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .common import ContractModel, FieldKey, StageDuration
from .evidence import LocalizationResult, OcrRun, ValidatedPanel
from .extraction import VisionRun


class AutomatedStatus(StrEnum):
    MATCHES = "matches"
    DISCREPANCY = "discrepancy"
    REVIEW = "review"
    DOES_NOT_APPLY = "does_not_apply"


class OverallSummary(StrEnum):
    NO_AUTOMATED_DISCREPANCY = "no_automated_discrepancy_detected"
    NEEDS_REVIEW = "needs_review"
    AUTOMATED_DISCREPANCY = "automated_discrepancy_detected"


class RuleResult(ContractModel):
    rule_id: str
    rule_version: str
    field_key: FieldKey
    applicable: bool
    automated_status: AutomatedStatus
    expected_value: str | float | None
    detected_value: str | float | None
    evidence_quote: str | None
    localization_ids: tuple[str, ...] = ()
    facts_consumed: tuple[str, ...] = ()
    reason_code: str
    explanation: str
    requires_human_review: bool

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_reader_agreement(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        return {
            key: item
            for key, item in value.items()
            if key not in {"readerAgreement", "reader_agreement"}
        }


class AdditionalRuleResult(ContractModel):
    field_id: str
    label: str
    match_mode: Literal["literal_phrase", "text", "regex", "warning"] = "literal_phrase"
    automated_status: AutomatedStatus
    expected_value: str | None
    detected_value: str | None
    evidence_quote: str | None
    evidence_block_ids: tuple[str, ...] = ()
    reason_code: str
    explanation: str
    requires_human_review: bool


class ReviewTask(ContractModel):
    field_key: FieldKey
    reason_code: str
    message: str
    localization_ids: tuple[str, ...] = ()


class AnalysisResponse(ContractModel):
    schema_version: Literal["analysis-response-v1"]
    analysis_id: str
    mode: Literal["connected"]
    reader_mode: Literal["ocr", "llm"] = "llm"
    overall_summary: OverallSummary
    panels: tuple[ValidatedPanel, ...]
    vision_run: VisionRun
    ocr_run: OcrRun
    # `ocrRun` is retained as the first-panel compatibility view. New clients
    # use `ocrRuns`, which contains exactly one run for each submitted panel.
    ocr_runs: tuple[OcrRun, ...] = ()
    localizations: tuple[LocalizationResult, ...]
    rule_results: tuple[RuleResult, ...]
    additional_rule_results: tuple[AdditionalRuleResult, ...] = ()
    review_tasks: tuple[ReviewTask, ...]
    stage_durations: tuple[StageDuration, ...]
    total_duration_ms: Annotated[float, Field(ge=0)]

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_reader_mode(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        key = "readerMode" if "readerMode" in migrated else "reader_mode"
        if migrated.get(key) == "both":
            migrated[key] = "llm"
        return migrated

    @model_validator(mode="after")
    def validate_evidence_references(self) -> AnalysisResponse:
        panel_ids = {panel.panel_id for panel in self.panels}
        ocr_runs = self.ocr_runs or (self.ocr_run,)
        if self.ocr_run.panel_id not in panel_ids:
            raise ValueError("OCR run references an unknown panel")
        if {run.panel_id for run in ocr_runs} != panel_ids:
            raise ValueError("OCR runs must cover every submitted panel exactly once")
        token_ids = {
            token.token_id
            for run in ocr_runs
            for token in run.tokens
        }
        if len(token_ids) != sum(len(run.tokens) for run in ocr_runs):
            raise ValueError("OCR token IDs must be unique across a label set")
        localization_ids: set[str] = set()
        for location in self.localizations:
            if location.localization_id in localization_ids:
                raise ValueError("localization IDs must be unique")
            localization_ids.add(location.localization_id)
            if location.panel_id not in panel_ids:
                raise ValueError("localization references an unknown panel")
            if not set(location.accepted_token_ids).issubset(token_ids):
                raise ValueError("localization references unknown OCR token IDs")
        for rule in self.rule_results:
            if not set(rule.localization_ids).issubset(localization_ids):
                raise ValueError("rule references unknown localization IDs")
        return self


class HealthResponse(ContractModel):
    status: Literal["ok"]


class VersionResponse(ContractModel):
    service: Literal["ttb-label-verification-api"]
    version: str
    analysis_schema_version: Literal["analysis-response-v1"]


class CapabilitiesResponse(ContractModel):
    """Server-side reader capabilities safe to expose to the web client."""

    vision_available: bool
    available_reader_modes: tuple[Literal["ocr", "llm"], ...]
