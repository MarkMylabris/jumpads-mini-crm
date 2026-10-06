#!/bin/sh
set -e
# Миграции перед стартом (с повторами, пока поднимается PostgreSQL)
for i in 1 2 3 4 5 6 7 8 9 10; do
    alembic upgrade head && break
    [ "$i" = 10 ] && exit 1
    echo "Migration failed, retrying in 3s ($i/10)..."
    sleep 3
done
exec "$@"
