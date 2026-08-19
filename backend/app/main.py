from __future__ import annotations

import asyncio
import csv
import json
import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Annotated, Any

from dotenv import load_dotenv
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import ValidationError

from app.schemas import (
    AnalysisRequest,
    AnalysisResponse,
    CapabilitiesResponse,
    CaseDetail,
    CaseListResponse,
    CatalogCaseAvailability,
    CatalogProvenance,
    ClearQueueResponse,
    CurrentUser,
    HealthResponse,
    HumanDecisionRequest,
    LoginRequest,
    SessionConflictResponse,
    SourceCatalogImportIssue,
    SourceCatalogImportJob,
    SourceCatalogImportRequest,
    SourceCatalogImportResponse,
    SourceCatalogResponse,
    UserPreferencesResponse,
    UserPreferencesUpdate,
    ValidatedPanel,
    VersionResponse,
)
from app.services import auth_store, case_store, recognition_cache, s3_catalog
from app.services.connected_analysis import run_connected_analysis
from app.services.image_validation import (
    MAX_UPLOAD_BYTES,
    UploadValidationError,
    validate_image,
)
from app.services.openrouter_vision import vision_available

SERVICE_VERSION = "0.2.0"
SESSION_COOKIE_NAME = "ttb_verification_session"


def _analysis_concurrency() -> int:
    """Limit CPU-heavy local OCR work; a batch must not exhaust the host."""
    try:
        return max(1, int(os.getenv("VERIFICATION_ANALYSIS_CONCURRENCY", "1")))
    except ValueError:
        return 1


ANALYSIS_SEMAPHORE = asyncio.Semaphore(_analysis_concurrency())
CATALOG_IMPORT_SEMAPHORE = asyncio.Semaphore(1)
ANALYSIS_TASKS: set[asyncio.Task[None]] = set()

# Local credentials belong in the project-root `.env`, which is ignored by Git.
# Explicit environment variables still win, so deployment platforms can inject
# secrets without placing a file on disk.
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
_configured_web_dist = os.getenv("VERIFICATION_WEB_DIST")
WEB_DIST = (
    Path(_configured_web_dist).resolve()
    if _configured_web_dist
    else (Path(__file__).resolve().parents[2] / "dist").resolve()
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # SQLite can persist a claim, but the in-memory task that owned it cannot
    # survive a process/container restart. Reclaim those cases on startup.
    for case_id in case_store.recover_interrupted_cases():
        if case_store.claim_case(case_id):
            _start_analysis_task(case_id)
    yield
    tasks = tuple(ANALYSIS_TASKS)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(
    title="TTB Label Verification API",
    version=SERVICE_VERSION,
    description="Fixture-backed contract API for the connected verification vertical slice.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type"],
)


def _session_cookie_secure() -> bool:
    """Allow local HTTP development; production must set this true behind TLS."""
    return os.getenv("VERIFICATION_SESSION_SECURE", "false").strip().lower() in {"1", "true", "yes"}


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=int(auth_store.session_ttl().total_seconds()),
        httponly=True,
        secure=_session_cookie_secure(),
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/", httponly=True, secure=_session_cookie_secure(), samesite="lax")


def require_current_user(request: Request) -> CurrentUser:
    session = auth_store.get_session(request.cookies.get(SESSION_COOKIE_NAME))
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return session.user


@app.get("/api/v1/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/api/v1/version", response_model=VersionResponse)
def version() -> VersionResponse:
    return VersionResponse(
        service="ttb-label-verification-api",
        version=SERVICE_VERSION,
        analysis_schema_version="analysis-response-v1",
    )


@app.post("/api/v1/auth/login", response_model=CurrentUser, responses={409: {"model": SessionConflictResponse}})
def login(credentials: LoginRequest, response: Response) -> CurrentUser:
    try:
        auth_store.ensure_bootstrap_user()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=f"Authentication bootstrap is invalid: {exc}") from exc
    if not auth_store.has_users():
        raise HTTPException(
            status_code=503,
            detail="No reviewer accounts are configured. Create one with the container user-admin command.",
        )
    try:
        authenticated = auth_store.authenticate(
            credentials.username, credentials.password, force=credentials.replace_existing
        )
    except auth_store.ActiveSessionError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=SessionConflictResponse().model_dump(by_alias=True),
        ) from exc
    if authenticated is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")
    token, session = authenticated
    _set_session_cookie(response, token)
    # The browser only needs the identity; the expiry stays server-side as a
    # property of the opaque cookie session.
    return session.user


@app.post("/api/v1/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response) -> None:
    auth_store.revoke_session(request.cookies.get(SESSION_COOKIE_NAME))
    _clear_session_cookie(response)
    # Let FastAPI apply the declared 204 status while retaining the cookie
    # mutation on its injected response object.


@app.get("/api/v1/auth/me", response_model=CurrentUser)
def current_user(user: CurrentUser = Depends(require_current_user)) -> CurrentUser:
    return user


@app.get("/api/v1/preferences", response_model=UserPreferencesResponse)
def get_preferences(user: CurrentUser = Depends(require_current_user)) -> UserPreferencesResponse:
    return UserPreferencesResponse(preferences=auth_store.get_preferences(user.user_id))


@app.put("/api/v1/preferences", response_model=UserPreferencesResponse)
def put_preferences(
    update: UserPreferencesUpdate, user: CurrentUser = Depends(require_current_user)
) -> UserPreferencesResponse:
    return UserPreferencesResponse(
        preferences=auth_store.save_preferences(user.user_id, update.preferences)
    )


@app.get("/api/v1/preferences/queue")
def get_queue_preferences(user: CurrentUser = Depends(require_current_user)) -> dict[str, object]:
    """Compatibility endpoint for the UI's direct queue-preferences document."""
    return auth_store.get_preference_scope(user.user_id, "queue")


@app.put("/api/v1/preferences/queue")
def put_queue_preferences(
    preferences: dict[str, Any], user: CurrentUser = Depends(require_current_user)
) -> dict[str, object]:
    if len(preferences) > 100:
        raise HTTPException(status_code=422, detail="Too many preference keys")
    return auth_store.save_preference_scope(user.user_id, "queue", preferences)


@app.get("/api/v1/preferences/analysis")
def get_analysis_preferences(user: CurrentUser = Depends(require_current_user)) -> dict[str, object]:
    """Return analysis-reader settings for the signed-in reviewer."""
    return auth_store.get_preference_scope(user.user_id, "analysis")


@app.put("/api/v1/preferences/analysis")
def put_analysis_preferences(
    preferences: dict[str, Any], user: CurrentUser = Depends(require_current_user)
) -> dict[str, object]:
    reader_mode = preferences.get("readerMode")
    if reader_mode not in {"ocr", "llm"}:
        raise HTTPException(status_code=422, detail="readerMode must be ocr or llm")
    return auth_store.save_preference_scope(user.user_id, "analysis", {"readerMode": reader_mode})


@app.get("/api/v1/capabilities", response_model=CapabilitiesResponse)
def capabilities(_: CurrentUser = Depends(require_current_user)) -> CapabilitiesResponse:
    available = vision_available()
    return CapabilitiesResponse(
        vision_available=available,
        available_reader_modes=("ocr", "llm") if available else ("ocr",),
        catalog_configured=s3_catalog.catalog_is_configured(),
    )


@app.get("/api/v1/recognition-cache")
def recognition_cache_stats(_: CurrentUser = Depends(require_current_user)) -> dict[str, object]:
    counts = recognition_cache.stats()
    return {
        "schemaVersion": "recognition-cache-stats-v1",
        "ocrEntries": counts["ocr"],
        "llmEntries": counts["llm"],
        "totalEntries": counts["total"],
    }


@app.delete("/api/v1/recognition-cache")
def clear_recognition_cache(_: CurrentUser = Depends(require_current_user)) -> dict[str, object]:
    cleared = recognition_cache.clear()
    return {
        "schemaVersion": "recognition-cache-clear-v1",
        "clearedOcrEntries": cleared["ocr"],
        "clearedLlmEntries": cleared["llm"],
        "clearedTotalEntries": cleared["total"],
    }


def _available_request(request: AnalysisRequest) -> AnalysisRequest:
    """A stale browser setting cannot make a no-key server submit fake vision work."""
    if request.reader_mode != "ocr" and not vision_available():
        return request.model_copy(update={"reader_mode": "ocr"})
    return request


async def _process_claimed_case(case_id: str) -> None:
    # Tesseract creates an external CPU-heavy process. Serialising by default
    # is deliberate: background batch requests must not make later cases fail.
    async with ANALYSIS_SEMAPHORE:
        work = case_store.get_case_work(case_id)
        if work is None:
            return
        request, panel_paths = work
        try:
            contents = await asyncio.to_thread(
                lambda: tuple(path.read_bytes() for path in panel_paths)
            )
            validated = await asyncio.to_thread(
                lambda: tuple(
                    validate_image(content, panel_input.panel_id)
                    for panel_input, content in zip(request.panels, contents)
                )
            )
            panels = tuple(item[0] for item in validated)
            blanks = tuple(item[1] for item in validated)
            analysis = await run_connected_analysis(request, panels, contents, blanks=blanks)
            case_store.complete_case(case_id, analysis)
        except Exception as exc:  # noqa: BLE001 - queue jobs must persist terminal failure
            case_store.fail_case(case_id, str(exc) or exc.__class__.__name__)


def _catalog_http_error(exc: s3_catalog.CatalogError) -> HTTPException:
    status_code = 503 if exc.code in {"catalog_not_configured", "source_unavailable", "source_http_error"} else 422
    return HTTPException(status_code=status_code, detail={"code": exc.code, "message": str(exc)})


def _catalog_request(application_content: bytes, reader_mode: str) -> AnalysisRequest:
    """Read a catalog application as the explicit verification contract.

    Older catalog objects may contain retired routing facts. They are not part
    of the verification request and must not influence extraction or matching.
    """
    payload = json.loads(application_content)
    if not isinstance(payload, dict):
        raise ValidationError.from_exception_data("AnalysisRequest", [])
    payload.pop("facts", None)
    return _available_request(
        AnalysisRequest.model_validate(payload).model_copy(update={"reader_mode": reader_mode})
    )


def _import_one_catalog_case(
    source: s3_catalog.CatalogSource,
    catalog: Any,
    source_case_id: str,
    selection: SourceCatalogImportRequest,
    user: CurrentUser,
) -> tuple[str | None, str | None, SourceCatalogImportIssue | None, str | None]:
    """Import one catalog item and return imported, duplicate, issue, pending-analysis IDs."""
    item = s3_catalog.selected_case(catalog, source_case_id)
    if item is None:
        return None, None, SourceCatalogImportIssue(
            source_case_id=source_case_id,
            code="case_not_in_catalog",
            message="The selected case is not present in this catalog version.",
        ), None
    existing = case_store.find_catalog_import(
        catalog_url=source.catalog_url,
        catalog_version=catalog.catalog_version,
        source_case_id=source_case_id,
    )
    if existing is not None:
        return None, existing.case_id, None, None
    try:
        application_content = source.fetch_object(item.application)
        request = _catalog_request(application_content, selection.reader_mode)
        # The manifest is the catalogue's operator-facing index. Keep its
        # stable reference on the imported case even when an older application
        # JSON does not yet carry one itself.
        if item.case_reference:
            request = request.model_copy(update={"case_reference": item.case_reference})
        if tuple(panel.panel_id for panel in request.panels) != tuple(panel.panel_id for panel in item.panels):
            raise s3_catalog.CatalogError(
                "panel_manifest_mismatch",
                "Application panel IDs do not match the catalog panel list and order.",
            )
        panel_contents = tuple(source.fetch_object(panel) for panel in item.panels)
        validated = tuple(
            validate_image(content, panel_input.panel_id)[0]
            for panel_input, content in zip(request.panels, panel_contents)
        )
        provenance = CatalogProvenance(
            catalog_url=source.catalog_url,
            catalog_version=catalog.catalog_version,
            source_case_id=source_case_id,
            case_reference=item.case_reference,
            application=item.application,
            panels=item.panels,
        )
        created = case_store.create_case(
            request,
            validated,
            panel_contents,
            filenames=tuple(panel_input.file for panel_input in request.panels),
            display_name=item.display_name,
            current_user_id=user.user_id,
            current_username=user.username,
            source=provenance,
        )
        pending = created.case_id if selection.auto_process and case_store.claim_case(created.case_id) else None
        return created.case_id, None, None, pending
    except s3_catalog.CatalogError as exc:
        issue = SourceCatalogImportIssue(source_case_id=source_case_id, code=exc.code, message=str(exc))
    except UploadValidationError as exc:
        issue = SourceCatalogImportIssue(source_case_id=source_case_id, code=exc.code, message=exc.message)
    except ValidationError:
        issue = SourceCatalogImportIssue(
            source_case_id=source_case_id,
            code="invalid_application",
            message="Catalog application JSON does not match verification-request-v1.",
        )
    except Exception as exc:  # noqa: BLE001 - one bad source must not abort the selected batch
        issue = SourceCatalogImportIssue(
            source_case_id=source_case_id,
            code="import_failed",
            message=str(exc) or "Catalog case import failed.",
        )
    return None, None, issue, None


def _start_analysis_task(case_id: str) -> None:
    task = asyncio.create_task(_process_claimed_case(case_id))
    ANALYSIS_TASKS.add(task)
    task.add_done_callback(ANALYSIS_TASKS.discard)


async def _accumulate_catalog_imports(
    source: s3_catalog.CatalogSource,
    catalog: Any,
    selection: SourceCatalogImportRequest,
    user: CurrentUser,
    imported: list[str],
    duplicates: list[str],
    issues: list[SourceCatalogImportIssue],
    pending_analysis: list[str],
    on_case_done: Callable[[], None] | None = None,
) -> None:
    for source_case_id in selection.source_case_ids:
        imported_id, duplicate_id, issue, pending_id = await asyncio.to_thread(
            _import_one_catalog_case, source, catalog, source_case_id, selection, user
        )
        if imported_id:
            imported.append(imported_id)
        if duplicate_id:
            duplicates.append(duplicate_id)
        if issue:
            issues.append(issue)
        if pending_id:
            pending_analysis.append(pending_id)
        if on_case_done is not None:
            on_case_done()


async def _run_catalog_import_job(job_id: str, user: CurrentUser) -> None:
    selection = case_store.get_catalog_import_job_request(job_id)
    if selection is None:
        return
    imported: list[str] = []
    duplicates: list[str] = []
    issues: list[SourceCatalogImportIssue] = []
    pending_analysis: list[str] = []
    completed = 0
    try:
        async with CATALOG_IMPORT_SEMAPHORE:
            case_store.update_catalog_import_job(
                job_id, status="running", completed_cases=0, imported_case_ids=imported,
                duplicate_case_ids=duplicates, issues=issues,
            )
            source, catalog = await asyncio.to_thread(s3_catalog.configured_catalog)

            def record_case_done() -> None:
                nonlocal completed
                completed += 1
                case_store.update_catalog_import_job(
                    job_id, status="running", completed_cases=completed,
                    imported_case_ids=imported, duplicate_case_ids=duplicates, issues=issues,
                )

            await _accumulate_catalog_imports(
                source, catalog, selection, user,
                imported, duplicates, issues, pending_analysis,
                on_case_done=record_case_done,
            )
            case_store.update_catalog_import_job(
                job_id, status="complete", completed_cases=completed,
                imported_case_ids=imported, duplicate_case_ids=duplicates, issues=issues,
                finished=True,
            )
        # Queue display is newest-first, so begin recognition at its visible top.
        for case_id in reversed(pending_analysis):
            _start_analysis_task(case_id)
    except Exception as exc:  # noqa: BLE001 - persist terminal state for polling clients
        case_store.update_catalog_import_job(
            job_id, status="failed", completed_cases=completed,
            imported_case_ids=imported, duplicate_case_ids=duplicates, issues=issues,
            error_message=str(exc) or exc.__class__.__name__, finished=True,
        )


@app.get("/api/v1/sources/s3/catalog", response_model=SourceCatalogResponse)
def get_s3_catalog(user: CurrentUser = Depends(require_current_user)) -> SourceCatalogResponse:
    del user
    try:
        source, catalog = s3_catalog.configured_catalog()
    except s3_catalog.CatalogError as exc:
        raise _catalog_http_error(exc) from exc
    imports = case_store.list_catalog_imports()
    return SourceCatalogResponse(
        catalog_version=catalog.catalog_version,
        cases=tuple(
            CatalogCaseAvailability(
                source_case_id=item.source_case_id,
                display_name=item.display_name,
                category=item.category,
                panel_count=len(item.panels),
                already_imported_case_id=imports.get((source.catalog_url, item.source_case_id)),
            )
            for item in catalog.cases
        ),
    )


@app.post("/api/v1/sources/s3/import", response_model=SourceCatalogImportResponse, status_code=201)
async def import_s3_catalog_cases(
    selection: SourceCatalogImportRequest,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(require_current_user),
) -> SourceCatalogImportResponse:
    """Snapshot selected public-catalog cases into the authenticated user's queue.

    Failures are deliberately per case: a bad public object must not discard
    the rest of a reviewer-selected batch.
    """
    try:
        source, catalog = s3_catalog.configured_catalog()
    except s3_catalog.CatalogError as exc:
        raise _catalog_http_error(exc) from exc

    imported: list[str] = []
    duplicates: list[str] = []
    issues: list[SourceCatalogImportIssue] = []
    pending_analysis: list[str] = []
    await _accumulate_catalog_imports(
        source, catalog, selection, user, imported, duplicates, issues, pending_analysis
    )
    # The queue is displayed newest-first. Start the newest imported item first
    # so processing moves from the visible top of the queue toward the bottom.
    for case_id in reversed(pending_analysis):
        background_tasks.add_task(_process_claimed_case, case_id)

    return SourceCatalogImportResponse(
        imported_case_ids=tuple(imported),
        duplicate_case_ids=tuple(duplicates),
        issues=tuple(issues),
    )


@app.post(
    "/api/v1/sources/s3/import-jobs",
    response_model=SourceCatalogImportJob,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_s3_catalog_import_job(
    selection: SourceCatalogImportRequest,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(require_current_user),
) -> SourceCatalogImportJob:
    job = case_store.create_catalog_import_job(
        selection, current_user_id=user.user_id, current_username=user.username
    )
    background_tasks.add_task(_run_catalog_import_job, job.job_id, user)
    return job


@app.get("/api/v1/sources/s3/import-jobs/{job_id}", response_model=SourceCatalogImportJob)
def get_s3_catalog_import_job(
    job_id: str, user: CurrentUser = Depends(require_current_user)
) -> SourceCatalogImportJob:
    job = case_store.get_catalog_import_job(job_id, current_user_id=user.user_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Catalog import job not found")
    return job


@app.get("/api/v1/cases", response_model=CaseListResponse)
def list_cases(_: CurrentUser = Depends(require_current_user)) -> CaseListResponse:
    return CaseListResponse(schema_version="case-queue-v1", cases=case_store.list_cases())


@app.get("/api/v1/cases/report.csv")
def download_cases_report(_: CurrentUser = Depends(require_current_user)) -> Response:
    """Download a case-level audit report for the entire shared queue.

    Removed cases stay in this export so a downloaded report remains an audit
    record rather than a view of only the currently visible work.
    """
    destination = StringIO(newline="")
    writer = csv.DictWriter(
        destination,
        fieldnames=(
            "case_reference",
            "case_id",
            "display_name",
            "category",
            "artwork_files",
            "source_case_id",
            "created_by",
            "assigned_to",
            "processing_status",
            "analysis_status",
            "reader_mode",
            "automated_outcome",
            "outcome",
            "decision_status",
            "decision_source",
            "human_reviewed",
            "match_count",
            "discrepancy_count",
            "review_count",
            "error_message",
            "review_note",
            "created_at",
            "completed_at",
            "reviewed_at",
            "removed_at",
        ),
        lineterminator="\n",
    )
    writer.writeheader()
    for case in case_store.list_case_reports():
        rules = (
            (*case.analysis.rule_results, *case.analysis.additional_rule_results)
            if case.analysis
            else ()
        )
        status_counts = {
            status: sum(1 for rule in rules if rule.automated_status == status)
            for status in ("matches", "discrepancy", "review")
        }
        writer.writerow(
            {
                "case_reference": case.case_reference or "",
                "case_id": case.case_id,
                "display_name": case.display_name,
                "category": case.category or "",
                "artwork_files": "; ".join(panel.file for panel in case.panels),
                "source_case_id": case.source.source_case_id if case.source else "",
                "created_by": case.created_by_username or "",
                "assigned_to": case.assigned_to_username or "",
                "processing_status": case.processing_status,
                "analysis_status": case.analysis_status,
                "reader_mode": case.analysis.reader_mode if case.analysis else "",
                "automated_outcome": case.automated_outcome or "",
                "outcome": case.outcome or "",
                "decision_status": case.decision_status,
                "decision_source": case.decision_source or "",
                "human_reviewed": str(bool(case.decision_source and case.decision_source.startswith("human_"))).lower(),
                "match_count": status_counts["matches"],
                "discrepancy_count": status_counts["discrepancy"],
                "review_count": status_counts["review"],
                "error_message": case.error_message or "",
                "review_note": case.review_note or "",
                "created_at": case.created_at.isoformat(),
                "completed_at": case.updated_at.isoformat() if case.processing_status == "complete" else "",
                "reviewed_at": case.reviewed_at.isoformat() if case.reviewed_at else "",
                "removed_at": case.removed_at.isoformat() if case.removed_at else "",
            }
        )
    filename = f"treasury-work-queue-{datetime.now(UTC):%Y-%m-%d}.csv"
    return Response(
        content=destination.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/v1/cases", response_model=ClearQueueResponse)
def clear_cases(_: CurrentUser = Depends(require_current_user)) -> ClearQueueResponse:
    return ClearQueueResponse(
        schema_version="case-queue-clear-v1",
        cleared_cases=case_store.clear_cases(),
    )


@app.get("/api/v1/cases/{case_id}", response_model=CaseDetail)
def get_case(case_id: str, _: CurrentUser = Depends(require_current_user)) -> CaseDetail:
    case = case_store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


@app.get("/api/v1/cases/{case_id}/image")
@app.get("/api/v1/cases/{case_id}/image/{panel_id}")
def case_image(case_id: str, panel_id: str | None = None, _: CurrentUser = Depends(require_current_user)) -> FileResponse:
    stored = case_store.image_path(case_id, panel_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Case not found")
    path, panel = stored
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Case artwork not found")
    return FileResponse(path, media_type=panel.mime_type, filename=panel.file)


@app.delete("/api/v1/cases/{case_id}", response_model=CaseDetail)
def remove_case(case_id: str, _: CurrentUser = Depends(require_current_user)) -> CaseDetail:
    case = case_store.remove_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


@app.post("/api/v1/cases/{case_id}/restore", response_model=CaseDetail)
def restore_case(case_id: str, _: CurrentUser = Depends(require_current_user)) -> CaseDetail:
    case = case_store.restore_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Removed case not found")
    return case


async def _validated_upload_set(
    request_json: str,
    panel: UploadFile | None,
    panels: list[UploadFile] | None,
    *,
    filename_in_errors: bool,
) -> tuple[
    AnalysisRequest,
    tuple[UploadFile, ...],
    tuple[bytes, ...],
    tuple[ValidatedPanel, ...],
    tuple[bool, ...],
]:
    try:
        request = _available_request(AnalysisRequest.model_validate_json(request_json))
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=json.loads(exc.json()),
        ) from exc

    uploads = tuple(panels or ()) or ((panel,) if panel else ())
    if len(uploads) != len(request.panels):
        raise HTTPException(
            status_code=422,
            detail={"code": "panel_count_mismatch", "message": "Upload one image for every ordered request panel."},
        )
    contents: list[bytes] = []
    validated_panels: list[ValidatedPanel] = []
    blanks: list[bool] = []
    for panel_input, upload in zip(request.panels, uploads):
        content = await upload.read(MAX_UPLOAD_BYTES + 1)
        try:
            validated_panel, blank = await asyncio.to_thread(
                validate_image, content, panel_input.panel_id
            )
        except UploadValidationError as exc:
            message = (
                f"{upload.filename or panel_input.file}: {exc.message}"
                if filename_in_errors
                else exc.message
            )
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": message},
            ) from exc
        contents.append(content)
        validated_panels.append(validated_panel)
        blanks.append(blank)
    return request, uploads, tuple(contents), tuple(validated_panels), tuple(blanks)


@app.post("/api/v1/cases", response_model=CaseDetail, status_code=201)
async def create_case(
    background_tasks: BackgroundTasks,
    request_json: Annotated[str, Form(alias="request")],
    panel: Annotated[UploadFile | None, File()] = None,
    panels: Annotated[list[UploadFile] | None, File()] = None,
    auto_process: Annotated[bool, Form(alias="autoProcess")] = True,
    display_name: Annotated[str | None, Form(alias="displayName")] = None,
    user: CurrentUser = Depends(require_current_user),
) -> CaseDetail:
    request, uploads, contents, validated_panels, _ = await _validated_upload_set(
        request_json, panel, panels, filename_in_errors=True
    )

    case = await asyncio.to_thread(
        case_store.create_case,
        request,
        validated_panels,
        contents,
        filenames=tuple(upload.filename or input.file for upload, input in zip(uploads, request.panels)),
        display_name=display_name,
        current_user_id=user.user_id,
        current_username=user.username,
    )
    if auto_process and case_store.claim_case(case.case_id):
        background_tasks.add_task(_process_claimed_case, case.case_id)
        refreshed = case_store.get_case(case.case_id)
        if refreshed is not None:
            return refreshed
    return case


@app.post("/api/v1/cases/{case_id}/scan", response_model=CaseDetail, status_code=202)
def scan_case(case_id: str, background_tasks: BackgroundTasks, _: CurrentUser = Depends(require_current_user)) -> CaseDetail:
    case = case_store.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    if not case_store.claim_case(case_id):
        raise HTTPException(status_code=409, detail="Case is already processing")
    background_tasks.add_task(_process_claimed_case, case_id)
    claimed = case_store.get_case(case_id)
    if claimed is None:  # pragma: no cover - defensive persistence check
        raise HTTPException(status_code=404, detail="Case not found")
    return claimed


def _claim_conflict(exc: case_store.CaseReviewClaimConflict) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "case_claimed_by_another_reviewer",
            "message": str(exc),
            "assignedToUsername": exc.assigned_to_username,
        },
    )


@app.post("/api/v1/cases/{case_id}/claim", response_model=CaseDetail)
def claim_review_case(case_id: str, user: CurrentUser = Depends(require_current_user)) -> CaseDetail:
    try:
        case = case_store.claim_review_case(
            case_id, current_user_id=user.user_id, current_username=user.username
        )
    except case_store.CaseReviewClaimConflict as exc:
        raise _claim_conflict(exc) from exc
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


@app.post("/api/v1/cases/{case_id}/release", response_model=CaseDetail)
def release_review_case(case_id: str, user: CurrentUser = Depends(require_current_user)) -> CaseDetail:
    try:
        case = case_store.release_review_case(case_id, current_user_id=user.user_id)
    except case_store.CaseReviewClaimConflict as exc:
        raise _claim_conflict(exc) from exc
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


@app.post("/api/v1/cases/{case_id}/decision", response_model=CaseDetail)
def decide_case(case_id: str, decision: HumanDecisionRequest, user: CurrentUser = Depends(require_current_user)) -> CaseDetail:
    try:
        case = case_store.save_human_decision(
            case_id,
            decision,
            current_user_id=user.user_id,
            current_username=user.username,
        )
    except case_store.CaseReviewClaimConflict as exc:
        raise _claim_conflict(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


@app.post("/api/v1/analyses", response_model=AnalysisResponse)
async def create_analysis(
    request_json: Annotated[str, Form(alias="request")],
    panel: Annotated[UploadFile | None, File()] = None,
    panels: Annotated[list[UploadFile] | None, File()] = None,
    _: CurrentUser = Depends(require_current_user),
) -> AnalysisResponse:
    request, _, contents, validated_panels, blanks = await _validated_upload_set(
        request_json, panel, panels, filename_in_errors=False
    )
    return await run_connected_analysis(request, validated_panels, contents, blanks=blanks)


@app.get("/", include_in_schema=False)
@app.get("/{full_path:path}", include_in_schema=False)
def web_application(full_path: str = "") -> FileResponse:
    """Serve the compiled Vite application without a separate Node server."""
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="API route not found")
    requested = (WEB_DIST / full_path).resolve()
    if full_path and requested.is_relative_to(WEB_DIST) and requested.is_file():
        return FileResponse(requested)
    index = WEB_DIST / "index.html"
    if index.is_file():
        return FileResponse(index, media_type="text/html")
    raise HTTPException(status_code=503, detail="Web application has not been built")
