# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2026-05-19]

### Changed
- **Active SMTP Address Resolution**: Fixed database statistics tracking for outgoing messages by querying the overriding active transport (`configured_addr`) first before falling back to the default account address (`addr`).
- **Documented Transport Commands**: Updated documentation to list `/addtransport`, `/rmtransport`, and `/setprimary` in the list of available administration commands in the README.

## [2026-05-02]

### Added
- **Multi-transport Support (Backup Relays)**: Added support for multiple email transports on a single account for high availability.
  - Core automatically fails over to backup relays if the primary server is down.
  - New admin command `/transports` to view configured relays, connectivity status, and usage statistics.
  - New admin commands `/addtransport` and `/rmtransport` to manage relays from the chat.
  - New CLI command `python bot.py init transport` for manual relay setup.
- **Transport Statistics Tracking**: The bot now tracks the number of messages sent and received per transport address.

## [2026-04-29]

### Added
- **JSON Stream API**: Implemented the `/{topic}/json` API endpoint to fully support the native ntfy agent and other automated scripts.
  - Supports fetching historical messages using the `since` query parameter (accepts Unix timestamps and durations like `10m`, `1h`).
  - Supports `poll=1` to close the connection after fetching history.
  - Supports live streaming of new messages using HTTP long-polling (NDJSON format) with 15-second `keepalive` events.
  - Integrated a robust in-memory Pub/Sub mechanism to broadcast messages to connected HTTP clients in real-time.
- **Bot Statistics**: 
  - Added the `/stats` command in Delta Chat to view the number of notifications received in the last 24 hours.
  - Added the `/url` command for administrators to set the bot's public URL, which is now displayed in the `/help` message.
  - The bot now automatically publishes a daily statistics report (with 📊 tag) to the `stats` topic every day at midnight.

### Changed
- **Database Retention Limits**: The database now automatically purges notifications older than 24 hours on every new incoming message to prevent infinite disk growth, while still keeping a strict maximum limit of 1000 messages per topic.
- **Database Performance**: Added SQLite indexes on `topic` and `created_at` columns in the `notifications` table to ensure database cleanup and querying operations run instantaneously without locking.
