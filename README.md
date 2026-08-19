# Treasury Label Verification

For an overview of the approach, tools used, and assumptions made, see the
[implementation approach](docs/IMPLEMENTATION_APPROACH.md). For setup and
run instructions, see below.

## Run locally

Docker Compose is the recommended local path. It uses the same production
runtime (including Tesseract OCR), with no cloud account or LLM key required.

### Docker Compose quickstart

Prerequisites: [Docker Desktop](https://www.docker.com/products/docker-desktop/)
or Docker Engine with the Compose plugin. Confirm it is running with
`docker compose version`.

Create your local configuration, then use an editor to replace the example
bootstrap password with a strong, unique one. `.env` is Git-ignored; do not put
real credentials in `.env.example` or commit `.env`.

```bash
cp .env.example .env
# Edit .env: set VERIFICATION_BOOTSTRAP_USERNAME and
# VERIFICATION_BOOTSTRAP_PASSWORD, then save it locally.
docker compose up --build
```

Open [http://localhost:8000](http://localhost:8000) and sign in with that
bootstrap account. The account is created on the first application launch, and its password
is not changed by later edits to `.env`; use the password-reset command below
instead. There is intentionally no public sign-up endpoint.

### Try the included OCR evidence-ready samples

The repository includes a small, direct-import bundle in
[`examples/`](examples/). It contains 12
real public TTB artwork cases selected from a completed OCR run because every
substantive configured check has an OCR evidence box. In the running app:

1. Open **Work queue** and choose **Import from files**.
2. Choose every JSON and image file in `examples/`.
3. Select **Batch import**.

Each JSON is paired with one or more artwork panels by filename and carries its
`COLA-…` reference. These are evaluation fixtures, not authoritative COLA
application records. Their warning-presentation check normally still routes to
human review: OCR can locate the statutory text but cannot confirm bold type.

OCR mode works entirely locally. To enable the optional LLM reader, add your
own `OPENROUTER_API_KEY` to `.env`, then
restart the service. Never commit that key.

For a background launch use `docker compose up --build -d`; inspect it with
`docker compose ps` and `docker compose logs -f app`. Stop it with
`docker compose down`, then restart later with `docker compose up -d`.

Compose stores the SQLite database, accounts, application records, and uploaded
artwork in its managed named volume, so they survive container rebuilds and
`docker compose down`. To erase all local application data and start over
(including reviewer accounts), run:

```bash
docker compose down --volumes
```

This reset is irreversible. On the next launch, the bootstrap credentials in
your `.env` create the first account again.

If port 8000 is already in use, set `VERIFICATION_PORT=8001` in `.env` and open
[http://localhost:8001](http://localhost:8001) instead.

For a public HTTPS deployment set `VERIFICATION_SESSION_SECURE=true`; leave it
false for local HTTP. Each username has one active browser session. A second
login asks for confirmation before it disconnects the first.

Reviewer queue filters and analysis-reader mode are stored per account in the
same persistent SQLite database as sessions. Queue filters also have a browser
local-storage fallback while the server preference is loading. Cases form a
shared queue; a new case is assigned to its uploader unless explicitly
released.

The work queue's **Download report** action creates a CSV audit export. It
includes the optional caller/catalogue case reference, case and artwork IDs,
assignment and decision state, reader mode, check counts, errors, notes, and
timestamps. Removed cases remain in the export.

Recognition concurrency is intentionally explicit. The defaults allow one
case pipeline, one Tesseract subprocess, and up to three concurrent outbound
vision calls (useful for multi-panel cases):

```dotenv
VERIFICATION_ANALYSIS_CONCURRENCY=1
VERIFICATION_OCR_CONCURRENCY=1
VERIFICATION_VISION_CONCURRENCY=3
```

Each scan runs exactly one selected reader. Increase these values only after
measuring CPU, memory, provider rate limits, and queue latency on the
deployment host.

### Reviewer account administration

Add the second and third reviewer from the running container; passwords are
prompted securely and never appear in shell history:

```bash
docker compose exec app python -m app.user_admin create reviewer-two
docker compose exec app python -m app.user_admin create reviewer-three
docker compose exec app python -m app.user_admin list
```

Use `reset-password USERNAME` to change a password. Resetting it also
disconnects that user's active session.

### Optional contributor workflow (without Docker)

This is useful when changing the React UI or FastAPI service because Vite and
Uvicorn reload independently. It is not required to evaluate the application.
Install Node.js 22+, Python 3.12+, [uv](https://docs.astral.sh/uv/), and the
host `tesseract` executable first. Keep the root `.env` bootstrap setup from
the quickstart; leave `VERIFICATION_DATA_DIR` blank there to use
`backend/data/`, or point it at another writable path.

In one terminal, start the API (it listens on port 8000 and persists local data
under `backend/data/` by default):

```bash
cd backend
uv sync --all-groups
uv run uvicorn app.main:app --reload --port 8000
```

In another terminal, start the UI:

```bash
npm ci
npm run dev
```

Open the Vite URL shown in the terminal (normally
[http://localhost:5173](http://localhost:5173)). The development server proxies
`/api` requests to the API on port 8000. Check `tesseract --version` if OCR
fails in this mode.

### Common local checks

- `docker compose config --quiet` validates the Compose configuration.
- `curl http://localhost:8000/api/v1/health` should return `{"status":"ok"}`.
- If the app will not start, use `docker compose logs app`; a common cause is
  Docker Desktop not running or port 8000 being occupied.
- If only OCR is offered in the UI, that is expected until a valid
  `OPENROUTER_API_KEY` is configured and the container is restarted.

The production image contains the compiled Vite UI, FastAPI/Uvicorn, and
Tesseract. Node and the frontend source are not included in the runtime stage.

## Deploy to AWS

The single-instance EC2 deployment is defined in [`infra/`](infra/).
It uses Terraform, ECR, an ARM64 EC2 instance, a separately protected EBS data
volume, SSM Session Manager, and optional Route 53 DNS. Review
[`infra/README.md`](infra/README.md) before applying anything.
