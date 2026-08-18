from __future__ import annotations

import json
import os
import shutil
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from app.schemas import (
    AnalysisRequest,
    AnalysisResponse,
    AnalysisStatus,
    CaseDetail,
    CaseOutcome,
    CasePanel,
    CaseSummary,
    CatalogProvenance,
    DecisionSource,
    DecisionStatus,
    HumanDecisionRequest,
    OverallSummary,
    ProcessingStatus,
    SourceCatalogImportIssue,
    SourceCatalogImportJob,
    SourceCatalogImportRequest,
    ValidatedPanel,
)

DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[2] / "data"
DATA_ROOT = Path(os.environ.get("VERIFICATION_DATA_DIR") or DEFAULT_DATA_ROOT)


class CaseReviewClaimConflict(RuntimeError):
    """Raised when a reviewer attempts to claim work owned by someone else."""

    def __init__(self, case_id: str, assigned_to_user_id: str | None, assigned_to_username: str | None):
        self.case_id = case_id
        self.assigned_to_user_id = assigned_to_user_id
        self.assigned_to_username = assigned_to_username
        owner = assigned_to_username or assigned_to_user_id or "another reviewer"
        super().__init__(f"Case {case_id} is already claimed by {owner}")


def _paths() -> tuple[Path, Path]:
    return DATA_ROOT / "verification.sqlite3", DATA_ROOT / "cases"


def _connect() -> sqlite3.Connection:
    database, cases_dir = _paths()
    database.parent.mkdir(parents=True, exist_ok=True)
    cases_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS verification_cases (
            case_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            request_json TEXT NOT NULL,
            panel_json TEXT NOT NULL,
            panel_path TEXT NOT NULL,
            panels_json TEXT,
            panel_paths_json TEXT,
            processing_status TEXT NOT NULL,
            analysis_status TEXT,
            decision_status TEXT,
            automated_outcome TEXT,
            outcome TEXT,
            decision_source TEXT,
            analysis_json TEXT,
            error_message TEXT,
            review_note TEXT,
            reviewer TEXT,
            reviewed_at TEXT,
            created_by_user_id TEXT,
            created_by_username TEXT,
            assigned_to_user_id TEXT,
            assigned_to_username TEXT,
            reviewed_by_user_id TEXT,
            reviewed_by_username TEXT,
            source_json TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS verification_catalog_import_jobs (
            job_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            username TEXT NOT NULL,
            request_json TEXT NOT NULL,
            status TEXT NOT NULL,
            total_cases INTEGER NOT NULL,
            completed_cases INTEGER NOT NULL DEFAULT 0,
            imported_case_ids_json TEXT NOT NULL DEFAULT '[]',
            duplicate_case_ids_json TEXT NOT NULL DEFAULT '[]',
            issues_json TEXT NOT NULL DEFAULT '[]',
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            finished_at TEXT
        )
        """
    )
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(verification_cases)")}
    migrations = {
        "removed_at": "TEXT",
        "panels_json": "TEXT",
        "panel_paths_json": "TEXT",
        "analysis_status": "TEXT",
        "decision_status": "TEXT",
        "created_by_user_id": "TEXT",
        "created_by_username": "TEXT",
        "assigned_to_user_id": "TEXT",
        "assigned_to_username": "TEXT",
        "reviewed_by_user_id": "TEXT",
        "reviewed_by_username": "TEXT",
        "source_json": "TEXT",
    }
    for column, definition in migrations.items():
        if column not in columns:
            connection.execute(f"ALTER TABLE verification_cases ADD COLUMN {column} {definition}")
    legacy_rows = connection.execute(
        """
        SELECT case_id, processing_status, automated_outcome, outcome,
               decision_source, analysis_json, decision_status
        FROM verification_cases
        WHERE analysis_status IS NULL OR decision_status IS NULL
           OR decision_status IN ('awaiting_application_data', 'out_of_scope')
        """
    ).fetchall()
    for row in legacy_rows:
        recovered_automated_outcome = row["automated_outcome"]
        decision_source = row["decision_source"]
        analysis_status = {
            ProcessingStatus.QUEUED.value: AnalysisStatus.QUEUED.value,
            ProcessingStatus.PROCESSING.value: AnalysisStatus.ANALYZING.value,
            ProcessingStatus.COMPLETE.value: AnalysisStatus.COMPLETE.value,
            ProcessingStatus.ERROR.value: AnalysisStatus.ERROR.value,
        }[row["processing_status"]]
        processing_status = row["processing_status"]
        if row["outcome"]:
            processing_status = row["processing_status"]
            decision_status = row["outcome"]
            outcome = row["outcome"]
        elif row["analysis_json"]:
            recovered = _outcome(AnalysisResponse.model_validate_json(row["analysis_json"]))
            processing_status = ProcessingStatus.COMPLETE.value
            analysis_status = AnalysisStatus.COMPLETE.value
            decision_status = _decision_for_outcome(recovered).value
            outcome = recovered.value
            recovered_automated_outcome = recovered.value
            decision_source = DecisionSource.AUTOMATED.value
        else:
            if processing_status == ProcessingStatus.COMPLETE.value:
                processing_status = ProcessingStatus.QUEUED.value
                analysis_status = AnalysisStatus.QUEUED.value
            decision_status = DecisionStatus.AWAITING_ANALYSIS.value
            outcome = None
        connection.execute(
            """
            UPDATE verification_cases
            SET processing_status = ?, analysis_status = ?, decision_status = ?,
                automated_outcome = ?, outcome = ?, decision_source = ?
            WHERE case_id = ?
            """,
            (
                processing_status,
                analysis_status,
                decision_status,
                recovered_automated_outcome,
                outcome,
                decision_source,
                row["case_id"],
            ),
        )
    connection.commit()
    return connection


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _catalog_import_job_from_row(row: sqlite3.Row) -> SourceCatalogImportJob:
    return SourceCatalogImportJob(
        job_id=row["job_id"],
        status=row["status"],
        total_cases=row["total_cases"],
        completed_cases=row["completed_cases"],
        imported_case_ids=tuple(json.loads(row["imported_case_ids_json"])),
        duplicate_case_ids=tuple(json.loads(row["duplicate_case_ids_json"])),
        issues=tuple(SourceCatalogImportIssue.model_validate(item) for item in json.loads(row["issues_json"])),
        error_message=row["error_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        finished_at=row["finished_at"],
    )


def create_catalog_import_job(
    request: SourceCatalogImportRequest, *, current_user_id: str, current_username: str
) -> SourceCatalogImportJob:
    job_id = str(uuid4())
    now = _now()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO verification_catalog_import_jobs (
                job_id, user_id, username, request_json, status, total_cases,
                completed_cases, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'queued', ?, 0, ?, ?)
            """,
            (
                job_id,
                current_user_id,
                current_username,
                request.model_dump_json(by_alias=True),
                len(request.source_case_ids),
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM verification_catalog_import_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    assert row is not None
    return _catalog_import_job_from_row(row)


def get_catalog_import_job(job_id: str, *, current_user_id: str) -> SourceCatalogImportJob | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM verification_catalog_import_jobs WHERE job_id = ? AND user_id = ?",
            (job_id, current_user_id),
        ).fetchone()
    return _catalog_import_job_from_row(row) if row else None


def get_catalog_import_job_request(job_id: str) -> SourceCatalogImportRequest | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT request_json FROM verification_catalog_import_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    return SourceCatalogImportRequest.model_validate_json(row["request_json"]) if row else None


def update_catalog_import_job(
    job_id: str,
    *,
    status: str,
    completed_cases: int,
    imported_case_ids: list[str],
    duplicate_case_ids: list[str],
    issues: list[SourceCatalogImportIssue],
    error_message: str | None = None,
    finished: bool = False,
) -> None:
    now = _now()
    with _connect() as connection:
        connection.execute(
            """
            UPDATE verification_catalog_import_jobs
            SET status = ?, completed_cases = ?, imported_case_ids_json = ?,
                duplicate_case_ids_json = ?, issues_json = ?, error_message = ?,
                updated_at = ?, finished_at = ?
            WHERE job_id = ?
            """,
            (
                status,
                completed_cases,
                json.dumps(imported_case_ids),
                json.dumps(duplicate_case_ids),
                json.dumps([issue.model_dump(by_alias=True) for issue in issues]),
                error_message[:1000] if error_message else None,
                now,
                now if finished else None,
                job_id,
            ),
        )


def _outcome(analysis: AnalysisResponse) -> CaseOutcome:
    if analysis.overall_summary == OverallSummary.NO_AUTOMATED_DISCREPANCY:
        return CaseOutcome.PASS
    if analysis.overall_summary == OverallSummary.AUTOMATED_DISCREPANCY:
        return CaseOutcome.FAIL
    return CaseOutcome.NEEDS_REVIEW


def _decision_for_outcome(outcome: CaseOutcome) -> DecisionStatus:
    if outcome == CaseOutcome.PASS:
        return DecisionStatus.PASS
    if outcome == CaseOutcome.FAIL:
        return DecisionStatus.FAIL
    return DecisionStatus.NEEDS_REVIEW


def _analysis_status(row: sqlite3.Row) -> AnalysisStatus:
    if row["analysis_status"]:
        return AnalysisStatus(row["analysis_status"])
    return {
        ProcessingStatus.QUEUED.value: AnalysisStatus.QUEUED,
        ProcessingStatus.PROCESSING.value: AnalysisStatus.ANALYZING,
        ProcessingStatus.COMPLETE.value: AnalysisStatus.COMPLETE,
        ProcessingStatus.ERROR.value: AnalysisStatus.ERROR,
    }[row["processing_status"]]


def _decision_status(row: sqlite3.Row) -> DecisionStatus:
    if row["decision_status"]:
        try:
            return DecisionStatus(row["decision_status"])
        except ValueError:
            pass
    if row["outcome"]:
        return _decision_for_outcome(CaseOutcome(row["outcome"]))
    return DecisionStatus.AWAITING_ANALYSIS


def _panel_from_row(row: sqlite3.Row) -> CasePanel:
    return CasePanel.model_validate_json(row["panel_json"])


def _panels_from_row(row: sqlite3.Row) -> tuple[CasePanel, ...]:
    if row["panels_json"]:
        return tuple(CasePanel.model_validate(item) for item in json.loads(row["panels_json"]))
    return (_panel_from_row(row),)


def _panel_paths_from_row(row: sqlite3.Row) -> tuple[Path, ...]:
    if row["panel_paths_json"]:
        return tuple(Path(value) for value in json.loads(row["panel_paths_json"]))
    return (Path(row["panel_path"]),)


def _summary_from_row(row: sqlite3.Row, *, duplicate_image_count: int = 0) -> CaseSummary:
    request = AnalysisRequest.model_validate_json(row["request_json"])
    return CaseSummary(
        case_id=row["case_id"],
        display_name=row["display_name"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        category=request.category,
        expected=request.expected,
        panel=_panel_from_row(row),
        panels=_panels_from_row(row),
        processing_status=row["processing_status"],
        analysis_status=_analysis_status(row),
        decision_status=_decision_status(row),
        automated_outcome=row["automated_outcome"],
        outcome=row["outcome"],
        decision_source=row["decision_source"],
        error_message=row["error_message"],
        review_note=row["review_note"],
        reviewed_at=row["reviewed_at"],
        created_by_user_id=row["created_by_user_id"],
        created_by_username=row["created_by_username"],
        assigned_to_user_id=row["assigned_to_user_id"],
        assigned_to_username=row["assigned_to_username"],
        reviewed_by_user_id=row["reviewed_by_user_id"],
        reviewed_by_username=row["reviewed_by_username"],
        source=CatalogProvenance.model_validate_json(row["source_json"]) if row["source_json"] else None,
        duplicate_image_count=duplicate_image_count,
        removed_at=row["removed_at"],
    )


def _detail_from_row(row: sqlite3.Row) -> CaseDetail:
    summary = _summary_from_row(row, duplicate_image_count=_duplicate_count(row))
    analysis = (
        AnalysisResponse.model_validate_json(row["analysis_json"])
        if row["analysis_json"]
        else None
    )
    return CaseDetail(**summary.model_dump(), analysis=analysis)


def create_case(
    request: AnalysisRequest,
    panels: tuple[ValidatedPanel, ...],
    contents: tuple[bytes, ...],
    *,
    filenames: tuple[str, ...],
    display_name: str | None,
    current_user_id: str | None = None,
    current_username: str | None = None,
    source: CatalogProvenance | None = None,
) -> CaseDetail:
    if len(panels) != len(contents) or len(panels) != len(filenames):
        raise ValueError("Each label panel needs matching content and a filename")
    case_id = str(uuid4())
    created_at = _now()
    _, cases_dir = _paths()
    case_dir = cases_dir / case_id
    case_dir.mkdir(parents=True, exist_ok=False)
    panel_paths: list[Path] = []
    public_panels: list[CasePanel] = []
    for index, (panel, content, filename) in enumerate(zip(panels, contents, filenames), start=1):
        suffix = ".jpg" if panel.detected_mime_type == "image/jpeg" else ".png"
        panel_path = case_dir / f"panel-{index:02d}{suffix}"
        panel_path.write_bytes(content)
        panel_paths.append(panel_path)
        public_panels.append(
            CasePanel(
                panel_id=panel.panel_id,
                file=filename,
                url=f"/api/v1/cases/{case_id}/image/{panel.panel_id}",
                sha256=panel.source_sha256,
                mime_type=panel.detected_mime_type,
                width=panel.width,
                height=panel.height,
            )
        )
    public_panel = public_panels[0]
    label = (display_name or "").strip() or request.expected.brand_name or "Untitled COLA case"
    decision_status = DecisionStatus.AWAITING_ANALYSIS
    initial_processing_status = ProcessingStatus.QUEUED
    initial_analysis_status = AnalysisStatus.QUEUED
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO verification_cases (
                case_id, display_name, created_at, updated_at, request_json,
                panel_json, panel_path, panels_json, panel_paths_json, processing_status,
                analysis_status, decision_status,
                created_by_user_id, created_by_username, assigned_to_user_id,
                assigned_to_username, source_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                case_id,
                label,
                created_at,
                created_at,
                request.model_dump_json(by_alias=True),
                public_panel.model_dump_json(by_alias=True),
                str(panel_paths[0]),
                json.dumps([panel.model_dump(by_alias=True) for panel in public_panels]),
                json.dumps([str(path) for path in panel_paths]),
                initial_processing_status.value,
                initial_analysis_status.value,
                decision_status.value,
                current_user_id,
                current_username,
                # New work belongs to its uploader until that person releases
                # it explicitly.  Old rows intentionally stay unassigned.
                current_user_id,
                current_username,
                source.model_dump_json(by_alias=True) if source else None,
            ),
        )
    detail = get_case(case_id)
    if detail is None:  # pragma: no cover - defensive persistence check
        raise RuntimeError("The case was not persisted")
    return detail


def find_catalog_import(
    *, catalog_url: str, catalog_version: str, source_case_id: str
) -> CaseDetail | None:
    """Return an existing import for a stable source case across catalog revisions.

    ``catalog_version`` remains in the call contract and recorded provenance,
    but a manifest revision must not make the same stable sourceCaseId appear
    importable again.
    """
    del catalog_version
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM verification_cases WHERE source_json IS NOT NULL ORDER BY created_at DESC"
        ).fetchall()
    for row in rows:
        try:
            source = CatalogProvenance.model_validate_json(row["source_json"])
        except ValidationError:  # pragma: no cover - tolerate historical malformed rows
            continue
        if (
            source.catalog_url == catalog_url
            and source.source_case_id == source_case_id
        ):
            return _detail_from_row(row)
    return None


def list_cases() -> tuple[CaseSummary, ...]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM verification_cases ORDER BY created_at DESC"
        ).fetchall()
    counts = Counter(_panel_from_row(row).sha256 for row in rows if row["removed_at"] is None)
    return tuple(
        _summary_from_row(
            row,
            duplicate_image_count=(counts[_panel_from_row(row).sha256] - 1) if row["removed_at"] is None else 0,
        )
        for row in rows
    )


def _duplicate_count(row: sqlite3.Row) -> int:
    target_sha = _panel_from_row(row).sha256
    with _connect() as connection:
        rows = connection.execute("SELECT panel_json, removed_at FROM verification_cases").fetchall()
    if row["removed_at"] is not None:
        return 0
    return max(0, sum(
        CasePanel.model_validate_json(item["panel_json"]).sha256 == target_sha and item["removed_at"] is None
        for item in rows
    ) - 1)


def get_case(case_id: str) -> CaseDetail | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM verification_cases WHERE case_id = ?", (case_id,)
        ).fetchone()
    return _detail_from_row(row) if row else None


def get_case_work(case_id: str) -> tuple[AnalysisRequest, tuple[Path, ...]] | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT request_json, panel_path, panel_paths_json FROM verification_cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()
    if row is None:
        return None
    return AnalysisRequest.model_validate_json(row["request_json"]), _panel_paths_from_row(row)


def remove_case(case_id: str) -> CaseDetail | None:
    """Hide a queue item while retaining its artwork for a later restore."""
    with _connect() as connection:
        result = connection.execute(
            "UPDATE verification_cases SET removed_at = ?, updated_at = ? WHERE case_id = ? AND removed_at IS NULL",
            (_now(), _now(), case_id),
        )
    if result.rowcount != 1:
        return None
    return get_case(case_id)


def restore_case(case_id: str) -> CaseDetail | None:
    with _connect() as connection:
        result = connection.execute(
            "UPDATE verification_cases SET removed_at = NULL, updated_at = ? WHERE case_id = ? AND removed_at IS NOT NULL",
            (_now(), case_id),
        )
    if result.rowcount != 1:
        return None
    return get_case(case_id)


def clear_cases() -> int:
    """Permanently clear the local development queue and its stored artwork."""
    with _connect() as connection:
        cleared_cases = connection.execute("SELECT COUNT(*) FROM verification_cases").fetchone()[0]
        connection.execute("DELETE FROM verification_cases")

    # Artwork is private queue state, not a reusable fixture catalogue.  Remove
    # the exact controlled case-storage directory, then recreate it for the
    # next import.  This also clears soft-removed items and any orphaned files.
    _, cases_dir = _paths()
    if cases_dir.exists():
        shutil.rmtree(cases_dir)
    cases_dir.mkdir(parents=True, exist_ok=True)
    return int(cleared_cases)


def recover_interrupted_cases() -> tuple[str, ...]:
    """Return active work claimed by a previous server process to the queue.

    Analysis workers are in-memory tasks. No task can survive a process or
    container restart, so every persisted ``processing`` claim is abandoned
    when a new process starts.
    """
    now = _now()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT case_id, removed_at
            FROM verification_cases
            WHERE processing_status = ?
            ORDER BY created_at DESC
            """,
            (ProcessingStatus.PROCESSING.value,),
        ).fetchall()
        connection.execute(
            """
            UPDATE verification_cases
            SET processing_status = ?, analysis_status = ?, decision_status = ?,
                updated_at = ?, error_message = NULL
            WHERE processing_status = ?
            """,
            (
                ProcessingStatus.QUEUED.value,
                AnalysisStatus.QUEUED.value,
                DecisionStatus.AWAITING_ANALYSIS.value,
                now,
                ProcessingStatus.PROCESSING.value,
            ),
        )
    return tuple(row["case_id"] for row in rows if row["removed_at"] is None)


def claim_case(case_id: str) -> bool:
    now = _now()
    with _connect() as connection:
        result = connection.execute(
            """
            UPDATE verification_cases
            SET processing_status = ?, analysis_status = ?, updated_at = ?, error_message = NULL,
                analysis_json = NULL, automated_outcome = NULL, outcome = NULL,
                decision_source = NULL, review_note = NULL, reviewer = NULL,
                reviewed_at = NULL, reviewed_by_user_id = NULL,
                reviewed_by_username = NULL
            WHERE case_id = ? AND processing_status != ?
            """,
            (
                ProcessingStatus.PROCESSING.value,
                AnalysisStatus.ANALYZING.value,
                now,
                case_id,
                ProcessingStatus.PROCESSING.value,
            ),
        )
    return result.rowcount == 1


def complete_case(case_id: str, analysis: AnalysisResponse) -> None:
    now = _now()
    with _connect() as connection:
        row = connection.execute(
            "SELECT case_id FROM verification_cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        if row is None:
            return
        outcome = _outcome(analysis)
        decision_status = _decision_for_outcome(outcome)
        connection.execute(
            """
            UPDATE verification_cases
            SET processing_status = ?, analysis_status = ?, decision_status = ?, updated_at = ?, analysis_json = ?,
                automated_outcome = ?, outcome = ?, decision_source = ?,
                error_message = NULL
            WHERE case_id = ?
            """,
            (
                ProcessingStatus.COMPLETE.value,
                AnalysisStatus.COMPLETE.value,
                decision_status.value,
                now,
                analysis.model_dump_json(by_alias=True),
                outcome.value,
                outcome.value,
                DecisionSource.AUTOMATED.value,
                case_id,
            ),
        )


def fail_case(case_id: str, message: str) -> None:
    with _connect() as connection:
        connection.execute(
            """
            UPDATE verification_cases
            SET processing_status = ?, analysis_status = ?, updated_at = ?, error_message = ?,
                outcome = NULL, decision_source = NULL
            WHERE case_id = ?
            """,
            (ProcessingStatus.ERROR.value, AnalysisStatus.ERROR.value, _now(), message[:1000], case_id),
        )


def save_human_decision(
    case_id: str,
    decision: HumanDecisionRequest,
    *,
    current_user_id: str | None = None,
    current_username: str | None = None,
) -> CaseDetail | None:
    current = get_case(case_id)
    if current is None:
        return None
    if current.processing_status != ProcessingStatus.COMPLETE:
        raise ValueError("Only completed cases can receive a human decision")
    if current.assigned_to_user_id and current.assigned_to_user_id != current_user_id:
        raise CaseReviewClaimConflict(
            case_id, current.assigned_to_user_id, current.assigned_to_username
        )
    source = (
        DecisionSource.HUMAN_CONFIRMED
        if current.automated_outcome == decision.outcome
        else DecisionSource.HUMAN_OVERRIDDEN
    )
    now = _now()
    with _connect() as connection:
        result = connection.execute(
            """
            UPDATE verification_cases
            SET outcome = ?, decision_status = ?, decision_source = ?, review_note = ?, reviewer = NULL,
                reviewed_by_user_id = ?, reviewed_by_username = ?, reviewed_at = ?, updated_at = ?
            WHERE case_id = ?
              AND processing_status = ?
              AND (assigned_to_user_id IS NULL OR assigned_to_user_id = ?)
            """,
            (
                decision.outcome.value,
                _decision_for_outcome(decision.outcome).value,
                source.value,
                decision.note.strip() or None,
                current_user_id,
                current_username,
                now,
                now,
                case_id,
                ProcessingStatus.COMPLETE.value,
                current_user_id,
            ),
        )
        if result.rowcount != 1:
            row = connection.execute(
                """SELECT assigned_to_user_id, assigned_to_username
                   FROM verification_cases WHERE case_id = ?""", (case_id,)
            ).fetchone()
            if row is not None and row["assigned_to_user_id"] is not None:
                raise CaseReviewClaimConflict(
                    case_id, row["assigned_to_user_id"], row["assigned_to_username"]
                )
            raise ValueError("Only completed cases can receive a human decision")
    return get_case(case_id)


def claim_review_case(
    case_id: str, *, current_user_id: str, current_username: str
) -> CaseDetail | None:
    """Atomically claim a case, or raise conflict if another reviewer owns it.

    Re-claiming a case already owned by the same authenticated user is
    idempotent.  This deliberately does not alter scan/processing state.
    """
    now = _now()
    with _connect() as connection:
        result = connection.execute(
            """
            UPDATE verification_cases
            SET assigned_to_user_id = ?, assigned_to_username = ?, updated_at = ?
            WHERE case_id = ?
              AND (assigned_to_user_id IS NULL OR assigned_to_user_id = ?)
            """,
            (current_user_id, current_username, now, case_id, current_user_id),
        )
        claimed = result.rowcount == 1
        row = None if claimed else connection.execute(
            """SELECT assigned_to_user_id, assigned_to_username
               FROM verification_cases WHERE case_id = ?""", (case_id,)
        ).fetchone()
    if claimed:
        return get_case(case_id)
    if row is None:
        return None
    raise CaseReviewClaimConflict(case_id, row["assigned_to_user_id"], row["assigned_to_username"])


def release_review_case(
    case_id: str, *, current_user_id: str
) -> CaseDetail | None:
    """Release the caller's claim; another reviewer's claim cannot be released."""
    now = _now()
    with _connect() as connection:
        result = connection.execute(
            """
            UPDATE verification_cases
            SET assigned_to_user_id = NULL, assigned_to_username = NULL, updated_at = ?
            WHERE case_id = ? AND assigned_to_user_id = ?
            """,
            (now, case_id, current_user_id),
        )
        released = result.rowcount == 1
        row = None if released else connection.execute(
            """SELECT assigned_to_user_id, assigned_to_username
               FROM verification_cases WHERE case_id = ?""", (case_id,)
        ).fetchone()
    if released:
        return get_case(case_id)
    if row is None:
        return None
    if row["assigned_to_user_id"] is not None:
        raise CaseReviewClaimConflict(case_id, row["assigned_to_user_id"], row["assigned_to_username"])
    return get_case(case_id)


def image_path(case_id: str, panel_id: str | None = None) -> tuple[Path, CasePanel] | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT panel_path, panel_json, panel_paths_json, panels_json FROM verification_cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()
    if row is None:
        return None
    panels = _panels_from_row(row)
    paths = _panel_paths_from_row(row)
    index = next((index for index, panel in enumerate(panels) if panel.panel_id == panel_id), None)
    if panel_id is not None and index is None:
        return None
    selected_index = 0 if index is None else index
    return paths[selected_index], panels[selected_index]
