-- Database roles.
--
-- Row-level security is bypassed entirely by superusers and, unless FORCE is
-- set, by a table's owner. So the application must never connect as either a
-- superuser or as a role with BYPASSRLS, or the second lock is decorative.
--
-- retail_app  owns the schema and runs migrations. FORCE ROW LEVEL SECURITY
--             (set by apply_rls) means the policies bind it too.
-- CREATEDB    is only so Django can build its test database.
--
-- The password comes from APP_DB_PASSWORD, which compose passes to the
-- database container and uses to build the application's DATABASE_URL, so
-- the two cannot drift apart. Run by hand for development it falls back to
-- `retail_app`, which is why production refuses to start on that password
-- (config/settings/guards.py).

\getenv app_password APP_DB_PASSWORD
\if :{?app_password}
\else
  \set app_password 'retail_app'
\endif

CREATE ROLE retail_app WITH LOGIN PASSWORD :'app_password' CREATEDB NOSUPERUSER NOBYPASSRLS;

-- Since Postgres 15 the public schema belongs to the role that created the
-- database and nobody else may create in it. In development retail_app makes
-- its own database and owns it; in the container the database is created by
-- the superuser before this runs, so without the two statements below
-- `manage.py migrate` fails on the very first table of a fresh install.
--
-- Run by hand this file may be pointed at the maintenance database, whose
-- public schema should be left alone; hence the guard.
DO $$
BEGIN
    IF current_database() <> 'postgres' THEN
        EXECUTE 'ALTER SCHEMA public OWNER TO retail_app';
        EXECUTE 'GRANT ALL ON SCHEMA public TO retail_app';
        EXECUTE format('ALTER DATABASE %I OWNER TO retail_app', current_database());
    END IF;
END
$$;
