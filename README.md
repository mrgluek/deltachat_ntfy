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

## Bot Configuration (Account Setup)

By default, the bot uses `deltabot-cli` which offers two ways to set up the Delta Chat account:

### Automatic Setup (Recommended)
On the first run, the bot will generate an onboarding QR code in the console. 
- Use `docker-compose logs -f` to see the QR code.
- Scan it with your Delta Chat mobile app.
- A new account will be automatically created on a chatmail server.

### Manual Setup (Custom Email/Password)
If you want to use a specific email account:
```bash
# Using Docker
docker-compose exec ntfy_bot python bot.py init bot@example.com "YOUR_PASSWORD"

# Manual run
python bot.py init bot@example.com "YOUR_PASSWORD"
```

## Running the Bot

### Using Docker (Recommended)

1. Clone the repository
2. `docker-compose up -d`
3. Scan the QR code generated in the console logs (using `docker-compose logs -f`) to add the bot, or check the terminal output for the invite link.

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
