# Putting this on a server

Written for whoever puts the system online — you, or somebody you hire. It
assumes one small server (2 vCPU, 4 GB is plenty for a handful of shops), a
domain you control, and nothing else.

Everything here has been run except the parts that need a real domain. The
image builds and boots; a shop signs up, signs in and opens every page in its
menu; nginx terminates TLS in front of it and serves the product photos; the
nightly dump has been restored into an empty database and the shop opened
again from it. What has not been done is a certificate for a name that exists
on the public internet, so that is the step to take slowly.

## 1. Before you touch the server

- A domain, with an A record pointing at the server. `app.example.com` below.
- An SMTP account that can send mail. Without one, nobody can reset a
  password and invitations go nowhere — the app refuses to start rather than
  pretend otherwise.
- Somewhere off this server to keep backups. A second machine, object storage,
  anything that is not the disk the database is on.

## 2. The server

```
sudo apt update && sudo apt install -y podman podman-compose nginx certbot python3-certbot-nginx
git clone <your remote> /opt/retail && cd /opt/retail
cp .env.example .env
```

Docker works just as well, and on a machine that already runs Docker use
that rather than installing a second container engine: read `docker compose`
for `podman compose` throughout, and drop the `podman unshare` in step 3.

**On a server that already hosts other things**, check first that port 8000
is free (`ss -tlnp | grep 8000`), that there is swap (`swapon --show` --
without it a memory spike kills a process rather than slowing one down), and
set the two sizing values below. The default of three web workers and two
Celery processes assumes a machine of its own.

Fill in `.env`. These are the ones that matter:

```
DJANGO_SETTINGS_MODULE=config.settings.prod
SECRET_KEY=            # 50+ random characters; the app will not start without
ALLOWED_HOSTS=app.example.com
EMAIL_HOST=smtp.yourprovider.com
POSTGRES_PASSWORD=     # compose creates the database with this
APP_DB_PASSWORD=       # the password the application itself connects with
```

On a shared or small server, add:

```
WEB_WORKERS=2          # default 3
CELERY_CONCURRENCY=1   # default 2
```

That is about 600 MB for the whole stack instead of a gigabyte.

Without an SMTP account the app refuses to start, because a password reset
that goes nowhere is worse than one that fails. To run without email on
purpose, leave `EMAIL_HOST` empty and set `EMAIL_OFF=true`: nothing is sent,
the forgot-password page says so, and an owner resets a staff password under
Settings > Staff.

Comment out or delete the development `DATABASE_URL` and `REDIS_URL` lines.
Compose sets both for the containers: inside its network the database is
`db`, not `127.0.0.1`, and the application connects as `retail_app` — a role
with no superuser rights, so row-level security binds it too. Both passwords
should be random and different.

Generate them the same way:

```
python3 -c "import secrets,string; print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(64)))"
```

Letters and digits only for `APP_DB_PASSWORD` — it goes into a connection
URL, where `@`, `:`, `/` and `#` mean something else.

`APP_DB_PASSWORD` is read when the database is **first created** and never
again. Changing it later means changing it in the database as well
(`ALTER ROLE retail_app PASSWORD '...'`).

`ALLOWED_HOSTS` must be the real domain. `localhost`, `127.0.0.1` and `*` are
refused on purpose: a wildcard means the site answers to any name pointed at
it.

## 3. Start it

Product photos are written by the application and read by nginx, so the
directory has to belong to the user inside the container (uid 10001) and be
readable from outside it:

```
mkdir -p /opt/retail/media
podman unshare chown -R 10001:10001 /opt/retail/media   # docker, or podman as root: no `unshare`
```

Then:

```
podman compose up -d --build
podman compose exec web python manage.py migrate
podman compose exec web python manage.py sync_permissions
podman compose exec web python manage.py seed_plans
podman compose exec web python manage.py createsuperuser
```

Do **not** run `seed_demo` here. It builds a shop full of pretend data.

Check it is alive before going further:

```
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: app.example.com' \
     -H 'X-Forwarded-Proto: https' http://127.0.0.1:8000/healthz     # 200
```

## 3b. Or the ordinary way, without containers

On a machine that already runs other Django sites, installing this one the
same way they are installed is usually less trouble than adding a container
engine. `ops/systemd/` has the three unit files and
`ops/nginx-site-example.conf` the site, both taken from a real install.

```
# Python 3.11 or newer: the permission catalogue uses StrEnum.
git clone <your remote> /var/www/retail && cd /var/www/retail
python3.12 -m venv venv && ./venv/bin/pip install -r requirements.txt

# The database, as a role that is NOT a superuser -- row-level security is
# bypassed entirely by one, and tenant isolation would rest on the
# application alone.
sudo -u postgres psql -c "CREATE ROLE retail_app LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS"
sudo -u postgres psql -c "CREATE DATABASE retail OWNER retail_app"
sudo -u postgres psql -d retail -c "ALTER SCHEMA public OWNER TO retail_app"

cp .env.example .env     # DATABASE_URL, REDIS_URL, the keys from step 2
mkdir -p media private staticfiles
./venv/bin/python manage.py migrate
./venv/bin/python manage.py sync_permissions
./venv/bin/python manage.py seed_plans
./venv/bin/python manage.py apply_rls          # the second lock
./venv/bin/python manage.py collectstatic --noinput
chown -R www-data:www-data media private

cp ops/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now retail retail-worker retail-beat
```

**If the machine shares its Redis with other projects**, give this one a
database of its own: `redis-cli info keyspace` shows which are taken, then
`REDIS_URL=redis://127.0.0.1:6379/3` and a different one for Celery.

gunicorn listens on a unix socket, so there is no port to reserve and
nothing of this is reachable from outside nginx.

## 4. TLS and nginx

```
sudo cp ops/nginx.conf /etc/nginx/sites-available/retail
sudo sed -i 's/app.example.com/your.real.domain/g' /etc/nginx/sites-available/retail
sudo nginx -t          # before enabling it, not after
sudo ln -s /etc/nginx/sites-available/retail /etc/nginx/sites-enabled/
sudo certbot --nginx -d your.real.domain
sudo nginx -t && sudo systemctl reload nginx
```

nginx serves the product photos out of `/opt/retail/media` — change that
alias if you cloned somewhere else — and proxies everything else. It does
**not** serve `/static/`: the built CSS and JavaScript live inside the image, where
this machine cannot see them, and a copy on the host goes stale the first
time the image is rebuilt. The application serves them itself, with hashed
filenames and a year-long cache header.

The app sets secure-only cookies and redirects to HTTPS, so **it cannot be
used over plain HTTP at all**. If sign-in fails with "CSRF verification
failed", the proxy is not passing `X-Forwarded-Proto: https` — that is the
first thing to check, not the application.

## 5. Backups, the same day

Not next week. A retail system holds the only record of what a shop sold.

The database is inside a container with no published port, so the backup
runs through compose. That means **the crontab of the user who runs the
containers**, not root's — `crontab -e`, not `sudo crontab -e`:

```
crontab -e
0 2 * * * cd /opt/retail && BACKUP_REMOTE=you@backup-host:/backups BACKUP_DIR=$HOME/backups ops/backup.sh >> $HOME/retail-backup.log 2>&1
```

Installed without containers, the database is on the host and the dump has
to be taken by the superuser -- the application's own role is bound by
row-level security and `pg_dump` refuses rather than handing back a file
with no rows in it:

```
0 2 * * * cd /var/www/retail && PG_SUDO_USER=postgres \
    DATABASE_URL='postgresql:///retail?host=/var/run/postgresql&port=5433' \
    ops/backup.sh >> /var/log/retail-backup.log 2>&1
```

It dumps, refuses to keep a dump it cannot read back, refuses to keep one
that is suspiciously small, copies it off the machine and keeps 30 days.

Then **restore it somewhere else and open the result**. A backup you have
never restored is a rumour. `ops/restore.sh` does the restore.

## 6. Before a real shop uses it

- Sign in as the shop owner, add one product, sell it, refund it, and close
  the till. Ten minutes, and it catches a broken deployment better than any
  check here.
- Plug in the actual barcode reader the shop will use. Most send Enter, some
  send Tab; both work, but confirm it with the model in the shop, not a
  different one.
- Print a receipt on the actual printer.
- **Fiscal receipts are queued, not filed.** Sales are recorded and queued for
  the revenue authority, and the platform health page shows the backlog — but
  no provider is connected, so nothing is submitted to TRA. A shop that must
  issue fiscal receipts cannot rely on this yet. See `apps/pos/tasks.py`.

## 7. Watch these in the first week

| Where | What you are looking for |
|---|---|
| `/platform/health/` | the fiscal backlog, failed jobs, shops past due |
| `podman compose logs -f web` | 500s, especially around a cash-up |
| Cash-ups in each shop | shifts that are short; a pattern is a person, not a bug |
| `/var/log/retail-backup.log` | last night's dump ran and copied off |

## Rolling back

The image is tagged per deploy. If a release misbehaves:

```
podman compose down
git checkout <previous tag>
podman compose up -d --build
```

Migrations are the exception: if one has run, restore the database from the
dump taken before the deploy rather than trying to reverse it.
