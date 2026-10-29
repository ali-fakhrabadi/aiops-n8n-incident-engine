#!/usr/bin/env bash
# Runs once, when the PostgreSQL data volume is first initialised.
#
# Least-privilege roles on top of the bootstrap superuser:
#   aiops_owner  owns the aiops database and schema; runs migrations (DDL)
#   aiops_app    API and worker; reads and writes data, cannot change the schema
#   n8n          owns a separate n8n database and cannot connect to aiops
#
# Passwords are read from the mounted secret files and handed to psql through its
# environment (\getenv), never on a command line where `ps` could show them.
set -euo pipefail

read_secret() {
    local file="/run/secrets/$1"
    if [[ ! -s "$file" ]]; then
        echo "missing or empty secret: $file" >&2
        exit 1
    fi
    cat "$file"
}

owner_password="$(read_secret aiops_owner_db_password)"
app_password="$(read_secret aiops_app_db_password)"
n8n_password="$(read_secret n8n_db_password)"
export AIOPS_OWNER_PASSWORD="$owner_password"
export AIOPS_APP_PASSWORD="$app_password"
export N8N_DB_PASSWORD="$n8n_password"

psql --no-psqlrc -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv owner_password AIOPS_OWNER_PASSWORD
\getenv app_password AIOPS_APP_PASSWORD
\getenv n8n_password N8N_DB_PASSWORD
\getenv db POSTGRES_DB

CREATE ROLE aiops_owner WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'owner_password';
CREATE ROLE aiops_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'app_password';

ALTER DATABASE :"db" OWNER TO aiops_owner;
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE :"db" TO aiops_app;

ALTER SCHEMA public OWNER TO aiops_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO aiops_app;

-- Tables and sequences the owner creates later (migrations) are usable by the app role.
ALTER DEFAULT PRIVILEGES FOR ROLE aiops_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aiops_app;
ALTER DEFAULT PRIVILEGES FOR ROLE aiops_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO aiops_app;

-- n8n keeps its workflows, credentials and executions in a database of its own.
CREATE ROLE n8n WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'n8n_password';
CREATE DATABASE n8n OWNER n8n;
REVOKE ALL ON DATABASE n8n FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE n8n TO n8n;
SQL
