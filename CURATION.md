# Repository boundary

This repository is the minimal deployable and testable application. It was
assembled from the larger research workspace using an explicit allowlist.

## Included

- `app/` — the Vite/React verification console, queue, case intake, settings,
  review workspace, and artwork evidence viewer.
- `backend/app/` — the FastAPI service, authentication, SQLite persistence,
  queue ownership, public-catalog import, recognition cache, Tesseract reader,
  OpenRouter vision reader, evidence alignment, versioned field library, and
  deterministic comparison.
- `backend/tests/` — API, authentication, persistence, catalog, field-library,
  and analysis regression tests (including test-only fake-reader helpers).
- `backend/scripts/export_contract_schemas.py` — deterministic JSON Schema
  generation and drift checking.
- `schemas/` — the six public request, extraction, geometry, localization,
  rule-result, and response contracts used by the application.
- `infra/` — Terraform for the existing single-instance AWS deployment and its
  separately bootstrapped encrypted state bucket.
- `docs/` — the application architecture, verification input contract, and
  public-catalog integration contract.
- Root Docker, Compose, Node, TypeScript, Vite, lint, and environment-example
  files required to build and run the application.

## Deliberately excluded

- Downloaded TTB artwork, COLA datasets, and generated catalog objects.
- Annotation drafts, benchmark results, OCR experiments, model comparison
  outputs, screenshots, and UI mockups.
- The research-era product design document. It remains in the research
  workspace because several implementation assumptions and relative links no
  longer describe the current single-reader application.
- Applicant forks, extracted reference-project code, and borrowed fixtures.
- Historical ground-truth and leaderboard UIs, Next.js, Cloudflare/Wrangler,
  Drizzle, D1, and OpenAI Sites scaffolding.
- Synthetic sample packs and bulk-import demonstration directories. Curated
  fixtures can be added later as an explicit, reviewed change.
- Local `.env` files, API keys, SQLite databases, uploads, recognition caches,
  virtual environments, dependency directories, build output, IDE metadata,
  Terraform state, and deployment secrets.
- Machine-specific launch agents and temporary deployment bundles.

The public reference catalog is external runtime data configured by
`VERIFICATION_S3_CATALOG_URL`; it is never embedded in the application image or
Git repository.

## Validation performed

The current curated snapshot was validated on 2026-08-18:

- `npm ci`, ESLint, TypeScript, and the Vite production build;
- 72 backend tests, Ruff, and checked-in JSON Schema drift checking;
- a fresh multi-stage Docker build whose context was 850 KB; and
- an isolated Compose run that passed the health endpoint, served the UI, and
  accepted a temporary bootstrap login.

The Docker context excludes local agent worktrees, credentials, database data,
caches, and the wider research workspace.
