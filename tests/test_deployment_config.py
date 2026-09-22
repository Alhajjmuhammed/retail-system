"""
What production refuses to start without, and what it must not forget.

These are the settings that fail on the first day rather than in a test: a
shared secret key, a wildcard host, and the CSRF origin check that turns
every form in the system into "CSRF verification failed" with nothing in the
log to say why.
"""

import pytest
from django.core.exceptions import ImproperlyConfigured

from config.settings.guards import check_database, check_production_config

GOOD_KEY = "x" * 60


def test_a_development_secret_key_stops_the_boot():
    for bad in ("", "change-me", "dev-only-insecure-key-change-me", "short"):
        with pytest.raises(ImproperlyConfigured, match="SECRET_KEY"):
            check_production_config(bad, ["shop.example.com"], "smtp.example.com")


def test_a_wildcard_host_stops_the_boot():
    for hosts in ([], ["*"], ["localhost"], ["127.0.0.1", "0.0.0.0"]):
        with pytest.raises(ImproperlyConfigured, match="ALLOWED_HOSTS"):
            check_production_config(GOOD_KEY, hosts, "smtp.example.com")


def test_no_mail_host_stops_the_boot():
    """Password resets and invitations go by email; without one they vanish."""
    with pytest.raises(ImproperlyConfigured, match="EMAIL_HOST"):
        check_production_config(GOOD_KEY, ["shop.example.com"], "")


def test_a_real_configuration_is_accepted():
    check_production_config(GOOD_KEY, ["shop.example.com"], "smtp.example.com")


def test_the_forms_will_work_behind_a_proxy():
    """
    Django checks a POST's Origin against the host it believes it serves.
    Behind nginx and HTTPS that check fails unless the domains are trusted,
    and it fails for every form at once.
    """
    from config.settings.guards import trusted_origins

    assert trusted_origins(["shop.example.com", "www.shop.example.com"]) == [
        "https://shop.example.com", "https://www.shop.example.com",
    ]
    # A wildcard subdomain host is a host pattern, not an origin.
    assert trusted_origins([".shop.example.com"]) == ["https://shop.example.com"]
    # And nothing local is trusted in production.
    assert trusted_origins(["localhost", "127.0.0.1", "*"]) == []


def test_production_locks_the_doors():
    """Read from the file rather than imported: importing it needs the real
    environment, and these are the lines that must not quietly change."""
    from pathlib import Path

    source = Path("config/settings/prod.py").read_text()
    for setting in ("DEBUG = False", "SESSION_COOKIE_SECURE = True",
                    "CSRF_COOKIE_SECURE = True", "SECURE_SSL_REDIRECT = True",
                    "SECURE_HSTS_SECONDS = 31536000", 'X_FRAME_OPTIONS = "DENY"',
                    "CSRF_TRUSTED_ORIGINS"):
        assert setting in source, setting


def test_the_committed_database_password_stops_the_boot():
    """
    `retail_app:retail_app` is in the README, in .env.example and in
    ops/bootstrap_db.sql. An operator who fills in only the keys the guide
    names would otherwise put every sale the shop has ever made behind a
    password published in this repository.
    """
    for bad in ("", "retail_app", "postgres", "change-me"):
        with pytest.raises(ImproperlyConfigured, match="APP_DB_PASSWORD"):
            check_database({"PASSWORD": bad})

    check_database({"PASSWORD": "a-real-random-password"})


def test_the_database_role_is_not_the_one_that_made_the_database():
    """
    Since Postgres 15 only the database's creator may create in the public
    schema. Compose creates it as the superuser, so unless bootstrap hands
    the schema over, `migrate` fails on the first table of a fresh install.
    """
    from pathlib import Path

    bootstrap = Path("ops/bootstrap_db.sql").read_text()
    assert "ALTER SCHEMA public OWNER TO retail_app" in bootstrap
    assert "NOSUPERUSER NOBYPASSRLS" in bootstrap
    # And the password must come from the environment, not from this file.
    assert "APP_DB_PASSWORD" in bootstrap


def test_the_health_check_asks_the_way_production_answers():
    """
    Production answers only to the domains in ALLOWED_HOSTS and redirects
    anything that did not arrive over https. A health check that sends
    neither header gets a 400 or a 301 -- the first restarts a healthy
    container in a loop, the second passes while the app is dead.
    """
    from pathlib import Path

    for path in ("Dockerfile", "compose.yaml"):
        source = Path(path).read_text()
        check = [line for line in source.splitlines() if "/healthz" in line]
        assert check, path
        joined = "\n".join(check) + source
        assert "ALLOWED_HOSTS" in joined, path
        assert "X-Forwarded-Proto" in joined, path


def test_the_containers_are_told_where_the_database_is():
    """
    .env is also the development file, and points at 127.0.0.1 -- which
    inside a container is the container. Compose has to say `db`.
    """
    from pathlib import Path

    compose = Path("compose.yaml").read_text()
    assert "DATABASE_URL: postgres://retail_app:" in compose
    assert "@db:5432/retail" in compose
    assert "REDIS_URL: redis://redis:6379/0" in compose


def test_the_backup_is_taken_and_given_back_by_the_right_roles():
    """
    Two different roles, for two different reasons. The dump is taken by the
    superuser, because row-level security applies to the application's own
    role and its dump would come back empty. The restore is done as
    retail_app, because --no-owner hands every table to whoever restores it
    -- and a shop that restores as the superuser comes back up to
    "permission denied for table accounts_user" on its first page.
    """
    from pathlib import Path

    backup = Path("ops/backup.sh").read_text()
    restore = Path("ops/restore.sh").read_text()
    assert 'DB_USER="${DB_USER:-retail}"' in backup
    assert 'DB_USER="${DB_USER:-retail_app}"' in restore
    # Neither may assume a client on the host: the database container
    # publishes no port, so there is nothing there to connect to.
    for script in (backup, restore):
        assert "$COMPOSE exec -T" in script


def test_nginx_does_not_serve_files_it_cannot_see():
    """
    The built CSS and JavaScript are inside the image. An alias on the host
    serves 404s until somebody notices the whole site is unstyled, so the
    application serves them and nginx only proxies.
    """
    from pathlib import Path

    conf = Path("ops/nginx.conf").read_text()
    assert "alias /app/staticfiles/" not in conf
    assert "alias /app/media/" not in conf
    assert "location /media/" in conf


def test_running_without_email_has_to_be_said_out_loud():
    """
    Forgetting SMTP must stop the boot -- a password reset that goes nowhere
    is worse than one that fails. Deciding to run without it is allowed, but
    only by saying so.
    """
    with pytest.raises(ImproperlyConfigured, match="EMAIL_OFF"):
        check_production_config(GOOD_KEY, ["shop.example.com"], "")

    check_production_config(GOOD_KEY, ["shop.example.com"], "", email_off=True)


def test_nginx_does_not_answer_for_names_it_was_not_given():
    """
    `server_name _` in the first file nginx loads makes this the default for
    every name pointed at the machine. On a box with somebody else's sites
    on it, that is their traffic.
    """
    from pathlib import Path

    conf = Path("ops/nginx.conf").read_text()
    assert "server_name _;" not in conf


def test_the_machine_decides_how_many_workers():
    """A VPS that also runs a mail server has less room than a spare box."""
    from pathlib import Path

    assert "WEB_WORKERS" in Path("Dockerfile").read_text()
    assert "CELERY_CONCURRENCY" in Path("compose.yaml").read_text()


def test_no_template_carries_script_the_browser_will_refuse():
    """
    The site sends `script-src 'self'`, so an inline <script> block and an
    `onclick=` attribute are both simply not run -- and nothing says so
    except the browser console, which nobody is watching in production.

    This was not theory: the confirm dialog's store, the till's service
    worker, the Print buttons and the web font were all inline, and all
    dead on the live site while every page still returned 200.
    """
    import re
    from pathlib import Path

    inline_block = re.compile(r"<script(?![^>]*\b(src=|type=[\"']application/json))[^>]*>")
    handler = re.compile(r"\son(?:click|submit|change|load|error|input|focus|blur)\s*=\s*[\"']")

    offences = []
    for template in Path("templates").rglob("*.html"):
        text = template.read_text()
        for pattern, what in ((inline_block, "inline <script>"), (handler, "inline handler")):
            for match in pattern.finditer(text):
                line = text[:match.start()].count("\n") + 1
                offences.append(f"{template}:{line} {what}")
    assert not offences, (
        "These do not run under the site's Content-Security-Policy. Move the "
        "code to a file in static/js/ and pass any values as data- attributes:"
        "\n    " + "\n    ".join(offences)
    )


def test_the_policy_allows_the_javascript_this_site_actually_ships():
    """
    Alpine's standard build evaluates every x-show and @click through
    `new Function()`. Without 'unsafe-eval' the whole interface is inert:
    tabs do nothing, the basket never opens, and each page still returns 200.
    """
    from pathlib import Path

    alpine = list(Path("static/vendor").glob("alpine-*.js"))
    assert alpine, "Alpine is no longer shipped; this rule can go"

    for name in ("ops/nginx.conf", "ops/nginx-site-example.conf"):
        conf = Path(name).read_text()
        script_src = [line for line in conf.splitlines() if "script-src" in line]
        assert script_src, f"{name} sends no script-src"
        assert "'unsafe-eval'" in script_src[0], (
            f"{name}: Alpine cannot run under this policy. Either allow "
            "'unsafe-eval' or move to Alpine's CSP build."
        )
