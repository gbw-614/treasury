from __future__ import annotations

import pytest

from app.schemas import SourceCatalog
from app.services.s3_catalog import CatalogError, CatalogSource


def test_catalog_objects_stay_under_configured_origin_and_prefix() -> None:
    source = CatalogSource("https://example-bucket.s3.us-west-2.amazonaws.com/catalog/manifest.json")
    assert source.object_url("cases/a/application.json") == (
        "https://example-bucket.s3.us-west-2.amazonaws.com/catalog/cases/a/application.json"
    )
    for key in ("../other.json", "/other.json", "https://elsewhere.example/object.json"):
        with pytest.raises(CatalogError) as error:
            source.object_url(key)
        assert error.value.code == "invalid_object_key"


def test_catalog_requires_https_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERIFICATION_S3_CATALOG_URL", "http://catalog.example/manifest.json")
    with pytest.raises(CatalogError) as error:
        CatalogSource.configured()
    assert error.value.code == "invalid_catalog_url"


def test_v2_catalog_requires_and_accepts_field_library_version() -> None:
    payload = {
        "schemaVersion": "verification-source-catalog-v2",
        "catalogVersion": "test-v2",
        "fieldLibraryVersion": "v1",
        "cases": [],
    }
    assert SourceCatalog.model_validate(payload).field_library_version == "v1"

    payload.pop("fieldLibraryVersion")
    with pytest.raises(ValueError, match="fieldLibraryVersion"):
        SourceCatalog.model_validate(payload)
