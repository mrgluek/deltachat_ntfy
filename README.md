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
- `/transports` to show mail relays: relay used for sending, send queue, incoming status and storage (admin only)
- `/addtransport` to add a backup mail relay (admin only)
- `/rmtransport <addr>` to remove a mail relay (admin only)
- `/help` to see all available commands

Relay selection and failover are handled by the Delta Chat core (2.61+): it keeps sending through the relay that last worked and moves to the next one only if it cannot connect, so the relay used for sending can differ from the account's main address. `/transports` shows which relay that is, the core's send queue, and per relay the incoming status, the last received message and mailbox storage. The former `/setprimary` and `/resilient` commands are deprecated and only reply with this explanation.

### Target-Specific Commands in Group Chats

In group chats where multiple bots are present, you can address this bot specifically to prevent other bots from responding. Append the `@ntfy` suffix to any command, for example:
- `/help@ntfy`
- `/stats@ntfy`

A plain `/help` sent in a group chat is answered in a private 1:1 chat with the sender, so several bots don't flood the group with help texts. Use `/help@ntfy` to show the help in the group itself.

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

- **Multiple Mail Relays**: Supports multiple mail servers. Relay selection and failover are handled by the Delta Chat core (2.61+), which sends via the newest relay first and falls back to the next one if a relay is unreachable.
- **Header Aliases**: Use `t`/`ti` for Title, `p`/`prio` for Priority, `ta`/`tag` for Tags.
- **Tags & Emojis 🥳**: Add emojis to your messages using the `Tags` header (e.g., `Tags: warning,skull`). Non-emoji tags are appended to the message text.
- **Click Actions 🔗**: Use the `Click` header to add a clickable link to your notification.
- **Attachments 📎**:
  - **External URL**: Use `Attach: http://...` to have the bot download and send a file to the chat.
  - **Direct Upload**: Use `curl -T file.jpg -H 'Filename: file.jpg'` to upload a file directly as the message body.
- **Any Text Encoding 🔤**: Message bodies (plain text and JSON) and the Title/Tags headers are accepted in any encoding. The bot uses the `charset` from `Content-Type` if given, otherwise tries UTF-8 and then Windows-1251 (what Windows PowerShell 5.1 `Invoke-RestMethod` sends without a charset); undecodable bytes become `�` instead of failing the request.
- **Paced Sending (relay send limit) 🚦**: chatmail relays accept 60 messages a minute per sender (burst 10) and answer `450 Too much mail` above that, after which everything is late for minutes. A notification goes to every chat subscribed to its topic, so bursts multiply. All messages go through one queue (`outbox.py`): at most 50 a minute with bursts of 5, sent by priority - notifications with `Priority` 4-5 first, then command replies, then priority 3 (default), then 1-2 and the daily stats; same priority in arrival order, so a topic's notifications keep their order. Nothing is dropped. The HTTP request returns as soon as the notification is queued; attachments are kept until their last send. A backlog of 20+ messages is logged once a minute.
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

### Account Setup (Optional)

Skip this step and the bot creates its account by itself at first start: the Delta Chat core (2.62+) picks the fastest chatmail relay from its built-in list and later adds further relays in the background. The new address is logged (`docker compose logs ntfy_bot`). If no relay answers, the bot exits and the container restart tries again.

To choose the account yourself, initialize it before the first start with your email and password, or with `DCACCOUNT:server.tld` for an account on a specific chatmail relay:

```bash
docker compose run --rm ntfy_bot python bot.py init bot@example.com "YOUR_PASSWORD"
docker compose run --rm ntfy_bot python bot.py init DCACCOUNT:nine.testrun.org
```

Configure the admin on the server (or do it later inside the bot chat with the `/initadmin` command):

```bash
docker compose run --rm ntfy_bot python set_admin.py --email admin@example.com
docker compose run --rm ntfy_bot python set_admin.py --fingerprint 1234ABCD1234ABCD1234ABCD1234ABCD1234ABCD
```

Then start the bot normally with `docker compose up -d`.

**Backup Relays (Multi-transport)**
You can add secondary mail servers to ensure the bot remains reachable if your primary server goes down:

```bash
# Add a backup chatmail via URI
docker compose run --rm ntfy_bot python bot.py init transport DCACCOUNT:bot@arcanechat.me

# Add a backup via email credentials
docker compose run --rm ntfy_bot python bot.py init transport backup@example.com "PASSWORD"
```

You can also manage transports directly in Delta Chat using `/addtransport` and `/transports`.

### Environment Variables

| Variable | Description | Default |
| --- | --- | --- |
| `AUTH_TOKEN` | Optional bearer token / query param token for securing the HTTP notification endpoint. | *(empty)* |
| `DISPLAY_NAME` | Custom display name for the Delta Chat bot profile. | `Ntfy Bot` |
| `STATUS_TEXT` | Custom bio/status description for the Delta Chat bot profile. | `A Delta Chat bot that emulates a ntfy.sh backend...` |
| `PORT` | Web server port for HTTP API and web dashboard. | `8080` |
| `DB_PATH` | Path to SQLite database file. | `ntfy.db` |

## Running the Bot

### Using Docker (Recommended)

1. Clone the repository
2. Optional: choose the account and admin as described in the **Account Setup (Optional)** section above. Without it, the bot creates its own chatmail account at first start.
3. Start the bot: `docker compose up -d`
4. Message the bot in Delta Chat and use `/initadmin` to claim ownership.

### Updating and Releases

Run `./update.sh` (e.g. from cron) to deploy the newest release: the highest `vX.Y.Z` tag, from GitHub or, when GitHub is unreachable, from the Forgejo mirror; `-f` rebuilds the container without changes. Commits on `main` that are not released yet are not deployed. In `.env.local`, `UPDATE_REF=v1.4.0` stays on that release (to roll back; remove it to follow new releases again), `UPDATE_REF=main` follows the branch, and `MONITOR_URL` pings a healthcheck on start, success and failure.

Releasing: merge, then `git tag -a vX.Y.Z -m "..." <commit> && git push origin vX.Y.Z`. Pushing the tag (`.github/workflows/release.yml`) checks that `VERSION` in `bot.py` matches, publishes the GitHub release with that version's `CHANGELOG.md` section, and sends the ntfy notification and the Home Assistant add-on sync.

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

## Running Tests

Run the unit test suite:

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT, see [LICENSE](LICENSE).
