#!/bin/bash
set -e
cd "$(dirname "$0")"

echo "Checking for updates..."
git fetch

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse @{u})

FORCE=false
if [ "$1" = "-f" ] || [ "$1" = "--force" ]; then
    FORCE=true
fi

if [ "$LOCAL" != "$REMOTE" ] || [ "$FORCE" = true ]; then
    if [ "$FORCE" = true ]; then
        echo "🔄 Force update requested."
    else
        echo "🆕 New changes detected. Updating..."
        git pull
    fi
    docker compose up -d --build
    docker image prune -f
    echo "✅ Operation completed."
else
    echo "✅ Already up to date. Use -f to force rebuild."
fi
