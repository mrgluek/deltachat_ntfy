# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.6.0] - 2026-10-28

### Changed
- **Delta Chat Core 2.66.0**: `deltachat-rpc-server` is pinned to 2.66.0 (was 2.65.0). Core 2.66 removes the `configured_addr` config: `get_config("configured_addr")` now fails with "unknown key", and `addr` has been empty since core 2.61. The bot's own address now comes from its self contact (`dc_fingerprint.get_self_addr()`), which the core fills from the primary relay; this also works on older cores.
- **Account List Shows Addresses Again**: The account overview printed `None` as the email since core 2.61, because it read `addr`.

## [1.5.2] - 2026-10-28

### Security
- **Admin Check No Longer Fooled by a Display Name**: The contact's key fingerprint was found by searching the whole `get_contact_encryption_info()` text for hex strings, including the line with the contact's display name, so a contact named `x<admin fingerprint>x` passed the admin check. The fingerprint is now read only from the lines under each key header, and the bot's own key is recognised by the fingerprint in its invite link instead of by its address. When the keys cannot be told apart the check fails instead of guessing. The code is in `dc_fingerprint.py`, shared verbatim between the bots.

### Changed
- **Ready for the Core 2.66 Fingerprint Format**: Core 2.66 prints `Me:` instead of `Me (address):` and sorts the key blocks by fingerprint, so finding a key block by address stops working. The new parser handles both formats.
- **Core Pinned to 2.65.0**: `deltachat-rpc-server` was `>=2.62.0`, so any image rebuild pulled core 2.66.0, which removes the `configured_addr` option the bot still reads. Core upgrades are now made on purpose, after reading the core changelog.

## [1.5.1] - 2026-10-26

### Changed
- **Download Limit 64 KB Instead of 1 Byte**: The intent was to skip attachments, but with a limit of 1 byte the Delta Chat core treats every incoming message, plain text too, as too large and fetches it later one by one instead of in the normal batch, which delayed replies to commands. With 64 KB commands arrive at once, while photos and files sent to the bot (over 64 KB) are still not downloaded.

## [1.5.0] - 2026-10-24

### Added
- **The bot creates its own account.** `serve` without a configured account (a new install) lets the Delta Chat core (2.62+) create one: it probes its built-in chatmail relay list, configures the fastest relay and later adds further relays in the background. The new address is logged. If that fails, the error is logged and the bot exits, so the container restart retries. `bot.py init <addr> <password>` and `bot.py init DCACCOUNT:server.tld` still choose the account explicitly; `bot.py init transport ...` is unchanged. The README's account setup step is now optional.

### Changed
- **The server runs releases, not the tip of `main`.** `update.sh` deploys the newest `vX.Y.Z` tag, so a merge into `main` reaches the server only once it is released; several PRs can go out together. `UPDATE_REF` in `.env.local` pins a release (`UPDATE_REF=v1.4.0` - a rollback; empty again = newest) or follows a branch as before (`UPDATE_REF=main`). A release tag moved on GitHub or Forgejo makes the update fail instead of deploying the changed code. Errors that stopped the script with a message (both remotes unreachable, an unknown `UPDATE_REF`) now also ping the healthcheck's `/fail`, not only failed commands. The script now runs as one function, so the `git reset` that rewrites `update.sh` cannot confuse the running copy.
- **Releases are published by a tag** (new `.github/workflows/release.yml`): pushing `vX.Y.Z` checks that `bot.py` has that version, creates the GitHub release with the version's section of this file as its notes (an existing release is left alone), then sends the ntfy notification and the `bot-updated` dispatch to `hass-addons-deltachat` (now with `"version"` in the payload). Both used to go out on every push to `main` (`ntfy-changelog.yml`, removed), so Home Assistant users got unreleased code.
- Releasing: merge, then `git tag -a vX.Y.Z -m "..." <commit> && git push origin vX.Y.Z`.
- **Upgrading:** tag `v1.5.0` right after merging this. Until then the new `update.sh` would deploy `v1.4.0`, which still has the old script that follows `main` - and the server would switch back and forth on every run.

## [1.4.0] - 2026-10-18
- **Paced sending below the relay's limit**: chatmail relays allow each sender 60 messages a minute with a burst of 10 and answer `450 Too much mail` above it; the core retries, the retries count too, and everything after a burst arrives minutes late. A notification goes to every subscribed chat (up to 5 per topic today), so a publisher posting several at once could get there. All sends - notifications, daily stats, command replies - now go through one queue (`outbox.py`, the same module as in Uptime Bot): 50 a minute with bursts of 5, in one sending thread.
  - **Priorities**: ntfy priority 4-5 notifications first, then command replies, then priority 3 (default), then 1-2 and the daily stats. Within a priority in arrival order, so a topic's notifications keep their order in a chat. Notifications are never dropped or merged.
  - **Attachments**: the temp file of an uploaded or downloaded attachment is deleted after its last queued send (the core copies it when it sends), not right after the HTTP request. A temp file of a notification without subscribers is now deleted too (it was left behind).
  - The HTTP request returns as soon as the notification is queued. A backlog of 20+ messages is logged once a minute with an estimate of how long it takes to send.

## [1.3.2] - 2026-10-14
- **Shorter `/newgroup` Reply**: dropped the "Share this link or scan the QR code…" line; the reply is now just the confirmation and the join link.
- **MIT License**: the project is now licensed under MIT (`LICENSE`).

## [1.3.1] - 2026-10-14
- **Fixed HTTP 500 on Non-UTF-8 Notifications**: Posts whose body was not valid UTF-8 (e.g. Windows PowerShell 5.1 `Invoke-RestMethod` without a charset, which sends Windows-1251) crashed `request.text()` with `UnicodeDecodeError`, so the notification was lost. The body is now read as bytes and decoded with the `Content-Type` charset if given, else UTF-8, else Windows-1251, else UTF-8 with replacement characters, so a notification is never rejected because of its encoding. JSON bodies are decoded the same way, and Topic/Title/Tags/Filename headers with non-UTF-8 bytes are re-decoded too.

## [1.3.0] - 2026-10-14
- **`/transports` Shows the Relay Actually Used for Sending**: Since core 2.61 the core sends through the relay that last worked and switches only when it cannot connect, so the main address (`configured_addr`) says nothing about where mail goes out. `/transports` now lists relays in the order the core tries them, with the current sending relay first, shows the core's send queue (waiting messages and retries) and outgoing status, and per relay the incoming status, when the last message arrived, mailbox storage usage (⚠️ from 80%) and when it was added. The sending order and queue are read from the core's account database (read-only); the report is built by the shared `transports_view.py`.
- **Removed Misleading Per-Relay Sent/Received Counters**: They credited every message to `configured_addr` (or, in some places, the wrong address), so the numbers and "last sent" dates were wrong once the core sent through another relay. Counting them also cost extra RPC calls per message. The `transport_stats` table is left in place but no longer updated.
- **Clearer Log Line**: "Successfully sent msg_id …" is now "Queued msg_id … for sending": at that point the message is only handed to the core's send queue.

## [1.2.0] - 2026-09-28

### Removed
- **Bot-Side Relay Failover**: Removed the `MSG_FAILED` failover handler and the resilient-send `send_msg` patch. Both switched `configured_addr` and called `resend_messages`, but since Delta Chat core 2.61 the SMTP relay is no longer chosen by `configured_addr`: the core tries the newest relay first and falls back to the next one if a relay is unreachable. The old logic could no longer switch relays and only caused delayed duplicate resends.

### Changed
- **`/setprimary` and `/resilient` Deprecated**: Both commands are disabled and only reply with an explanation of how the core picks relays now. They are removed from `/help` and the README.
- **`/transports` Sending Order**: Relays are listed newest first, in the order the core tries them for sending, instead of marking `configured_addr` as "Used for sending".
- **Core Requirement**: `requirements.txt` now requires `deltachat-rpc-server>=2.62.0`.

## [1.1.5] - 2026-09-24

### Fixed
- **`update.sh` Deployed the Wrong Branch**: Branch detection took the first remote branch in alphabetical order, so a leftover PR branch such as `origin/claude/...` sorted before `origin/master` and was deployed instead (and new `master` commits were reported as "Already up to date"). It now follows the checked-out branch, falling back to `main`/`master`.

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
