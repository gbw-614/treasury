from app.field_library import evaluate_check, get_field_definition
from app.schemas import AnalysisRequest, BoundingBox, ValidatedPanel, VisionTextBlock
from app.services.connected_analysis import _library_rules
from tests.mock_analysis import build_mock_analysis


def test_text_check_is_case_punctuation_and_whitespace_insensitive() -> None:
    result = evaluate_check(
        get_field_definition("brand_name"), "TREASURY—SAMPLE\nBOURBON", "treasury sample"
    )

    assert result.matched is True
    assert result.reason_code == "expected_phrase_in_transcript"


def test_literal_evidence_preserves_expected_boundary_punctuation() -> None:
    field = get_field_definition("brand_name")
    transcript = (
        "GOVERNMENT WARNING: (1) According to the Surgeon General, women "
        "should not drink."
    )

    heading = evaluate_check(field, transcript, "GOVERNMENT WARNING:")
    body = evaluate_check(
        field,
        transcript,
        "(1) According to the Surgeon General, women should not drink.",
    )

    assert heading.evidence_quote == "GOVERNMENT WARNING:"
    assert body.evidence_quote == (
        "(1) According to the Surgeon General, women should not drink."
    )


def test_literal_evidence_does_not_absorb_adjacent_warning_punctuation() -> None:
    field = get_field_definition("brand_name")
    transcript = "GOVERNMENT WARNING:(1) According to the Surgeon General."

    heading = evaluate_check(field, transcript, "GOVERNMENT WARNING:")
    body = evaluate_check(field, transcript, "(1) According to the Surgeon General.")

    assert heading.evidence_quote == "GOVERNMENT WARNING:"
    assert body.evidence_quote == "(1) According to the Surgeon General."


def test_alcohol_regex_requires_a_statement_and_compares_captured_value() -> None:
    field = get_field_definition("alcohol_content")
    comma_decimal = evaluate_check(field, "ALCOHOL 11,5% BY VOLUME", "11.5")
    range_value = evaluate_check(field, "ALC. 11-13% BY VOL.", "11-13")
    trailing_value = evaluate_check(field, "ALC. BY VOL. 4.9%", "4.9")
    trailing_range = evaluate_check(field, "ALCOHOL BY VOLUME 4-6 PERCENT", "4-6")
    bare_number = evaluate_check(field, "11.5", "11.5")
    repeated_same = evaluate_check(
        field, "ALC. 4.2% BY VOL.\n4.2% ALCOHOL BY VOLUME", "4.2"
    )
    ambiguous = evaluate_check(
        field, "ALC. 6.7% BY VOL.\nALC. 4.2% BY VOL.", "4.2"
    )

    assert comma_decimal.matched is True
    assert comma_decimal.detected_value == "11,5"
    assert range_value.matched is True
    assert trailing_value.matched is True
    assert trailing_value.detected_value == "4.9"
    assert trailing_value.evidence_quote == "ALC. BY VOL. 4.9%"
    assert trailing_range.matched is True
    assert bare_number.matched is False
    assert bare_number.reason_code == "required_pattern_not_found"
    assert repeated_same.matched is True
    assert ambiguous.matched is False
    assert ambiguous.detected_value == "6.7, 4.2"
    assert ambiguous.reason_code == "multiple_distinct_values"
    assert ambiguous.evidence_quotes == (
        "ALC. 6.7% BY VOL.",
        "ALC. 4.2% BY VOL.",
    )


def test_proof_regex_supports_parenthesized_proof_and_optional_expected_value() -> None:
    field = get_field_definition("proof")

    assert evaluate_check(field, "80 (PROOF)", "80").matched is True
    assert evaluate_check(field, "94 PROOF", None).matched is True
    assert evaluate_check(field, "94 PROOF", "80").matched is False


def test_warning_uses_the_library_canonical_text() -> None:
    field = get_field_definition("government_warning")
    canonical = f"{field.heading} {field.body}"

    result = evaluate_check(field, canonical, None)

    assert result.matched is True
    assert result.evidence_quote == canonical
    assert evaluate_check(field, "GOVERNMENT WARNING: shortened", None).matched is False


def test_warning_accepts_expected_words_split_by_printed_hyphenation() -> None:
    field = get_field_definition("government_warning")
    canonical = f"{field.heading} {field.body}"
    hyphenated = canonical.replace("According", "Accord-ing").replace(
        "beverages", "bever-\nages", 1
    )

    result = evaluate_check(field, hyphenated, None)

    assert result.matched is True
    assert result.evidence_quote == hyphenated


def test_warning_does_not_accept_a_genuine_heading_misspelling() -> None:
    field = get_field_definition("government_warning")
    canonical = f"{field.heading} {field.body}"

    result = evaluate_check(
        field,
        canonical.replace("GOVERNMENT WARNING:", "GOVERNEMENT WARNING:"),
        None,
    )

    assert result.matched is False


def test_v2_request_selects_library_checks_and_preserves_v1_shape() -> None:
    request = AnalysisRequest.model_validate({
        "schemaVersion": "verification-request-v2",
        "checks": [
            {"fieldId": "brand_name", "required": True, "expectedValue": "Treasury Sample"},
            {"fieldId": "alcohol_content", "required": True, "expectedValue": "45"},
            {"fieldId": "government_warning", "required": True},
        ],
        "panels": [{"panelId": "p01", "file": "label.png"}],
    })

    assert request.expected is None
    assert [check.field_id for check in request.checks] == [
        "brand_name", "alcohol_content", "government_warning"
    ]


def test_v2_checks_run_through_the_backend_analysis_result() -> None:
    request = AnalysisRequest.model_validate({
        "schemaVersion": "verification-request-v2",
        "checks": [
            {"fieldId": "brand_name", "required": True, "expectedValue": "treasury sample"},
            {"fieldId": "alcohol_content", "required": True, "expectedValue": "45"},
            {"fieldId": "proof", "required": True, "expectedValue": "90"},
            {"fieldId": "government_warning", "required": True},
        ],
        "panels": [{"panelId": "p01", "file": "label.png"}],
    })
    panel = ValidatedPanel(
        panel_id="p01", source_sha256="a" * 64, detected_mime_type="image/png",
        width=100, height=100, exif_orientation=None, applied_transform="none",
        decoded_pixel_count=10_000, original_byte_count=100,
        preprocessing_version="test-v1",
    )

    result = build_mock_analysis(request, panel, blank=False)

    assert result.rule_results == ()
    assert {rule.field_id for rule in result.additional_rule_results} == {
        "brand_name", "alcohol_content", "proof", "government_warning"
    }
    assert {rule.automated_status for rule in result.additional_rule_results} == {"matches"}


def test_v2_match_uses_literal_block_evidence_and_respects_uncertainty() -> None:
    request = AnalysisRequest.model_validate({
        "schemaVersion": "verification-request-v2",
        "checks": [{"fieldId": "brand_name", "required": True, "expectedValue": "Treasury Sample"}],
        "panels": [{"panelId": "p01", "file": "label.png"}],
    })
    panel = ValidatedPanel(
        panel_id="p01", source_sha256="a" * 64, detected_mime_type="image/png",
        width=100, height=100, exif_orientation=None, applied_transform="none",
        decoded_pixel_count=10_000, original_byte_count=100,
        preprocessing_version="test-v1",
    )
    fixture = build_mock_analysis(request, panel, blank=False)
    extraction = fixture.vision_run.panels[0]
    clear_block = VisionTextBlock(
        block_id="p01-b001", text="TREASURY SAMPLE",
        model_bounding_box=BoundingBox(x=10, y=10, width=60, height=15),
        reading_order=0, legibility="clear",
    )
    clear_vision = fixture.vision_run.model_copy(update={
        "panels": (extraction.model_copy(update={
            "full_text": clear_block.text, "fields": (), "text_blocks": (clear_block,),
        }),),
    })

    clear_result = _library_rules(request, clear_vision)[0]
    assert clear_result.automated_status == "matches"
    assert clear_result.detected_value == "TREASURY SAMPLE"
    assert clear_result.evidence_block_ids == ("p01-b001",)

    uncertain_vision = clear_vision.model_copy(update={
        "panels": (clear_vision.panels[0].model_copy(update={
            "text_blocks": (clear_block.model_copy(update={
                "legibility": "uncertain", "uncertainty": "The first word is blurred.",
            }),),
        }),),
    })
    uncertain_result = _library_rules(request, uncertain_vision)[0]
    assert uncertain_result.automated_status == "review"
    assert uncertain_result.reason_code == "matched_text_uncertain"


def test_v2_ocr_warning_keeps_text_match_and_adds_presentation_review() -> None:
    request = AnalysisRequest.model_validate({
        "schemaVersion": "verification-request-v2",
        "readerMode": "ocr",
        "checks": [{"fieldId": "government_warning", "required": True}],
        "panels": [{"panelId": "p01", "file": "label.png"}],
    })
    panel = ValidatedPanel(
        panel_id="p01", source_sha256="a" * 64, detected_mime_type="image/png",
        width=100, height=100, exif_orientation=None, applied_transform="none",
        decoded_pixel_count=10_000, original_byte_count=100,
        preprocessing_version="test-v1",
    )
    fixture = build_mock_analysis(request, panel, blank=False)

    text_result, presentation_result = _library_rules(request, fixture.vision_run)

    assert text_result.field_id == "government_warning"
    assert text_result.automated_status == "matches"
    assert text_result.requires_human_review is False
    assert presentation_result.field_id == "government_warning_presentation"
    assert presentation_result.automated_status == "review"
    assert presentation_result.requires_human_review is True
    assert presentation_result.reason_code == "warning_heading_boldness_human_review"
    assert "only the GOVERNMENT WARNING: heading is bold" in presentation_result.explanation


def test_v2_failed_text_check_surfaces_only_a_high_scoring_closest_span() -> None:
    panel = ValidatedPanel(
        panel_id="p01", source_sha256="a" * 64, detected_mime_type="image/png",
        width=200, height=200, exif_orientation=None, applied_transform="none",
        decoded_pixel_count=40_000, original_byte_count=100,
        preprocessing_version="test-v1",
    )

    def result_for(expected: str, lines: tuple[str, ...]):
        request = AnalysisRequest.model_validate({
            "schemaVersion": "verification-request-v2",
            "checks": [{"fieldId": "class_type", "required": True, "expectedValue": expected}],
            "panels": [{"panelId": "p01", "file": "label.png"}],
        })
        fixture = build_mock_analysis(request, panel, blank=False)
        blocks = tuple(
            VisionTextBlock(
                block_id=f"p01-b{index:03d}", text=text,
                model_bounding_box=BoundingBox(x=10, y=index * 20, width=120, height=15),
                reading_order=index - 1, legibility="clear",
            )
            for index, text in enumerate(lines, start=1)
        )
        vision = fixture.vision_run.model_copy(update={
            "panels": (fixture.vision_run.panels[0].model_copy(update={
                "full_text": "\n".join(lines), "fields": (), "text_blocks": blocks,
            }),),
        })
        return _library_rules(request, vision)[0]

    close = result_for(
        "BELGIAN-STYLE QUAD ALE",
        ("BELGIAN-STYLE", "QUAD", "9.7% ALC./VOL.", "ALE"),
    )
    assert close.automated_status == "review"
    assert close.reason_code == "closest_text_differs"
    assert close.detected_value == "BELGIAN-STYLE QUAD"
    assert close.evidence_block_ids == ("p01-b001", "p01-b002")

    unrelated = result_for("A7Y7 LAGER DE FOUDRE", ("GERMAN STYLE LAGER",))
    assert unrelated.automated_status == "review"
    assert unrelated.reason_code == "expected_phrase_not_found"
    assert unrelated.detected_value is None
    assert unrelated.evidence_block_ids == ()
