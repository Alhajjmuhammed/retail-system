# Putting this on a server

Written for whoever puts the system online — you, or somebody you hire. It
assumes one small server (2 vCPU, 4 GB is plenty for a handful of shops), a
domain you control, and nothing else.

Everything here has been run except the parts that need a real domain: the
image builds, boots, serves pages and refuses to start when it is configured
wrongly. What has never been done is TLS on a public name, so that is the step
to take slowly.

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

Fill in `.env`. The four that matter most:

```
DJANGO_SETTINGS_MODULE=config.settings.prod
SECRET_KEY=            # 50+ random characters; the app will not start without
ALLOWED_HOSTS=app.example.com
EMAIL_HOST=smtp.yourprovider.com
POSTGRES_PASSWORD=     # compose creates the database with this
```

Generate the key with:

```
python3 -c "import secrets,string; print(''.join(secrets.choice(string.ascii_letters+string.digits) for _ in range(64)))"
```

`ALLOWED_HOSTS` must be the real domain. `localhost`, `127.0.0.1` and `*` are
refused on purpose: a wildcard means the site answers to any name pointed at
it.

## 3. Start it

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

## 4. TLS and nginx

```
sudo cp ops/nginx.conf /etc/nginx/sites-available/retail
sudo sed -i 's/app.example.com/your.real.domain/g' /etc/nginx/sites-available/retail
sudo ln -s /etc/nginx/sites-available/retail /etc/nginx/sites-enabled/
sudo certbot --nginx -d your.real.domain
sudo nginx -t && sudo systemctl reload nginx
```

The app sets secure-only cookies and redirects to HTTPS, so **it cannot be
used over plain HTTP at all**. If sign-in fails with "CSRF verification
failed", the proxy is not passing `X-Forwarded-Proto: https` — that is the
first thing to check, not the application.

## 5. Backups, the same day

Not next week. A retail system holds the only record of what a shop sold.

```
sudo crontab -e
0 2 * * * cd /opt/retail && BACKUP_REMOTE=you@backup-host:/backups ops/backup.sh >> /var/log/retail-backup.log 2>&1
```

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
