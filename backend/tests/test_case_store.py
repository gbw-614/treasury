from __future__ import annotations

import hashlib

import pytest

from app.schemas import AnalysisRequest, HumanDecisionRequest, ValidatedPanel
from app.services import case_store


def _request() -> AnalysisRequest:
    return AnalysisRequest.model_validate(
        {
            "schemaVersion": "verification-request-v1",
            "category": "distilled_spirits",
            "expected": {"brandName": "Queue Test", "classType": "Bourbon"},
            "panels": [{"panelId": "p01", "file": "label.png"}],
        }
    )


def _create(tmp_path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(case_store, "DATA_ROOT", tmp_path)
    content = b"not-an-image-but-persistence-does-not-decode-it"
    panel = ValidatedPanel(
        panel_id="p01",
        source_sha256=hashlib.sha256(content).hexdigest(),
        detected_mime_type="image/png",
        width=100,
        height=50,
        exif_orientation=None,
        applied_transform="none",
        decoded_pixel_count=5000,
        original_byte_count=len(content),
        preprocessing_version="test",
    )
    return case_store.create_case(
        _request(), (panel,), (content,), filenames=("label.png",), display_name=None,
        current_user_id="u-importer", current_username="Importer",
    ).case_id


def test_review_claims_are_atomic_and_decisions_are_attributed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_id = _create(tmp_path, monkeypatch)
    created = case_store.get_case(case_id)
    assert created is not None
    assert created.created_by_user_id == "u-importer"
    assert created.created_by_username == "Importer"
    assert created.assigned_to_user_id == "u-importer"
    assert created.assigned_to_username == "Importer"

    # Imported/uploaded work starts with its creator, who may deliberately
    # release it for another reviewer.
    released = case_store.release_review_case(case_id, current_user_id="u-importer")
    assert released is not None
    assert released.assigned_to_user_id is None

    claimed = case_store.claim_review_case(
        case_id, current_user_id="u-one", current_username="One"
    )
    assert claimed is not None
    assert claimed.assigned_to_user_id == "u-one"
    assert claimed.assigned_to_username == "One"
    with pytest.raises(case_store.CaseReviewClaimConflict) as conflict:
        case_store.claim_review_case(case_id, current_user_id="u-two", current_username="Two")
    assert conflict.value.assigned_to_username == "One"

    # A completed assigned case cannot be decided by a different user.
    with case_store._connect() as connection:
        connection.execute(
                """UPDATE verification_cases
                   SET processing_status = 'complete', automated_outcome = 'pass',
                       analysis_status = 'complete', decision_status = 'pass',
                       outcome = 'pass', decision_source = 'automated'
                   WHERE case_id = ?""",
            (case_id,),
        )
    with pytest.raises(case_store.CaseReviewClaimConflict):
        case_store.save_human_decision(
            case_id, HumanDecisionRequest(outcome="fail"),
            current_user_id="u-two", current_username="Two",
        )
    decided = case_store.save_human_decision(
        case_id, HumanDecisionRequest(outcome="fail", reviewer="forged"),
        current_user_id="u-one", current_username="One",
    )
    assert decided is not None
    assert decided.reviewed_by_user_id == "u-one"
    assert decided.reviewed_by_username == "One"


def test_only_claimant_can_release_a_review_case(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_id = _create(tmp_path, monkeypatch)
    case_store.release_review_case(case_id, current_user_id="u-importer")
    case_store.claim_review_case(case_id, current_user_id="u-one", current_username="One")
    with pytest.raises(case_store.CaseReviewClaimConflict):
        case_store.release_review_case(case_id, current_user_id="u-two")
    released = case_store.release_review_case(case_id, current_user_id="u-one")
    assert released is not None
    assert released.assigned_to_user_id is None


def test_interrupted_processing_claims_return_to_queue(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case_id = _create(tmp_path, monkeypatch)
    assert case_store.claim_case(case_id)

    interrupted = case_store.get_case(case_id)
    assert interrupted is not None
    assert interrupted.processing_status == "processing"
    assert interrupted.analysis_status == "analyzing"

    recovered = case_store.recover_interrupted_cases()

    assert recovered == (case_id,)
    queued = case_store.get_case(case_id)
    assert queued is not None
    assert queued.processing_status == "queued"
    assert queued.analysis_status == "queued"
    assert queued.decision_status == "awaiting_analysis"
    assert queued.error_message is None
    assert case_store.claim_case(case_id)
