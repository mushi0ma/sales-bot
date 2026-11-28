#!/bin/sh
# First start of platform-postgres only (docker-entrypoint-initdb.d): one database and one role per tool.
set -eu
for app in langfuse twenty hatchet; do
  var="$(echo "$app" | tr a-z A-Z)_DB_PASSWORD"
  eval "pw=\${$var:?$var is empty}"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    -v app="$app" -v pw="$pw" <<'SQL'
CREATE ROLE :"app" LOGIN PASSWORD :'pw';
CREATE DATABASE :"app" OWNER :"app";
SQL
done
