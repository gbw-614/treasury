# Application architecture

This document describes the deployable application in this repository. It is
the map for maintainers; the JSON payload details remain in the
[verification input contract](VERIFICATION_INPUT_CONTRACT.md).

## Runtime shape

The production artifact is one multi-stage Docker image:

1. Node builds the React/Vite application.
2. The runtime stage installs Python, the locked backend dependencies, and
   Tesseract with English language data.
3. Uvicorn runs FastAPI. FastAPI serves both `/api/v1/*` and the compiled UI.

The service is intentionally a single-host application. SQLite and uploaded
artwork live below `VERIFICATION_DATA_DIR` (`/data` in Compose), which must be
mounted on persistent storage. There is no separate Node server or database
service in production.

## Request and processing flow

```text
browser or catalog
       |
       v
explicit verification-request-v2 + ordered artwork panels
       |
       v
validate JSON, media type, dimensions, byte limits, and panel order
       |
       v
persist case and immutable artwork snapshot in SQLite/data directory
       |
       v
run exactly one selected reader: Tesseract OCR OR blind LLM vision
       |
       v
deterministic expected-versus-detected comparison
       |
       v
pass / fail / needs review -> optional human disposition
```

Expected application values are never sent to the LLM extraction prompt. The
reader gathers evidence; deterministic Python rules compare it with the
explicit expected values supplied by the case. The application does not infer
missing application facts from a COLA class code.

OCR mode provides transcript tokens, confidence values, and geometry from
Tesseract. LLM mode provides literal text blocks, model-supplied geometry, and
warning-presentation observations; deterministic code applies the configured
checks rather than asking the model to decide which text is a brand or class.
Tesseract does not establish typography such as boldness, so rules that require
visual presentation evidence remain conservative in OCR mode. Legacy v1
requests remain readable for existing queued work, but new cases use v2.

## Persistent state

`backend/app/services/case_store.py` owns case, catalog-job, and artwork
persistence. `backend/app/services/auth_store.py` owns users, sessions,
preferences, and authentication audit rows. Both use tables in the same
`verification.sqlite3` database and enable WAL mode.

- Passwords are hashed with scrypt.
- Only hashes of opaque session tokens are stored.
- The browser receives an HTTP-only, same-site session cookie.
- A username may have one active session; replacement requires confirmation.
- Up to three enabled reviewer accounts are supported.
- Queue filters and reader preferences are stored per user.
- Cases share one queue and default to the uploading reviewer.
- A case may carry an optional operational `caseReference`; catalog imports
  preserve the manifest's reference and the queue can export it in CSV reports.

Recognition results are cached by reader type, artwork hash, and reader
version. Clearing that cache never changes completed case analyses.

## Background work and concurrency

Case processing is asynchronous relative to the HTTP request, but it remains
in the FastAPI process. Startup recovers cases that were left in a processing
state after interruption. The environment variables below provide explicit
resource limits:

- `VERIFICATION_ANALYSIS_CONCURRENCY` limits concurrently active case
  pipelines.
- `VERIFICATION_OCR_CONCURRENCY` limits Tesseract subprocess work.
- `VERIFICATION_VISION_CONCURRENCY` limits outbound vision requests, including
  multi-panel fan-out.

This design is appropriate for the current single-instance deployment. Moving
to multiple application replicas would require external job coordination and
shared storage rather than merely increasing replica count.

## Public catalog boundary

The configured catalog is public, read-only source data. The backend fetches a
fixed manifest URL, validates same-origin object paths, byte counts, hashes,
image properties, application schemas, and ordered panel IDs, then snapshots
accepted cases into local storage. Runtime work never depends on an S3 list
operation or on mutable remote objects after import. See
[public catalog integration](public-s3-catalog.md).

## Source map

### Frontend

- `app/verify/page.tsx` — authenticated application orchestration and the
  review workspace.
- `app/verify/work-queue.tsx` — queue table, filters, assignments, and queue
  actions.
- `app/verify/case-entry-form.tsx` — manual, single-file, and batch case
  intake.
- `app/verify/catalog-import.tsx` — public-catalog selection and import
  progress.
- `app/components/evidence-viewer.tsx` — panel display, evidence boxes, zoom,
  hover, and pin behavior.
- `app/verification-types.ts` — frontend representations of API contracts.

### Backend

- `backend/app/main.py` — FastAPI lifecycle, authentication dependency, routes,
  and background-job coordination.
- `backend/app/schemas/` — Pydantic request, extraction, queue, and result
  contracts.
- `backend/app/field_library.py` and `backend/app/config/` — the versioned
  field definitions and deterministic matching modes used by v2 requests.
- `backend/app/services/connected_analysis.py` — reader orchestration and
  deterministic comparison.
- `backend/app/services/openrouter_vision.py` — blind OpenRouter/Gemini
  extraction adapter.
- `backend/app/services/tesseract_ocr.py` — local Tesseract adapter.
- `backend/app/services/quote_alignment.py` — transcript-to-geometry alignment.
- `backend/app/services/image_validation.py` — upload decoding and safety
  limits.
- `backend/app/services/s3_catalog.py` — constrained public-catalog retrieval.
- `backend/app/services/recognition_cache.py` — recognition-result cache.
- `backend/app/services/case_store.py` and `auth_store.py` — persistence.

Test-only analysis builders live under `backend/tests/`; Docker copies only
`backend/app/`, so they are not included in the production runtime.

## Change discipline

Preserve these invariants when extending the application:

1. Keep expected values out of reader prompts.
2. Keep extraction separate from deterministic comparison.
3. Treat unreadable, absent, or unsupported evidence as review—not as proof of
   a discrepancy.
4. Preserve raw reader results and evidence provenance in completed analyses.
5. Keep catalog imports explicit, validated, and snapshotted.
6. Do not silently change a completed case when prompts, models, or rules
   change; run a new analysis instead.
7. Update Pydantic contracts first, regenerate checked-in schemas, and keep
   frontend types aligned.

The required regression gate is: frontend lint/build, backend tests and Ruff,
schema drift check, Docker build/health smoke, and browser checks of login,
queue intake, settings, and review behavior.
