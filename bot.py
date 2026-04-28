import asyncio
import io
import logging
import os
import threading
import time
import tempfile
import urllib.parse


import aiohttp
from aiohttp import web
from deltachat2 import events, MsgData
from deltabot_cli import BotCli

import database

try:
    import qrcode
except ImportError:
    qrcode = None

import emoji

# Initialize logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("ntfy_bot")

dc_cli = BotCli("ntfybot")

# Global references
dc_bot_instance = None
dc_accid = None

def get_priority_emoji(priority_raw: str) -> str:
    priority_raw = str(priority_raw).lower()
    if priority_raw in ("5", "max", "urgent"):
        return "🔴"
    elif priority_raw in ("4", "high"):
        return "🟠"
    elif priority_raw in ("3", "default"):
        return "🟢"
    elif priority_raw in ("2", "low"):
        return "🔵"
    elif priority_raw in ("1", "min"):
        return "⚪️"
    return "🟢" # default

def parse_priority(priority_raw: str) -> int:
    priority_raw = str(priority_raw).lower()
    if priority_raw in ("5", "max", "urgent"):
        return 5
    elif priority_raw in ("4", "high"):
        return 4
    elif priority_raw in ("3", "default"):
        return 3
    elif priority_raw in ("2", "low"):
        return 2
    elif priority_raw in ("1", "min"):
        return 1
    return 3

def parse_tags(tags_raw: str):
    if not tags_raw:
        return [], []
    tags_list = [t.strip() for t in tags_raw.split(',') if t.strip()]
    emojis = []
    text_tags = []
    for tag in tags_list:
        emojized = emoji.emojize(f":{tag}:", language='alias')
        if emojized != f":{tag}:":
            emojis.append(emojized)
        else:
            text_tags.append(tag)
    return emojis, text_tags

def format_notification(title: str, message: str, priority_raw: str, tags_raw: str, click_raw: str, topic: str, is_private: bool) -> str:
    priority_emoji = get_priority_emoji(priority_raw)
    emojis, text_tags = parse_tags(tags_raw)
    
    emoji_prefix = priority_emoji
    if emojis:
        emoji_prefix += " " + "".join(emojis)

    formatted = ""
    if is_private:
        formatted += f"[{topic}]\n"

    if title:
        formatted += f"{emoji_prefix} **{title}**\n\n"
    elif priority_raw and priority_raw not in ("3", "default", "") or emojis:
        # If no title but non-default priority or custom emojis, still show emoji header
        formatted += f"{emoji_prefix}\n\n"
    elif is_private:
        formatted += "\n"
        
    formatted += message
    
    if text_tags:
        formatted += "\n\nTags: " + ", ".join(text_tags)
        
    if click_raw:
        formatted += f"\n\n🔗 [Open Link]({click_raw})"
        
    return formatted

async def handle_ntfy_post(request):
    # Log incoming request for debugging
    logger.info(f"Incoming POST to {request.path}")
    logger.info(f"Headers: {dict(request.headers)}")
    
    topic = request.match_info.get('topic')
    if not topic:
        # Fallback to headers or query params
        topic = request.headers.get('X-Topic') or request.headers.get('Topic') or request.query.get('topic') or request.query.get('t')

    title = request.headers.get('Title') or request.headers.get('X-Title') or request.headers.get('ti') or request.headers.get('t', '')
    priority_raw = request.headers.get('Priority') or request.headers.get('X-Priority') or request.headers.get('prio') or request.headers.get('p', '3')
    tags_raw = request.headers.get('Tags') or request.headers.get('X-Tags') or request.headers.get('tag') or request.headers.get('ta', '')
    click_raw = request.headers.get('Click') or request.headers.get('X-Click', '')
    attach_url = request.headers.get('Attach') or request.headers.get('X-Attach', '')
    filename = request.headers.get('Filename') or request.headers.get('X-Filename') or request.headers.get('File') or request.headers.get('f', '')
    
    file_path = None
    message = ""
    
    # Handle JSON body (Uptime Kuma uses this)
    content_type = request.headers.get('Content-Type', '')
    if 'application/json' in content_type:
        try:
            data = await request.json()
            if not topic:
                topic = data.get('topic')
            message = data.get('message', '')
            title = data.get('title', title)
            priority_raw = str(data.get('priority', priority_raw))
            click_raw = data.get('click', click_raw)
            attach_url = data.get('attach', attach_url)
            filename = data.get('filename', filename)
            if 'tags' in data:
                if isinstance(data['tags'], list):
                    tags_raw = ','.join(data['tags'])
                else:
                    tags_raw = str(data['tags'])
        except Exception as e:
            logger.error(f"Failed to parse JSON body: {e}")
            message = await request.text()
    else:
        if filename and not attach_url:
            # Direct binary body upload (Step 6)
            body = await request.read()
            if body:
                ext = os.path.splitext(filename)[1]
                f = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
                f.write(body)
                f.close()
                file_path = f.name
                message = "📎 File attachment" # Default message if only file is sent
        else:
            message = await request.text()
            
    # External attachment from URL (Step 5)
    if attach_url and not file_path:
        try:
            parsed_url = urllib.parse.urlparse(attach_url)
            ext = os.path.splitext(parsed_url.path)[1]
            
            async with aiohttp.ClientSession() as session:
                async with session.get(attach_url) as resp:
                    if resp.status == 200:
                        body = await resp.read()
                        f = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
                        f.write(body)
                        f.close()
                        file_path = f.name
                    else:
                        logger.warning(f"Failed to download attachment from {attach_url}, status: {resp.status}")
        except Exception as e:
            logger.error(f"Error downloading attachment from {attach_url}: {e}")

    
    if not topic:
        logger.warning(f"Request to {request.path} failed: Topic required")
        return web.Response(status=400, text="Topic required. Use /{topic}, Topic header or 'topic' in JSON.")

    if not message or not str(message).strip():
        return web.Response(status=400, text="Message body required")

    # Save to database
    priority_int = parse_priority(priority_raw)
    database.add_notification(topic, title, message, priority_int)

    # Broadcast to subscribers
    subscribers = database.get_subscribers(topic)
    if subscribers and dc_bot_instance and dc_accid is not None:
        for dc_chat_id in subscribers:
            logger.info(f"Preparing to send topic '{topic}' to chat {dc_chat_id}...")
            
            success = False
            for acc_id in dc_bot_instance.rpc.get_all_account_ids():
                is_private = True
                try:
                    chat_info = dc_bot_instance.rpc.get_basic_chat_info(acc_id, dc_chat_id)
                    if isinstance(chat_info, dict):
                        if "chat_type" in chat_info:
                            is_private = (chat_info["chat_type"] == "Single")
                        else:
                            is_private = (chat_info.get("type", 1) == 1)
                    else:
                        if hasattr(chat_info, "chat_type"):
                            is_private = (chat_info.chat_type == "Single")
                        else:
                            is_private = (getattr(chat_info, "type", 1) == 1)
                    logger.info(f"Chat {dc_chat_id} found on account {acc_id}, is_private: {is_private}")
                except Exception as e:
                    # This exception means the chat doesn't exist on this account, try next account
                    continue

                try:
                    formatted_msg = format_notification(title, message, priority_raw, tags_raw, click_raw, topic, is_private)
                    
                    msg_data = MsgData(text=formatted_msg)
                    if file_path:
                        msg_data.file = file_path
                    
                    if not is_private:
                        msg_data.override_sender_name = f"#{topic}"
                    
                    logger.info(f"Calling send_msg for chat {dc_chat_id} on account {acc_id}...")
                    msg_id = dc_bot_instance.rpc.send_msg(acc_id, dc_chat_id, msg_data)
                    logger.info(f"Successfully sent msg_id {msg_id} to chat {dc_chat_id} on account {acc_id}")
                    success = True
                    break # Break out of account loop, we found it!
                except Exception as e:
                    logger.error(f"Failed to send to {dc_chat_id} on account {acc_id}: {e}")
                    # Even if send_msg fails, we found the right account but something else broke.
                    # We log it and break out of the account loop.
                    with open("data/debug.log", "a") as f:
                        f.write(f"Failed to send to {dc_chat_id} on account {acc_id}: {e}\n")
                    break

            if not success:
                logger.warning(f"Could not find chat {dc_chat_id} on any configured account.")
                
        # Clean up temp file
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                logger.error(f"Failed to delete temp file {file_path}: {e}")

    return web.json_response({"id": "ntfy-compat", "time": int(time.time()), "event": "message", "topic": topic, "message": message})

async def handle_index(request):
    html = """<!DOCTYPE html>
<html>
<head>
    <title>Delta Chat Ntfy Bot</title>
    <link rel="icon" type="image/png" href="/favicon-96x96.png" sizes="96x96" />
    <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
    <link rel="shortcut icon" href="/favicon.ico" />
    <link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png" />
    <meta name="apple-mobile-web-app-title" content="Ntfy Bot" />
    <link rel="manifest" href="/site.webmanifest" />
    <style>
        body {
            background-color: #22272e;
            color: #adbac7;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
            margin: 0;
            padding: 2rem;
            display: flex;
            justify-content: center;
        }
        .container {
            background-color: #1c2128;
            border-top: 5px solid #31958b;
            border-radius: 4px;
            padding: 2rem;
            box-shadow: 0 8px 24px rgba(0,0,0,0.2);
            max-width: 800px;
            width: 100%;
        }
        .header {
            display: flex;
            align-items: center;
            margin-bottom: 1rem;
        }
        .logo {
            height: 2.5rem;
            margin-right: 1rem;
            border-radius: 4px;
        }
        h1 {
            color: #adbac7;
            margin: 0;
            font-size: 1.5rem;
        }
        code {
            background-color: #22272e;
            padding: 0.2rem 0.4rem;
            border-radius: 3px;
            font-family: ui-monospace, SFMono-Regular, SF Mono, Menlo, Consolas, Liberation Mono, monospace;
            font-size: 85%;
        }
        a {
            color: #539bf5;
            text-decoration: none;
        }
        a:hover {
            text-decoration: underline;
        }
        .qr-wrapper {
            margin-top: 1.5rem;
            padding: 1rem;
            background: #ffffff;
            display: inline-block;
            border-radius: 6px;
        }
        .qr-code {
            background: transparent;
            color: #000000;
            padding: 0;
            margin: 0;
            font-family: "Courier New", Courier, monospace;
            font-size: 10px;
            line-height: 1;
            letter-spacing: -0.1em;
            word-spacing: 0;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <img src="/icon.png" alt="Logo" class="logo">
            <h1>Delta Chat Ntfy Bot</h1>
        </div>
        <p>This server is running the <a href="https://github.com/mrgluek/deltachat_ntfy">Delta Chat Ntfy Bot</a>.<br>
        <br>
        Send POST requests to <code>topic</code> to send messages to Delta Chat:<br>
        <br>
        <code>curl -d "Hello from ntfy" https://ntfy.gluek.info/test</code><br>
        <br></p>
"""
    
    if dc_bot_instance:
        try:
            accounts = dc_bot_instance.rpc.get_all_account_ids()
            active_accid = None
            for aid in accounts:
                try:
                    if dc_bot_instance.rpc.get_config(aid, "configured") == "1":
                        active_accid = aid
                        break
                except:
                    pass
            
            if active_accid:
                # Use the dedicated method to get the secure join link
                qrdata = dc_bot_instance.rpc.get_chat_securejoin_qr_code(active_accid, None)
                if qrdata:
                    html += f"""
        <p><a href="{qrdata}">Add this bot</a> to Delta Chat:</p>
"""
                    if qrcode:
                        qr = qrcode.QRCode(version=1, box_size=1, border=2)
                        qr.add_data(qrdata)
                        qr.make(fit=True)
                        f = io.StringIO()
                        qr.print_ascii(out=f)
                        html += f'<div class="qr-wrapper"><pre class="qr-code">{f.getvalue()}</pre></div>'
        except Exception as e:
            logger.error(f"handle_index: error rendering bot block: {e}")
            html += f"<!-- Error: {e} -->"
            
    html += """
    </div>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")

async def handle_robots(request):
    return web.Response(text="User-agent: *\nDisallow: /\n", content_type="text/plain")

async def handle_static(request):
    filename = request.path.lstrip('/')
    # List of allowed static files for security
    allowed_files = [
        'favicon-96x96.png', 'favicon.svg', 'favicon.ico', 
        'apple-touch-icon.png', 'site.webmanifest', 'icon.png',
        'web-app-manifest-192x192.png', 'web-app-manifest-512x512.png'
    ]
    if filename in allowed_files and os.path.exists(filename):
        return web.FileResponse(filename)
    return web.Response(status=404)


async def _run_web_server():
    app = web.Application()
    app.router.add_get('/', handle_index)
    app.router.add_get('/robots.txt', handle_robots)
    # Add routes for all static files
    for static_file in [
        'favicon-96x96.png', 'favicon.svg', 'favicon.ico', 
        'apple-touch-icon.png', 'site.webmanifest', 'icon.png',
        'web-app-manifest-192x192.png', 'web-app-manifest-512x512.png'
    ]:
        app.router.add_get(f'/{static_file}', handle_static)
    
    app.router.add_post('/', handle_ntfy_post)
    app.router.add_post('/{topic}', handle_ntfy_post)
    app.router.add_put('/', handle_ntfy_post)
    app.router.add_put('/{topic}', handle_ntfy_post)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, '0.0.0.0', port)
    
    logger.info(f"Starting web server on 0.0.0.0:{port}...")
    await site.start()
    logger.info("Web server is UP and running.")
    
    # Keep server running
    while True:
        await asyncio.sleep(3600)

def start_web_server_thread():
    """Start the web server in a separate thread with its own event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_run_web_server())

@dc_cli.on(events.NewMessage(command="/debug"))
def debug_command(bot, accid, event):
    """Print debug info about subscriptions."""
    chat_id = event.msg.chat_id
    args = event.payload
    
    topic = args.strip() if args.strip() else "test"
    subs = database.get_subscribers(topic)
    all_topics = database.get_subscriptions(chat_id)
    
    debug_msg = f"**Debug Info**\n"
    debug_msg += f"dc_bot_instance: {'Set' if dc_bot_instance else 'None'}\n"
    debug_msg += f"dc_accid: {dc_accid}\n"
    debug_msg += f"Your chat ID: {chat_id}\n"
    debug_msg += f"Your subscribed topics: {all_topics}\n"
    debug_msg += f"Subscribers for '{topic}': {subs}\n"
    
    try:
        chat_info = bot.rpc.get_basic_chat_info(accid, chat_id)
        if isinstance(chat_info, dict):
            chat_type = chat_info.get("type", 1)
        else:
            chat_type = getattr(chat_info, "type", 1)
        debug_msg += f"Your chat type: {chat_type} (is_private: {chat_type == 1})\n"
    except Exception as e:
        debug_msg += f"Failed to get your chat type: {e}\n"
        
    bot.rpc.send_msg(accid, chat_id, MsgData(text=debug_msg))

@dc_cli.on_init
def on_init(bot, args):
    """Called when the Delta Chat bot starts."""
    global dc_bot_instance, dc_accid
    bot.logger.info("Initializing Delta Chat ntfy bot...")
    
    dc_bot_instance = bot
    
    for accid in bot.rpc.get_all_account_ids():
        dc_accid = accid
        bot.rpc.set_config(accid, "displayname", "Ntfy Bot")
        bot.rpc.set_config(accid, "selfstatus", "A Delta Chat bot that emulates a ntfy.sh backend to broadcast notifications from HTTP POST requests to Delta Chat users and groups: https://github.com/mrgluek/deltachat_ntfy")
        # Set bot avatar if icon file exists
        try:
            base_dir = os.path.dirname(os.path.abspath(__file__))
            for icon_name in ["icon.png", os.path.join("data", "icon.png")]:
                icon_path = os.path.join(base_dir, icon_name)
                if os.path.exists(icon_path):
                    bot.rpc.set_config(accid, "selfavatar", icon_path)
                    bot.logger.info(f"Avatar set from {icon_path}")
                    break
            else:
                bot.logger.warning(f"icon.png not found in {base_dir}")
        except Exception as e:
            bot.logger.warning(f"Could not set avatar: {e}")
    
    # Start web server in a separate thread so it works independently
    web_thread = threading.Thread(target=start_web_server_thread, daemon=True)
    web_thread.start()

@dc_cli.on_start
def on_start(bot, _args):
    """Called after the bot is fully started. Print QR code for easy setup."""
    global dc_bot_instance, dc_accid
    dc_bot_instance = bot
    accounts = bot.rpc.get_all_account_ids()
    if accounts:
        dc_accid = accounts[0]
        try:
            qrdata = bot.rpc.get_chat_securejoin_qr_code(dc_accid, None)
            print("\n" + "=" * 50)
            print("To add this bot, scan the QR code or copy the link below:\n")

            if qrcode:
                qr = qrcode.QRCode(version=1, box_size=1, border=2)
                qr.add_data(qrdata)
                qr.make(fit=True)
                f = io.StringIO()
                qr.print_ascii(out=f)
                print(f.getvalue())
            else:
                print("(Install 'qrcode' package to see ASCII QR code here)")

            print(qrdata)
            print("\n" + "=" * 50 + "\n")
        except Exception as e:
            bot.logger.error(f"Failed to generate QR code: {e}")

@dc_cli.on(events.NewMessage(command="/help"))
def help_command(bot, accid, event):
    msg = event.msg
    contact = bot.rpc.get_contact(accid, msg.from_id)
    sender_email = contact.address
    
    admin_email = database.get_config("admin_dc_email")
    
    help_text = (
        f"👋 Hi {sender_email}!\n\n"
        f"I'm the Ntfy Bot. I receive HTTP POST requests and broadcast them to subscribed topics.\n\n"
        f"Commands:\n"
        f"/sub <topic> — Subscribe to a topic\n"
        f"/unsub <topic> — Unsubscribe from a topic\n"
        f"/list — Show subscribed topics\n"
        f"/last — Show last 5 notifications from subscribed topics\n"
        f"/newgroup [name] — Create a dedicated group chat for alerts\n"
        f"/donate — Support bot development ❤️\n"
        f"/help — Show this help message\n\n"
    )
    
    if not admin_email:
        help_text += (
            f"Initialisation Command:\n"
            f"/initadmin — Claim bot ownership (if no admin is set)\n\n"
        )
    elif admin_email.lower() == sender_email.lower():
        help_text += (
            f"Admin Commands:\n"
            f"/accounts — List configured bot accounts\n"
            f"/rmaccount <id> — Delete a bot account\n\n"
        )
        
    help_text += f"Run your own bot: https://github.com/mrgluek/deltachat_ntfy"

    bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=help_text))

@dc_cli.on(events.NewMessage(command="/newgroup"))
def newgroup_command(bot, accid, event):
    msg = event.msg
    group_name = event.payload.strip() or "Ntfy Alerts"
    
    try:
        # Create a new group chat
        new_chat_id = bot.rpc.create_group_chat(accid, group_name, False)
        # Get secure join link
        qrdata = bot.rpc.get_chat_securejoin_qr_code(accid, new_chat_id)
        
        reply_text = (
            f"✅ Group '{group_name}' created!\n\n"
            f"Join link: {qrdata}\n\n"
            f"Share this link or scan the QR code to join the group. Once inside the group, use /sub to subscribe the group to topics!"
        )
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=reply_text))
    except Exception as e:
        bot.logger.error(f"Failed to create group: {e}")
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=f"❌ Failed to create group: {e}"))

@dc_cli.on(events.NewMessage(command="/donate"))
def donate_command(bot, accid, event):
    msg = event.msg
    support_msg = (
        "❤️ Support Bot Development\n\n"
        "If you find this bot useful, you can support its development and server costs here:\n\n"
        "☕️ Ko-fi: https://ko-fi.com/gluek (🌍 world cards, paypal, no commissions)\n"
        "🚀 Tribute: https://web.tribute.tg/d/IWb (🇷🇺 russian cards, SBP, high commissions)\n\n"
        "Thank you! 🙏"
    )
    bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=support_msg))

@dc_cli.on(events.NewMessage(command="/initadmin"))
def initadmin_command(bot, accid, event):
    msg = event.msg
    contact = bot.rpc.get_contact(accid, msg.from_id)
    sender_email = contact.address
    
    current_admin = database.get_config("admin_dc_email")
    if current_admin:
        if current_admin.lower() == sender_email.lower():
            bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="✅ You are already the administrator."))
        else:
            bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="❌ Administrator is already set."))
        return
        
    database.set_config("admin_dc_email", sender_email)
    bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=f"👑 You are now the bot administrator ({sender_email})."))

@dc_cli.on(events.NewMessage(command="/accounts"))
def accounts_command(bot, accid, event):
    msg = event.msg
    contact = bot.rpc.get_contact(accid, msg.from_id)
    sender_email = contact.address
    
    admin_email = database.get_config("admin_dc_email")
    if not admin_email or admin_email.lower() != sender_email.lower():
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="❌ This command is only for the administrator."))
        return
        
    accounts = bot.rpc.get_all_account_ids()
    reply = f"🤖 **Configured Bot Accounts:** {len(accounts)}\n\n"
    
    for aid in accounts:
        try:
            addr = bot.rpc.get_config(aid, "addr")
            state = bot.rpc.get_config(aid, "configured")
            reply += f"• Account ID: {aid}\n  Email: {addr}\n  Configured: {state}\n\n"
        except Exception as e:
            reply += f"• Account ID: {aid} (Error reading: {e})\n\n"
            
    reply += "To delete an account, use: /rmaccount <id>\nNote: The bot now automatically routes messages to the correct account based on the chat ID."
    bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=reply))

@dc_cli.on(events.NewMessage(command="/rmaccount"))
def rmaccount_command(bot, accid, event):
    msg = event.msg
    contact = bot.rpc.get_contact(accid, msg.from_id)
    sender_email = contact.address
    
    admin_email = database.get_config("admin_dc_email")
    if not admin_email or admin_email.lower() != sender_email.lower():
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="❌ This command is only for the administrator."))
        return
        
    try:
        target_aid = int(event.payload.strip())
    except ValueError:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="❌ Usage: /rmaccount <account_id>"))
        return
        
    if target_aid == accid:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="❌ You cannot delete the account you are currently using to talk to the bot!"))
        return
        
    try:
        # Assuming the method is remove_account in deltachat2
        if hasattr(bot.rpc, "remove_account"):
            bot.rpc.remove_account(target_aid)
        else:
            bot.rpc.delete_account(target_aid)
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=f"✅ Account {target_aid} has been successfully deleted."))
    except Exception as e:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=f"❌ Failed to delete account {target_aid}: {e}"))

@dc_cli.on(events.NewMessage(command="/sub"))
def sub_command(bot, accid, event):
    msg = event.msg
    topic = event.payload.strip()
    
    if not topic:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="Usage: /sub <topic>"))
        return
        
    if database.subscribe(msg.chat_id, topic):
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=f"✅ Subscribed to '{topic}'"))
    else:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=f"ℹ️ Already subscribed to '{topic}'"))

@dc_cli.on(events.NewMessage(command="/unsub"))
def unsub_command(bot, accid, event):
    msg = event.msg
    topic = event.payload.strip()
    
    if not topic:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="Usage: /unsub <topic>"))
        return
        
    if database.unsubscribe(msg.chat_id, topic):
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=f"✅ Unsubscribed from '{topic}'"))
    else:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=f"ℹ️ You were not subscribed to '{topic}'"))

@dc_cli.on(events.NewMessage(command="/list"))
def list_command(bot, accid, event):
    msg = event.msg
    topics = database.get_subscriptions(msg.chat_id)
    
    if not topics:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="You are not subscribed to any topics."))
        return
        
    topics_list = "\n".join([f"- {t}" for t in topics])
    bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=f"📋 Subscribed topics:\n{topics_list}"))

@dc_cli.on(events.NewMessage(command="/last"))
def last_command(bot, accid, event):
    msg = event.msg
    topics = database.get_subscriptions(msg.chat_id)
    
    if not topics:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="You are not subscribed to any topics, so there are no notifications."))
        return
        
    recent = database.get_recent_notifications(topics, limit=5)
    
    if not recent:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="No recent notifications for your topics."))
        return
        
    lines = ["🕒 **Last 5 Notifications**\n"]
    for notif in recent:
        emoji = get_priority_emoji(str(notif['priority']))
        title_str = f"**{notif['title']}** " if notif['title'] else ""
        lines.append(f"{emoji} [{notif['topic']}] {title_str}\n{notif['message']}")
        lines.append("---")
        
    # Remove last separator
    if lines[-1] == "---":
        lines.pop()
        
    bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="\n".join(lines)))

if __name__ == "__main__":
    import sys
    # If no subcommand is provided, default to 'serve' to start the bot
    if len(sys.argv) == 1:
        sys.argv.append("serve")
    dc_cli.start()
