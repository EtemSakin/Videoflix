# Videoflix Backend Delivery

**Review date:** 2026-11-06

## Verified implementation

- The backend implements the ten documented API routes for registration,
  activation, login, logout, token refresh, password reset, video listing,
  HLS manifests, and HLS segments.
- The final PostgreSQL-backed suite passed: **181 tests**.
- Branch-aware Coverage.py measured **95%** across `accounts` and `videos`,
  excluding test modules and migrations. Coverage.py ran only in a disposable
  test container; it is not a runtime dependency.
- Ruff, the project's 79-character line target, PEP 257 for the updated tests,
  the 14-physical-line function check, Django's system check, and
  `makemigrations --check --dry-run` passed.
- The real video pipeline test generated HLS outputs for all three required
  resolutions using FFmpeg and a real RQ job.

## Clean-clone Docker smoke check

A Windows checkout with `core.autocrlf=true` initially converted
`backend.entrypoint.sh` to CRLF. Docker then restarted the backend and worker
because the shell rejected `set -eu`. `.gitattributes` now pins that script to
LF; a new checkout with this rule produced zero CRLF pairs.

After the entrypoint was normalized in the disposable clone, the complete
Compose stack built and started. PostgreSQL and Redis became healthy, Django
started without system-check errors, and the worker listened on the `default`
queue. The smoke check applied all 33 migrations only to the isolated
`videoflix-commit40` PostgreSQL volume. It verified Django's PostgreSQL
connection, a Django cache write/read through Redis, FFmpeg in the backend and
worker, and the anonymous video-list response (`401`).

The separate Docker worker processed a generated two-second test clip. The
database status became `ready`; a thumbnail and the `480p`, `720p`, and `1080p`
rendition directories were present in the shared media volume.

## External prerequisites and limits

- Real registration, activation, and password-reset delivery requires the
  owner to provide valid SMTP settings in the private local `.env`. No SMTP
  credentials were available for a real external delivery test; automated
  email tests use Django's in-memory test backend.
- A manual browser check used a pre-created active test account. Login and the
  video catalog succeeded; a generated three-second clip played through the
  official frontend's HLS.js player at 480p. The browser received HTTP 200 for
  the manifest and segment, both video elements reached `readyState` 4, and
  the browser console showed no errors.
- This browser check did not cover registration, clicking a real activation
  email, SMTP delivery, or selecting 720p/1080p in the frontend. The isolated
  RQ/FFmpeg integration test covered all three rendition outputs and their
  authenticated API delivery. Automated email tests use Django's in-memory
  backend.
- Account-enumeration risk remains at registration because the documented
  duplicate-account response differs from successful registration. Password
  reset returns the same response for known and unknown addresses, but sends
  existing-account mail synchronously, so response timing can still differ.
  Login uses a generic credential error; no claim of complete timing
  indistinguishability is made.
- Public deployment is not required by the official assignment. No service
  was deployed publicly.

The [official Developer-Akademie Docker files](https://github.com/Developer-Akademie-Backendkurs/material.videoflix-docker-files)
are relevant course setup material. This repository's Compose configuration
and environment variables are its implemented runtime contract; use the
instructions in `README.md` for this backend.
