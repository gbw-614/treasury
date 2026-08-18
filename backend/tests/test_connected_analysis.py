from __future__ import annotations

import asyncio
import io

import pytest
from PIL import Image, ImageDraw

from app.schemas import (
    AnalysisRequest,
    BoundingBox,
    FieldKey,
    LocationStatus,
    OcrRun,
    OcrToken,
)
from app.services import case_store, recognition_cache
from app.services.connected_analysis import run_connected_analysis
from app.services.image_validation import validate_image
from app.services.mock_analysis import CANONICAL_WARNING_BODY, build_mock_analysis
from app.services.openrouter_vision import _normalized_box_to_pixels
from app.services.quote_alignment import align_quote


@pytest.fixture(autouse=True)
def isolated_recognition_cache(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(case_store, "DATA_ROOT", tmp_path)


def image_bytes(text: str = "Treasury Sample") -> bytes:
    image = Image.new("RGB", (640, 400), "white")
    draw = ImageDraw.Draw(image)
    draw.text((20, 20), text, fill="black")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def request(brand_name: str = "Treasury Sample") -> AnalysisRequest:
    return AnalysisRequest.model_validate(
        {
            "schemaVersion": "verification-request-v1",
            "category": "distilled_spirits",
            "expected": {
                "brandName": brand_name,
                "classType": "Bourbon Whisky",
                "abvPercent": 45,
                "proof": 90,
                "governmentWarning": {
                    "heading": "GOVERNMENT WARNING:",
                    "body": CANONICAL_WARNING_BODY,
                },
            },
            "panels": [{"panelId": "p01", "file": "display-only.png"}],
        }
    )


def test_llm_pipeline_uses_blind_extraction_and_model_geometry(monkeypatch) -> None:
    content = image_bytes()
    panel, blank = validate_image(content, "p01")
    fixture = build_mock_analysis(request(), panel, blank=False)
    monkeypatch.setattr(
        "app.services.connected_analysis.run_openrouter_vision",
        lambda content, panel: fixture.vision_run,
    )
    monkeypatch.setattr(
        "app.services.connected_analysis.run_tesseract",
        lambda content, panel: fixture.ocr_run,
    )

    result = asyncio.run(run_connected_analysis(request(), panel, content, blank=blank))

    assert result.mode == "connected"
    assert result.overall_summary == "no_automated_discrepancy_detected"
    assert {
        rule.automated_status for rule in result.rule_results if rule.applicable
    } == {"matches"}
    assert all(
        location.status == LocationStatus.UNAVAILABLE for location in result.localizations
    )
    assert all(
        candidate.model_bounding_box for candidate in result.vision_run.panels[0].fields
    )


def test_identical_image_reuses_each_single_reader_result(monkeypatch) -> None:
    content = image_bytes()
    panel, blank = validate_image(content, "p01")
    fixture = build_mock_analysis(request(), panel, blank=False)
    calls = {"ocr": 0, "llm": 0}

    def vision(content, panel):
        calls["llm"] += 1
        return fixture.vision_run

    def ocr(content, panel):
        calls["ocr"] += 1
        return fixture.ocr_run

    monkeypatch.setattr("app.services.connected_analysis.run_openrouter_vision", vision)
    monkeypatch.setattr("app.services.connected_analysis.run_tesseract", ocr)

    llm_request = request()
    ocr_request = request().model_copy(update={"reader_mode": "ocr"})
    first = asyncio.run(run_connected_analysis(llm_request, panel, content, blank=blank))
    second = asyncio.run(run_connected_analysis(llm_request, panel, content, blank=blank))
    third = asyncio.run(run_connected_analysis(ocr_request, panel, content, blank=blank))
    fourth = asyncio.run(run_connected_analysis(ocr_request, panel, content, blank=blank))

    assert first.overall_summary == second.overall_summary
    assert calls == {"ocr": 1, "llm": 1}
    assert recognition_cache.stats() == {"ocr": 1, "llm": 1, "total": 2}
    assert second.vision_run.duration_ms == 0
    assert third.overall_summary == fourth.overall_summary
    assert fourth.ocr_run.ocr_duration_ms == 0


def test_ocr_mode_does_not_call_the_vision_provider(monkeypatch) -> None:
    content = image_bytes()
    panel, blank = validate_image(content, "p01")
    base_request = request()
    ocr_request = base_request.model_copy(update={"reader_mode": "ocr"})
    fixture = build_mock_analysis(base_request, panel, blank=False)

    def vision_was_not_called(*_args: object) -> None:
        raise AssertionError("OCR-only mode must not call the vision provider")

    monkeypatch.setattr("app.services.connected_analysis.run_openrouter_vision", vision_was_not_called)
    monkeypatch.setattr(
        "app.services.connected_analysis.run_tesseract",
        lambda content, panel: fixture.ocr_run,
    )

    result = asyncio.run(run_connected_analysis(ocr_request, panel, content, blank=blank))

    assert result.reader_mode == "ocr"
    assert result.vision_run.provider == "local"
    assert result.ocr_run.engine == "fixture"


def test_llm_mode_does_not_call_tesseract(monkeypatch) -> None:
    content = image_bytes()
    panel, blank = validate_image(content, "p01")
    base_request = request()
    llm_request = base_request.model_copy(update={"reader_mode": "llm"})
    fixture = build_mock_analysis(base_request, panel, blank=False)

    def ocr_was_not_called(*_args: object) -> None:
        raise AssertionError("LLM-only mode must not call Tesseract")

    monkeypatch.setattr(
        "app.services.connected_analysis.run_openrouter_vision",
        lambda content, panel: fixture.vision_run,
    )
    monkeypatch.setattr("app.services.connected_analysis.run_tesseract", ocr_was_not_called)

    result = asyncio.run(run_connected_analysis(llm_request, panel, content, blank=blank))

    assert result.reader_mode == "llm"
    assert result.ocr_run.engine == "not-run"


def test_normalized_model_box_converts_to_original_pixels() -> None:
    content = image_bytes()
    panel, _ = validate_image(content, "p01")

    box = _normalized_box_to_pixels(
        {"xMin": 100, "yMin": 200, "xMax": 600, "yMax": 500},
        panel,
    )

    assert box == {"x": 64, "y": 80, "width": 320, "height": 120}
    with pytest.raises(ValueError, match="out-of-range"):
        _normalized_box_to_pixels(
            {"xMin": 700, "yMin": 200, "xMax": 600, "yMax": 500},
            panel,
        )


def test_connected_pipeline_reports_selected_reader_mismatch(monkeypatch) -> None:
    content = image_bytes()
    panel, blank = validate_image(content, "p01")
    fixture = build_mock_analysis(request(), panel, blank=False)
    monkeypatch.setattr(
        "app.services.connected_analysis.run_openrouter_vision",
        lambda content, panel: fixture.vision_run,
    )
    monkeypatch.setattr(
        "app.services.connected_analysis.run_tesseract",
        lambda content, panel: fixture.ocr_run,
    )

    result = asyncio.run(
        run_connected_analysis(
            request(brand_name="Different Application Brand"),
            panel,
            content,
            blank=blank,
        )
    )
    brand = next(
        rule for rule in result.rule_results if rule.field_key == FieldKey.BRAND_NAME
    )
    assert result.overall_summary == "needs_review"
    assert brand.automated_status == "review"
    assert brand.localization_ids == ("loc-brand_name-p01",)


def test_brand_candidate_matches_expected_phrase_inside_classified_text(monkeypatch) -> None:
    content = image_bytes()
    panel, blank = validate_image(content, "p01")
    fixture = build_mock_analysis(request(), panel, blank=False)
    extraction = fixture.vision_run.panels[0]
    fields = tuple(
        candidate.model_copy(
            update={
                "raw_text": "Xtatic by RICCO",
                "normalized_value": "Xtatic by RICCO",
                "evidence_quote": "Xtatic by RICCO",
            }
        )
        if candidate.field_key == FieldKey.BRAND_NAME
        else candidate
        for candidate in extraction.fields
    )
    altered_run = fixture.vision_run.model_copy(
        update={"panels": (extraction.model_copy(update={"fields": fields}),)}
    )
    monkeypatch.setattr(
        "app.services.connected_analysis.run_openrouter_vision",
        lambda content, panel: altered_run,
    )

    result = asyncio.run(
        run_connected_analysis(request("Xtatic"), panel, content, blank=blank)
    )
    brand = next(
        rule for rule in result.rule_results if rule.field_key == FieldKey.BRAND_NAME
    )

    assert brand.automated_status == "matches"
    assert brand.detected_value == "Xtatic by RICCO"
    assert brand.reason_code == "expected_phrase_in_candidate"


def test_brand_falls_back_to_a_phrase_in_the_transcript(monkeypatch) -> None:
    content = image_bytes()
    panel, blank = validate_image(content, "p01")
    fixture = build_mock_analysis(request(), panel, blank=False)
    extraction = fixture.vision_run.panels[0]
    fields = tuple(
        candidate.model_copy(
            update={
                "raw_text": "Different Selected Brand",
                "normalized_value": "Different Selected Brand",
                "evidence_quote": "Different Selected Brand",
            }
        )
        if candidate.field_key == FieldKey.BRAND_NAME
        else candidate
        for candidate in extraction.fields
    )
    altered_run = fixture.vision_run.model_copy(
        update={
            "panels": (
                extraction.model_copy(
                    update={"fields": fields, "full_text": extraction.full_text + "\nXtatic"}
                ),
            )
        }
    )
    monkeypatch.setattr(
        "app.services.connected_analysis.run_openrouter_vision",
        lambda content, panel: altered_run,
    )

    result = asyncio.run(
        run_connected_analysis(request("Xtatic"), panel, content, blank=blank)
    )
    brand = next(
        rule for rule in result.rule_results if rule.field_key == FieldKey.BRAND_NAME
    )

    assert brand.automated_status == "matches"
    assert brand.detected_value == "Xtatic"
    assert brand.reason_code == "expected_phrase_in_transcript"
    assert brand.localization_ids == ()


def test_transcript_fallback_uses_literal_presence_without_semantic_exceptions(monkeypatch) -> None:
    content = image_bytes()
    panel, blank = validate_image(content, "p01")
    fixture = build_mock_analysis(request(), panel, blank=False)
    extraction = fixture.vision_run.panels[0]
    fields = tuple(
        candidate.model_copy(
            update={
                "raw_text": "Different Selected Brand",
                "normalized_value": "Different Selected Brand",
                "evidence_quote": "Different Selected Brand",
            }
        )
        if candidate.field_key == FieldKey.BRAND_NAME
        else candidate
        for candidate in extraction.fields
    )
    altered_run = fixture.vision_run.model_copy(
        update={
            "panels": (
                extraction.model_copy(
                    update={
                        "fields": fields,
                        "full_text": extraction.full_text + "\nBottled by Xtatic Company",
                    }
                ),
            )
        }
    )
    monkeypatch.setattr(
        "app.services.connected_analysis.run_openrouter_vision",
        lambda content, panel: altered_run,
    )

    result = asyncio.run(
        run_connected_analysis(request("Xtatic"), panel, content, blank=blank)
    )
    brand = next(
        rule for rule in result.rule_results if rule.field_key == FieldKey.BRAND_NAME
    )

    assert brand.automated_status == "matches"
    assert brand.detected_value == "Xtatic"
    assert brand.reason_code == "expected_phrase_in_transcript"


def test_additional_expected_label_text_matches_only_against_blind_transcript(monkeypatch) -> None:
    content = image_bytes()
    panel, blank = validate_image(content, "p01")
    payload = request().model_dump(mode="json", by_alias=True)
    payload["expected"]["additionalFields"] = [
        {
            "id": "country-of-origin",
            "label": "Country of origin",
            "expectedText": "Product of Italy",
            "matchMode": "literal_phrase",
        }
    ]
    expected_request = AnalysisRequest.model_validate(payload)
    fixture = build_mock_analysis(expected_request, panel, blank=False)
    extraction = fixture.vision_run.panels[0]
    altered_run = fixture.vision_run.model_copy(
        update={
            "panels": (
                extraction.model_copy(
                    update={"full_text": extraction.full_text + "\nPRODUCT OF ITALY"}
                ),
            )
        }
    )
    monkeypatch.setattr(
        "app.services.connected_analysis.run_openrouter_vision",
        lambda content, panel: altered_run,
    )

    result = asyncio.run(
        run_connected_analysis(expected_request, panel, content, blank=blank)
    )

    assert result.overall_summary == "no_automated_discrepancy_detected"
    assert len(result.additional_rule_results) == 1
    additional = result.additional_rule_results[0]
    assert additional.label == "Country of origin"
    assert additional.automated_status == "matches"
    assert additional.detected_value == "PRODUCT OF ITALY"
    assert additional.reason_code == "expected_phrase_in_transcript"


def test_missing_additional_expected_text_routes_to_review_not_failure(monkeypatch) -> None:
    content = image_bytes()
    panel, blank = validate_image(content, "p01")
    payload = request().model_dump(mode="json", by_alias=True)
    payload["expected"]["additionalFields"] = [
        {
            "id": "vintage",
            "label": "Vintage",
            "expectedText": "1987",
        }
    ]
    expected_request = AnalysisRequest.model_validate(payload)
    fixture = build_mock_analysis(expected_request, panel, blank=False)
    monkeypatch.setattr(
        "app.services.connected_analysis.run_openrouter_vision",
        lambda content, panel: fixture.vision_run,
    )

    result = asyncio.run(
        run_connected_analysis(expected_request, panel, content, blank=blank)
    )

    assert result.overall_summary == "needs_review"
    additional = result.additional_rule_results[0]
    assert additional.automated_status == "review"
    assert additional.detected_value is None
    assert additional.reason_code == "expected_phrase_not_found"


def test_additional_expected_fields_require_unique_ids_and_labels() -> None:
    payload = request().model_dump(mode="json", by_alias=True)
    payload["expected"]["additionalFields"] = [
        {"id": "origin", "label": "Origin", "expectedText": "Italy"},
        {"id": "origin", "label": "origin", "expectedText": "France"},
    ]

    with pytest.raises(ValueError, match="unique"):
        AnalysisRequest.model_validate(payload)


def test_class_type_matches_expected_phrase_inside_classified_text(monkeypatch) -> None:
    content = image_bytes()
    panel, blank = validate_image(content, "p01")
    expected_request = request().model_copy(
        update={
            "expected": request().expected.model_copy(update={"class_type": "PINOT NOIR"})
        }
    )
    fixture = build_mock_analysis(expected_request, panel, blank=False)
    extraction = fixture.vision_run.panels[0]
    fields = tuple(
        candidate.model_copy(
            update={
                "raw_text": "PINOT NOIR 2021",
                "normalized_value": "PINOT NOIR 2021",
                "evidence_quote": "PINOT NOIR 2021",
            }
        )
        if candidate.field_key == FieldKey.CLASS_TYPE
        else candidate
        for candidate in extraction.fields
    )
    altered_run = fixture.vision_run.model_copy(
        update={"panels": (extraction.model_copy(update={"fields": fields}),)}
    )
    monkeypatch.setattr(
        "app.services.connected_analysis.run_openrouter_vision",
        lambda content, panel: altered_run,
    )

    result = asyncio.run(
        run_connected_analysis(expected_request, panel, content, blank=blank)
    )
    class_type = next(
        rule for rule in result.rule_results if rule.field_key == FieldKey.CLASS_TYPE
    )

    assert class_type.automated_status == "matches"
    assert class_type.detected_value == "PINOT NOIR 2021"
    assert class_type.reason_code == "expected_phrase_in_candidate"


def test_clear_vision_match_passes_when_ocr_cannot_locate_evidence(monkeypatch) -> None:
    content = image_bytes()
    panel, blank = validate_image(content, "p01")
    fixture = build_mock_analysis(request(), panel, blank=False)
    ocr_without_geometry = fixture.ocr_run.model_copy(
        update={"tokens": (), "transcript": ""}
    )
    monkeypatch.setattr(
        "app.services.connected_analysis.run_openrouter_vision",
        lambda content, panel: fixture.vision_run,
    )
    monkeypatch.setattr(
        "app.services.connected_analysis.run_tesseract",
        lambda content, panel: ocr_without_geometry,
    )

    result = asyncio.run(run_connected_analysis(request(), panel, content, blank=blank))

    assert result.overall_summary == "no_automated_discrepancy_detected"
    assert result.review_tasks == ()
    applicable_rules = [rule for rule in result.rule_results if rule.applicable]
    assert {rule.automated_status for rule in applicable_rules} == {"matches"}
    assert {rule.reason_code for rule in applicable_rules} == {"normalized_match"}


def test_missing_warning_extraction_requires_review_not_automatic_failure(monkeypatch) -> None:
    content = image_bytes()
    panel, blank = validate_image(content, "p01")
    fixture = build_mock_analysis(request(), panel, blank=False)
    vision_panel = fixture.vision_run.panels[0].model_copy(
        update={
            "fields": tuple(
                candidate
                for candidate in fixture.vision_run.panels[0].fields
                if candidate.field_key
                not in {
                    FieldKey.GOVERNMENT_WARNING_HEADING,
                    FieldKey.GOVERNMENT_WARNING_BODY,
                }
            )
        }
    )
    vision_without_warning = fixture.vision_run.model_copy(
        update={"panels": (vision_panel,)}
    )
    monkeypatch.setattr(
        "app.services.connected_analysis.run_openrouter_vision",
        lambda content, panel: vision_without_warning,
    )
    monkeypatch.setattr(
        "app.services.connected_analysis.run_tesseract",
        lambda content, panel: fixture.ocr_run,
    )

    result = asyncio.run(run_connected_analysis(request(), panel, content, blank=blank))

    assert result.overall_summary == "needs_review"
    warning_rules = [
        rule
        for rule in result.rule_results
        if rule.field_key
        in {
            FieldKey.GOVERNMENT_WARNING_HEADING,
            FieldKey.GOVERNMENT_WARNING_BODY,
        }
    ]
    assert {rule.automated_status for rule in warning_rules} == {"review"}
    assert {rule.reason_code for rule in warning_rules} == {"vision_evidence_unavailable"}


@pytest.mark.parametrize(
    ("field_key", "altered_text"),
    [
        (FieldKey.GOVERNMENT_WARNING_HEADING, "Government Warning:"),
        (
            FieldKey.GOVERNMENT_WARNING_BODY,
            CANONICAL_WARNING_BODY.replace("Surgeon General,", "Surgeon General"),
        ),
    ],
)
def test_warning_text_requires_exact_heading_case_and_body_punctuation(
    monkeypatch,
    field_key: FieldKey,
    altered_text: str,
) -> None:
    content = image_bytes()
    panel, blank = validate_image(content, "p01")
    fixture = build_mock_analysis(request(), panel, blank=False)
    extraction = fixture.vision_run.panels[0]
    fields = tuple(
        candidate.model_copy(
            update={"raw_text": altered_text, "evidence_quote": altered_text}
        )
        if candidate.field_key == field_key
        else candidate
        for candidate in extraction.fields
    )
    presentation = extraction.warning_presentation
    if field_key == FieldKey.GOVERNMENT_WARNING_HEADING:
        presentation = presentation.model_copy(update={"heading_all_caps": False})
    altered_run = fixture.vision_run.model_copy(
        update={
            "panels": (
                extraction.model_copy(
                    update={"fields": fields, "warning_presentation": presentation}
                ),
            )
        }
    )
    monkeypatch.setattr(
        "app.services.connected_analysis.run_openrouter_vision",
        lambda content, panel: altered_run,
    )
    monkeypatch.setattr(
        "app.services.connected_analysis.run_tesseract",
        lambda content, panel: fixture.ocr_run,
    )

    result = asyncio.run(run_connected_analysis(request(), panel, content, blank=blank))
    rule = next(item for item in result.rule_results if item.field_key == field_key)

    assert result.overall_summary == "needs_review"
    assert rule.automated_status == "review"


def test_warning_boldness_failure_requires_review(monkeypatch) -> None:
    content = image_bytes()
    panel, blank = validate_image(content, "p01")
    fixture = build_mock_analysis(request(), panel, blank=False)
    extraction = fixture.vision_run.panels[0]
    presentation = extraction.warning_presentation.model_copy(
        update={"heading_only_bold": False}
    )
    altered_run = fixture.vision_run.model_copy(
        update={
            "panels": (
                extraction.model_copy(update={"warning_presentation": presentation}),
            )
        }
    )
    monkeypatch.setattr(
        "app.services.connected_analysis.run_openrouter_vision",
        lambda content, panel: altered_run,
    )
    monkeypatch.setattr(
        "app.services.connected_analysis.run_tesseract",
        lambda content, panel: fixture.ocr_run,
    )

    result = asyncio.run(run_connected_analysis(request(), panel, content, blank=blank))
    heading = next(
        item
        for item in result.rule_results
        if item.field_key == FieldKey.GOVERNMENT_WARNING_HEADING
    )

    assert heading.automated_status == "review"
    assert heading.reason_code == "warning_presentation_noncompliant"
    assert "bold" in heading.explanation


def test_all_caps_warning_body_preserves_an_exact_wording_match(monkeypatch) -> None:
    content = image_bytes()
    panel, blank = validate_image(content, "p01")
    fixture = build_mock_analysis(request(), panel, blank=False)
    extraction = fixture.vision_run.panels[0]
    fields = tuple(
        candidate.model_copy(
            update={
                "raw_text": candidate.raw_text.upper(),
                "evidence_quote": candidate.evidence_quote.upper(),
            }
        )
        if candidate.field_key == FieldKey.GOVERNMENT_WARNING_BODY
        else candidate
        for candidate in extraction.fields
    )
    altered_run = fixture.vision_run.model_copy(
        update={"panels": (extraction.model_copy(update={"fields": fields}),)}
    )
    monkeypatch.setattr(
        "app.services.connected_analysis.run_openrouter_vision",
        lambda content, panel: altered_run,
    )

    result = asyncio.run(run_connected_analysis(request(), panel, content, blank=blank))
    body = next(
        item
        for item in result.rule_results
        if item.field_key == FieldKey.GOVERNMENT_WARNING_BODY
    )

    assert body.automated_status == "matches"
    assert body.reason_code == "normalized_match"


def test_unknown_warning_presentation_requires_review(monkeypatch) -> None:
    content = image_bytes()
    panel, blank = validate_image(content, "p01")
    fixture = build_mock_analysis(request(), panel, blank=False)
    extraction = fixture.vision_run.panels[0]
    altered_run = fixture.vision_run.model_copy(
        update={
            "panels": (
                extraction.model_copy(update={"warning_presentation": None}),
            )
        }
    )
    monkeypatch.setattr(
        "app.services.connected_analysis.run_openrouter_vision",
        lambda content, panel: altered_run,
    )
    monkeypatch.setattr(
        "app.services.connected_analysis.run_tesseract",
        lambda content, panel: fixture.ocr_run,
    )

    result = asyncio.run(run_connected_analysis(request(), panel, content, blank=blank))
    warning_rules = [
        item
        for item in result.rule_results
        if item.field_key
        in {
            FieldKey.GOVERNMENT_WARNING_HEADING,
            FieldKey.GOVERNMENT_WARNING_BODY,
        }
    ]

    assert result.overall_summary == "needs_review"
    assert {item.automated_status for item in warning_rules} == {"review"}
    assert {item.reason_code for item in warning_rules} == {
        "warning_presentation_uncertain"
    }


def test_connected_pipeline_keeps_evidence_on_its_source_panel(monkeypatch) -> None:
    content = image_bytes()
    second_content = image_bytes("Treasury Sample back")
    first, first_blank = validate_image(content, "p01")
    second, second_blank = validate_image(second_content, "p02")
    multi_request = AnalysisRequest.model_validate(
        {
            **request().model_dump(by_alias=True),
            "panels": [
                {"panelId": "p01", "file": "front.png"},
                {"panelId": "p02", "file": "back.png"},
            ],
        }
    )
    first_fixture = build_mock_analysis(request(), first, blank=False)
    second_fixture = build_mock_analysis(request(), second, blank=False)
    first_panel = first_fixture.vision_run.panels[0].model_copy(
        update={
            "fields": tuple(
                field
                for field in first_fixture.vision_run.panels[0].fields
                if field.field_key in {FieldKey.BRAND_NAME, FieldKey.CLASS_TYPE}
            )
        }
    )
    second_panel = second_fixture.vision_run.panels[0].model_copy(
        update={
            "fields": tuple(
                field
                for field in second_fixture.vision_run.panels[0].fields
                if field.field_key not in {FieldKey.BRAND_NAME, FieldKey.CLASS_TYPE}
            )
        }
    )
    first_vision = first_fixture.vision_run.model_copy(update={"panels": (first_panel,)})
    second_vision = second_fixture.vision_run.model_copy(update={"panels": (second_panel,)})
    second_ocr = second_fixture.ocr_run.model_copy(
        update={
            "tokens": tuple(
                token.model_copy(update={"token_id": f"p02-{token.token_id}"})
                for token in second_fixture.ocr_run.tokens
            )
        }
    )

    monkeypatch.setattr(
        "app.services.connected_analysis.run_openrouter_vision",
        lambda content, panel: first_vision if panel.panel_id == "p01" else second_vision,
    )
    monkeypatch.setattr(
        "app.services.connected_analysis.run_tesseract",
        lambda content, panel: first_fixture.ocr_run if panel.panel_id == "p01" else second_ocr,
    )

    result = asyncio.run(
        run_connected_analysis(
            multi_request,
            (first, second),
            (content, second_content),
            blanks=(first_blank, second_blank),
        )
    )

    assert [panel.panel_id for panel in result.panels] == ["p01", "p02"]
    assert [run.panel_id for run in result.ocr_runs] == ["p01", "p02"]
    abv_location = next(
        location
        for location in result.localizations
        if location.field_key == FieldKey.ALCOHOL_CONTENT
    )
    assert abv_location.panel_id == "p02"
    assert result.overall_summary == "no_automated_discrepancy_detected"


def test_alignment_refuses_to_choose_between_repeated_quotes() -> None:
    tokens = tuple(
        OcrToken(
            token_id=f"t{index}",
            text=text,
            bounding_box=BoundingBox(x=index * 30, y=10, width=25, height=10),
            confidence=95,
            block_id="b1",
            line_id="l1",
            reading_order=index,
        )
        for index, text in enumerate(("OLD", "TOM", "OLD", "TOM"), start=1)
    )
    ocr = OcrRun(
        schema_version="ocr-geometry-v1",
        adapter_version="test",
        engine="test",
        engine_version="1",
        language_data="eng",
        executable="test",
        preprocessing_version="none",
        panel_id="p01",
        source_sha256="0" * 64,
        original_width=200,
        original_height=100,
        transcript="OLD TOM OLD TOM",
        tokens=tokens,
        exit_status=0,
        warnings=(),
        preprocessing_duration_ms=0,
        ocr_duration_ms=0,
    )
    location = align_quote(
        field_key=FieldKey.BRAND_NAME,
        quote="OLD TOM",
        panel_id="p01",
        ocr_run=ocr,
    )
    assert location.status == LocationStatus.AMBIGUOUS
    assert location.display_boxes == ()
    assert location.accepted_token_ids == ()


def test_alignment_uses_printed_capitalization_to_disambiguate_brand() -> None:
    tokens = tuple(
        OcrToken(
            token_id=f"t{index}",
            text=text,
            bounding_box=BoundingBox(
                x=index * 30,
                y=10 if index < 3 else 60,
                width=25,
                height=20 if index < 3 else 10,
            ),
            confidence=95,
            block_id="b1",
            line_id="l1" if index < 3 else "l2",
            reading_order=index,
        )
        for index, text in enumerate(("OLD", "TOM", "Old", "Tom"), start=1)
    )
    ocr = OcrRun(
        schema_version="ocr-geometry-v1",
        adapter_version="test",
        engine="test",
        engine_version="1",
        language_data="eng",
        executable="test",
        preprocessing_version="none",
        panel_id="p01",
        source_sha256="0" * 64,
        original_width=200,
        original_height=100,
        transcript="OLD TOM Old Tom",
        tokens=tokens,
        exit_status=0,
        warnings=(),
        preprocessing_duration_ms=0,
        ocr_duration_ms=0,
    )
    location = align_quote(
        field_key=FieldKey.BRAND_NAME,
        quote="OLD TOM",
        panel_id="p01",
        ocr_run=ocr,
    )
    assert location.status == LocationStatus.LOCATED
    assert location.accepted_token_ids == ("t1", "t2")
    assert "case-preserving" in location.reason
