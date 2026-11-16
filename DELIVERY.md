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
- Compose uses the three official service names: `web`, `db`, and `redis`.
  The web entrypoint supervises RQ and Gunicorn inside the web service. Pinned
  image versions, health checks, and persistent media, PostgreSQL, and Redis
  volumes remain.
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

The tested application source was based on branch commit
`df6b1d15fd361f203ec26f32d56f5da7024f57a1`, plus the worker-supervision fix
recorded in this delivery. Verification used a separate Compose project,
test-only credentials, a dedicated port, and its own named volumes. No existing
Docker volumes or production data were removed.

### Django and PostgreSQL

- The PostgreSQL-backed Django test runner created a separate test database
  and passed **211 tests**, with no failures, errors, or skips.
- Coverage.py measured `core`, `accounts`, and `videos`. Only test files and
  generated migrations were omitted. The report covered **1,267 statements**
  and **228 branches**: **99.53% statement coverage** and **98.68% branch
  coverage** (six statements and three branch outcomes remain uncovered in
  `core/process_supervisor.py`). `coverage report --fail-under=95` passed.
- `python manage.py check` passed with no silenced issues.
- `python manage.py makemigrations --check --dry-run` reported no changes.
- Automated tests exercised all ten documented API routes, valid and invalid
  authentication and password flows, cookie behavior, email templates and
  token expiry, protected HLS access, path traversal defenses, uploads,
  processing failures and retries, media cleanup, concurrency, and admin
  state handling. No test skip directives were found.

### Docker, Redis, RQ, and static files

- `docker compose config --quiet` and the image build passed in the isolated
  Compose project. The `web`, `db`, and `redis` services became healthy.
- The PostgreSQL and Redis health checks passed. The web entrypoint applied
  committed migrations, collected static files, and created the configured
  test superuser automatically.
- Gunicorn 26.2.0 served the API with three workers. WhiteNoise returned the
  Django Admin CSS with HTTP 200 and the expected `text/css` content type.
- The RQ worker ran inside the web container. A real RQ job completed with
  result `6`; Redis was stopped and restarted in the isolated project, and the
  worker logged connection retries before completing a job after recovery.
- Killing the RQ child caused the supervisor to stop Gunicorn and exit the web
  container. Docker restarted the service; the recovered container was
  healthy and had both RQ and Gunicorn running. A normal web stop exited cleanly.
- Recreating the web container preserved the test database, superuser, Redis,
  uploaded source, thumbnail, and HLS files on their dedicated volumes.

### FFmpeg and HLS playback

- A 12-second 1080p test video was uploaded through Django Admin and processed
  by the real integrated RQ worker and FFmpeg. The video reached the ready
  state; source and thumbnail files existed; `index.m3u8` and TS segments were
  created for 480p, 720p, and 1080p.
- The official frontend was obtained from the
  [Developer Akademie frontend repository](https://github.com/Developer-Akademie-Backendkurs/project.Videoflix)
  into a temporary copy. Only the temporary copy's API base URL was changed.
  Browser login, catalog display, and playback succeeded at all three
  resolutions. Each rendition's manifest and segments returned HTTP 200;
  media elements reached `readyState=4` with no playback error. The browser
  console reported zero errors and zero warnings.
- The access and refresh cookies were HttpOnly and were removed by logout.
  The test-only development server used HTTP, so the cookies were not Secure.
  An unauthenticated protected API request after logout returned 401.
- Direct HTTP requests verified the allowed frontend Origin, generic denied
  responses for missing or untrusted Origin on unsafe requests, successful
  login/list/refresh/logout, and authenticated manifest and segment delivery.
  Browser/API writes must include an origin listed in
  `CORS_ALLOWED_ORIGINS`; the default is `http://127.0.0.1:5500`.

### Ruff and course-style audit

- Ruff's explicit docstring check passed for production modules.
- The general Ruff run reports **79 diagnostics**: 41 `EXE002` findings due
  to Python files appearing executable inside the Linux image even though Git
  tracks them as mode `100644`; 29 `SIM117` nested-context style findings in
  tests; seven `RUF012` findings for Django model/migration metadata; and two
  `I001` import-order findings.
- With a 79-character limit, the E/F Ruff check reports **six E501 lines**:
  three in `core/settings.py` and three in `videos/tests.py`. The AST audit
  found 173 production functions, none longer than 14 physical lines; nine
  of the 367 test functions exceed 14 lines. These are unresolved course-style
  deviations, not runtime failures. They remain visible here rather than being
  counted as passing checks.

### Checks not performed

- SMTP delivery to an external provider was not tested. Registration,
  activation, and reset messages used Django's in-memory test backend; no real
  SMTP credentials were configured.
- HTTPS production cookie behavior, public deployment, and browser flows that
  require clicking a real email link were not tested. Activation and reset
  tokens, messages, and endpoint behavior are covered by automated tests.

## Differences and intentional deviations

The repository's original Compose service was named `backend` and used the
older `DJANGO_*` and `POSTGRES_*` variables. Those project-owned files were
updated to expose the official `web` service and accept the official
environment names while retaining the previous aliases.

The [official Docker sample](https://github.com/Developer-Akademie-Backendkurs/material.videoflix-docker-files)
uses mutable database/cache tags, starts its worker with the web process, and
runs `makemigrations` during startup. This branch follows its three-service
layout and keeps the worker inside the web container, with a supervisor that
exits the container if either child service stops. Gunicorn and WhiteNoise
serve the API and static assets, following the course model. This project
retains pinned images and health checks, and applies committed migrations
instead of generating them at startup. These differences are documented in
`README.md`; upstream course materials were not edited or copied.

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
- Browser verification covered login, the protected catalog, and playback of
  the real test video at all three HLS resolutions. Registration activation
  and password-reset UI flows were not driven through a real inbox; their API
  and email-template flows are covered by automated tests.
- No public deployment or HTTPS production environment was requested or
  tested. The Docker verification used a disposable test project and
  credentials, not a shared or production database.
- Registration intentionally returns a different response for a duplicate
  email, as required by the current API contract. Password-reset responses
  remain generic, though synchronous email delivery can affect timing.
- Unsafe API writes without a trusted `Origin` are rejected. Direct curl or
  Postman writes therefore need an allowed `Origin` header.
