from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from decimal import Decimal
from uuid import uuid4

from rapidfuzz import fuzz

from app.field_library import (
    CheckEvaluation,
    evaluate_check,
    get_field_definition,
    normalize_text,
)
from app.schemas import (
    AdditionalRuleResult,
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
    VisionTextBlock,
)
from app.services import recognition_cache
from app.services.openrouter_vision import cache_identity as vision_cache_identity
from app.services.openrouter_vision import run_openrouter_vision, unavailable_vision_run
from app.services.quote_alignment import align_quote
from app.services.tesseract_ocr import cache_identity as ocr_cache_identity
from app.services.tesseract_ocr import run_tesseract


def _ocr_concurrency() -> int:
    """Bound Tesseract workers even when one analysis contains many panels."""
    try:
        return max(1, int(os.getenv("VERIFICATION_OCR_CONCURRENCY", "1")))
    except ValueError:
        return 1


OCR_SEMAPHORE = asyncio.Semaphore(_ocr_concurrency())


def _vision_concurrency() -> int:
    """Bound concurrent provider calls, including panels within one case."""
    try:
        return max(1, int(os.getenv("VERIFICATION_VISION_CONCURRENCY", "3")))
    except ValueError:
        return 3


VISION_SEMAPHORE = asyncio.Semaphore(_vision_concurrency())


def _cache_key(reader_type: str, image_sha256: str, reader_version: str) -> str:
    value = f"{reader_type}|{image_sha256}|{reader_version}".encode()
    return hashlib.sha256(value).hexdigest()


def _rebind_ocr_run(run: OcrRun, panel: ValidatedPanel) -> OcrRun:
    tokens = tuple(
        token.model_copy(update={"token_id": f"{panel.panel_id}-t{index:04d}"})
        for index, token in enumerate(run.tokens, start=1)
    )
    return run.model_copy(update={
        "panel_id": panel.panel_id,
        "source_sha256": panel.source_sha256,
        "original_width": panel.width,
        "original_height": panel.height,
        "tokens": tokens,
        "preprocessing_duration_ms": 0.0,
        "ocr_duration_ms": 0.0,
        "warnings": tuple(dict.fromkeys((*run.warnings, "Loaded from recognition cache."))),
    })


def _cached_ocr(content: bytes, panel: ValidatedPanel) -> OcrRun:
    identity = ocr_cache_identity()
    key = _cache_key("ocr", panel.source_sha256, identity)
    cached = recognition_cache.get(key, OcrRun)
    if cached is not None:
        return _rebind_ocr_run(cached, panel)
    result = run_tesseract(content, panel)
    if result.exit_status == 0:
        recognition_cache.put(
            key, reader_type="ocr", image_sha256=panel.source_sha256,
            reader_version=identity, result=result,
        )
    return result


def _rebind_vision_run(run: VisionRun, panel: ValidatedPanel) -> VisionRun:
    extraction = run.panels[0]
    fields = tuple(field.model_copy(update={"panel_id": panel.panel_id}) for field in extraction.fields)
    rebound = extraction.model_copy(update={"panel_id": panel.panel_id, "fields": fields})
    return run.model_copy(update={"panels": (rebound,), "duration_ms": 0.0})


def _cached_vision(content: bytes, panel: ValidatedPanel) -> VisionRun:
    identity = vision_cache_identity()
    key = _cache_key("llm", panel.source_sha256, identity)
    cached = recognition_cache.get(key, VisionRun)
    if cached is not None:
        return _rebind_vision_run(cached, panel)
    result = run_openrouter_vision(content, panel)
    if result.response_model != "unavailable":
        recognition_cache.put(
            key, reader_type="llm", image_sha256=panel.source_sha256,
            reader_version=identity, result=result,
        )
    return result


async def _run_ocr_limited(content: bytes, panel: ValidatedPanel) -> OcrRun:
    async with OCR_SEMAPHORE:
        return await asyncio.to_thread(_cached_ocr, content, panel)


async def _run_vision_limited(content: bytes, panel: ValidatedPanel) -> VisionRun:
    async with VISION_SEMAPHORE:
        return await asyncio.to_thread(_cached_vision, content, panel)

RULE_VERSION = "deterministic-comparison-v1"


def _normalized_words(value: str) -> str:
    # Ampersand and the word "and" are semantically equivalent in product
    # names/types, while punctuation elsewhere remains available for stricter
    # field-specific checks.
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold().replace("&", " and ")))


def _normalized_word_tuple(value: str) -> tuple[str, ...]:
    return tuple(_normalized_words(value).split())


def _contains_expected_phrase(expected: str, detected: str) -> bool:
    """Match the expected words as one contiguous phrase inside detected text."""
    expected_words = _normalized_word_tuple(expected)
    detected_words = _normalized_word_tuple(detected)
    if not expected_words or len(detected_words) < len(expected_words):
        return False
    width = len(expected_words)
    return any(
        detected_words[index : index + width] == expected_words
        for index in range(len(detected_words) - width + 1)
    )


def _transcript_phrase_match(
    vision: VisionRun,
    expected: str,
) -> tuple[str, str] | None:
    """Find expected words contiguously in the model's literal transcript."""
    expected_words = _normalized_word_tuple(expected)
    if not expected_words:
        return None
    for panel in vision.panels:
        spans = [
            (
                "and" if match.group() == "&" else match.group().casefold(),
                match.start(),
                match.end(),
            )
            for match in re.finditer(r"[a-z0-9]+|&", panel.full_text, flags=re.IGNORECASE)
        ]
        width = len(expected_words)
        for index in range(len(spans) - width + 1):
            if tuple(item[0] for item in spans[index : index + width]) == expected_words:
                literal = panel.full_text[spans[index][1] : spans[index + width - 1][2]]
                return panel.panel_id, literal
    return None


def _layout_text(value: str) -> str:
    """Ignore artwork line wrapping while preserving case and punctuation."""
    return " ".join(value.split())


def _warning_body_text(value: str) -> str:
    """Warning-body capitalization is typographic, not a wording change."""
    return _layout_text(value).casefold()


def _number(value: str | float | None) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str):
        return None
    match = re.search(r"\d+(?:\.\d+)?", value)
    return float(match.group()) if match else None


def _candidate_map(
    vision: VisionRun,
    request: AnalysisRequest | None = None,
) -> dict[FieldKey, VisionFieldCandidate]:
    """Choose one deterministic candidate per field across ordered panels.

    A clear candidate wins over an uncertain one; ties keep the first uploaded
    panel. The raw per-panel extraction remains in `vision.panels` for review.
    """
    candidates: dict[FieldKey, VisionFieldCandidate] = {}
    for panel in vision.panels:
        for candidate in panel.fields:
            current = candidates.get(candidate.field_key)
            if current is None or (
                current.legibility != "clear" and candidate.legibility == "clear"
            ):
                candidates[candidate.field_key] = candidate

    # Brand and class/type are literal presence checks. Prefer a candidate that
    # contains the supplied phrase over the first equally legible semantic
    # classification returned from another panel.
    if request is not None and request.expected is not None:
        expected_by_field = {
            FieldKey.BRAND_NAME: request.expected.brand_name,
            FieldKey.CLASS_TYPE: request.expected.class_type,
        }
        all_candidates = tuple(
            candidate for panel in vision.panels for candidate in panel.fields
        )
        for field_key, expected in expected_by_field.items():
            if not expected:
                continue
            matching = next(
                (
                    candidate
                    for candidate in all_candidates
                    if candidate.field_key == field_key
                    and _contains_expected_phrase(
                        str(expected),
                        str(candidate.normalized_value or candidate.raw_text),
                    )
                ),
                None,
            )
            if matching is not None:
                candidates[field_key] = matching
    return candidates


def _localizations(
    default_panel: ValidatedPanel,
    candidates: dict[FieldKey, VisionFieldCandidate],
    ocr_by_panel: dict[str, OcrRun],
) -> tuple[LocalizationResult, ...]:
    locations: list[LocalizationResult] = []
    for field_key in FieldKey:
        candidate = candidates.get(field_key)
        if candidate is None or not candidate.evidence_quote.strip():
            locations.append(
                LocalizationResult(
                    localization_id=f"loc-{field_key.value}",
                    field_key=field_key,
                    quote="",
                    panel_id=default_panel.panel_id,
                    status=LocationStatus.UNAVAILABLE,
                    reason="The vision reader supplied no literal evidence quote for this field.",
                )
            )
            continue
        ocr_run = ocr_by_panel[candidate.panel_id]
        aligned = align_quote(
            field_key=field_key,
            quote=candidate.evidence_quote,
            panel_id=candidate.panel_id,
            ocr_run=ocr_run,
        )
        locations.append(
            aligned.model_copy(update={"localization_id": f"loc-{field_key.value}-{candidate.panel_id}"})
        )
    return tuple(locations)


def _base_rule(
    *,
    field_key: FieldKey,
    expected: str | float | Decimal | None,
    candidate: VisionFieldCandidate | None,
    location: LocalizationResult,
    applicable: bool,
    matches: bool | None,
    detected: str | float | None,
    require_ocr_location: bool,
    missing_is_discrepancy: bool = False,
    formatting_requires_review: bool = False,
    formatting_review_explanation: str | None = None,
    formatting_discrepancy: str | None = None,
    mismatch_explanation: str = "The detected value differs from the expected value.",
    include_localization: bool = True,
    match_reason_code: str = "normalized_match",
    match_explanation: str = "The detected value matches after deterministic normalization.",
) -> RuleResult:
    expected_value = float(expected) if isinstance(expected, Decimal) else expected
    localization_ids = (
        (location.localization_id,)
        if candidate and include_localization
        else ()
    )
    if not applicable or expected_value is None:
        return RuleResult(
            rule_id=f"{field_key.value}-comparison",
            rule_version=RULE_VERSION,
            field_key=field_key,
            applicable=False,
            automated_status=AutomatedStatus.DOES_NOT_APPLY,
            expected_value=None,
            detected_value=detected,
            evidence_quote=candidate.evidence_quote if candidate else None,
            localization_ids=localization_ids,
            reason_code="not_applicable",
            explanation="No expected value was supplied for this field.",
            requires_human_review=False,
        )
    if candidate is None:
        status = (
            AutomatedStatus.DISCREPANCY
            if missing_is_discrepancy
            else AutomatedStatus.REVIEW
        )
        return RuleResult(
            rule_id=f"{field_key.value}-comparison",
            rule_version=RULE_VERSION,
            field_key=field_key,
            applicable=True,
            automated_status=status,
            expected_value=expected_value,
            detected_value=None,
            evidence_quote=None,
            reason_code=(
                "required_text_missing"
                if missing_is_discrepancy
                else "vision_evidence_unavailable"
            ),
            explanation=(
                "The selected reader found readable label text but no required warning text."
                if missing_is_discrepancy
                else "The vision reader did not return dependable evidence for this field."
            ),
            requires_human_review=True,
        )
    if candidate.legibility != "clear":
        return RuleResult(
            rule_id=f"{field_key.value}-comparison",
            rule_version=RULE_VERSION,
            field_key=field_key,
            applicable=True,
            automated_status=AutomatedStatus.REVIEW,
            expected_value=expected_value,
            detected_value=detected,
            evidence_quote=candidate.evidence_quote,
            localization_ids=localization_ids,
            reason_code="vision_evidence_uncertain",
            explanation=candidate.uncertainty or "The vision reader marked this text uncertain.",
            requires_human_review=True,
        )
    if formatting_discrepancy:
        return RuleResult(
            rule_id=f"{field_key.value}-comparison",
            rule_version=RULE_VERSION,
            field_key=field_key,
            applicable=True,
            automated_status=AutomatedStatus.REVIEW,
            expected_value=expected_value,
            detected_value=detected,
            evidence_quote=candidate.evidence_quote,
            localization_ids=localization_ids,
            reason_code="warning_presentation_noncompliant",
            explanation=formatting_discrepancy,
            requires_human_review=True,
        )
    if formatting_requires_review:
        return RuleResult(
            rule_id=f"{field_key.value}-comparison",
            rule_version=RULE_VERSION,
            field_key=field_key,
            applicable=True,
            automated_status=AutomatedStatus.REVIEW,
            expected_value=expected_value,
            detected_value=detected,
            evidence_quote=candidate.evidence_quote,
            localization_ids=localization_ids,
            reason_code="warning_presentation_uncertain",
            explanation=(
                formatting_review_explanation
                or "The warning presentation could not be confirmed automatically."
            ),
            requires_human_review=True,
        )
    if require_ocr_location and location.status != LocationStatus.LOCATED:
        return RuleResult(
            rule_id=f"{field_key.value}-comparison",
            rule_version=RULE_VERSION,
            field_key=field_key,
            applicable=True,
            automated_status=AutomatedStatus.REVIEW,
            expected_value=expected_value,
            detected_value=detected,
            evidence_quote=candidate.evidence_quote,
            localization_ids=localization_ids,
            reason_code=location.status.value,
            explanation="Tesseract could not uniquely locate the detected text on the artwork.",
            requires_human_review=True,
        )
    if matches is None:
        return RuleResult(
            rule_id=f"{field_key.value}-comparison",
            rule_version=RULE_VERSION,
            field_key=field_key,
            applicable=True,
            automated_status=AutomatedStatus.REVIEW,
            expected_value=expected_value,
            detected_value=detected,
            evidence_quote=candidate.evidence_quote,
            localization_ids=localization_ids,
            reason_code="detected_value_unparseable",
            explanation="The detected evidence could not be normalized for comparison.",
            requires_human_review=True,
        )
    return RuleResult(
        rule_id=f"{field_key.value}-comparison",
        rule_version=RULE_VERSION,
        field_key=field_key,
        applicable=True,
        automated_status=(AutomatedStatus.MATCHES if matches else AutomatedStatus.REVIEW),
        expected_value=expected_value,
        detected_value=detected,
        evidence_quote=candidate.evidence_quote,
        localization_ids=localization_ids,
        reason_code=match_reason_code if matches else "detected_value_differs",
        explanation=(
            match_explanation
            if matches
            else mismatch_explanation
        ),
        requires_human_review=not matches,
    )


def _rules(
    request: AnalysisRequest,
    vision: VisionRun,
    locations: tuple[LocalizationResult, ...],
) -> tuple[RuleResult, ...]:
    assert request.expected is not None
    candidates = _candidate_map(vision, request)
    by_location = {location.field_key: location for location in locations}
    expected = request.expected
    rules: list[RuleResult] = []

    for field_key, expected_value in (
        (FieldKey.BRAND_NAME, expected.brand_name),
        (FieldKey.CLASS_TYPE, expected.class_type),
    ):
        candidate = candidates.get(field_key)
        detected = (
            str(candidate.normalized_value or candidate.raw_text) if candidate else None
        )
        matches = (
            _normalized_words(str(expected_value)) == _normalized_words(detected)
            if detected
            else None
        )
        include_localization = True
        match_reason_code = "normalized_match"
        match_explanation = "The detected value matches after deterministic normalization."
        if (
            expected_value
            and matches is False
            and detected
            and _contains_expected_phrase(str(expected_value), detected)
        ):
            matches = True
            match_reason_code = "expected_phrase_in_candidate"
            match_explanation = (
                "The expected value appears as a contiguous phrase in the reader's classified text."
            )
        elif expected_value and matches is not True:
            transcript_match = _transcript_phrase_match(vision, str(expected_value))
            if transcript_match:
                panel_id, literal = transcript_match
                candidate = VisionFieldCandidate(
                    field_key=field_key,
                    raw_text=literal,
                    normalized_value=literal,
                    evidence_quote=literal,
                    model_bounding_box=None,
                    panel_id=panel_id,
                    legibility="clear",
                    uncertainty=None,
                )
                detected = literal
                matches = True
                include_localization = False
                match_reason_code = "expected_phrase_in_transcript"
                match_explanation = (
                    "The expected value appears as a contiguous phrase in the model transcript."
                )
        rules.append(
            _base_rule(
                field_key=field_key,
                expected=expected_value,
                candidate=candidate,
                location=by_location[field_key],
                applicable=True,
                matches=matches,
                detected=detected,
                require_ocr_location=request.reader_mode == "ocr",
                include_localization=include_localization,
                match_reason_code=match_reason_code,
                match_explanation=match_explanation,
            )
        )

    for field_key, expected_value, applicable in (
        (
            FieldKey.ALCOHOL_CONTENT,
            expected.abv_percent,
            expected.abv_percent is not None,
        ),
        (
            FieldKey.PROOF,
            expected.proof,
            request.category == BeverageCategory.DISTILLED_SPIRITS
            and expected.proof is not None,
        ),
    ):
        candidate = candidates.get(field_key)
        detected_number = (
            _number(candidate.normalized_value)
            if candidate
            else None
        )
        expected_number = float(expected_value) if expected_value is not None else None
        matches = (
            abs(detected_number - expected_number) <= 0.05
            if detected_number is not None and expected_number is not None
            else None
        )
        rules.append(
            _base_rule(
                field_key=field_key,
                expected=expected_value,
                candidate=candidate,
                location=by_location[field_key],
                applicable=applicable,
                matches=matches,
                detected=detected_number,
                require_ocr_location=request.reader_mode == "ocr",
            )
        )

    expected_warning = expected.government_warning
    warning_applicable = expected_warning is not None
    expected_heading = expected_warning.heading if expected_warning else None
    heading = candidates.get(FieldKey.GOVERNMENT_WARNING_HEADING)
    heading_exact_match = (
        _layout_text(heading.raw_text) == _layout_text(expected_heading)
        if heading and expected_heading
        else None
    )
    warning_panel = next(
        (panel for panel in vision.panels if heading and panel.panel_id == heading.panel_id),
        None,
    )
    presentation = warning_panel.warning_presentation if warning_panel else None
    heading_failures: list[str] = []
    if presentation and presentation.heading_all_caps is False:
        heading_failures.append("The warning heading is not printed in all capital letters.")
    if presentation and presentation.heading_only_bold is False:
        heading_failures.append(
            "The warning heading is not the only portion visibly bold relative to the warning body."
        )
    heading_presentation_unknown = bool(
        heading
        and (
            presentation is None
            or presentation.heading_all_caps is None
            or presentation.heading_only_bold is None
        )
    )
    rules.append(
        _base_rule(
            field_key=FieldKey.GOVERNMENT_WARNING_HEADING,
            expected=expected_heading,
            candidate=heading,
            location=by_location[FieldKey.GOVERNMENT_WARNING_HEADING],
            applicable=warning_applicable,
            matches=heading_exact_match,
            detected=heading.raw_text if heading else None,
            require_ocr_location=request.reader_mode == "ocr",
            # An extraction miss is not proof that mandatory text is absent.
            # The reviewer must decide this from the artwork unless a reader
            # positively extracts contradictory evidence.
            missing_is_discrepancy=False,
            formatting_requires_review=bool(
                heading_exact_match is True and heading_presentation_unknown
            ),
            formatting_review_explanation=(
                presentation.uncertainty
                if presentation and presentation.uncertainty
                else "The reader could not confirm that the warning heading is uppercase and is the only bold portion."
            ),
            formatting_discrepancy=" ".join(heading_failures) or None,
            mismatch_explanation="The warning heading is not printed exactly as required.",
        )
    )
    warning_body = candidates.get(FieldKey.GOVERNMENT_WARNING_BODY)
    expected_warning_body = expected_warning.body if expected_warning else None
    rules.append(
        _base_rule(
            field_key=FieldKey.GOVERNMENT_WARNING_BODY,
            expected=expected_warning_body,
            candidate=warning_body,
            location=by_location[FieldKey.GOVERNMENT_WARNING_BODY],
            applicable=warning_applicable,
            matches=(
                _warning_body_text(warning_body.raw_text)
                == _warning_body_text(expected_warning_body or "")
                if warning_body and expected_warning_body
                else None
            ),
            detected=warning_body.raw_text if warning_body else None,
            require_ocr_location=request.reader_mode == "ocr",
            missing_is_discrepancy=False,
            formatting_requires_review=bool(
                warning_body
                and _warning_body_text(warning_body.raw_text)
                == _warning_body_text(expected_warning_body or "")
                and (
                    presentation is None
                    or presentation.continuous_paragraph is None
                    or presentation.separate_and_apart is None
                    or presentation.legible_contrast is None
                    or presentation.text_appears_unusually_small is None
                    or presentation.text_appears_unusually_small is True
                )
            ),
            formatting_review_explanation=(
                "The warning appears unusually small; a reviewer must verify the physical type-size requirement."
                if presentation and presentation.text_appears_unusually_small is True
                else (
                    presentation.uncertainty
                    if presentation and presentation.uncertainty
                    else "The reader could not confirm the warning's paragraph layout, separation, contrast, and apparent prominence."
                )
            ),
            formatting_discrepancy=(
                " ".join(
                    message
                    for failed, message in (
                        (
                            presentation.continuous_paragraph is False,
                            "The warning is not presented as one continuous paragraph.",
                        ),
                        (
                            presentation.separate_and_apart is False,
                            "The warning is not visually separate and apart from surrounding text.",
                        ),
                        (
                            presentation.legible_contrast is False,
                            "The warning is not readily legible against its background.",
                        ),
                    )
                    if failed
                )
                if presentation
                else None
            )
            or None,
            mismatch_explanation="The warning body differs from the required statutory wording.",
        )
    )
    return tuple(rules)


def _additional_rules(
    request: AnalysisRequest,
    vision: VisionRun,
) -> tuple[AdditionalRuleResult, ...]:
    """Compare user-supplied optional fields against literal reader text only."""
    assert request.expected is not None
    results: list[AdditionalRuleResult] = []
    for field in request.expected.additional_fields:
        transcript_match = _transcript_phrase_match(vision, field.expected_text)
        if transcript_match:
            _, literal = transcript_match
            results.append(
                AdditionalRuleResult(
                    field_id=field.id,
                    label=field.label,
                    automated_status=AutomatedStatus.MATCHES,
                    expected_value=field.expected_text,
                    detected_value=literal,
                    evidence_quote=literal,
                    reason_code="expected_phrase_in_transcript",
                    explanation="The expected value appears as a contiguous phrase in the reader transcript.",
                    requires_human_review=False,
                )
            )
            continue
        results.append(
            AdditionalRuleResult(
                field_id=field.id,
                label=field.label,
                automated_status=AutomatedStatus.REVIEW,
                expected_value=field.expected_text,
                detected_value=None,
                evidence_quote=None,
                reason_code="expected_phrase_not_found",
                explanation=(
                    "The reader transcript did not contain the expected phrase; "
                    "a reviewer must inspect the artwork."
                ),
                requires_human_review=True,
            )
        )
    return tuple(results)


def _vision_block_ids(panel: VisionPanelExtraction, quote: str | None) -> tuple[str, ...]:
    """Find the smallest consecutive literal-block span supporting a quote."""
    if not quote or not panel.text_blocks:
        return ()
    target = " ".join(re.findall(r"[a-z0-9]+", quote.casefold().replace("&", " and ")))
    if not target:
        return ()
    blocks = tuple(sorted(panel.text_blocks, key=lambda block: block.reading_order))
    for width in range(1, len(blocks) + 1):
        for start in range(len(blocks) - width + 1):
            selected = blocks[start : start + width]
            combined = " ".join(block.text for block in selected)
            normalized = " ".join(
                re.findall(r"[a-z0-9]+", combined.casefold().replace("&", " and "))
            )
            if target in normalized:
                return tuple(block.block_id for block in selected)
    return ()


CLOSEST_TEXT_MIN_SCORE = 80.0


def _closest_text_block_span(
    vision: VisionRun, expected_value: str
) -> tuple[VisionPanelExtraction, str, tuple[str, ...], float] | None:
    """Return the highest-scoring reasonably sized consecutive block span."""
    expected = normalize_text(expected_value)
    expected_word_count = len(expected.split())
    if not expected or expected_word_count == 0:
        return None
    minimum_words = max(1, (expected_word_count + 1) // 2)
    maximum_words = max(minimum_words, (expected_word_count * 3 + 1) // 2)
    best: tuple[VisionPanelExtraction, str, tuple[str, ...], float] | None = None
    for panel in vision.panels:
        blocks = tuple(sorted(panel.text_blocks, key=lambda block: block.reading_order))
        for start in range(len(blocks)):
            for end in range(start + 1, len(blocks) + 1):
                selected = blocks[start:end]
                literal = " ".join(block.text for block in selected).strip()
                normalized = normalize_text(literal)
                word_count = len(normalized.split())
                if word_count > maximum_words:
                    break
                if word_count < minimum_words:
                    continue
                score = float(fuzz.ratio(expected, normalized))
                if best is None or score > best[3]:
                    best = (
                        panel,
                        literal,
                        tuple(block.block_id for block in selected),
                        score,
                    )
    if best is None or best[3] < CLOSEST_TEXT_MIN_SCORE:
        return None
    return best


def _union_vision_block_boxes(
    panel: VisionPanelExtraction, block_ids: tuple[str, ...]
) -> BoundingBox | None:
    boxes = tuple(
        block.model_bounding_box
        for block in panel.text_blocks
        if block.block_id in block_ids and block.model_bounding_box is not None
    )
    if not boxes:
        return None
    left = min(box.x for box in boxes)
    top = min(box.y for box in boxes)
    right = max(box.x + box.width for box in boxes)
    bottom = max(box.y + box.height for box in boxes)
    return BoundingBox(x=left, y=top, width=right - left, height=bottom - top)


def _legacy_candidates_from_literal_blocks(
    request: AnalysisRequest, vision: VisionRun
) -> VisionRun:
    """Derive v1 candidates locally so the model never assigns semantic roles."""
    if request.expected is None or not any(panel.text_blocks for panel in vision.panels):
        return vision
    expected = request.expected
    checks: tuple[tuple[FieldKey, str, str | None], ...] = (
        (FieldKey.BRAND_NAME, "brand_name", expected.brand_name),
        (FieldKey.CLASS_TYPE, "class_type", expected.class_type),
        (
            FieldKey.ALCOHOL_CONTENT,
            "alcohol_content",
            str(expected.abv_percent) if expected.abv_percent is not None else None,
        ),
        (
            FieldKey.PROOF,
            "proof",
            str(expected.proof) if expected.proof is not None else None,
        ),
        (
            FieldKey.GOVERNMENT_WARNING_HEADING,
            "brand_name",
            expected.government_warning.heading if expected.government_warning else None,
        ),
        (
            FieldKey.GOVERNMENT_WARNING_BODY,
            "brand_name",
            expected.government_warning.body if expected.government_warning else None,
        ),
    )
    fields_by_panel: dict[str, list[VisionFieldCandidate]] = {
        panel.panel_id: [] for panel in vision.panels
    }
    for field_key, definition_id, expected_value in checks:
        if expected_value is None:
            continue
        definition = get_field_definition(definition_id)
        for panel in vision.panels:
            evaluation = evaluate_check(definition, panel.full_text, expected_value)
            if not evaluation.matched or not evaluation.evidence_quote:
                continue
            block_ids = _vision_block_ids(panel, evaluation.evidence_quote)
            supporting = tuple(
                block for block in panel.text_blocks if block.block_id in block_ids
            )
            uncertain = tuple(block for block in supporting if block.legibility != "clear")
            fields_by_panel[panel.panel_id].append(VisionFieldCandidate(
                field_key=field_key,
                raw_text=evaluation.evidence_quote,
                normalized_value=evaluation.detected_value,
                evidence_quote=evaluation.evidence_quote,
                model_bounding_box=_union_vision_block_boxes(panel, block_ids),
                panel_id=panel.panel_id,
                legibility="uncertain" if uncertain else "clear",
                uncertainty="; ".join(
                    block.uncertainty or f"{block.block_id} was marked {block.legibility}"
                    for block in uncertain
                ) or None,
            ))
            break
    return vision.model_copy(update={
        "panels": tuple(
            panel.model_copy(update={"fields": tuple(fields_by_panel[panel.panel_id])})
            for panel in vision.panels
        ),
    })


def _library_rules(
    request: AnalysisRequest,
    vision: VisionRun,
) -> tuple[AdditionalRuleResult, ...]:
    """Apply v2 field-library checks to literal reader transcripts.

    This deliberately operates on full text rather than a closed FieldKey enum,
    so new configured fields do not require a model-prompt or category-engine
    change. Evidence localization remains best-effort for the legacy fields.
    """
    results: list[AdditionalRuleResult] = []
    for check in request.checks:
        definition = get_field_definition(check.field_id)
        if not check.required:
            results.append(
                AdditionalRuleResult(
                    field_id=definition.id,
                    label=definition.name,
                    match_mode=definition.check_type,
                    automated_status=AutomatedStatus.DOES_NOT_APPLY,
                    expected_value=check.expected_value,
                    detected_value=None,
                    evidence_quote=None,
                    reason_code="not_required",
                    explanation="This field is not required for this case.",
                    requires_human_review=False,
                )
            )
            continue
        panel_evaluations = tuple(
            (panel, evaluate_check(definition, panel.full_text, check.expected_value))
            for panel in vision.panels
        )
        if definition.check_type == "regex":
            # Regex values are properties of the complete label set. Looking
            # panel-by-panel could accept one matching value while overlooking
            # a conflicting value on another panel or elsewhere on a template.
            evaluation = evaluate_check(
                definition,
                "\n".join(panel.full_text for panel in vision.panels),
                check.expected_value,
            )
            selected_panel = next(
                (
                    panel
                    for panel in vision.panels
                    if any(
                        _vision_block_ids(panel, quote)
                        for quote in evaluation.evidence_quotes
                    )
                ),
                vision.panels[0],
            )
        else:
            selected = next(
                ((panel, item) for panel, item in panel_evaluations if item.matched),
                None,
            ) or next(
                ((panel, item) for panel, item in panel_evaluations if item.detected_value is not None),
                panel_evaluations[0],
            )
            selected_panel, evaluation = selected
        matched = evaluation.matched
        reason_code = evaluation.reason_code
        explanation = evaluation.explanation

        if (
            definition.check_type == "text"
            and not matched
            and check.expected_value
        ):
            closest = _closest_text_block_span(vision, check.expected_value)
            if closest:
                selected_panel, literal, _, score = closest
                evaluation = CheckEvaluation(
                    matched=False,
                    detected_value=literal,
                    evidence_quote=literal,
                    reason_code="closest_text_differs",
                    explanation=(
                        "The expected phrase was not found exactly; the closest "
                        f"literal wording scored {score:.1f}/100 and requires review."
                    ),
                    evidence_quotes=(literal,),
                )
                reason_code = evaluation.reason_code
                explanation = evaluation.explanation

        if definition.check_type == "regex":
            block_ids = tuple(dict.fromkeys(
                block_id
                for panel in vision.panels
                for quote in evaluation.evidence_quotes
                for block_id in _vision_block_ids(panel, quote)
            ))
            supporting_blocks = tuple(
                block
                for panel in vision.panels
                for block in panel.text_blocks
                if block.block_id in block_ids
            )
        else:
            block_ids = _vision_block_ids(selected_panel, evaluation.evidence_quote)
            supporting_blocks = tuple(
                block
                for block in selected_panel.text_blocks
                if block.block_id in block_ids
            )
        uncertain_blocks = tuple(
            block for block in supporting_blocks if block.legibility != "clear"
        )
        if matched and uncertain_blocks:
            matched = False
            reason_code = "matched_text_uncertain"
            details = "; ".join(
                block.uncertainty or f"{block.block_id} was marked {block.legibility}"
                for block in uncertain_blocks
            )
            explanation = (
                "The literal text appears to match, but Gemini marked the supporting "
                f"transcription as uncertain: {details}"
            )
        results.append(
            AdditionalRuleResult(
                field_id=definition.id,
                label=definition.name,
                match_mode=definition.check_type,
                automated_status=(
                    AutomatedStatus.MATCHES if matched else AutomatedStatus.REVIEW
                ),
                expected_value=check.expected_value,
                detected_value=evaluation.detected_value,
                evidence_quote=evaluation.evidence_quote,
                evidence_block_ids=block_ids,
                reason_code=reason_code,
                explanation=explanation,
                requires_human_review=not matched,
            )
        )
        # Text and presentation are intentionally separate results. OCR can
        # establish the statutory words and their geometry, but it cannot
        # establish whether only the heading is bold. Do not discard a text
        # match merely because the reviewer must still assess typography.
        if definition.check_type == "warning" and matched:
            if request.reader_mode == "ocr":
                presentation_status = AutomatedStatus.REVIEW
                presentation_detected = None
                presentation_reason = "warning_heading_boldness_human_review"
                presentation_explanation = (
                    "The statutory wording was located. Confirm visually that "
                    "only the GOVERNMENT WARNING: heading is bold relative to "
                    "the warning body."
                )
            else:
                presentation = selected_panel.warning_presentation
                failures: list[str] = []
                unknown = presentation is None
                if presentation:
                    if presentation.heading_all_caps is False:
                        failures.append("The warning heading is not all uppercase.")
                    if presentation.heading_only_bold is False:
                        failures.append("The warning heading is not the only portion bold relative to the body.")
                    if presentation.continuous_paragraph is False:
                        failures.append("The warning is not one continuous paragraph.")
                    if presentation.separate_and_apart is False:
                        failures.append("The warning is not separate and apart from surrounding text.")
                    if presentation.legible_contrast is False:
                        failures.append("The warning is not readily legible against its background.")
                    unknown = any(value is None for value in (
                        presentation.heading_all_caps,
                        presentation.heading_only_bold,
                        presentation.continuous_paragraph,
                        presentation.separate_and_apart,
                        presentation.legible_contrast,
                    )) or presentation.text_appears_unusually_small is None
                    if presentation.text_appears_unusually_small is True:
                        unknown = True
                        failures.append("The warning appears unusually small and needs human review.")
                presentation_status = (
                    AutomatedStatus.MATCHES
                    if not failures and not unknown
                    else AutomatedStatus.REVIEW
                )
                presentation_detected = "Confirmed by Gemini" if presentation_status == AutomatedStatus.MATCHES else None
                presentation_reason = "warning_presentation_noncompliant" if failures else "warning_presentation_uncertain"
                presentation_explanation = " ".join(failures) or (
                    presentation.uncertainty
                    if presentation and presentation.uncertainty
                    else "Gemini could not confirm the warning presentation."
                )
            results.append(
                AdditionalRuleResult(
                    field_id="government_warning_presentation",
                    label="Warning presentation",
                    match_mode="warning",
                    automated_status=presentation_status,
                    expected_value="Only the heading is bold",
                    detected_value=presentation_detected,
                    evidence_quote=evaluation.evidence_quote,
                    evidence_block_ids=block_ids,
                    reason_code=presentation_reason,
                    explanation=presentation_explanation,
                    requires_human_review=presentation_status == AutomatedStatus.REVIEW,
                )
            )
    return tuple(results)


def recompare_analysis(
    request: AnalysisRequest,
    previous: AnalysisResponse,
) -> AnalysisResponse:
    """Re-run deterministic comparison without another OCR/model call.

    The raw vision and OCR runs remain byte-for-byte equivalent model objects;
    only deterministic candidate selection, alignment, and rule results are rebuilt.
    """

    started = time.perf_counter()
    ocr_runs = previous.ocr_runs or (previous.ocr_run,)
    vision = previous.vision_run
    if previous.reader_mode == "ocr":
        surrogate = _ocr_surrogate_vision(request, previous.panels, ocr_runs)
        # Rebuild the expected-value-backed OCR candidates, while retaining
        # the immutable reader provenance/hash from the original analysis.
        vision = previous.vision_run.model_copy(update={"panels": surrogate.panels})
    elif request.schema_version == "verification-request-v1":
        vision = _legacy_candidates_from_literal_blocks(request, vision)
    candidates = _candidate_map(vision, request)
    locations = _localizations(
        previous.panels[0],
        candidates,
        {run.panel_id: run for run in ocr_runs},
    )
    if request.schema_version == "verification-request-v2":
        rules = ()
        additional_rules = _library_rules(request, vision)
    else:
        rules = _rules(request, vision, locations)
        additional_rules = _additional_rules(request, vision)
    if any(rule.automated_status == AutomatedStatus.DISCREPANCY for rule in rules):
        overall = OverallSummary.AUTOMATED_DISCREPANCY
    elif any(rule.automated_status == AutomatedStatus.REVIEW for rule in (*rules, *additional_rules)):
        overall = OverallSummary.NEEDS_REVIEW
    else:
        overall = OverallSummary.NO_AUTOMATED_DISCREPANCY
    review_tasks = tuple(
        ReviewTask(
            field_key=rule.field_key,
            reason_code=rule.reason_code,
            message=(
                "Confirm the detected discrepancy against the artwork."
                if rule.reason_code in {
                    "detected_value_differs",
                    "warning_presentation_noncompliant",
                    "required_text_missing",
                }
                else "Review this field because the selected reader did not produce dependable evidence."
            ),
            localization_ids=rule.localization_ids,
        )
        for rule in rules
        if rule.requires_human_review
    )
    comparison_ms = (time.perf_counter() - started) * 1000
    return previous.model_copy(
        update={
            "analysis_id": str(uuid4()),
            "vision_run": vision,
            "localizations": locations,
            "rule_results": rules,
            "additional_rule_results": additional_rules,
            "review_tasks": review_tasks,
            "overall_summary": overall,
            "stage_durations": previous.stage_durations
            + (StageDuration(stage="application_data_recomparison", milliseconds=comparison_ms),),
            "total_duration_ms": previous.total_duration_ms + comparison_ms,
        }
    )


def _combine_vision_runs(runs: tuple[VisionRun, ...]) -> VisionRun:
    """Present parallel per-panel vision calls as one label-set extraction."""
    first = runs[0]
    return VisionRun(
        schema_version="vision-extraction-v1",
        prompt_version=first.prompt_version,
        provider=first.provider,
        requested_model=first.requested_model,
        response_model=first.response_model,
        provider_request_id=",".join(run.provider_request_id for run in runs),
        raw_response_sha256="|".join(run.raw_response_sha256 for run in runs),
        panels=tuple(panel for run in runs for panel in run.panels),
        duration_ms=max(run.duration_ms for run in runs),
        input_tokens=sum(run.input_tokens or 0 for run in runs) or None,
        output_tokens=sum(run.output_tokens or 0 for run in runs) or None,
        estimated_cost_usd=sum(run.estimated_cost_usd or 0 for run in runs) or None,
    )


def _unavailable_ocr_run(panel: ValidatedPanel, reason: str) -> OcrRun:
    """Keep the evidence contract uniform when local OCR was not selected."""
    return OcrRun(
        schema_version="ocr-geometry-v1",
        adapter_version="not-run-v1",
        engine="not-run",
        engine_version="not-run",
        language_data="",
        executable="",
        preprocessing_version="not-run-v1",
        panel_id=panel.panel_id,
        source_sha256=panel.source_sha256,
        original_width=panel.width,
        original_height=panel.height,
        transcript="",
        tokens=(),
        exit_status=0,
        warnings=(reason,),
        preprocessing_duration_ms=0,
        ocr_duration_ms=0,
    )


def _ocr_surrogate_vision(
    request: AnalysisRequest,
    panels: tuple[ValidatedPanel, ...],
    ocr_runs: tuple[OcrRun, ...],
) -> VisionRun:
    """Use only OCR text to create conservative, box-backed field candidates.

    This is intentionally a match-or-review reader: it will not manufacture a
    differing value from a free-text transcript and turn it into a hard fail.
    """

    def positioned_line_blocks(run: OcrRun) -> tuple[VisionTextBlock, ...]:
        """Preserve Tesseract geometry as ordered literal line blocks."""
        lines: dict[str, list[OcrToken]] = {}
        for token in sorted(run.tokens, key=lambda item: item.reading_order):
            lines.setdefault(token.line_id, []).append(token)
        blocks: list[VisionTextBlock] = []
        for index, tokens in enumerate(lines.values()):
            left = min(token.bounding_box.x for token in tokens)
            top = min(token.bounding_box.y for token in tokens)
            right = max(
                token.bounding_box.x + token.bounding_box.width for token in tokens
            )
            bottom = max(
                token.bounding_box.y + token.bounding_box.height for token in tokens
            )
            blocks.append(VisionTextBlock(
                block_id=f"ocr-{run.panel_id}-line-{index + 1:04d}",
                text=" ".join(token.text for token in tokens),
                model_bounding_box=BoundingBox(
                    x=left,
                    y=top,
                    width=right - left,
                    height=bottom - top,
                ),
                reading_order=index,
                legibility="clear",
            ))
        return tuple(blocks)

    # v2 needs no expected-text backed OCR surrogate: generic checks run over
    # the OCR transcript directly below. Its positioned line blocks retain the
    # token-derived evidence geometry used by the review UI.
    if request.schema_version == "verification-request-v2":
        return VisionRun(
            schema_version="vision-extraction-v1",
            prompt_version="ocr-transcript-matcher-v1",
            provider="local",
            requested_model="Tesseract OCR",
            response_model="Tesseract OCR",
            provider_request_id="",
            raw_response_sha256="",
            panels=tuple(
                VisionPanelExtraction(
                    panel_id=panel.panel_id,
                    full_text=run.transcript,
                    fields=(),
                    text_blocks=positioned_line_blocks(run),
                )
                for panel, run in zip(panels, ocr_runs)
            ),
            duration_ms=sum(run.preprocessing_duration_ms + run.ocr_duration_ms for run in ocr_runs),
        )
    assert request.expected is not None
    expected_by_field: dict[FieldKey, str | None] = {
        FieldKey.BRAND_NAME: request.expected.brand_name,
        FieldKey.CLASS_TYPE: request.expected.class_type,
        FieldKey.ALCOHOL_CONTENT: str(request.expected.abv_percent) if request.expected.abv_percent is not None else None,
        FieldKey.PROOF: str(request.expected.proof) if request.expected.proof is not None else None,
        FieldKey.GOVERNMENT_WARNING_HEADING: (
            request.expected.government_warning.heading
            if request.expected.government_warning
            else None
        ),
        FieldKey.GOVERNMENT_WARNING_BODY: (
            request.expected.government_warning.body
            if request.expected.government_warning
            else None
        ),
    }
    by_panel = {run.panel_id: run for run in ocr_runs}
    fields_by_panel: dict[str, list[VisionFieldCandidate]] = {panel.panel_id: [] for panel in panels}
    for field_key, quote in expected_by_field.items():
        if quote is None or (field_key == FieldKey.PROOF and request.category != BeverageCategory.DISTILLED_SPIRITS):
            continue
        matches = [
            align_quote(field_key=field_key, quote=quote, panel_id=panel.panel_id, ocr_run=by_panel[panel.panel_id])
            for panel in panels
        ]
        location = next((item for item in matches if item.status == LocationStatus.LOCATED), None)
        if location is None:
            continue
        run = by_panel[location.panel_id]
        tokens = {token.token_id: token.text for token in run.tokens}
        raw_text = " ".join(tokens[token_id] for token_id in location.accepted_token_ids)
        fields_by_panel[location.panel_id].append(
            VisionFieldCandidate(
                field_key=field_key,
                raw_text=raw_text,
                normalized_value=raw_text,
                evidence_quote=quote,
                panel_id=location.panel_id,
                legibility="clear" if location.similarity == 1 else "uncertain",
                uncertainty=None if location.similarity == 1 else "OCR text was only a fuzzy match to the expected value.",
            )
        )
    return VisionRun(
        schema_version="vision-extraction-v1",
        prompt_version="ocr-transcript-matcher-v1",
        provider="local",
        requested_model="Tesseract OCR",
        response_model="Tesseract OCR",
        provider_request_id="",
        raw_response_sha256="",
        panels=tuple(
            VisionPanelExtraction(
                panel_id=panel.panel_id,
                full_text=by_panel[panel.panel_id].transcript,
                fields=tuple(fields_by_panel[panel.panel_id]),
            )
            for panel in panels
        ),
        duration_ms=sum(run.preprocessing_duration_ms + run.ocr_duration_ms for run in ocr_runs),
    )


async def run_connected_analysis(
    request: AnalysisRequest,
    panels: ValidatedPanel | tuple[ValidatedPanel, ...],
    contents: bytes | tuple[bytes, ...],
    *,
    blank: bool | None = None,
    blanks: tuple[bool, ...] | None = None,
) -> AnalysisResponse:
    # The singular arguments are supported for existing callers; new callers
    # submit ordered label-set tuples.
    normalized_panels = (panels,) if isinstance(panels, ValidatedPanel) else panels
    normalized_contents = (contents,) if isinstance(contents, bytes) else contents
    normalized_blanks = blanks if blanks is not None else ((bool(blank),) * len(normalized_panels))
    if not (
        normalized_panels
        and len(normalized_panels) == len(normalized_contents) == len(normalized_blanks)
        and tuple(panel.panel_id for panel in normalized_panels)
        == tuple(panel.panel_id for panel in request.panels)
    ):
        raise ValueError("The ordered artwork inputs must exactly match the request panels")
    started = time.perf_counter()
    use_vision = request.reader_mode == "llm"
    use_ocr = request.reader_mode == "ocr"
    vision_tasks = tuple(
        asyncio.to_thread(
            unavailable_vision_run,
            panel,
            "The supplied image appears blank or uniform; no provider call was made.",
        )
        if is_blank
        else _run_vision_limited(content, panel)
        for panel, content, is_blank in zip(normalized_panels, normalized_contents, normalized_blanks)
    ) if use_vision else ()
    ocr_tasks = tuple(
        _run_ocr_limited(content, panel)
        for panel, content in zip(normalized_panels, normalized_contents)
    ) if use_ocr else ()
    vision_results, ocr_results = await asyncio.gather(
        asyncio.gather(*vision_tasks), asyncio.gather(*ocr_tasks)
    )
    ocr_runs = tuple(ocr_results) if use_ocr else tuple(
        _unavailable_ocr_run(panel, "OCR was not selected for this analysis.")
        for panel in normalized_panels
    )
    vision = (
        _combine_vision_runs(tuple(vision_results))
        if use_vision
        else _ocr_surrogate_vision(request, normalized_panels, ocr_runs)
    )
    if request.schema_version == "verification-request-v1" and use_vision:
        vision = _legacy_candidates_from_literal_blocks(request, vision)

    alignment_started = time.perf_counter()
    candidates = _candidate_map(vision, request)
    locations = _localizations(
        normalized_panels[0],
        candidates,
        {run.panel_id: run for run in ocr_runs},
    )
    alignment_duration = (time.perf_counter() - alignment_started) * 1000
    rules_started = time.perf_counter()
    if request.schema_version == "verification-request-v2":
        rules = ()
        additional_rules = _library_rules(request, vision)
    else:
        rules = _rules(request, vision, locations)
        additional_rules = _additional_rules(request, vision)
    rules_duration = (time.perf_counter() - rules_started) * 1000

    if any(rule.automated_status == AutomatedStatus.DISCREPANCY for rule in rules):
        overall = OverallSummary.AUTOMATED_DISCREPANCY
    elif any(rule.automated_status == AutomatedStatus.REVIEW for rule in (*rules, *additional_rules)):
        overall = OverallSummary.NEEDS_REVIEW
    else:
        overall = OverallSummary.NO_AUTOMATED_DISCREPANCY

    review_tasks = tuple(
        ReviewTask(
            field_key=rule.field_key,
            reason_code=rule.reason_code,
            message=(
                "Confirm the detected discrepancy against the artwork."
                if rule.reason_code in {
                    "detected_value_differs",
                    "warning_presentation_noncompliant",
                    "required_text_missing",
                }
                else "Review this field because the selected reader did not produce dependable evidence."
            ),
            localization_ids=rule.localization_ids,
        )
        for rule in rules
        if rule.requires_human_review
    )
    total_duration = (time.perf_counter() - started) * 1000
    return AnalysisResponse(
        schema_version="analysis-response-v1",
        analysis_id=str(uuid4()),
        mode="connected",
        reader_mode=request.reader_mode,
        overall_summary=overall,
        panels=normalized_panels,
        vision_run=vision,
        ocr_run=ocr_runs[0],
        ocr_runs=ocr_runs,
        localizations=locations,
        rule_results=rules,
        additional_rule_results=additional_rules,
        review_tasks=review_tasks,
        stage_durations=(
            StageDuration(
                stage="ocr_preprocessing",
                milliseconds=sum(run.preprocessing_duration_ms for run in ocr_runs),
            ),
            StageDuration(stage="ocr", milliseconds=sum(run.ocr_duration_ms for run in ocr_runs)),
            StageDuration(stage="vision", milliseconds=vision.duration_ms if use_vision else 0),
            StageDuration(stage="alignment", milliseconds=alignment_duration),
            StageDuration(stage="rules", milliseconds=rules_duration),
        ),
        total_duration_ms=total_duration,
    )
