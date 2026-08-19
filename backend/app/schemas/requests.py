from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.field_library import get_field_definition

from .common import BeverageCategory, ContractModel, migrate_legacy_reader_mode

ShortText = Annotated[str, Field(min_length=1, max_length=250)]


class ExpectedValues(ContractModel):
    brand_name: ShortText | None = None
    class_type: ShortText | None = None
    abv_percent: Annotated[Decimal, Field(ge=0, le=100)] | None = None
    proof: Annotated[Decimal, Field(ge=0, le=200)] | None = None


class ExpectedGovernmentWarning(ContractModel):
    heading: ShortText
    body: Annotated[str, Field(min_length=1, max_length=2000)]


class AdditionalExpectedField(ContractModel):
    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,59}$")]
    label: ShortText
    expected_text: Annotated[str, Field(min_length=1, max_length=1000)]
    match_mode: Literal["literal_phrase"] = "literal_phrase"


class ExplicitExpectedValues(ExpectedValues):
    government_warning: ExpectedGovernmentWarning | None = None
    additional_fields: tuple[AdditionalExpectedField, ...] = ()

    @model_validator(mode="after")
    def validate_additional_fields(self) -> ExplicitExpectedValues:
        if len(self.additional_fields) > 20:
            raise ValueError("expected.additionalFields supports at most 20 entries")
        ids = [field.id for field in self.additional_fields]
        labels = [field.label.strip().casefold() for field in self.additional_fields]
        if len(ids) != len(set(ids)):
            raise ValueError("every additional field id must be unique")
        if len(labels) != len(set(labels)):
            raise ValueError("every additional field label must be unique")
        return self


class PanelInput(ContractModel):
    panel_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")]
    file: ShortText


class FieldCheck(ContractModel):
    """A case selection from the versioned server-side field library."""

    field_id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,59}$")]
    required: bool = True
    expected_value: Annotated[str, Field(min_length=1, max_length=1000)] | None = None

    @model_validator(mode="after")
    def validate_field_library_reference(self) -> FieldCheck:
        try:
            definition = get_field_definition(self.field_id)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if definition.check_type == "text" and self.required and self.expected_value is None:
            raise ValueError("required text checks require expectedValue")
        return self


class AnalysisRequest(ContractModel):
    schema_version: Literal["verification-request-v1", "verification-request-v2"]
    # A caller-provided operational identifier, such as a COLA number. It is
    # intentionally not used by extraction or matching rules.
    case_reference: ShortText | None = None
    # Category remains available for v1 and metadata, but v2 checks do not
    # use a beverage-category rule engine.
    category: BeverageCategory | None = None
    expected: ExplicitExpectedValues | None = None
    checks: tuple[FieldCheck, ...] = ()
    panels: tuple[PanelInput, ...]
    reader_mode: Literal["ocr", "llm"] = "llm"

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_routing_facts(cls, value: object) -> object:
        """Read old queued/catalog requests while dropping retired options."""
        if isinstance(value, dict) and "facts" in value:
            value = {key: item for key, item in value.items() if key != "facts"}
        # Historical queued work used the dual-reader mode. New work has
        # exactly one authoritative reader; retain the LLM path.
        return migrate_legacy_reader_mode(value)

    @model_validator(mode="after")
    def validate_panels(self) -> AnalysisRequest:
        if not 1 <= len(self.panels) <= 6:
            raise ValueError("verification-request-v1 requires between one and six panels")
        panel_ids = [panel.panel_id for panel in self.panels]
        if len(panel_ids) != len(set(panel_ids)):
            raise ValueError("every panelId must be unique within a label set")
        if self.schema_version == "verification-request-v1" and self.expected is None:
            raise ValueError("verification-request-v1 requires expected")
        if self.schema_version == "verification-request-v2":
            if self.expected is not None:
                raise ValueError("verification-request-v2 uses checks, not expected")
            if not self.checks:
                raise ValueError("verification-request-v2 requires at least one check")
            ids = [check.field_id for check in self.checks]
            if len(ids) != len(set(ids)):
                raise ValueError("every check fieldId must be unique")
        return self
