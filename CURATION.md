# Repository boundary

This repository is the minimal deployable and testable application. It was
assembled from the larger research workspace using an explicit allowlist.

## Included

- `app/` — the Vite/React verification console, queue, case intake, settings,
  review workspace, and artwork evidence viewer.
- `backend/app/` — the FastAPI service, authentication, SQLite persistence,
  queue ownership, public-catalog import, recognition cache, Tesseract reader,
  OpenRouter vision reader, evidence alignment, and deterministic comparison.
- `backend/tests/` — API, authentication, persistence, catalog, and analysis
  regression tests.
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

The initial curated snapshot was validated on 2026-08-18:

- locked npm install, zero-advisory audit, ESLint, TypeScript, and Vite build;
- 56 backend tests, Ruff, and checked-in JSON Schema drift check;
- independent multi-stage Docker build and container health/UI smoke test;
- Terraform formatting plus successful validation of both roots with Terraform
  1.13.5; and
- authenticated browser inspection of login, work queue, Add Case, settings,
  and empty Review states.

During that pass, disconnected leaderboard and annotation-workbench CSS was
removed. The compiled stylesheet fell from 81.25 KB to 54.93 KB, and the empty
Review state was corrected so it renders once at the full workspace width.
