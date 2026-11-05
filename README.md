# Videoflix Backend

Videoflix is a Django REST API for account management and a protected video
catalog. Docker Compose runs the backend, PostgreSQL, Redis, and a separate
RQ worker. FFmpeg is installed in the backend image used by both application
containers.

## Prerequisites

- Docker Desktop with the Docker Compose plugin (or Docker Engine and Compose)
- Git
- The official Videoflix frontend, when testing browser integration

The Developer-Akademie Docker materials are available in the
[official Docker files repository](https://github.com/Developer-Akademie-Backendkurs/material.videoflix-docker-files).
This backend keeps its own pinned images, environment variable names, health
checks, and worker service; use the instructions in this README for this
repository.

## Start locally

From the backend repository root, create a local environment file and edit its
placeholder values:

```powershell
Copy-Item .env.example .env
```

On macOS or Linux, use `cp .env.example .env`. Set a long random value for
`DJANGO_SECRET_KEY` and a local-only PostgreSQL password. Keep `.env` private;
it is ignored by Git. The committed `.env.example` contains placeholders only.

Build and start the services:

```sh
docker compose up --build -d
docker compose ps
```

The API is available at <http://127.0.0.1:8000/>. Only the backend port is
published, bound to the local machine. PostgreSQL and Redis are available to
the other Compose services on the private Compose network. Compose waits for
their health checks before starting the backend and worker.

Apply database migrations explicitly after starting the services:

```sh
docker compose exec backend python manage.py migrate
docker compose exec backend python manage.py createsuperuser
```

The custom user model uses an email address as its login identifier. The
entrypoint does not run migrations or create accounts automatically. These
commands target the database configured in your local `.env`; check that file
before running them. Do not point local development commands at a shared or
production database.

## Email configuration

Registration, activation, and password-reset messages use Django's SMTP
backend. Configure `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`,
`EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` or `EMAIL_USE_SSL`,
`DEFAULT_FROM_EMAIL`, and `FRONTEND_BASE_URL` in `.env`. SMTP credentials must
remain local and must never be committed. Automated mail tests use Django's
in-memory test email backend and do not require real SMTP credentials. This
is the project's SMTP test mode; it captures test messages without contacting
an SMTP server. To exercise registration or password reset manually, configure
working SMTP settings in `.env`.

## Add and process a video

1. Open <http://127.0.0.1:8000/admin/> and sign in with the superuser.
2. Add a video with its title, category, and source file. The admin validates
   the upload and queues processing after the database transaction commits.
3. Keep the worker running. It uses the shared media volume and FFmpeg to
   generate the 480p, 720p, and 1080p HLS renditions and thumbnail.
4. Check progress and errors with:

```sh
docker compose logs -f worker
```

Uploaded source files, thumbnails, and HLS output are stored in the persistent
`media_data` volume. PostgreSQL and Redis data use their own persistent volumes.
The API exposes catalog and playback resources under `/api/`; protected video
resources require the authentication cookies issued by the account endpoints.

## Frontend integration

The official frontend is maintained separately in the
[Videoflix frontend repository](https://github.com/Developer-Akademie-Backendkurs/project.Videoflix).
Run it using its own instructions (commonly with Live Server at
`http://127.0.0.1:5500`). The backend's default CORS and CSRF trusted origins
match that local address, and credentialed cross-origin requests are enabled
for the HttpOnly JWT cookies. If the frontend uses another address, update
`CORS_ALLOWED_ORIGINS` and `CSRF_TRUSTED_ORIGINS` in `.env` to that exact origin.

## Tests and checks

Run the Django test suite in a disposable test database managed by Django's
test runner:

```sh
docker compose run --rm backend python manage.py test
```

Useful project checks are:

```sh
docker compose exec backend python manage.py check
docker compose exec backend python manage.py makemigrations --check --dry-run
docker compose exec backend ffmpeg -version
docker compose exec worker python manage.py rqinfo
```

The test runner creates and tears down its test database; it does not apply
test migrations to the normal development database. The migration and system
checks above are read-only checks. The `rqinfo` command inspects the configured
queue; worker processing logs are available through the Compose logs command.

## Troubleshooting

- **Docker engine unavailable:** Start Docker Desktop and confirm
  `docker info` succeeds.
- **Missing environment values:** Ensure `.env` exists and replace the
  `replace-with-...` placeholders. Compose intentionally rejects missing
  required database credentials and Django secret key.
- **Database not ready:** Run `docker compose ps` and
  `docker compose logs --tail=100 db`; wait until the database health check
  passes before retrying.
- **Redis or worker issues:** Inspect `docker compose logs --tail=100 redis`
  and `docker compose logs --tail=100 worker`. The worker consumes the `default`
  RQ queue.
- **Code or dependency changes not visible:** Rebuild and recreate the
  application images with `docker compose up --build -d backend worker`.
- **Missing FFmpeg:** Rebuild the application image, then check
  `docker compose exec backend ffmpeg -version`.
- **Frontend requests blocked:** Verify the frontend's exact origin in both
  CORS and CSRF variables, then restart the backend service.

Use `docker compose stop` to stop this project's services while preserving
their data volumes. Do not remove volumes unless you intentionally want to
delete the local database, Redis data, or uploaded media.
