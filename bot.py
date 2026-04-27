import asyncio
import io
import logging
import os
import threading
import time

from aiohttp import web
from deltachat2 import events, MsgData
from deltabot_cli import BotCli

import database

try:
    import qrcode
except ImportError:
    qrcode = None

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

def format_notification(title: str, message: str, priority_raw: str) -> str:
    emoji = get_priority_emoji(priority_raw)
    formatted = ""
    if title:
        formatted += f"{emoji} **{title}**\n\n"
    elif priority_raw and priority_raw not in ("3", "default", ""):
        # If no title but non-default priority, still show emoji
        formatted += f"{emoji}\n\n"
        
    formatted += message
    return formatted

async def handle_ntfy_post(request):
    # Log incoming request for debugging
    logger.info(f"Incoming POST to {request.path}")
    logger.info(f"Headers: {dict(request.headers)}")
    
    topic = request.match_info.get('topic')
    if not topic:
        # Fallback to headers or query params
        topic = request.headers.get('X-Topic') or request.headers.get('Topic') or request.query.get('topic') or request.query.get('t')
    
    if not topic:
        logger.warning(f"Request to {request.path} failed: Topic required")
        return web.Response(status=400, text="Topic required. Use /{topic}, ?topic=... or Topic header.")

    title = request.headers.get('Title', '')
    priority_raw = request.headers.get('Priority', '3')
    message = await request.text()
    
    if not message.strip():
        return web.Response(status=400, text="Message body required")

    # Save to database
    priority_int = parse_priority(priority_raw)
    database.add_notification(topic, title, message, priority_int)

    # Broadcast to subscribers
    subscribers = database.get_subscribers(topic)
    if subscribers and dc_bot_instance and dc_accid is not None:
        formatted_msg = format_notification(title, message, priority_raw)
        for dc_chat_id in subscribers:
            try:
                dc_bot_instance.rpc.send_msg(dc_accid, dc_chat_id, MsgData(text=formatted_msg))
            except Exception as e:
                logger.error(f"Failed to send to {dc_chat_id}: {e}")

    return web.json_response({"id": "ntfy-compat", "time": int(time.time()), "event": "message", "topic": topic, "message": message})

async def handle_index(request):
    return web.Response(text="Delta Chat Ntfy Bot is running! 🚀\n\nSend POST requests to /{topic} to broadcast notifications.")


async def _run_web_server():
    app = web.Application()
    app.router.add_get('/', handle_index)
    app.router.add_post('/', handle_ntfy_post)
    app.router.add_post('/{topic}', handle_ntfy_post)
    
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
    
    help_text = (
        f"👋 Hi {sender_email}!\n\n"
        f"I'm the Ntfy Bot. I receive HTTP POST requests and broadcast them to subscribed topics.\n\n"
        f"Commands:\n"
        f"/sub <topic> — Subscribe to a topic\n"
        f"/unsub <topic> — Unsubscribe from a topic\n"
        f"/list — Show subscribed topics\n"
        f"/last — Show last 5 notifications from subscribed topics\n"
        f"/donate — Support bot development ❤️\n"
        f"/help — Show this help message\n\n"
        f"Admin Commands:\n"
        f"/initadmin — Claim bot ownership (if no admin is set)\n\n"
        f"Run your own bot: https://github.com/mrgluek/deltachat_ntfy"
    )
    bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=help_text))

@dc_cli.on(events.NewMessage(command="/donate"))
def donate_command(bot, accid, event):
    msg = event.msg
    support_msg = (
        "❤️ Support Bot Development\n\n"
        "If you find this bot useful, you can support its development and server costs here:\n\n"
        "🔗 https://web.tribute.tg/d/IWb\n\n"
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
