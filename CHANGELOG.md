# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.4] - 2026-09-24

### Changed
- **Private `/help` in Groups**: A plain `/help` sent in a group chat is now answered in a private 1:1 chat with the sender instead of the group, so several bots don't flood it with help texts (the reply ends with a note on how to show it in the group). Addressed `/help@ntfy` is still answered in the group. Previously a plain `/help` was answered in the group, or silently ignored when other bots were present.

## [1.1.3] - 2026-09-16

### Security
- **SSRF Defense (`Attach` URL validation)**: Added rigorous IP and DNS validation via `is_safe_url()` before fetching external attachments. Blocks local hostnames (`localhost`, `*.local`, `*.internal`, `*.lan`), loopback (`127.0.0.0/8`, `::1`), private ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`), link-local/cloud metadata (`169.254.169.254`), multicast, and DNS rebinding to private IPs.
- **Unbounded Attachment & Body Protection**: Enforced `client_max_size = 15MB` on `web.Application` and streamed external attachment downloads in chunks with an explicit 15MB cap to prevent memory exhaustion (DoS).
- **Per-IP Rate Limiting**: Added thread-safe sliding-window rate limiting across all public endpoints (`/`, `/{topic}`, `/{topic}/json`, and incoming notification POSTs) with `Retry-After: 60` headers on HTTP 429.
- **Dependency Hardening**: Pinned `aiohttp>=3.10.5,<4.0.0`, `qrcode>=7.4.2,<8.0.0`, and `emoji>=2.12.0,<3.0.0` in `requirements.txt`.

## [1.1.2] - 2026-09-09

### Added
- **Private Chat Enforcement**: Enforce private 1:1 chat for `/addtransport` and `/initadmin` to prevent credential exposure in group chats.
- **Bot Ownership Claiming (`/initadmin`)**: Added `/initadmin` command handler to allow the bot owner to claim admin ownership in private chat with cryptographic fingerprint binding.
- **Automated Data Retention & Pruning**: Periodic background cleanup task pruning notification history and flushing transport stats.
- **Comprehensive Unit Test Suite**: Added `tests/test_database.py` and `tests/test_transport_commands.py` with mock fallbacks for headless CI/offline environments.

### Fixed
- **Resilient Send Concurrency**: Protected initial transport send with `resilient_lock` and guaranteed `try...finally` restoration of `configured_addr` in background resend workers.
- **Transport Command Error Sanitization**: Sanitized error output in `/transports`, `/addtransport`, `/rmtransport`, `/setprimary`, and `/resilient`.

### Changed
- **SQLite Performance Optimization**: Enabled WAL mode (`journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`) and in-memory buffered transport statistics.

## [1.1.1] - 2026-09-07

### Added
- **Configurable Display Name & Status Text**:
  - `on_init` now checks `DISPLAY_NAME` and `STATUS_TEXT` environment variables with `/data/options.json` fallback instead of overwriting display name with static strings.

## [2026-07-06]

### Fixed
- **Fix Dependency Conflict/NameError:** Pinned `deltabot-cli==8.1.2` and `deltachat2[full]<1.0.0` in `requirements.txt` to resolve dependency conflicts and avoid the `ChatType` NameError/ImportError bugs introduced in newer, incompatible versions of `deltachat2`.

## [2026-07-03]

### Fixed
- **Zombie Process Reaping:** Enabled `init: true` in Docker Compose to automatically reap zombie processes in the bot container, preventing PID limit exhaustion.

## [2026-06-29]

### Added
- **Main Page and Static Assets Caching**:
  - Added in-memory HTML caching for the bot's home page, avoiding redundant CPU/IO operations (like database config lookups, account status checks, and QR code generation) on every visit.
  - Configured long-term `Cache-Control` headers (`public, max-age=31536000, immutable`) for static assets (icons, favicons, web app manifest) to allow browser-side caching.

### Fixed
- **Admin Verification & /url Command Improvement**:
  - Migrated `/url` and `/rmaccount` admin checks to use the unified, fingerprint-supported `_is_dc_admin` helper.
  - Added support for running `/url` without arguments to query and display the current configured bot URL.

## [2026-06-25]

### Changed
- **Bidirectional Suffix Matching:** Suffix matching is now bidirectional (e.g. `@ntfy` will match NTFY bot, even with partial entries).
- **Smart Group Chat Command Filtering:** The bot now automatically ignores unaddressed general `/help` and `/stats` commands in group chats if other bots are present in the chat.

### Added
- **Target-Specific Command Suffixes:** Added support for addressing this bot specifically in group chats using the `/command@ntfy` suffix.

## [2026-06-16]

### Added
- **Automatic Transport Failover:** Implemented a robust, event-driven transport failover mechanism. The bot now listens to the core's `MSG_FAILED` event. When a message fails to deliver, it automatically switches `configured_addr` to the next configured backup transport, and schedules a resend of the message using exponential backoff (5s, 10s, 20s, 40s...) via an asynchronous timer thread. The failover process is limited to a maximum of 10 attempts per message to prevent infinite loops, and the administrator is alerted only on the first failure.

### Fixed
- **E2E Failover Loop & Key Fallback**:
  - Added fallback support for both `chat_id` and `chatId` keys in message snapshots to prevent `chat 'Unknown' (ID: None)` errors.
  - Downgraded permanent E2E and resend logs to `WARNING`.
  - Removed administrative failover alert messages completely, relying entirely on structured logging to prevent any potential loop risks.


## [2026-06-14]

### Added
- **Topic Web UI Message Publishing**: Added a beautiful, interactive, and collapsible form directly on the topic view page (e.g. `/{topic}`) to publish notifications directly from the browser. Supports message content, custom active priority states, title, tags, click URLs, and server token authorization with secure `localStorage` persistence.
- **Real-time Updates on Topic Pages**: Integrated NDJSON stream subscriber client on the topic view page. New notifications now slide down and fade in automatically in real-time without requiring a page refresh. Includes connection drop recovery and auto-reconnection.

### Fixed
- **Topic stream prefix mismatch**: Fixed an incorrect dollar sign prefix in the browser frontend fetch request URL for live updates, ensuring the stream listens to the correct topic (e.g., `/chat-ru/json` instead of `/$chat-ru/json`) and receives new notifications immediately.

## [2026-06-05]

### Added
- **DPI Bypass Hack**: Integrated a patched `deltachat-rpc-server` binary into the Docker setup to bypass SSL DPI connection blocks when communicating with chatmail.
- **Resilient Sending Mode**: Added `/resilient` admin command to configure resilient mode (accepts `on`/`off`/`1`/`0`/`true`/`false`, or no arguments to query current status). When enabled, each outgoing message is sent through all configured mail relays using resending mechanism in a non-blocking background thread to bypass chatmail blocking issues without causing UI delays, while ensuring deduplication into a single message bubble on the recipient client.

## [2026-06-02]

### Fixed
- **UnicodeEncodeError with surrogate escape headers**: Implemented a robust sanitization utility to decode and strip invalid surrogate characters from incoming HTTP headers (such as `X-Title` or `Title`) sent by non-UTF-8 clients (like Windows PowerShell 5.1). Added auto-detection and correct decoding for Russian `CP1251` and Western `CP1252` encoding pages to safely preserve non-English titles.

## [2026-05-22]

### Changed
- Standardized the welcome greeting to return the exact same detailed output as the `/help` command instead of a custom welcome prefix message.

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
