# Delta Chat Ntfy Bot

A bot for Delta Chat that emulates the backend of [ntfy.sh](https://ntfy.sh) and broadcasts HTTP POST requests to subscribed Delta Chat users/groups.

It provides a modern web interface for each topic (e.g., `https://ntfy.gluek.info/mytopic`) displaying historical notifications, streaming new notifications in real-time, and allowing users to publish notifications directly from their browser. The home page and static assets are cached for optimal performance.

## Usage

Start the bot. Add it to Delta Chat. Message the bot:

- `/initadmin` to claim bot ownership
- `/url https://ntfy.gluek.info` to set the bot's public URL (admin only)
- `/sub mytopic` to subscribe to the topic "mytopic"
- `/unsub mytopic` to unsubscribe
- `/list` to list your subscribed topics
- `/last` to see the last 5 notifications from your subscribed topics
- `/stats` to see the number of notifications received in the last 24 hours
- `/transports` to show configured mail relays and statistics (admin only)
- `/addtransport` to add a backup mail relay (admin only)
- `/rmtransport <addr>` to remove a mail relay (admin only)
- `/setprimary <addr>` to switch the primary mail relay (admin only)
- `/resilient` to toggle resilient sending mode across all relays (admin only)
- `/help` to see all available commands

### Target-Specific Commands in Group Chats

In group chats where multiple bots are present, you can address this bot specifically to prevent other bots from responding. Append the `@ntfy` suffix to any command, for example:
- `/help@ntfy`
- `/stats@ntfy`

### API Subscription (JSON Stream)

The bot supports the `ntfy` JSON stream API for programmatic subscriptions. You can use this to integrate with automated agents or scripts.

```bash
# Fetch all cached messages and stream new ones
curl -s "https://ntfy.gluek.info/mytopic/json?since=all"

# Only fetch historical messages, then disconnect
curl -s "https://ntfy.gluek.info/mytopic/json?since=all&poll=1"

# Fetch messages from the last 10 minutes (also supports s, h or unix timestamps)
curl -s "https://ntfy.gluek.info/mytopic/json?since=10m"
```

### Sending notifications

1. **Web Interface (Browser)**:
   Navigate to the topic page directly (e.g., `https://ntfy.gluek.info/mytopic`) and expand the **✏️ Send Notification** section. Fill in the message, priority, tags, click URL, and optional server auth token (saved in browser `localStorage`), and click **Send**.

2. **HTTP Request (`curl`)**:
   Use `curl` or any `ntfy` client and point it to your bot's web server URL.

```bash
# Basic message
curl -d 'Backup successful 😀' https://ntfy.gluek.info/mytopic

# With Title and Priority
curl -H 'Title: Backup Status' -H 'Priority: high' -d 'Backup successful 😀' https://ntfy.gluek.info/mytopic
```

The bot supports the following `ntfy` Priority values:

- 5, max, urgent: 🔴
- 4, high: 🟠
- 3, default: 🟢
- 2, low: 🔵
- 1, min: ⚪️

### Advanced Features

- **Automatic Transport Failover**: Supports multiple mail servers. The bot automatically detects message delivery failures via raw core events, switches `configured_addr` to a backup transport in round-robin fashion, and schedules a resend of the message using exponential backoff (5s, 10s, 20s, 40s...) via an asynchronous timer thread (up to a maximum of 10 attempts per message) to prevent loop propagation and CPU spikes.
- **Header Aliases**: Use `t`/`ti` for Title, `p`/`prio` for Priority, `ta`/`tag` for Tags.
- **Tags & Emojis 🥳**: Add emojis to your messages using the `Tags` header (e.g., `Tags: warning,skull`). Non-emoji tags are appended to the message text.
- **Click Actions 🔗**: Use the `Click` header to add a clickable link to your notification.
- **Attachments 📎**:
  - **External URL**: Use `Attach: http://...` to have the bot download and send a file to the chat.
  - **Direct Upload**: Use `curl -T file.jpg -H 'Filename: file.jpg'` to upload a file directly as the message body.
- **Group Support 👥**: Add the bot to any group and use `/sub <topic>` inside the group. The bot will appear to send messages on behalf of the topic (e.g., as `#topic`) using the `override_sender_name` feature.

### Testing Examples

Use your actual bot URL: `https://ntfy.gluek.info`.

```bash
# 1. Basic message with aliases
curl -H 'ti: Alert' -H 'p: 5' -d 'Critical error' https://ntfy.gluek.info/test

# 2. Tags and Emojis
curl -H 'ta: warning,skull,fire' -d 'Something is burning!' https://ntfy.gluek.info/test

# 3. Clickable Link
curl -H 'Click: https://github.com' -d 'Check the repo' https://ntfy.gluek.info/test

# 4. External Attachment (URL)
curl -H 'Attach: https://github.com/fluidicon.png' -d 'New avatar attached' https://ntfy.gluek.info/test

# 5. Direct File Upload
curl -T my_photo.jpg -H 'Filename: photo.jpg' https://ntfy.gluek.info/test

# 6. The 'Ultimate' Test (Combined)
curl -X POST https://ntfy.gluek.info/test \
  -H 'Title: System Status' \
  -H 'Tags: tada,rocket,check' \
  -H 'Click: https://status.example.com' \
  -H 'Attach: https://www.python.org/static/img/python-logo.png' \
  -d 'Everything is working perfectly! Check the attached logo and link.'
```

### Account Setup

Before running the bot, you must initialize at least one Delta Chat account.

**Manual Setup (Custom Email/Password)**
If you want to use a specific email account:

```bash
docker compose run --rm ntfy_bot python bot.py init bot@example.com "YOUR_PASSWORD"
```

**Automatic Setup (Chatmail)**
If you want to use a chatmail server (which doesn't require a pre-existing password for new accounts), choose a desired address at a chatmail domain (e.g., `nine.testrun.org`):

```bash
docker compose run --rm ntfy_bot python bot.py init mybot@nine.testrun.org
```

Once configured, you can start the bot normally with `docker compose up -d`.

**Backup Relays (Multi-transport)**
You can add secondary mail servers to ensure the bot remains reachable if your primary server goes down:

```bash
# Add a backup chatmail via URI
docker compose run --rm ntfy_bot python bot.py init transport DCACCOUNT:bot@arcanechat.me

# Add a backup via email credentials
docker compose run --rm ntfy_bot python bot.py init transport backup@example.com "PASSWORD"
```

You can also manage transports directly in Delta Chat using `/addtransport` and `/transports`.

## Running the Bot

### Using Docker (Recommended)

1. Clone the repository
2. Initialize the account as described in the **Account Setup** section above.
3. Start the bot: `docker compose up -d`
4. Message the bot in Delta Chat and use `/initadmin` to claim ownership.

### Running Manually

1. Create a Python virtual environment: `python3 -m venv venv`
2. Activate it: `source venv/bin/activate`
3. Install requirements: `pip install -r requirements.txt`
4. Set environment variables if needed (e.g., `PORT=8080`, `DB_PATH=ntfy.db`)
5. Run the bot: `python bot.py`

## Reverse Proxy (Caddy)

For production use with HTTPS, you can use the provided `Caddyfile` example. A `caddy` service is also included in `docker-compose.yml` (commented out by default).

To use it:

1. Edit `Caddyfile` with your domain name.
2. Uncomment the `caddy` service in `docker-compose.yml`.
3. Run `docker compose up -d`.
