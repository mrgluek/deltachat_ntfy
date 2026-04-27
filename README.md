# Delta Chat Ntfy Bot

A bot for Delta Chat that emulates the backend of [ntfy.sh](https://ntfy.sh) and broadcasts HTTP POST requests to subscribed Delta Chat users/groups.

## Usage

Start the bot. Add it to Delta Chat. Message the bot:
- `/initadmin` to claim bot ownership
- `/sub mytopic` to subscribe to the topic "mytopic"
- `/unsub mytopic` to unsubscribe
- `/list` to list your subscribed topics
- `/last` to see the last 5 notifications from your subscribed topics

### Sending notifications

Use `curl` or any `ntfy` client and point it to your bot's web server URL.

```bash
# Basic message
curl -d "Backup successful 😀" http://localhost:8080/mytopic

# With Title and Priority
curl -H "Title: Backup Status" -H "Priority: high" -d "Backup successful 😀" http://localhost:8080/mytopic
```

The bot supports the following `ntfy` Priority values:
- 5, max, urgent: 🔴
- 4, high: 🟠
- 3, default: 🟢
- 2, low: 🔵
- 1, min: ⚪️

### Account Setup
Before running the bot, you must initialize at least one Delta Chat account.

**Manual Setup (Custom Email/Password)**
If you want to use a specific email account:
```bash
docker-compose run --rm ntfy_bot python bot.py init bot@example.com "YOUR_PASSWORD"
```

**Automatic Setup (Chatmail)**
If you want to use a chatmail server (which doesn't require a pre-existing password for new accounts), choose a desired address at a chatmail domain (e.g., `nine.testrun.org`):
```bash
docker-compose run --rm ntfy_bot python bot.py init mybot@nine.testrun.org
```

Once configured, you can start the bot normally with `docker-compose up -d`.

## Running the Bot

### Using Docker (Recommended)

1. Clone the repository
2. Initialize the account as described in the **Account Setup** section above.
3. Start the bot: `docker-compose up -d`
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
3. Run `docker-compose up -d`.
