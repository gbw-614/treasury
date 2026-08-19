from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .common import BeverageCategory, ContractModel

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ObjectKey = Annotated[str, Field(min_length=1, max_length=500)]


class SourceObject(ContractModel):
    key: ObjectKey
    sha256: Sha256
    bytes: Annotated[int, Field(ge=1, le=10 * 1024 * 1024)]

    @field_validator("key")
    @classmethod
    def safe_key(cls, value: str) -> str:
        # Object keys are resolved only below the catalog's configured prefix.
        # This prevents a public manifest from turning this service into a URL
        # fetcher or escaping into a neighbouring S3 prefix.
        if value.startswith("/") or ".." in value.split("/") or "://" in value:
            raise ValueError("object key must be a relative, non-traversing path")
        return value


class SourcePanel(SourceObject):
    panel_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")]


class SourceCatalogCase(ContractModel):
    source_case_id: Annotated[str, Field(min_length=1, max_length=120)]
    # A stable, operator-facing reference from the source catalogue. It is
    # copied into the imported application request and never used for matching.
    case_reference: Annotated[str | None, Field(min_length=1, max_length=250)] = None
    display_name: Annotated[str | None, Field(max_length=250)] = None
    category: BeverageCategory | None = None
    application: SourceObject
    panels: tuple[SourcePanel, ...]

    @model_validator(mode="after")
    def unique_panels(self) -> SourceCatalogCase:
        panel_ids = [panel.panel_id for panel in self.panels]
        if not 1 <= len(panel_ids) <= 6:
            raise ValueError("catalog case requires between one and six panels")
        if len(panel_ids) != len(set(panel_ids)):
            raise ValueError("catalog panel IDs must be unique")
        return self


class SourceCatalog(ContractModel):
    # v2 carries the field-library version used by its application requests.
    # Keep v1 readable so already published catalogues remain importable.
    schema_version: Literal["verification-source-catalog-v1", "verification-source-catalog-v2"]
    catalog_version: Annotated[str, Field(min_length=1, max_length=120)]
    field_library_version: Annotated[str | None, Field(min_length=1, max_length=120)] = None
    cases: tuple[SourceCatalogCase, ...]

    @model_validator(mode="after")
    def unique_cases(self) -> SourceCatalog:
        if self.schema_version == "verification-source-catalog-v2" and not self.field_library_version:
            raise ValueError("v2 catalog requires fieldLibraryVersion")
        source_case_ids = [case.source_case_id for case in self.cases]
        if len(source_case_ids) != len(set(source_case_ids)):
            raise ValueError("catalog sourceCaseId values must be unique")
        if len(source_case_ids) > 1000:
            raise ValueError("catalog contains too many cases")
        return self


class CatalogProvenance(ContractModel):
    source_type: Literal["public_s3_catalog"] = "public_s3_catalog"
    catalog_url: str
    catalog_version: str
    source_case_id: str
    case_reference: str | None = None
    application: SourceObject
    panels: tuple[SourcePanel, ...]


class CatalogCaseAvailability(ContractModel):
    source_case_id: str
    display_name: str | None = None
    category: BeverageCategory | None = None
    panel_count: int
    already_imported_case_id: str | None = None


class SourceCatalogResponse(ContractModel):
    schema_version: Literal["verification-source-catalog-response-v1"] = "verification-source-catalog-response-v1"
    catalog_version: str
    cases: tuple[CatalogCaseAvailability, ...]


class SourceCatalogImportRequest(ContractModel):
    source_case_ids: Annotated[tuple[str, ...], Field(min_length=1, max_length=200)]
    auto_process: bool = True
    reader_mode: Literal["ocr", "llm"] = "llm"

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

    @field_validator("source_case_ids")
    @classmethod
    def unique_selected_cases(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("sourceCaseIds must be unique")
        return values


class SourceCatalogImportIssue(ContractModel):
    source_case_id: str
    code: str
    message: str


class SourceCatalogImportResponse(ContractModel):
    schema_version: Literal["verification-source-catalog-import-v1"] = "verification-source-catalog-import-v1"
    imported_case_ids: tuple[str, ...] = ()
    duplicate_case_ids: tuple[str, ...] = ()
    issues: tuple[SourceCatalogImportIssue, ...] = ()


class SourceCatalogImportJob(ContractModel):
    schema_version: Literal["verification-source-catalog-import-job-v1"] = "verification-source-catalog-import-job-v1"
    job_id: str
    status: Literal["queued", "running", "complete", "failed"]
    total_cases: int
    completed_cases: int
    imported_case_ids: tuple[str, ...] = ()
    duplicate_case_ids: tuple[str, ...] = ()
    issues: tuple[SourceCatalogImportIssue, ...] = ()
    error_message: str | None = None
    created_at: str
    updated_at: str
    finished_at: str | None = None
