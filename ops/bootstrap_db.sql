-- Database roles.
--
-- Row-level security is bypassed entirely by superusers and, unless FORCE is
-- set, by a table's owner. So the application must never connect as either a
-- superuser or as a role with BYPASSRLS, or the second lock is decorative.
--
-- retail_app  owns the schema and runs migrations. FORCE ROW LEVEL SECURITY
--             (set by apply_rls) means the policies bind it too.
-- CREATEDB    is only so Django can build its test database.

CREATE ROLE retail_app WITH LOGIN PASSWORD 'retail_app' CREATEDB NOSUPERUSER NOBYPASSRLS;
