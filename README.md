# Videoflix Backend

Videoflix is a Django REST API for account management and a protected video
catalog. Docker Compose runs Django, PostgreSQL, and Redis in the course's three-service
layout. The web entrypoint starts an RQ worker alongside the Django server.
FFmpeg is installed in the application image.

## Prerequisites

- Docker Desktop with the Docker Compose plugin (or Docker Engine and Compose)
- Git
- The official Videoflix frontend, when testing browser integration

The official Docker setup defines `web`, `db`, and `redis` and the environment
variables documented in its `.env.template`. This project uses those service
and environment names, including the worker running in the web container.
The project keeps version-pinned images and database and Redis health checks.
Gunicorn serves the API and WhiteNoise serves Django Admin static files, as in
the course setup. Version-pinned images, health checks and committed-only
migrations remain deliberate deviations from the starter files.

## Start locally

From the backend repository root, create a local environment file and edit its
placeholder values:

```powershell
Copy-Item .env.template .env
```

On macOS or Linux, use `cp .env.template .env`. Set a long random value for
`SECRET_KEY`, a local-only PostgreSQL password, and a strong
`DJANGO_SUPERUSER_PASSWORD` so Compose can create the staff account. Keep
`.env` private; it is ignored by Git. The committed `.env.template` contains
placeholders only. Its environment names follow the official course template;
existing `.env` files using the former `DJANGO_*`, `POSTGRES_*`, and
`REDIS_CACHE_URL` names remain supported. `WEB_PORT` defaults to `8000` and
can be changed if that port is already in use.

Build and start the services:

```sh
docker compose up --build -d
docker compose ps
```

The API is available at <http://127.0.0.1:8000/>. Only the web port is
published, bound to the local machine. PostgreSQL and Redis are available to
the other Compose services on the private Compose network. Compose waits for
healthy PostgreSQL and Redis services before starting Django. The web
entrypoint applies committed migrations and collects static files before
starting the background RQ worker and Gunicorn.

On each web server start, the entrypoint applies committed migrations and
creates a superuser when all three `DJANGO_SUPERUSER_*` variables are set. It
never generates migrations at container startup. The user model keeps email
as its login identifier and has an optional unique `username` for the course
superuser bootstrap. The accompanying migration only adds a nullable column.

If you leave the superuser password empty, create the staff account
interactively after starting the services:

```sh
docker compose exec web python manage.py createsuperuser
```

This command targets the database configured in your local `.env`; check that
file before running it. Do not point local development commands at a shared or
production database.

## Email configuration

Registration, activation, and password-reset messages use Django's SMTP
backend. Configure `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`,
`EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS` or `EMAIL_USE_SSL`,
`DEFAULT_FROM_EMAIL`, and `FRONTEND_BASE_URL` in `.env`. SMTP credentials must
remain local and must never be committed. Automated mail tests use Django's
in-memory test email backend and do not require real SMTP credentials. To
exercise registration or password reset manually, configure working SMTP
settings in `.env`. If `DEFAULT_FROM_EMAIL` is blank, the SMTP username is used
as the sender address.

## Add and process a video

1. Open <http://127.0.0.1:8000/admin/> and sign in with the superuser.
2. Add a video with its title, category, and source file. The admin validates
   the upload and queues processing after the database transaction commits.
3. Keep the web service running. Its RQ worker shares the media volume and
   uses FFmpeg to generate HLS in 480p, 720p and 1080p and the thumbnail.
4. Check progress and errors with:

```sh
docker compose logs -f web
```

Uploaded source files, thumbnails, and HLS output are stored in the persistent
`media_data` volume. PostgreSQL and Redis data use their own persistent volumes.
The API exposes catalog and playback resources under `/api/`; protected video
resources require the authentication cookies issued by the account endpoints.

## API overview

| Method | Endpoint | Access |
| --- | --- | --- |
| `POST` | `/api/register/` | Public |
| `GET` | `/api/activate/<uidb64>/<token>/` | Public |
| `POST` | `/api/login/` | Public |
| `POST` | `/api/logout/` | Refresh-token cookie |
| `POST` | `/api/token/refresh/` | Refresh-token cookie |
| `POST` | `/api/password_reset/` | Public |
| `POST` | `/api/password_confirm/<uidb64>/<token>/` | Public |
| `GET` | `/api/video/` | Access-token cookie |
| `GET` | `/api/video/<movie_id>/<resolution>/index.m3u8` | Access-token cookie |
| `GET` | `/api/video/<movie_id>/<resolution>/<segment>/` | Access-token cookie |

The authentication cookies are named `access_token` and `refresh_token` and
are HttpOnly. Browser requests include credentials. For direct API clients,
unsafe `/api/` requests must send an allowed `Origin` header; the default
origin is `http://127.0.0.1:5500`.

## Project structure

- `core/` contains Django settings and root URL routing.
- `accounts/` contains the email-based user model, authentication, activation,
  password reset, and account API views.
- `videos/` contains video administration, upload validation, RQ tasks,
  FFmpeg processing, caching, and protected HLS endpoints.

The project keeps the clear `accounts` and `videos` app names. Other student
repositories use names such as `auth_app` and `video_app`, but renaming these
apps would not improve the official API contract and would add migration and
app-label risk.

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
docker compose run --rm web python manage.py test
```

Useful project checks are:

```sh
docker compose exec web python manage.py check
docker compose exec web python manage.py makemigrations --check --dry-run
docker compose exec web ffmpeg -version
docker compose exec web python manage.py shell -c "from django_rq import get_queue; print(get_queue('default').connection.ping())"
```

The test runner creates and tears down its test database; it does not apply
test migrations to the normal development database. The migration and system
checks above are read-only checks. The Redis ping inspects the configured RQ
connection; worker status and processing output are available through
`docker compose ps` and `docker compose logs web`.

To measure branch-aware coverage, install Coverage.py in the running web
container, then run the suite and enforce the project's 95% threshold:

```sh
docker compose exec web python -m pip install coverage
docker compose exec web python -m coverage erase
docker compose exec web python -m coverage run \
  --branch --source=core,accounts,videos \
  --omit='*/tests.py,*/migrations/*' manage.py test
docker compose exec web python -m coverage report --precision=2 --fail-under=95
```

The report covers application code in `core`, `accounts`, and `videos`; test
modules and generated migrations are excluded. Coverage.py is a development
tool and is not required by the application at runtime.

## Troubleshooting

- **Docker engine unavailable:** Start Docker Desktop and confirm
  `docker info` succeeds.
- **Missing environment values:** Ensure `.env` exists and replace the
  `replace-with-...` placeholders. PostgreSQL requires a non-empty local
  password; the web entrypoint requires a Django secret key and DB password.
- **Database not ready:** Run `docker compose ps` and
  `docker compose logs --tail=100 db`; wait until the database health check
  passes before retrying.
- **Redis or worker issues:** Inspect `docker compose logs --tail=100 redis`
  and `docker compose logs --tail=100 web`. The web service starts an RQ worker for the `default` queue.
- **Code or dependency changes not visible:** Rebuild and recreate the
  application images with `docker compose up --build -d web`.
- **Missing FFmpeg:** Rebuild the application image, then check
  `docker compose exec web ffmpeg -version`.
- **Frontend requests blocked:** Verify the frontend's exact origin in both
  CORS and CSRF variables, then restart the backend service.

Use `docker compose stop` to stop this project's services while preserving
their data volumes. Do not remove volumes unless you intentionally want to
delete the local database, Redis data, or uploaded media.
