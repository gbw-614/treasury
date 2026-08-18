from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from app.schemas import (
    AnalysisRequest,
    AnalysisResponse,
    AutomatedStatus,
    BeverageCategory,
    BoundingBox,
    FieldKey,
    LocalizationResult,
    LocationStatus,
    OcrRun,
    OcrToken,
    OverallSummary,
    ReviewTask,
    RuleResult,
    StageDuration,
    ValidatedPanel,
    VisionFieldCandidate,
    VisionPanelExtraction,
    VisionRun,
    VisionWarningPresentation,
)
from app.warning_requirements import CANONICAL_WARNING_BODY


@dataclass(frozen=True)
class SampleFixture:
    """Optional deterministic values used only by backend tests."""

    brand_name: str
    class_type: str
    abv_percent: float | None
    proof: float | None
    warning_heading: str | None
    warning_body: str | None
    force_review: bool


@dataclass(frozen=True)
class MockField:
    key: FieldKey
    raw_text: str
    normalized_value: str | float


MOCK_FIELDS = (
    MockField(FieldKey.BRAND_NAME, "Treasury Sample", "Treasury Sample"),
    MockField(FieldKey.CLASS_TYPE, "Bourbon Whisky", "Bourbon Whisky"),
    MockField(FieldKey.ALCOHOL_CONTENT, "45% Alc./Vol.", 45.0),
    MockField(FieldKey.PROOF, "90 Proof", 90.0),
    MockField(
        FieldKey.GOVERNMENT_WARNING_HEADING,
        "GOVERNMENT WARNING:",
        "GOVERNMENT WARNING:",
    ),
    MockField(
        FieldKey.GOVERNMENT_WARNING_BODY,
        CANONICAL_WARNING_BODY,
        CANONICAL_WARNING_BODY,
    ),
)


def _display_number(value: float) -> str:
    return f"{value:g}"


def _fixture_fields(fixture: SampleFixture) -> tuple[MockField, ...]:
    fields: list[MockField] = [
        MockField(FieldKey.BRAND_NAME, fixture.brand_name, fixture.brand_name),
        MockField(FieldKey.CLASS_TYPE, fixture.class_type, fixture.class_type),
    ]
    if fixture.abv_percent is not None:
        fields.append(
            MockField(
                FieldKey.ALCOHOL_CONTENT,
                f"{_display_number(fixture.abv_percent)}% Alc./Vol.",
                fixture.abv_percent,
            )
        )
    if fixture.proof is not None:
        fields.append(
            MockField(
                FieldKey.PROOF,
                f"{_display_number(fixture.proof)} Proof",
                fixture.proof,
            )
        )
    if fixture.warning_heading is not None:
        fields.append(
            MockField(
                FieldKey.GOVERNMENT_WARNING_HEADING,
                fixture.warning_heading,
                fixture.warning_heading,
            )
        )
    if fixture.warning_body is not None:
        fields.append(
            MockField(
                FieldKey.GOVERNMENT_WARNING_BODY,
                fixture.warning_body,
                fixture.warning_body,
            )
        )
    return tuple(fields)


def _normalize_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _tokenize_fields(
    fields: Iterable[MockField], width: int, height: int
) -> tuple[tuple[OcrToken, ...], dict[FieldKey, tuple[str, ...]]]:
    tokens: list[OcrToken] = []
    field_tokens: dict[FieldKey, tuple[str, ...]] = {}
    fields_tuple = tuple(fields)
    margin_x = max(1, width // 20)
    usable_width = max(1, width - 2 * margin_x)
    line_height = max(1, height // 18)
    line_spacing = max(1, height // (len(fields_tuple) + 2))

    for line_index, field in enumerate(fields_tuple, start=1):
        words = field.raw_text.split()
        word_width = max(1, usable_width // max(1, len(words)))
        y = min(max(0, line_spacing * line_index), max(0, height - line_height))
        ids: list[str] = []
        for word_index, word in enumerate(words):
            token_id = f"t{len(tokens) + 1:03d}"
            x = min(margin_x + word_index * word_width, max(0, width - 1))
            box_width = max(1, min(word_width - 1 if word_width > 1 else 1, width - x))
            token = OcrToken(
                token_id=token_id,
                text=word,
                bounding_box=BoundingBox(
                    x=x,
                    y=y,
                    width=box_width,
                    height=min(line_height, height - y),
                ),
                confidence=96.0,
                block_id="b001",
                line_id=f"l{line_index:03d}",
                reading_order=len(tokens),
            )
            tokens.append(token)
            ids.append(token_id)
        field_tokens[field.key] = tuple(ids)

    return tuple(tokens), field_tokens


def _boxes_for_tokens(
    tokens: tuple[OcrToken, ...], token_ids: tuple[str, ...]
) -> tuple[BoundingBox, ...]:
    accepted = set(token_ids)
    return tuple(token.bounding_box for token in tokens if token.token_id in accepted)


def _union_box(boxes: tuple[BoundingBox, ...]) -> BoundingBox | None:
    if not boxes:
        return None
    left = min(box.x for box in boxes)
    top = min(box.y for box in boxes)
    right = max(box.x + box.width for box in boxes)
    bottom = max(box.y + box.height for box in boxes)
    return BoundingBox(
        x=left,
        y=top,
        width=right - left,
        height=bottom - top,
    )

def _rule(
    *,
    field_key: FieldKey,
    expected: str | float | Decimal | None,
    detected: str | float | None,
    evidence_quote: str | None,
    localization_id: str | None,
    unreadable: bool,
    confirmed_absent: bool = False,
    applicable: bool = True,
) -> RuleResult:
    rule_id = f"{field_key.value}-comparison"
    localizations = (localization_id,) if localization_id else ()

    if not applicable or expected is None:
        return RuleResult(
            rule_id=rule_id,
            rule_version="fixture-rule-v1",
            field_key=field_key,
            applicable=False,
            automated_status=AutomatedStatus.DOES_NOT_APPLY,
            expected_value=None,
            detected_value=detected,
            evidence_quote=evidence_quote,
            localization_ids=localizations,
            reason_code="not_applicable",
            explanation="No expected value was supplied for this field.",
            requires_human_review=False,
        )

    serialized_expected: str | float = (
        float(expected) if isinstance(expected, Decimal) else expected
    )
    if confirmed_absent:
        return RuleResult(
            rule_id=rule_id,
            rule_version="fixture-rule-v1",
            field_key=field_key,
            applicable=True,
            automated_status=AutomatedStatus.REVIEW,
            expected_value=serialized_expected,
            detected_value=None,
            evidence_quote=None,
            localization_ids=(),
            reason_code="required_text_missing",
            explanation="The selected fixture reader did not find the required text.",
            requires_human_review=True,
        )

    if unreadable or detected is None:
        return RuleResult(
            rule_id=rule_id,
            rule_version="fixture-rule-v1",
            field_key=field_key,
            applicable=True,
            automated_status=AutomatedStatus.REVIEW,
            expected_value=serialized_expected,
            detected_value=None,
            evidence_quote=None,
            localization_ids=(),
            reason_code="evidence_unreadable",
            explanation="The selected fixture reader could not recover dependable evidence.",
            requires_human_review=True,
        )

    if isinstance(serialized_expected, float) and isinstance(detected, float):
        matches = serialized_expected == detected
    else:
        matches = _normalize_text(str(serialized_expected)) == _normalize_text(
            str(detected)
        )

    status = AutomatedStatus.MATCHES if matches else AutomatedStatus.REVIEW
    return RuleResult(
        rule_id=rule_id,
        rule_version="fixture-rule-v1",
        field_key=field_key,
        applicable=True,
        automated_status=status,
        expected_value=serialized_expected,
        detected_value=detected,
        evidence_quote=evidence_quote,
        localization_ids=localizations,
        reason_code="normalized_match" if matches else "detected_value_differs",
        explanation=(
            "The detected value matches the expected value after deterministic normalization."
            if matches
            else "The detected value differs from the expected value."
        ),
        requires_human_review=not matches,
    )


def build_mock_analysis(
    request: AnalysisRequest,
    panel: ValidatedPanel,
    *,
    blank: bool,
    fixture: SampleFixture | None = None,
) -> AnalysisResponse:
    unreadable = blank or bool(fixture and fixture.force_review)
    fixture_fields = _fixture_fields(fixture) if fixture else MOCK_FIELDS
    active_fields = () if unreadable else fixture_fields
    tokens, field_token_ids = _tokenize_fields(active_fields, panel.width, panel.height)
    candidates = tuple(
        VisionFieldCandidate(
            field_key=field.key,
            raw_text=field.raw_text,
            normalized_value=field.normalized_value,
            evidence_quote=field.raw_text,
            model_bounding_box=_union_box(
                _boxes_for_tokens(tokens, field_token_ids.get(field.key, ()))
            ),
            panel_id=panel.panel_id,
            legibility="clear",
        )
        for field in active_fields
    )
    transcript = "\n".join(field.raw_text for field in active_fields)
    raw_fixture = json.dumps(
        [candidate.model_dump(mode="json", by_alias=True) for candidate in candidates],
        sort_keys=True,
    ).encode("utf-8")
    vision_run = VisionRun(
        schema_version="vision-extraction-v1",
        prompt_version="blind-extraction-fixture-v1",
        provider="fixture",
        requested_model="fixture-reader-v1",
        response_model="fixture-reader-v1",
        provider_request_id=f"fixture-{uuid4()}",
        raw_response_sha256=hashlib.sha256(raw_fixture).hexdigest(),
        panels=(
            VisionPanelExtraction(
                panel_id=panel.panel_id,
                full_text=transcript,
                fields=candidates,
                warning_presentation=(
                    VisionWarningPresentation(
                        heading_all_caps=True,
                        heading_only_bold=True,
                        continuous_paragraph=True,
                        separate_and_apart=True,
                        legible_contrast=True,
                        text_appears_unusually_small=False,
                    )
                    if not unreadable
                    and any(
                        field.key == FieldKey.GOVERNMENT_WARNING_HEADING
                        for field in active_fields
                    )
                    else None
                ),
                observations=(
                    (
                        "The supplied image appears blank or uniform."
                        if blank
                        else "This curated sample is routed to human review because its artwork is not dependably readable."
                    ),
                )
                if unreadable
                else (),
            ),
        ),
        duration_ms=0.0,
    )
    ocr_run = OcrRun(
        schema_version="ocr-geometry-v1",
        adapter_version="fixture-ocr-v1",
        engine="fixture",
        engine_version="fixture-1",
        language_data="fixture-eng-v1",
        executable="not-invoked",
        preprocessing_version=panel.preprocessing_version,
        panel_id=panel.panel_id,
        source_sha256=panel.source_sha256,
        original_width=panel.width,
        original_height=panel.height,
        transcript=transcript,
        tokens=tokens,
        exit_status=0,
        warnings=(
            (
                "Uniform image; fixture returned no text."
                if blank
                else "Curated difficult sample; fixture returned no dependable text."
            ),
        )
        if unreadable
        else (),
        preprocessing_duration_ms=0.0,
        ocr_duration_ms=0.0,
    )

    localizations: list[LocalizationResult] = []
    localization_by_field: dict[FieldKey, str] = {}
    detected = {field.key: field for field in active_fields}
    for field_key in FieldKey:
        field = detected.get(field_key)
        localization_id = f"loc-{field_key.value}"
        localization_by_field[field_key] = localization_id
        token_ids = field_token_ids.get(field_key, ())
        localizations.append(
            LocalizationResult(
                localization_id=localization_id,
                field_key=field_key,
                quote=field.raw_text if field else "",
                panel_id=panel.panel_id,
                status=(
                    LocationStatus.LOCATED if token_ids else LocationStatus.UNAVAILABLE
                ),
                accepted_token_ids=token_ids,
                display_boxes=_boxes_for_tokens(tokens, token_ids),
                similarity=1.0 if token_ids else None,
                reason=(
                    "The fixture quote maps exactly to fixture OCR tokens."
                    if token_ids
                    else "No readable quote was available to locate."
                ),
            )
        )

    expected = request.expected
    field_expectations: tuple[
        tuple[FieldKey, str | float | Decimal | None, bool, bool], ...
    ] = (
        (FieldKey.BRAND_NAME, expected.brand_name, True, False),
        (FieldKey.CLASS_TYPE, expected.class_type, True, False),
        (
            FieldKey.ALCOHOL_CONTENT,
            expected.abv_percent,
            expected.abv_percent is not None,
            False,
        ),
        (
            FieldKey.PROOF,
            expected.proof,
            request.category == BeverageCategory.DISTILLED_SPIRITS
            and expected.proof is not None,
            False,
        ),
        (
            FieldKey.GOVERNMENT_WARNING_HEADING,
            "GOVERNMENT WARNING:",
            True,
            bool(fixture and not unreadable and fixture.warning_heading is None),
        ),
        (
            FieldKey.GOVERNMENT_WARNING_BODY,
            CANONICAL_WARNING_BODY,
            True,
            bool(fixture and not unreadable and fixture.warning_body is None),
        ),
    )
    rules = tuple(
        _rule(
            field_key=field_key,
            expected=expected_value,
            detected=(
                detected[field_key].normalized_value if field_key in detected else None
            ),
            evidence_quote=(
                detected[field_key].raw_text if field_key in detected else None
            ),
            localization_id=localization_by_field[field_key],
            unreadable=unreadable,
            confirmed_absent=confirmed_absent,
            applicable=applicable,
        )
        for field_key, expected_value, applicable, confirmed_absent in field_expectations
    )

    if any(rule.automated_status == AutomatedStatus.DISCREPANCY for rule in rules):
        overall = OverallSummary.AUTOMATED_DISCREPANCY
    elif any(rule.automated_status == AutomatedStatus.REVIEW for rule in rules):
        overall = OverallSummary.NEEDS_REVIEW
    else:
        overall = OverallSummary.NO_AUTOMATED_DISCREPANCY

    review_tasks = tuple(
        ReviewTask(
            field_key=rule.field_key,
            reason_code=rule.reason_code,
            message=(
                "Confirm the detected discrepancy against the artwork."
                if rule.reason_code in {"detected_value_differs", "required_text_missing"}
                else "Review this field because readable evidence was unavailable."
            ),
            localization_ids=rule.localization_ids,
        )
        for rule in rules
        if rule.requires_human_review
    )

    return AnalysisResponse(
        schema_version="analysis-response-v1",
        analysis_id=str(uuid4()),
        mode="connected",
        reader_mode=request.reader_mode,
        overall_summary=overall,
        panels=(panel,),
        vision_run=vision_run,
        ocr_run=ocr_run,
        localizations=tuple(localizations),
        rule_results=rules,
        review_tasks=review_tasks,
        stage_durations=(
            StageDuration(stage="validate_artwork", milliseconds=0.0),
            StageDuration(stage="fixture_analysis", milliseconds=0.0),
        ),
        total_duration_ms=0.0,
    )
