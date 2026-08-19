from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app.main import SESSION_COOKIE_NAME, app
from app.schemas import (
    AnalysisResponse,
    BoundingBox,
    LocalizationResult,
    OcrRun,
    SourceCatalog,
    SourceObject,
    SourcePanel,
)
from app.services import auth_store, case_store, s3_catalog
from app.services.blind_request import build_blind_vision_request
from app.services.image_validation import validate_image
from scripts.export_contract_schemas import CONTRACTS, render_schema
from tests.mock_analysis import CANONICAL_WARNING_BODY, build_mock_analysis

client = TestClient(app)


@pytest.fixture(autouse=True)
def fixture_analysis_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def analyze_fixture(request, panels, contents, *, blanks):
        del contents
        return build_mock_analysis(request, panels[0], blank=blanks[0])

    monkeypatch.setattr("app.main.run_connected_analysis", analyze_fixture)


@pytest.fixture(autouse=True)
def fixture_authenticated_user(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The application API is session-protected; legacy contract tests log in."""
    monkeypatch.setattr("app.services.auth_store.DATA_ROOT", tmp_path)
    client.cookies.clear()
    auth_store.create_user("contract-test", "contract-test-password")
    token, _ = auth_store.authenticate("contract-test", "contract-test-password", force=False)  # type: ignore[misc]
    client.cookies.set(SESSION_COOKIE_NAME, token)
    yield
    client.cookies.clear()


def image_bytes(*, blank: bool = False, image_format: str = "PNG") -> bytes:
    image = Image.new("RGB", (320, 180), "white")
    if not blank:
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 20, 300, 160), fill="navy")
        draw.rectangle((40, 40, 280, 140), fill="gold")
    output = io.BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


def request_payload(
    *,
    brand_name: str = "Treasury Sample",
    class_type: str = "Bourbon Whisky",
) -> dict[str, object]:
    return {
        "schemaVersion": "verification-request-v1",
        "category": "distilled_spirits",
        "expected": {
            "brandName": brand_name,
            "classType": class_type,
            "abvPercent": 45,
            "proof": 90,
            "governmentWarning": {
                "heading": "GOVERNMENT WARNING:",
                "body": CANONICAL_WARNING_BODY,
            },
        },
        "panels": [{"panelId": "p01", "file": "display-only.png"}],
    }


def analyze(
    payload: dict[str, object],
    content: bytes,
    *,
    filename: str = "label.png",
    content_type: str = "application/octet-stream",
):
    return client.post(
        "/api/v1/analyses",
        data={"request": json.dumps(payload)},
        files={"panel": (filename, content, content_type)},
    )


def enqueue(
    payload: dict[str, object],
    content: bytes,
    *,
    auto_process: bool,
):
    return client.post(
        "/api/v1/cases",
        data={
            "request": json.dumps(payload),
            "autoProcess": str(auto_process).lower(),
            "displayName": "Queue test",
        },
        files={"panel": ("label.png", content, "image/png")},
    )


def enqueue_multiple(
    payload: dict[str, object],
    contents: list[bytes],
    *,
    auto_process: bool,
):
    return client.post(
        "/api/v1/cases",
        data={
            "request": json.dumps(payload),
            "autoProcess": str(auto_process).lower(),
            "displayName": "Multi-panel queue test",
        },
        files=[
            ("panels", (f"panel-{index + 1}.png", content, "image/png"))
            for index, content in enumerate(contents)
        ],
    )


def test_queue_preserves_additional_expected_label_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.services.case_store.DATA_ROOT", tmp_path)
    payload = request_payload()
    payload["expected"]["additionalFields"] = [  # type: ignore[index]
        {
            "id": "country-of-origin",
            "label": "Country of origin",
            "expectedText": "Product of Italy",
            "matchMode": "literal_phrase",
        }
    ]

    created = enqueue(payload, image_bytes(), auto_process=False)

    assert created.status_code == 201, created.text
    case = client.get(f"/api/v1/cases/{created.json()['caseId']}")
    assert case.status_code == 200
    assert case.json()["expected"]["additionalFields"] == [
        {
            "id": "country-of-origin",
            "label": "Country of origin",
            "expectedText": "Product of Italy",
            "matchMode": "literal_phrase",
        }
    ]


def test_queue_report_includes_case_reference_and_audit_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.services.case_store.DATA_ROOT", tmp_path)
    payload = request_payload()
    payload["caseReference"] = "COLA-TEST-00001"
    created = enqueue(payload, image_bytes(), auto_process=False)
    assert created.status_code == 201, created.text

    report = client.get("/api/v1/cases/report.csv")

    assert report.status_code == 200, report.text
    assert report.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=\"treasury-work-queue-" in report.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(report.text)))
    assert len(rows) == 1
    assert rows[0]["case_reference"] == "COLA-TEST-00001"
    assert rows[0]["artwork_files"] == "label.png"
    assert rows[0]["processing_status"] == "queued"
    assert rows[0]["human_reviewed"] == "false"


def test_catalog_import_accepts_and_serializes_v2_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.services.case_store.DATA_ROOT", tmp_path)
    image = image_bytes()
    application = json.dumps({
        "schemaVersion": "verification-request-v2",
        "checks": [
            {"fieldId": "brand_name", "required": True, "expectedValue": "V2 catalog brand"},
            {"fieldId": "alcohol_content", "required": True, "expectedValue": "45"},
            {"fieldId": "government_warning", "required": True},
        ],
        "panels": [{"panelId": "p01", "file": "label.png"}],
    }).encode()
    application_object = SourceObject(
        key="cases/v2/application.json", sha256=hashlib.sha256(application).hexdigest(), bytes=len(application)
    )
    panel_object = SourcePanel(
        panelId="p01", key="cases/v2/p01.png", sha256=hashlib.sha256(image).hexdigest(), bytes=len(image)
    )
    catalog = SourceCatalog.model_validate({
        "schemaVersion": "verification-source-catalog-v1", "catalogVersion": "v2-test",
        "cases": [{
            "sourceCaseId": "v2-case", "displayName": None,
            "application": application_object.model_dump(by_alias=True),
            "panels": [panel_object.model_dump(by_alias=True)],
        }],
    })
    source = s3_catalog.CatalogSource("https://catalog.example.test/catalog/v2.json")
    objects = {application_object.key: application, panel_object.key: image}
    monkeypatch.setattr(s3_catalog, "configured_catalog", lambda: (source, catalog))
    monkeypatch.setattr(s3_catalog.CatalogSource, "fetch_object", lambda _self, obj: objects[obj.key])

    imported = client.post("/api/v1/sources/s3/import", json={"sourceCaseIds": ["v2-case"], "autoProcess": False})

    assert imported.status_code == 201, imported.text
    case = client.get(f"/api/v1/cases/{imported.json()['importedCaseIds'][0]}")
    assert case.status_code == 200, case.text
    assert case.json()["requestSchemaVersion"] == "verification-request-v2"
    assert case.json()["expected"] is None
    assert case.json()["displayName"] == "V2 catalog brand"
    assert case.json()["checks"] == [
        {"fieldId": "brand_name", "required": True, "expectedValue": "V2 catalog brand"},
        {"fieldId": "alcohol_content", "required": True, "expectedValue": "45"},
        {"fieldId": "government_warning", "required": True, "expectedValue": None},
    ]


def test_catalog_import_snapshots_cases_assigns_uploader_and_reports_duplicates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.services.case_store.DATA_ROOT", tmp_path)
    image = image_bytes()
    application = json.dumps(request_payload(brand_name="Public catalog bourbon")).encode()
    application_object = SourceObject(
        key="cases/public-bourbon/application.json",
        sha256=hashlib.sha256(application).hexdigest(),
        bytes=len(application),
    )
    panel_object = SourcePanel(
        panelId="p01",
        key="cases/public-bourbon/p01.png",
        sha256=hashlib.sha256(image).hexdigest(),
        bytes=len(image),
    )
    catalog = SourceCatalog.model_validate({
        "schemaVersion": "verification-source-catalog-v1",
        "catalogVersion": "test-v1",
        "cases": [{
            "sourceCaseId": "public-bourbon",
            "caseReference": "COLA-TEST-00001",
            "displayName": "Public bourbon",
            "application": application_object.model_dump(by_alias=True),
            "panels": [panel_object.model_dump(by_alias=True)],
        }],
    })
    source = s3_catalog.CatalogSource("https://catalog.example.test/catalog/manifest.json")
    objects = {application_object.key: application, panel_object.key: image}
    monkeypatch.setattr(s3_catalog, "configured_catalog", lambda: (source, catalog))
    monkeypatch.setattr(s3_catalog.CatalogSource, "fetch_object", lambda _self, obj: objects[obj.key])

    catalog_response = client.get("/api/v1/sources/s3/catalog")
    assert catalog_response.status_code == 200, catalog_response.text
    assert catalog_response.json()["catalogVersion"] == "test-v1"
    assert catalog_response.json()["cases"][0]["alreadyImportedCaseId"] is None

    imported = client.post("/api/v1/sources/s3/import", json={
        "sourceCaseIds": ["public-bourbon", "missing"], "autoProcess": False,
        "readerMode": "ocr",
    })
    assert imported.status_code == 201, imported.text
    body = imported.json()
    assert len(body["importedCaseIds"]) == 1
    assert body["issues"] == [{
        "sourceCaseId": "missing", "code": "case_not_in_catalog",
        "message": "The selected case is not present in this catalog version.",
    }]
    case = client.get(f"/api/v1/cases/{body['importedCaseIds'][0]}").json()
    assert case["createdByUsername"] == "contract-test"
    assert case["assignedToUsername"] == "contract-test"
    assert case["source"]["sourceCaseId"] == "public-bourbon"
    assert case["source"]["caseReference"] == "COLA-TEST-00001"
    assert case["source"]["catalogVersion"] == "test-v1"
    assert case["caseReference"] == "COLA-TEST-00001"
    work = case_store.get_case_work(body["importedCaseIds"][0])
    assert work is not None
    assert work[0].reader_mode == "ocr"

    again = client.post("/api/v1/sources/s3/import", json={"sourceCaseIds": ["public-bourbon"]})
    assert again.status_code == 201
    assert again.json()["importedCaseIds"] == []
    assert again.json()["duplicateCaseIds"] == body["importedCaseIds"]
    assert client.get("/api/v1/sources/s3/catalog").json()["cases"][0]["alreadyImportedCaseId"] == body["importedCaseIds"][0]

    revised_catalog = catalog.model_copy(update={"catalog_version": "test-v2"})
    monkeypatch.setattr(s3_catalog, "configured_catalog", lambda: (source, revised_catalog))
    assert client.get("/api/v1/sources/s3/catalog").json()["cases"][0]["alreadyImportedCaseId"] == body["importedCaseIds"][0]


def test_catalog_auto_processing_starts_at_visible_top_of_queue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.services.case_store.DATA_ROOT", tmp_path)
    image = image_bytes()
    cases = []
    objects: dict[str, bytes] = {}
    for index in range(3):
        source_case_id = f"public-{index}"
        application = json.dumps(request_payload(brand_name=f"Public {index}"), sort_keys=True).encode()
        application_object = SourceObject(
            key=f"cases/{source_case_id}/application.json",
            sha256=hashlib.sha256(application).hexdigest(),
            bytes=len(application),
        )
        panel_object = SourcePanel(
            panelId="p01",
            key=f"cases/{source_case_id}/p01.png",
            sha256=hashlib.sha256(image).hexdigest(),
            bytes=len(image),
        )
        cases.append({
            "sourceCaseId": source_case_id,
            "displayName": f"Public {index}",
            "application": application_object.model_dump(by_alias=True),
            "panels": [panel_object.model_dump(by_alias=True)],
        })
        objects[application_object.key] = application
        objects[panel_object.key] = image

    catalog = SourceCatalog.model_validate({
        "schemaVersion": "verification-source-catalog-v1",
        "catalogVersion": "queue-order-v1",
        "cases": cases,
    })
    source = s3_catalog.CatalogSource("https://catalog.example.test/catalog/manifest.json")
    monkeypatch.setattr(s3_catalog, "configured_catalog", lambda: (source, catalog))
    monkeypatch.setattr(s3_catalog.CatalogSource, "fetch_object", lambda _self, obj: objects[obj.key])
    started: list[str] = []

    async def record_start(case_id: str) -> None:
        started.append(case_id)

    monkeypatch.setattr("app.main._process_claimed_case", record_start)
    response = client.post("/api/v1/sources/s3/import", json={
        "sourceCaseIds": ["public-0", "public-1", "public-2"],
        "autoProcess": True,
        "readerMode": "ocr",
    })

    assert response.status_code == 201, response.text
    imported_ids = response.json()["importedCaseIds"]
    assert started == list(reversed(imported_ids))
    listed_ids = [item["caseId"] for item in client.get("/api/v1/cases").json()["cases"]]
    assert listed_ids == list(reversed(imported_ids))


def test_catalog_import_job_persists_progress_and_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.services.case_store.DATA_ROOT", tmp_path)
    image = image_bytes()
    application = json.dumps(request_payload(brand_name="Async public case")).encode()
    application_object = SourceObject(
        key="cases/async/application.json",
        sha256=hashlib.sha256(application).hexdigest(),
        bytes=len(application),
    )
    panel_object = SourcePanel(
        panelId="p01",
        key="cases/async/p01.png",
        sha256=hashlib.sha256(image).hexdigest(),
        bytes=len(image),
    )
    catalog = SourceCatalog.model_validate({
        "schemaVersion": "verification-source-catalog-v1",
        "catalogVersion": "async-v1",
        "cases": [{
            "sourceCaseId": "async-case",
            "displayName": "Async public case",
            "application": application_object.model_dump(by_alias=True),
            "panels": [panel_object.model_dump(by_alias=True)],
        }],
    })
    source = s3_catalog.CatalogSource("https://catalog.example.test/catalog/manifest.json")
    objects = {application_object.key: application, panel_object.key: image}
    monkeypatch.setattr(s3_catalog, "configured_catalog", lambda: (source, catalog))
    monkeypatch.setattr(s3_catalog.CatalogSource, "fetch_object", lambda _self, obj: objects[obj.key])

    started = client.post("/api/v1/sources/s3/import-jobs", json={
        "sourceCaseIds": ["async-case", "missing"],
        "autoProcess": False,
        "readerMode": "ocr",
    })
    assert started.status_code == 202, started.text
    assert started.json()["status"] == "queued"

    progress = client.get(f"/api/v1/sources/s3/import-jobs/{started.json()['jobId']}")
    assert progress.status_code == 200, progress.text
    body = progress.json()
    assert body["status"] == "complete"
    assert body["completedCases"] == 2
    assert body["totalCases"] == 2
    assert len(body["importedCaseIds"]) == 1
    assert body["duplicateCaseIds"] == []
    assert body["issues"][0]["sourceCaseId"] == "missing"


def test_health_and_version_contracts() -> None:
    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    version = client.get("/api/v1/version")
    assert version.status_code == 200
    assert version.json() == {
        "service": "ttb-label-verification-api",
        "version": "0.2.0",
        "analysisSchemaVersion": "analysis-response-v1",
    }


def test_recognition_cache_management_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("app.services.case_store.DATA_ROOT", tmp_path)

    stats = client.get("/api/v1/recognition-cache")
    assert stats.status_code == 200
    assert stats.json() == {
        "schemaVersion": "recognition-cache-stats-v1",
        "ocrEntries": 0,
        "llmEntries": 0,
        "totalEntries": 0,
    }

    cleared = client.delete("/api/v1/recognition-cache")
    assert cleared.status_code == 200
    assert cleared.json() == {
        "schemaVersion": "recognition-cache-clear-v1",
        "clearedOcrEntries": 0,
        "clearedLlmEntries": 0,
        "clearedTotalEntries": 0,
    }


def test_capabilities_hide_vision_without_a_server_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.main.vision_available", lambda: False)

    response = client.get("/api/v1/capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "visionAvailable": False,
        "availableReaderModes": ["ocr"],
    }


def test_no_key_downgrades_a_stale_llm_reader_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.main.vision_available", lambda: False)
    payload = request_payload()
    payload["readerMode"] = "both"

    response = analyze(payload, image_bytes())

    assert response.status_code == 200, response.text
    assert response.json()["readerMode"] == "ocr"


def test_case_queue_persists_manual_scan_and_human_decision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.services.case_store.DATA_ROOT", tmp_path)

    created = enqueue(request_payload(), image_bytes(), auto_process=False)
    assert created.status_code == 201, created.text
    case = created.json()
    case_id = case["caseId"]
    assert case["processingStatus"] == "queued"
    assert case["outcome"] is None
    assert case["analysis"] is None

    listing = client.get("/api/v1/cases")
    assert listing.status_code == 200
    assert listing.json()["cases"][0]["caseId"] == case_id
    artwork = client.get(case["panel"]["url"])
    assert artwork.status_code == 200
    assert artwork.headers["content-type"] == "image/png"

    scan = client.post(f"/api/v1/cases/{case_id}/scan")
    assert scan.status_code == 202, scan.text
    completed = client.get(f"/api/v1/cases/{case_id}").json()
    assert completed["processingStatus"] == "complete"
    assert completed["automatedOutcome"] == "pass"
    assert completed["outcome"] == "pass"
    assert completed["decisionSource"] == "automated"
    assert completed["analysis"]["overallSummary"] == "no_automated_discrepancy_detected"

    decision = client.post(
        f"/api/v1/cases/{case_id}/decision",
        json={
            "outcome": "fail",
            "note": "Reviewer found an artwork mismatch",
            "reviewer": "Test reviewer",
        },
    )
    assert decision.status_code == 200, decision.text
    reviewed = decision.json()
    assert reviewed["outcome"] == "fail"
    assert reviewed["automatedOutcome"] == "pass"
    assert reviewed["decisionSource"] == "human_overridden"
    assert reviewed["reviewNote"] == "Reviewer found an artwork mismatch"


def test_auto_process_routes_uncertain_case_to_review(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.services.case_store.DATA_ROOT", tmp_path)
    created = enqueue(request_payload(), image_bytes(blank=True), auto_process=True)
    assert created.status_code == 201, created.text
    completed = client.get(f"/api/v1/cases/{created.json()['caseId']}").json()
    assert completed["processingStatus"] == "complete"
    assert completed["outcome"] == "needs_review"
    assert completed["decisionSource"] == "automated"


def test_legacy_catalog_facts_are_ignored_not_routed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.services.case_store.DATA_ROOT", tmp_path)
    payload = request_payload()
    payload["facts"] = {"origin": "unknown", "privateFormulaFact": "must-not-route"}

    response = enqueue(payload, image_bytes(), auto_process=True)

    assert response.status_code == 201, response.text
    body = client.get(f"/api/v1/cases/{response.json()['caseId']}").json()
    assert body["analysisStatus"] == "complete"
    assert body["decisionStatus"] == "pass"
    assert body["outcome"] == "pass"
    assert "routing" not in body
    assert "applicationDataAudit" not in body


def test_queue_persists_ordered_multi_panel_label_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.services.case_store.DATA_ROOT", tmp_path)
    payload = request_payload()
    payload["panels"] = [
        {"panelId": "p01", "file": "front.png"},
        {"panelId": "p02", "file": "back.png"},
    ]

    created = enqueue_multiple(payload, [image_bytes(), image_bytes(blank=True)], auto_process=False)

    assert created.status_code == 201, created.text
    case = created.json()
    assert [panel["panelId"] for panel in case["panels"]] == ["p01", "p02"]
    assert [panel["file"] for panel in case["panels"]] == ["panel-1.png", "panel-2.png"]
    assert case["panel"] == case["panels"][0]
    assert client.get(case["panels"][0]["url"]).status_code == 200
    assert client.get(case["panels"][1]["url"]).status_code == 200
    assert client.get(f"/api/v1/cases/{case['caseId']}/image/p99").status_code == 404


def test_queue_marks_duplicate_artwork_and_can_remove_a_case(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.services.case_store.DATA_ROOT", tmp_path)
    artwork = image_bytes()
    first = enqueue(request_payload(), artwork, auto_process=False)
    second = enqueue(request_payload(), artwork, auto_process=False)
    assert first.status_code == 201
    assert second.status_code == 201

    cases = client.get("/api/v1/cases").json()["cases"]
    assert len(cases) == 2
    assert {case["duplicateImageCount"] for case in cases} == {1}

    removed = client.delete(f"/api/v1/cases/{first.json()['caseId']}")
    assert removed.status_code == 200
    assert removed.json()["removedAt"] is not None
    assert client.get(f"/api/v1/cases/{first.json()['caseId']}").status_code == 200
    assert client.get(first.json()["panel"]["url"]).status_code == 200

    cases_after_removal = client.get("/api/v1/cases").json()["cases"]
    assert len(cases_after_removal) == 2
    assert {case["duplicateImageCount"] for case in cases_after_removal} == {0}

    restored = client.post(f"/api/v1/cases/{first.json()['caseId']}/restore")
    assert restored.status_code == 200
    assert restored.json()["removedAt"] is None
    restored_cases = client.get("/api/v1/cases").json()["cases"]
    assert {case["duplicateImageCount"] for case in restored_cases} == {1}


def test_clear_queue_permanently_removes_active_and_removed_cases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("app.services.case_store.DATA_ROOT", tmp_path)
    first = enqueue(request_payload(), image_bytes(), auto_process=False)
    second = enqueue(request_payload(), image_bytes(), auto_process=False)
    assert first.status_code == 201
    assert second.status_code == 201
    assert client.delete(f"/api/v1/cases/{first.json()['caseId']}").status_code == 200

    cleared = client.delete("/api/v1/cases")

    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == {
        "schemaVersion": "case-queue-clear-v1",
        "clearedCases": 2,
    }
    assert client.get("/api/v1/cases").json()["cases"] == []
    assert not (tmp_path / "cases" / first.json()["caseId"]).exists()
    assert not (tmp_path / "cases" / second.json()["caseId"]).exists()


def test_happy_path_returns_evidence_linked_matches() -> None:
    payload = request_payload()
    payload["readerMode"] = "ocr"
    response = analyze(payload, image_bytes())
    assert response.status_code == 200, response.text
    body = response.json()
    AnalysisResponse.model_validate(body)

    assert body["schemaVersion"] == "analysis-response-v1"
    assert body["mode"] == "connected"
    assert body["overallSummary"] == "no_automated_discrepancy_detected"
    assert body["panels"][0]["detectedMimeType"] == "image/png"
    assert body["panels"][0]["width"] == 320
    assert body["panels"][0]["height"] == 180
    assert body["reviewTasks"] == []

    applicable_rules = [rule for rule in body["ruleResults"] if rule["applicable"]]
    assert {rule["automatedStatus"] for rule in applicable_rules} == {"matches"}
    assert all("readerAgreement" not in rule for rule in applicable_rules)
    tokens = {token["tokenId"] for token in body["ocrRun"]["tokens"]}
    assert tokens
    for location in body["localizations"]:
        assert location["status"] == "located"
        assert location["acceptedTokenIds"]
        assert set(location["acceptedTokenIds"]).issubset(tokens)
        assert len(location["displayBoxes"]) == len(location["acceptedTokenIds"])


def test_compiled_web_application_is_served_with_spa_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    index = tmp_path / "index.html"
    index.write_text("<h1>Treasury</h1>", encoding="utf-8")
    monkeypatch.setattr("app.main.WEB_DIST", tmp_path)

    assert client.get("/").text == "<h1>Treasury</h1>"
    assert client.get("/review/case-123").text == "<h1>Treasury</h1>"
    assert client.get("/api/not-a-route").status_code == 404

def test_expected_value_mismatch_requires_review() -> None:
    response = analyze(
        request_payload(brand_name="Different Expected Brand"), image_bytes()
    )
    assert response.status_code == 200
    body = response.json()

    assert body["overallSummary"] == "needs_review"
    brand = next(
        rule for rule in body["ruleResults"] if rule["fieldKey"] == "brand_name"
    )
    assert brand["automatedStatus"] == "review"
    assert brand["expectedValue"] == "Different Expected Brand"
    assert brand["detectedValue"] == "Treasury Sample"
    assert brand["requiresHumanReview"] is True
    assert body["reviewTasks"][0]["fieldKey"] == "brand_name"


def test_blank_panel_returns_review_without_invented_boxes() -> None:
    response = analyze(request_payload(), image_bytes(blank=True))
    assert response.status_code == 200
    body = response.json()

    assert body["overallSummary"] == "needs_review"
    assert body["ocrRun"]["tokens"] == []
    assert body["visionRun"]["panels"][0]["fields"] == []
    assert body["reviewTasks"]
    for location in body["localizations"]:
        assert location["status"] == "location_unavailable"
        assert location["acceptedTokenIds"] == []
        assert location["displayBoxes"] == []


def test_invalid_upload_is_rejected_using_decoded_content() -> None:
    response = analyze(
        request_payload(),
        b"not an image",
        filename="looks-valid.png",
        content_type="image/png",
    )
    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "invalid_image"


def test_request_contract_rejects_unknown_fields_and_panel_count_mismatches() -> None:
    unknown = request_payload()
    unknown["unexpected"] = True
    unknown_response = analyze(unknown, image_bytes())
    assert unknown_response.status_code == 422
    assert "extra_forbidden" in unknown_response.text

    multiple = request_payload()
    multiple["panels"] = [
        {"panelId": "p01", "file": "one.png"},
        {"panelId": "p02", "file": "two.png"},
    ]
    multiple_response = analyze(multiple, image_bytes())
    assert multiple_response.status_code == 422
    assert "panel_count_mismatch" in multiple_response.text


def test_blind_provider_payload_cannot_include_expected_values_or_filename() -> None:
    content = image_bytes()
    panel, _ = validate_image(content, "p01")
    provider_payload = build_blind_vision_request(panel, content)
    serialized = json.dumps(provider_payload)

    assert "Secret Expected Brand" not in serialized
    assert "Bourbon Whisky" not in serialized
    assert "do-not-leak.png" not in serialized
    assert "filename" not in serialized.casefold()
    assert "expected" not in {
        str(key).casefold()
        for key in provider_payload
    }
    assert panel.source_sha256 in serialized


def test_checked_in_json_schemas_match_pydantic_contracts() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    for filename, model in CONTRACTS.items():
        checked_in = (repository_root / "schemas" / filename).read_text()
        assert checked_in == render_schema(filename, model)


def test_contracts_reject_unbacked_or_out_of_bounds_highlights() -> None:
    bad_location = {
        "localizationId": "loc-brand",
        "fieldKey": "brand_name",
        "quote": "Treasury Sample",
        "panelId": "p01",
        "status": "located",
        "acceptedTokenIds": [],
        "displayBoxes": [{"x": 0, "y": 0, "width": 10, "height": 10}],
        "reason": "invalid fixture",
    }
    try:
        LocalizationResult.model_validate(bad_location)
    except ValueError as exc:
        assert "requires OCR token IDs and boxes" in str(exc)
    else:
        raise AssertionError("unbacked display geometry was accepted")

    bad_ocr = {
        "schemaVersion": "ocr-geometry-v1",
        "adapterVersion": "fixture",
        "engine": "fixture",
        "engineVersion": "1",
        "languageData": "eng",
        "executable": "fixture",
        "preprocessingVersion": "none",
        "panelId": "p01",
        "sourceSha256": "0" * 64,
        "originalWidth": 100,
        "originalHeight": 100,
        "transcript": "text",
        "tokens": [
            {
                "tokenId": "t1",
                "text": "text",
                "boundingBox": BoundingBox(x=95, y=0, width=10, height=10).model_dump(
                    by_alias=True
                ),
                "confidence": 90,
                "blockId": "b1",
                "lineId": "l1",
                "readingOrder": 0,
            }
        ],
        "exitStatus": 0,
        "warnings": [],
        "preprocessingDurationMs": 0,
        "ocrDurationMs": 0,
    }
    try:
        OcrRun.model_validate(bad_ocr)
    except ValueError as exc:
        assert "exceeds panel width" in str(exc)
    else:
        raise AssertionError("out-of-bounds OCR geometry was accepted")
