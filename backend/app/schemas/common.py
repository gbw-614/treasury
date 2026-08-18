from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class ContractModel(BaseModel):
    """Strict, immutable base for every versioned service contract."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class BeverageCategory(StrEnum):
    WINE = "wine"
    DISTILLED_SPIRITS = "distilled_spirits"
    MALT_BEVERAGE = "malt_beverage"


class FieldKey(StrEnum):
    BRAND_NAME = "brand_name"
    CLASS_TYPE = "class_type"
    ALCOHOL_CONTENT = "alcohol_content"
    PROOF = "proof"
    GOVERNMENT_WARNING_HEADING = "government_warning_heading"
    GOVERNMENT_WARNING_BODY = "government_warning_body"


class BoundingBox(ContractModel):
    x: Annotated[int, Field(ge=0)]
    y: Annotated[int, Field(ge=0)]
    width: Annotated[int, Field(gt=0)]
    height: Annotated[int, Field(gt=0)]


class StageDuration(ContractModel):
    stage: Annotated[str, Field(min_length=1, max_length=100)]
    milliseconds: Annotated[float, Field(ge=0)]
