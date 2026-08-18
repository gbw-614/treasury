from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from .common import BeverageCategory, ContractModel
from .requests import ExplicitExpectedValues
from .results import AnalysisResponse
from .sources import CatalogProvenance


class AnalysisStatus(StrEnum):
    QUEUED = "queued"
    ANALYZING = "analyzing"
    COMPLETE = "complete"
    ERROR = "error"


class DecisionStatus(StrEnum):
    AWAITING_ANALYSIS = "awaiting_analysis"
    READY_FOR_DECISION = "ready_for_decision"
    PASS = "pass"
    FAIL = "fail"
    NEEDS_REVIEW = "needs_review"


class ProcessingStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETE = "complete"
    ERROR = "error"


class CaseOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NEEDS_REVIEW = "needs_review"


class DecisionSource(StrEnum):
    AUTOMATED = "automated"
    HUMAN_CONFIRMED = "human_confirmed"
    HUMAN_OVERRIDDEN = "human_overridden"


class CasePanel(ContractModel):
    panel_id: str = "p01"
    file: str
    url: str
    sha256: str
    mime_type: Literal["image/jpeg", "image/png"]
    width: int
    height: int


class CaseSummary(ContractModel):
    case_id: str
    display_name: str
    created_at: datetime
    updated_at: datetime
    category: BeverageCategory
    expected: ExplicitExpectedValues
    panel: CasePanel
    panels: tuple[CasePanel, ...] = ()
    processing_status: ProcessingStatus
    analysis_status: AnalysisStatus
    decision_status: DecisionStatus
    automated_outcome: CaseOutcome | None
    outcome: CaseOutcome | None
    decision_source: DecisionSource | None
    error_message: str | None
    review_note: str | None
    reviewed_at: datetime | None
    # These are deliberately snapshots of the authenticated actor.  The auth
    # system owns the user records; retaining both values makes old audit rows
    # readable even if a username is later changed.
    created_by_user_id: str | None = None
    created_by_username: str | None = None
    assigned_to_user_id: str | None = None
    assigned_to_username: str | None = None
    reviewed_by_user_id: str | None = None
    reviewed_by_username: str | None = None
    source: CatalogProvenance | None = None
    duplicate_image_count: int = Field(default=0, ge=0)
    removed_at: datetime | None


class CaseDetail(CaseSummary):
    analysis: AnalysisResponse | None


class CaseListResponse(ContractModel):
    schema_version: Literal["case-queue-v1"]
    cases: tuple[CaseSummary, ...]


class ClearQueueResponse(ContractModel):
    schema_version: Literal["case-queue-clear-v1"]
    cleared_cases: Annotated[int, Field(ge=0)]


class HumanDecisionRequest(ContractModel):
    outcome: Literal[CaseOutcome.PASS, CaseOutcome.FAIL]
    note: Annotated[str, Field(max_length=2000)] = ""
    # Kept as an ignored compatibility field for clients from queue-v1.  The
    # authenticated session, not request content, supplies reviewer identity.
    reviewer: Annotated[str | None, Field(min_length=1, max_length=120)] = None
