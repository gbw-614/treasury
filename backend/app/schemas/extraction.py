from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .common import BoundingBox, ContractModel, FieldKey


class VisionFieldCandidate(ContractModel):
    field_key: FieldKey
    raw_text: str
    normalized_value: str | float | None
    evidence_quote: str
    model_bounding_box: BoundingBox | None = None
    panel_id: str
    legibility: Literal["clear", "uncertain", "unreadable"]
    uncertainty: str | None = None


class VisionTextBlock(ContractModel):
    """One literal, positioned text block returned in reading order."""

    block_id: str
    text: str
    model_bounding_box: BoundingBox | None = None
    reading_order: Annotated[int, Field(ge=0)]
    legibility: Literal["clear", "uncertain", "unreadable"]
    uncertainty: str | None = None


class VisionWarningPresentation(ContractModel):
    """Visual properties that cannot be established by a text transcript alone."""

    heading_all_caps: bool | None
    heading_only_bold: bool | None
    continuous_paragraph: bool | None
    separate_and_apart: bool | None
    legible_contrast: bool | None
    text_appears_unusually_small: bool | None
    uncertainty: str | None = None

    @model_validator(mode="before")
    @classmethod
    def migrate_heading_bold(cls, value: object) -> object:
        """Load cached v4 model output after the more precise field rename."""
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        old_key = "headingBold" if "headingBold" in migrated else "heading_bold"
        new_key = "headingOnlyBold" if "headingBold" in migrated else "heading_only_bold"
        if old_key in migrated and new_key not in migrated:
            migrated[new_key] = migrated.pop(old_key)
        return migrated


class VisionPanelExtraction(ContractModel):
    panel_id: str
    full_text: str
    # `fields` remains only for cached pre-v8 extractions. New calls return
    # literal blocks rather than semantic brand/class/value guesses.
    fields: tuple[VisionFieldCandidate, ...] = ()
    text_blocks: tuple[VisionTextBlock, ...] = ()
    warning_presentation: VisionWarningPresentation | None = None
    observations: tuple[str, ...] = ()


class VisionRun(ContractModel):
    schema_version: Literal["vision-extraction-v1"]
    prompt_version: str
    provider: str
    requested_model: str
    response_model: str
    provider_request_id: str
    raw_response_sha256: str
    panels: tuple[VisionPanelExtraction, ...]
    duration_ms: Annotated[float, Field(ge=0)]
    input_tokens: Annotated[int, Field(ge=0)] | None = None
    output_tokens: Annotated[int, Field(ge=0)] | None = None
    estimated_cost_usd: Annotated[float, Field(ge=0)] | None = None
