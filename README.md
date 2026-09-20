# Retail platform

Multi-tenant retail and point-of-sale. One deployment, many shops, each on a
subscription. Django MTV + HTMX + Tailwind, shared Postgres with row-level
security, one domain for every tenant.

See the structure document for the data model, permission catalogue and
screen map.

## Deploying

    cp .env.example .env        # then edit it
    python -c "from django.core.management.utils import get_random_secret_key as k; print(k())"
    podman compose up -d --build
    podman compose exec web python manage.py migrate
    podman compose exec web python manage.py sync_permissions
    podman compose exec web python manage.py seed_plans
    podman compose exec web python manage.py createsuperuser

`config/settings/prod.py` refuses to start without a real `SECRET_KEY` and
`ALLOWED_HOSTS`, rather than silently running on the development defaults.

Put nginx in front with `ops/nginx.conf`, and put `ops/backup.sh` on a nightly
cron with `BACKUP_REMOTE` set — a backup on the machine that dies is not a
backup. `ops/restore.sh` is written and ready; try it once before you need it.

## Running it

Postgres and Redis:

    podman start retail-db retail-redis     # or podman run, see ops/

The application must **not** connect as a superuser. RLS is bypassed entirely
by superusers, which would make tenant isolation depend on application code
alone. `ops/bootstrap_db.sql` creates the `retail_app` role it should use.

    python -m venv .venv
    .venv/bin/pip install -r requirements-dev.txt
    cp .env.example .env
    .venv/bin/python manage.py migrate
    .venv/bin/python manage.py sync_permissions
    .venv/bin/python manage.py seed_plans
    .venv/bin/python manage.py seed_demo      # development only
    .venv/bin/python manage.py runserver

CSS (no Node required, the binary is self-contained):

    ./tailwindcss -i static/src/input.css -o static/css/app.css --watch

Background work (trials, usage, fiscal queue, stock alerts):

    .venv/bin/celery -A config worker -l info
    .venv/bin/celery -A config beat -l info

Without these, trials never expire, the fiscal queue never drains and usage
is never recorded for billing.

Tests:

    .venv/bin/python -m pytest

## Demo logins

Password for all four: `demo12345`

| Email | Role | Notes |
| --- | --- | --- |
| owner@demo.test | Owner | Everything, locked role |
| manager@demo.test | Manager | Approval PIN 1234, both branches |
| cashier@demo.test | Cashier | 5% discount ceiling, no cost prices |
| stock@demo.test | Stock clerk | No till access |

## Commands

| Command | What it does |
| --- | --- |
| `sync_permissions` | Push the code-declared permission catalogue into the database. Run on every deploy. |
| `seed_plans` | Create or update the subscription plans. |
| `apply_rls` | Enable row-level security on every table with a `tenant_id` column. Run after adding an app. |
| `seed_demo` | A demo shop with staff, for development. |

## Scheduled tasks

| Task | When | Why |
| --- | --- | --- |
| `advance_subscriptions` | 02:00 | trialing → past due → grace → read-only. Never deletion. |
| `snapshot_usage` | 02:30 | Per-branch billing counts and the platform dashboard. |
| `send_pending_fiscal_receipts` | every 10 min | Receipts queue while a shop is offline. |
| `clear_abandoned_carts` | 03:00 | Open baskets nobody finished. Held ones are kept. |
| `raise_stock_alerts` | 06:30 | Low stock and approaching expiry, emitted as events. |

## Rules that are not negotiable

- `tenant_id` comes from the session, never from a request parameter.
- Plan limits are checked at write time, never at read time.
- Stock and money are append-only. A correction is a reversing entry.
- Features are flags on a plan. There is no `if plan == "business"` anywhere.
- New capability = new app + its own `permissions.py`. Nothing existing changes.
