# Videoflix Backend Alignment Delivery

**Review date:** 2026-11-16

**Working branch:** `chore/align-course-setup`

## Course-standard alignment

- The ten documented account, authentication, password-reset, catalog, HLS
  manifest, and segment endpoints keep their existing paths and response
  contracts. JWTs remain in HttpOnly cookies.
- `.env.template` uses the official course names for the Django superuser,
  database, Redis, Django, and SMTP settings. `core/settings.py` also accepts
  the existing project aliases so local `.env` files can be migrated without
  changing credentials.
- Compose now exposes the course service names `web`, `db`, and `redis`. The
  project keeps a separate `worker`, pinned image versions, health checks,
  and persistent media, PostgreSQL, and Redis volumes.
- The web entrypoint applies committed migrations and optionally bootstraps
  the configured course superuser. It does not generate migrations at
  container startup. The custom email-login user has an optional unique
  `username` for that bootstrap; migration `0002_user_username` only adds a
  nullable column.
- The project keeps its `core`, `accounts`, and `videos` Django apps. Renaming
  them to names used by some student repositories would add migration and
  app-label risk without changing the official API.
- No student repository code was copied. The official Docker materials and
  endpoint/checklist documents were used as references.

## Verification on 2026-11-16

- A local fresh clone at commit `238a6b8` passed `docker compose config
  --quiet` and `docker compose up --build -d`. PostgreSQL, Redis, and web
  became healthy; the worker started after web health. The web entrypoint
  applied all committed migrations, including the additive username
  migration. The test-only stack used a separate port and newly created
  volumes; no existing database or Docker volume was removed.
- The PostgreSQL-backed Django suite passed: **210 tests**. Coverage.py ran
  with branch measurement across `core`, `accounts`, and `videos`, excluding
  only test modules and generated migrations. It reported **100.00% line
  coverage and 100.00% branch coverage** (1,230 statements and 214 branches).
  `coverage report --fail-under=95` passed.
- `manage.py check`, `makemigrations --check --dry-run`, Ruff, and Compose
  configuration validation passed. FFmpeg 5.1.9 was available in the fresh
  application image. Redis ping returned `True`, and a job submitted to the
  running RQ worker completed with result `6`.
- The integration suite processed a real test video through RQ and FFmpeg,
  checked the 480p, 720p, and 1080p renditions, and fetched protected HLS
  manifests and segments.
- The official frontend checkout was served from a temporary copy with its
  API base redirected to the isolated test port. Browser login and the
  protected video catalog returned HTTP 200, the page reached the empty
  catalog state, the console had no errors, and `document.cookie` was empty.
  The official frontend checkout itself was not modified.

## Differences and intentional deviations

The repository's original Compose service was named `backend` and used the
older `DJANGO_*` and `POSTGRES_*` variables. Those project-owned files were
updated to expose the official `web` service and accept the official
environment names while retaining the previous aliases.

The official Docker sample uses mutable database/cache tags and starts its
worker with the web process. This project retains pinned images, health
checks, and a separate worker so the worker can restart independently. The
entrypoint applies committed migrations instead of running `makemigrations`
at startup. The web container uses Django's development server for this local
course stack. These choices are documented in `README.md`; the official
materials repository was not edited or vendored.

The course checklist says not to change Docker files received from its setup.
The files in this repository were not byte-for-byte copies of that setup, so
this branch adapted the repository's own Docker files rather than replacing
them with upstream files. If the evaluator requires the supplied Docker
files to remain byte-for-byte unchanged, that remains a rubric-specific
deviation to confirm with the mentor.

## Remaining limits and risks

- SMTP delivery was not tested against a real provider because no SMTP
  credentials were available. Registration, activation, and reset email
  behavior is covered with Django's in-memory test backend.
- Browser verification covered login and the protected catalog. Registration
  activation and password-reset UI flows were not driven through a real inbox;
  their API and email-template flows are covered by automated tests.
- No public deployment or HTTPS production environment was requested or
  tested. The Docker verification used a disposable test project and
  credentials, not a shared or production database.
- Registration intentionally returns a different response for a duplicate
  email, as required by the current API contract. Password-reset responses
  remain generic, though synchronous email delivery can affect timing.
- Unsafe API writes without a trusted `Origin` are rejected. Direct curl or
  Postman writes therefore need an allowed `Origin` header.
