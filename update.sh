#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "Checking for updates..."
git fetch

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse @{u})

if [ "$LOCAL" != "$REMOTE" ]; then
    echo "🆕 New changes detected. Updating..."
    git pull
    docker-compose down
    docker-compose up -d --build
    docker image prune -f
    echo "✅ Updated, restarted, and cleaned up old images."
else
    echo "✅ Already up to date. No rebuild needed."
fi
