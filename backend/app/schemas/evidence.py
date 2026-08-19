from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .common import BoundingBox, ContractModel, FieldKey


class OcrToken(ContractModel):
    token_id: str
    text: str
    bounding_box: BoundingBox
    confidence: Annotated[float, Field(ge=0, le=100)]
    block_id: str
    line_id: str
    reading_order: Annotated[int, Field(ge=0)]


class OcrRun(ContractModel):
    schema_version: Literal["ocr-geometry-v1"]
    adapter_version: str
    engine: str
    engine_version: str
    language_data: str
    executable: str
    preprocessing_version: str
    panel_id: str
    source_sha256: str
    original_width: Annotated[int, Field(gt=0)]
    original_height: Annotated[int, Field(gt=0)]
    transcript: str
    tokens: tuple[OcrToken, ...]
    exit_status: int
    warnings: tuple[str, ...]
    preprocessing_duration_ms: Annotated[float, Field(ge=0)]
    ocr_duration_ms: Annotated[float, Field(ge=0)]

    @model_validator(mode="after")
    def validate_token_geometry(self) -> OcrRun:
        token_ids = [token.token_id for token in self.tokens]
        if len(token_ids) != len(set(token_ids)):
            raise ValueError("OCR token IDs must be unique")
        for token in self.tokens:
            box = token.bounding_box
            if box.x + box.width > self.original_width:
                raise ValueError(f"OCR token {token.token_id} exceeds panel width")
            if box.y + box.height > self.original_height:
                raise ValueError(f"OCR token {token.token_id} exceeds panel height")
        return self


class LocationStatus(StrEnum):
    LOCATED = "located"
    AMBIGUOUS = "location_ambiguous"
    UNAVAILABLE = "location_unavailable"


class LocalizationResult(ContractModel):
    localization_id: str
    field_key: FieldKey
    quote: str
    panel_id: str
    status: LocationStatus
    accepted_token_ids: tuple[str, ...] = ()
    display_boxes: tuple[BoundingBox, ...] = ()
    similarity: Annotated[float, Field(ge=0, le=1)] | None = None
    runner_up_similarity: Annotated[float, Field(ge=0, le=1)] | None = None
    reason: str

    @model_validator(mode="after")
    def validate_display_evidence(self) -> LocalizationResult:
        if self.status == LocationStatus.LOCATED:
            if not self.accepted_token_ids or not self.display_boxes:
                raise ValueError("located evidence requires OCR token IDs and boxes")
            if len(self.accepted_token_ids) != len(self.display_boxes):
                raise ValueError(
                    "every displayed box must be backed by one OCR token ID"
                )
        elif self.accepted_token_ids or self.display_boxes:
            raise ValueError("ambiguous or unavailable evidence cannot expose boxes")
        return self


class ValidatedPanel(ContractModel):
    panel_id: str
    source_sha256: str
    detected_mime_type: Literal["image/jpeg", "image/png"]
    width: Annotated[int, Field(gt=0)]
    height: Annotated[int, Field(gt=0)]
    exif_orientation: int | None
    applied_transform: str
    decoded_pixel_count: Annotated[int, Field(gt=0)]
    original_byte_count: Annotated[int, Field(gt=0)]
    preprocessing_version: str
