# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2026-04-29]

### Added
- **JSON Stream API**: Implemented the `/{topic}/json` API endpoint to fully support the native ntfy agent and other automated scripts.
  - Supports fetching historical messages using the `since` query parameter (accepts Unix timestamps and durations like `10m`, `1h`).
  - Supports `poll=1` to close the connection after fetching history.
  - Supports live streaming of new messages using HTTP long-polling (NDJSON format) with 15-second `keepalive` events.
  - Integrated a robust in-memory Pub/Sub mechanism to broadcast messages to connected HTTP clients in real-time.
- **Bot Statistics**: 
  - Added the `/stats` command in Delta Chat to view the number of notifications received in the last 24 hours.
  - The bot now automatically publishes a daily statistics report (with 📊 tag) to the `stats` topic every day at midnight.

### Changed
- **Database Retention Limits**: The database now automatically purges notifications older than 24 hours on every new incoming message to prevent infinite disk growth, while still keeping a strict maximum limit of 1000 messages per topic.
- **Database Performance**: Added SQLite indexes on `topic` and `created_at` columns in the `notifications` table to ensure database cleanup and querying operations run instantaneously without locking.
