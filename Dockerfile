# Two stages: build the CSS, then ship a runtime with no build tools in it.
FROM python:3.13-slim AS css

WORKDIR /build
ADD https://github.com/tailwindlabs/tailwindcss/releases/latest/download/tailwindcss-linux-x64 /usr/local/bin/tailwindcss
RUN chmod +x /usr/local/bin/tailwindcss
COPY assets ./assets
COPY templates ./templates
COPY apps ./apps
RUN tailwindcss -i assets/tailwind/input.css -o static/css/app.css --minify


FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=config.settings.prod

# libpq for psycopg, curl for the container health check.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
COPY --from=css /build/static/css/app.css ./static/css/app.css

# collectstatic has to import production settings, which refuse to load on
# development defaults -- so the build hands them throwaway values that pass
# those checks. None of this reaches the running container: the real values
# come from the environment at run time, and a container started without them
# still refuses to boot.
RUN SECRET_KEY=build-time-placeholder-not-used-at-runtime-0000000000 \
    DATABASE_URL=postgres://u:p@localhost/db \
    ALLOWED_HOSTS=build.invalid \
    EMAIL_HOST=build.invalid \
    python manage.py collectstatic --noinput

# Never run as root.
RUN useradd --create-home --uid 10001 retail && chown -R retail:retail /app
USER retail

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# Two workers per core is the usual starting point; threads keep the offline
# sync endpoint responsive while a report is running.
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", "--threads", "2", \
     "--timeout", "60", "--access-logfile", "-"]
