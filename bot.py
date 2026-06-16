import asyncio
import io
import logging
import json
import os
import threading
import time
import datetime
import tempfile
import urllib.parse
import html
import re


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
import collections

# Initialize logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("ntfy_bot")

dc_cli = BotCli("ntfybot")
bot_qr_cache = {} # Cache for secure join links to keep them stable on refresh

# Global references
dc_bot_instance = None
dc_accid = None

# Security settings
AUTH_TOKEN = os.getenv("AUTH_TOKEN")
RATE_LIMIT_WINDOW = 60 # seconds
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "30")) # messages per minute per IP
rate_limit_cache = collections.defaultdict(list)
listeners = collections.defaultdict(list) # Pub/Sub for JSON streams
web_server_loop = None

def push_to_listeners(topic, msg_payload):
    logger.info(f"push_to_listeners: topic='{topic}', listeners={list(listeners.keys())}, web_server_loop={web_server_loop}")
    if topic in listeners:
        logger.info(f"push_to_listeners: found {len(listeners[topic])} queues for '{topic}'")
        for q in listeners[topic]:
            if web_server_loop:
                try:
                    web_server_loop.call_soon_threadsafe(q.put_nowait, msg_payload)
                    logger.info("push_to_listeners: successfully queued event via call_soon_threadsafe")
                except Exception as e:
                    logger.error(f"Failed to push to listener queue: {e}")
            else:
                logger.warning("push_to_listeners: web_server_loop is None!")
    else:
        logger.info(f"push_to_listeners: no listeners subscribed to '{topic}'")

def get_client_ip(request):
    """Get real client IP, respecting X-Forwarded-For from Caddy."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote

def is_rate_limited(ip):
    """Simple rate limiter."""
    now = time.time()
    rate_limit_cache[ip] = [t for t in rate_limit_cache[ip] if now - t < RATE_LIMIT_WINDOW]
    if len(rate_limit_cache[ip]) >= RATE_LIMIT_MAX:
        return True
    rate_limit_cache[ip].append(now)
    return False
def sanitize_string(s: str) -> str:
    if not isinstance(s, str):
        return s
    try:
        s.encode('utf-8')
        return s
    except UnicodeEncodeError:
        try:
            b = s.encode('utf-8', 'surrogateescape')
            try:
                return b.decode('utf-8')
            except UnicodeDecodeError:
                for encoding in ('cp1251', 'cp1252'):
                    try:
                        return b.decode(encoding)
                    except UnicodeDecodeError:
                        continue
                return b.decode('utf-8', 'replace')
        except Exception:
            return "".join(c for c in s if not (0xD800 <= ord(c) <= 0xDFFF))

def get_priority_emoji(priority_raw: str) -> str:
    priority_map = {
        "5": "🚨", "max": "🚨", "urgent": "🚨",
        "4": "⚠️", "high": "⚠️",
        "3": "✅", "default": "✅", "": "✅",
        "2": "ℹ️", "low": "ℹ️",
        "1": "💤", "min": "💤"
    }
    return priority_map.get(str(priority_raw).lower(), "✅")

def linkify(text):
    """Escape HTML and wrap URLs in <a> tags."""
    if not text:
        return ""
    # Escape HTML to prevent XSS
    text = html.escape(text)
    # Wrap URLs
    url_pattern = re.compile(r'(https?://[^\s<>"]+)')
    return url_pattern.sub(r'<a href="\1" target="_blank" rel="noopener noreferrer">\1</a>', text)

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
        formatted += f"`[{topic}]` \n\n"

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
    topic = request.match_info.get('topic')
    
    # 1. Rate limiting
    ip = get_client_ip(request)
    if is_rate_limited(ip):
        logger.warning(f"Rate limit exceeded for IP {ip}")
        return web.Response(text="Rate limit exceeded. Try again later.", status=429)

    # 2. Authentication
    if AUTH_TOKEN:
        auth_header = request.headers.get("Authorization")
        token_from_query = request.query.get("auth")
        token_from_header = request.headers.get("X-Auth-Token")
        
        valid = False
        if auth_header == f"Bearer {AUTH_TOKEN}":
            valid = True
        elif token_from_query == AUTH_TOKEN:
            valid = True
        elif token_from_header == AUTH_TOKEN:
            valid = True
            
        if not valid:
            logger.warning(f"Unauthorized access attempt from IP {ip}")
            return web.Response(text="Unauthorized. Please provide a valid AUTH_TOKEN.", status=401)

    # Log incoming request for debugging
    logger.info(f"Incoming POST to {request.path}")
    logger.info(f"Headers: {dict(request.headers)}")
    
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

    # Sanitize all strings to prevent UnicodeEncodeError with surrogate characters
    topic = sanitize_string(topic)
    title = sanitize_string(title)
    priority_raw = sanitize_string(priority_raw)
    tags_raw = sanitize_string(tags_raw)
    click_raw = sanitize_string(click_raw)
    attach_url = sanitize_string(attach_url)
    filename = sanitize_string(filename)
    message = sanitize_string(message)

    if not topic:
        logger.warning(f"Request to {request.path} failed: Topic required")
        return web.Response(status=400, text="Topic required. Use /{topic}, Topic header or 'topic' in JSON.")

    if not message or not str(message).strip():
        return web.Response(status=400, text="Message body required")

    # Save to database
    priority_int = parse_priority(priority_raw)
    database.add_notification(topic, title, message, priority_int)

    # Pub/Sub push
    msg_payload = {
        "id": f"ntfy-{int(time.time()*1000)}",
        "time": int(time.time()),
        "event": "message",
        "topic": topic,
        "message": message
    }
    if title: msg_payload["title"] = title
    if priority_int: msg_payload["priority"] = priority_int
    if tags_raw: msg_payload["tags"] = [t.strip() for t in tags_raw.split(',') if t.strip()]
    if click_raw: msg_payload["click"] = click_raw
    if attach_url: msg_payload["attach"] = attach_url
    
    push_to_listeners(topic, msg_payload)

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
                    
                    # Track sending stats
                    try:
                        addr = dc_bot_instance.rpc.get_config(acc_id, "configured_addr") or dc_bot_instance.rpc.get_config(acc_id, "addr")
                        if addr:
                            database.increment_transport_sent(addr)
                    except Exception:
                        pass
                        
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
    bot_url = database.get_config("bot_url") or "https://ntfy.gluek.info"
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
            flex-direction: column;
            align-items: center;
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
        <b>Quick start:</b><br>
        <br>
        1. Send POST requests to any topic (e.g. <code>test</code>) to send alert to all topic subscribers in Delta Chat:<br>
        <br>
        <code>curl -d "Hello from ntfy" SERVER_URL/test</code><br>
        <br>
        2. Check it live at: <a href="SERVER_URL/test">SERVER_URL/test</a><br>
        <br>
        More topics to try: 
        <a href="SERVER_URL/stats">stats</a>, 
        <a href="SERVER_URL/changelog">changelog</a>, 
        <a href="SERVER_URL/announcements">announcements</a>
        </p>
"""
    html = html.replace("SERVER_URL", bot_url.rstrip('/'))
    
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
                # Use cached QR link if available, otherwise generate new one
                qrdata = bot_qr_cache.get(active_accid)
                if not qrdata:
                    qrdata = dc_bot_instance.rpc.get_chat_securejoin_qr_code(active_accid, None)
                    bot_qr_cache[active_accid] = qrdata
                
                if qrdata:
                    html += f"""
        <p>3. <a href="{qrdata}">Add this bot</a> to Delta Chat and send <code>/help</code> to see available commands:</p>
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

async def handle_topic_view(request):
    topic = request.match_info.get('topic')
    
    # Security: don't allow accessing hidden files or common requested files not in static list
    if not topic or topic.startswith('.') or topic in ['favicon.ico', 'robots.txt']:
        return web.Response(status=404)

    bot_url = database.get_config("bot_url") or "https://ntfy.gluek.info"
    topic_url = f"{bot_url.rstrip('/')}/{topic}"
    
    # Get SecureJoin link (same as on index page)
    subscribe_link = "#"
    if dc_bot_instance and dc_accid is not None:
        try:
            subscribe_link = dc_bot_instance.rpc.get_chat_securejoin_qr_code(dc_accid, None)
        except:
            pass
    
    # URL parts for display
    parsed_url = urllib.parse.urlparse(bot_url)
    display_host = parsed_url.netloc or "ntfy.gluek.info"

    notifications = database.get_recent_notifications([topic], limit=50)
    server_tz = datetime.datetime.now().astimezone().tzname()
    
    auth_field_html = ""
    if AUTH_TOKEN:
        auth_field_html = """
                    <div class="form-group" style="margin-bottom: 1rem;">
                        <label for="pub-token">Auth Token *</label>
                        <div class="input-with-toggle">
                            <input type="password" class="form-control" id="pub-token" placeholder="Enter server AUTH_TOKEN" required>
                            <span class="password-toggle" onclick="togglePasswordVisibility()">👁️</span>
                        </div>
                    </div>
        """

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Topic: {topic} - Ntfy Bot</title>
    <link rel="icon" type="image/png" href="/favicon-96x96.png" sizes="96x96" />
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{
            background-color: #22272e;
            color: #adbac7;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
            margin: 0;
            padding: 2rem;
            display: flex;
            flex-direction: column;
            align-items: center;
            min-height: 100vh;
        }}
        .container {{
            max-width: 800px;
            width: 100%;
            flex: 1;
        }}
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 2rem;
            border-bottom: 1px solid #444c56;
            padding-bottom: 1rem;
        }}
        .topic-path {{
            font-size: 1.2rem;
            font-weight: 500;
            display: flex;
            align-items: center;
            gap: 0.2rem;
        }}
        .home-link {{
            color: #768390;
            text-decoration: none;
        }}
        .home-link:hover {{
            color: #539bf5;
        }}
        .topic-name {{
            color: #adbac7;
        }}
        .actions {{
            display: flex;
            align-items: center;
            gap: 0.8rem;
        }}
        .copy-btn-inline {{
            cursor: pointer;
            font-size: 0.9rem;
            opacity: 0.4;
            transition: opacity 0.2s;
            user-select: none;
            padding: 4px;
        }}
        .copy-btn-inline:hover {{
            opacity: 1;
        }}
        .btn-subscribe {{
            background-color: #31958b;
            color: white;
            padding: 0.5rem 1rem;
            border-radius: 6px;
            text-decoration: none;
            font-weight: 600;
            font-size: 0.9rem;
            transition: background-color 0.2s;
        }}
        .btn-subscribe:hover {{
            background-color: #3aa69a;
        }}
        .notification-list {{
            display: flex;
            flex-direction: column;
            gap: 1rem;
        }}
        .notification-card {{
            background-color: #1c2128;
            border: 1px solid #444c56;
            border-radius: 6px;
            padding: 1rem;
            padding-right: 2.5rem;
            position: relative;
            transition: border-color 0.1s ease;
        }}
        .notification-card:hover {{
            border-color: #768390;
        }}
        .priority-indicator {{
            position: absolute;
            left: 0;
            top: 0;
            bottom: 0;
            width: 4px;
            border-top-left-radius: 6px;
            border-bottom-left-radius: 6px;
        }}
        .priority-5 {{ background-color: #f85149; }}
        .priority-4 {{ background-color: #f0883e; }}
        .priority-3 {{ background-color: #3fb950; }}
        .priority-2 {{ background-color: #539bf5; }}
        .priority-1 {{ background-color: #768390; }}
        
        .meta {{
            display: flex;
            justify-content: space-between;
            font-size: 0.8rem;
            color: #768390;
            margin-bottom: 0.5rem;
        }}
        .title {{
            font-weight: 600;
            margin-bottom: 0.5rem;
            display: block;
            color: #adbac7;
        }}
        .message {{
            line-height: 1.5;
            white-space: pre-wrap;
            word-break: break-word;
        }}
        .copy-btn {{
            position: absolute;
            right: 0.8rem;
            bottom: 0.8rem;
            cursor: pointer;
            font-size: 1.1rem;
            opacity: 0.3;
            transition: opacity 0.2s, transform 0.1s;
            user-select: none;
        }}
        .copy-btn:hover {{
            opacity: 1;
            transform: scale(1.15);
        }}
        .empty-state {{
            text-align: center;
            padding: 4rem 0;
            color: #768390;
        }}
        .footer {{
            margin-top: 4rem;
            padding: 2rem 0;
            border-top: 1px solid #444c56;
            text-align: center;
            font-size: 0.85rem;
            color: #768390;
            width: 100%;
        }}
        .footer a {{
            color: #539bf5;
            text-decoration: none;
        }}
        .footer a:hover {{
            text-decoration: underline;
        }}
        .message a {{
            color: #539bf5;
            text-decoration: none;
        }}
        .message a:hover {{
            text-decoration: underline;
        }}
        @media (max-width: 600px) {{
            body {{ padding: 1rem; }}
            .topic-path {{ font-size: 1rem; }}
        }}

        .publish-card {{
            background: rgba(28, 33, 40, 0.6);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border: 1px solid rgba(68, 76, 86, 0.5);
            border-radius: 8px;
            margin-bottom: 2rem;
            padding: 1.2rem;
            box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.3);
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }}
        .publish-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            cursor: pointer;
            user-select: none;
        }}
        .publish-header h3 {{
            margin: 0;
            font-size: 1.1rem;
            color: #539bf5;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}
        .publish-toggle-icon {{
            transition: transform 0.3s;
            font-size: 0.8rem;
            color: #768390;
        }}
        .publish-card.collapsed .publish-toggle-icon {{
            transform: rotate(-90deg);
        }}
        .publish-body {{
            margin-top: 1.2rem;
            display: flex;
            flex-direction: column;
            gap: 1rem;
            transition: max-height 0.3s ease-out, opacity 0.3s ease-out;
            max-height: 1000px;
            opacity: 1;
            overflow: hidden;
        }}
        .publish-card.collapsed .publish-body {{
            max-height: 0;
            opacity: 0;
            margin-top: 0;
        }}
        .form-group {{
            display: flex;
            flex-direction: column;
            gap: 0.4rem;
        }}
        .form-row {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 1rem;
        }}
        @media (max-width: 600px) {{
            .form-row {{
                grid-template-columns: 1fr;
            }}
        }}
        .form-group label {{
            font-size: 0.85rem;
            color: #768390;
            font-weight: 500;
        }}
        .form-control {{
            background-color: #22272e;
            border: 1px solid #444c56;
            border-radius: 6px;
            color: #adbac7;
            padding: 0.6rem 0.8rem;
            font-size: 0.9rem;
            transition: border-color 0.2s, box-shadow 0.2s;
            font-family: inherit;
            box-sizing: border-box;
            width: 100%;
        }}
        .form-control:focus {{
            outline: none;
            border-color: #539bf5;
            box-shadow: 0 0 0 3px rgba(83, 155, 245, 0.15);
        }}
        textarea.form-control {{
            resize: vertical;
            min-height: 80px;
        }}
        .priority-select-wrapper {{
            display: flex;
            gap: 0.4rem;
            flex-wrap: wrap;
        }}
        .priority-btn {{
            flex: 1;
            min-width: 60px;
            padding: 0.5rem;
            border-radius: 6px;
            border: 1px solid #444c56;
            background-color: #22272e;
            color: #768390;
            font-size: 0.85rem;
            cursor: pointer;
            transition: all 0.2s;
            text-align: center;
            font-weight: 500;
            user-select: none;
        }}
        .priority-btn:hover {{
            border-color: #768390;
            color: #adbac7;
        }}
        .priority-btn.active[data-priority="1"] {{ background-color: rgba(118, 131, 144, 0.15); border-color: #768390; color: #768390; }}
        .priority-btn.active[data-priority="2"] {{ background-color: rgba(83, 155, 245, 0.15); border-color: #539bf5; color: #539bf5; }}
        .priority-btn.active[data-priority="3"] {{ background-color: rgba(63, 185, 80, 0.15); border-color: #3fb950; color: #3fb950; }}
        .priority-btn.active[data-priority="4"] {{ background-color: rgba(240, 136, 62, 0.15); border-color: #f0883e; color: #f0883e; }}
        .priority-btn.active[data-priority="5"] {{ background-color: rgba(248, 81, 73, 0.15); border-color: #f85149; color: #f85149; }}

        .btn-publish-submit {{
            background-color: #31958b;
            color: white;
            border: none;
            padding: 0.7rem 1.2rem;
            border-radius: 6px;
            font-weight: 600;
            font-size: 0.95rem;
            cursor: pointer;
            transition: background-color 0.2s, transform 0.1s;
            align-self: flex-start;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}
        .btn-publish-submit:hover {{
            background-color: #3aa69a;
        }}
        .btn-publish-submit:active {{
            transform: scale(0.98);
        }}
        .btn-publish-submit:disabled {{
            background-color: #444c56;
            color: #768390;
            cursor: not-allowed;
        }}
        
        .publish-status {{
            padding: 0.6rem 0.8rem;
            border-radius: 6px;
            font-size: 0.9rem;
            display: none;
        }}
        .publish-status.success {{
            display: block;
            background-color: rgba(63, 185, 80, 0.15);
            border: 1px solid #3fb950;
            color: #3fb950;
        }}
        .publish-status.error {{
            display: block;
            background-color: rgba(248, 81, 73, 0.15);
            border: 1px solid #f85149;
            color: #f85149;
        }}
        
        .input-with-toggle {{
            position: relative;
            display: flex;
            align-items: center;
        }}
        .input-with-toggle input {{
            padding-right: 2.5rem;
        }}
        .password-toggle {{
            position: absolute;
            right: 0.8rem;
            cursor: pointer;
            user-select: none;
            color: #768390;
            font-size: 0.9rem;
        }}
        .password-toggle:hover {{
            color: #adbac7;
        }}
        
        @keyframes slideIn {{
            from {{
                opacity: 0;
                transform: translateY(-10px);
            }}
            to {{
                opacity: 1;
                transform: translateY(0);
            }}
        }}
        .notification-card.new-card {{
            animation: slideIn 0.3s cubic-bezier(0.4, 0, 0.2, 1) forwards;
        }}
    </style>
    <script>
        let currentPriority = 3;

        document.addEventListener('DOMContentLoaded', () => {{
            // Restore form collapsed/expanded state
            const isCollapsed = localStorage.getItem('publish_collapsed') !== 'false';
            const card = document.getElementById('publishCard');
            if (isCollapsed) {{
                card.classList.add('collapsed');
            }} else {{
                card.classList.remove('collapsed');
                document.getElementById('publishToggleIcon').innerText = '▲';
            }}
            
            // Restore priority
            const savedPriority = localStorage.getItem('publish_priority');
            if (savedPriority) {{
                setPriority(parseInt(savedPriority));
            }}
            
            // Restore auth token if exists
            const tokenInput = document.getElementById('pub-token');
            if (tokenInput) {{
                const savedToken = localStorage.getItem('publish_token');
                if (savedToken) {{
                    tokenInput.value = savedToken;
                }}
            }}
            
            // Start listening for real-time updates
            startLiveUpdates();
        }});

        function togglePublishForm() {{
            const card = document.getElementById('publishCard');
            const icon = document.getElementById('publishToggleIcon');
            const isCollapsed = card.classList.toggle('collapsed');
            icon.innerText = isCollapsed ? '▼' : '▲';
            localStorage.setItem('publish_collapsed', isCollapsed);
        }}

        function setPriority(level) {{
            currentPriority = level;
            localStorage.setItem('publish_priority', level);
            document.querySelectorAll('.priority-btn').forEach(btn => {{
                if (parseInt(btn.getAttribute('data-priority')) === level) {{
                    btn.classList.add('active');
                }} else {{
                    btn.classList.remove('active');
                }}
            }});
        }}

        function togglePasswordVisibility() {{
            const tokenInput = document.getElementById('pub-token');
            const toggleIcon = document.querySelector('.password-toggle');
            if (tokenInput.type === 'password') {{
                tokenInput.type = 'text';
                toggleIcon.innerText = '🔒';
            }} else {{
                tokenInput.type = 'password';
                toggleIcon.innerText = '👁️';
            }}
        }}

        async function submitPublishForm(event) {{
            event.preventDefault();
            
            const message = document.getElementById('pub-message').value;
            const title = document.getElementById('pub-title').value;
            const tags = document.getElementById('pub-tags').value;
            const click = document.getElementById('pub-click').value;
            const tokenInput = document.getElementById('pub-token');
            const token = tokenInput ? tokenInput.value : '';
            
            const statusDiv = document.getElementById('pub-status');
            const submitBtn = document.getElementById('pub-submit-btn');
            
            // Show loading state
            submitBtn.disabled = true;
            submitBtn.querySelector('span').innerText = 'Sending...';
            statusDiv.className = 'publish-status';
            statusDiv.style.display = 'none';
            
            // Save token
            if (token) {{
                localStorage.setItem('publish_token', token);
            }}
            
            const headers = {{
                'Content-Type': 'application/json'
            }};
            
            if (token) {{
                headers['Authorization'] = 'Bearer ' + token;
            }}
            
            const payload = {{
                message: message,
                priority: currentPriority
            }};
            if (title) payload.title = title;
            if (tags) payload.tags = tags.split(',').map(t => t.trim()).filter(t => t.length > 0);
            if (click) payload.click = click;
            
            try {{
                const response = await fetch('/{topic}', {{
                    method: 'POST',
                    headers: headers,
                    body: JSON.stringify(payload)
                }});
                
                if (response.ok) {{
                    statusDiv.innerText = 'Message published successfully!';
                    statusDiv.className = 'publish-status success';
                    
                    document.getElementById('pub-message').value = '';
                    
                    setTimeout(() => {{
                        window.location.reload();
                    }}, 1000);
                }} else {{
                    const text = await response.text();
                    statusDiv.innerText = 'Failed to publish (' + response.status + '): ' + text;
                    statusDiv.className = 'publish-status error';
                }}
            }} catch (err) {{
                statusDiv.innerText = 'Error: ' + err.message;
                statusDiv.className = 'publish-status error';
            }} finally {{
                submitBtn.disabled = false;
                submitBtn.querySelector('span').innerText = 'Send';
            }}
        }}

        async function startLiveUpdates() {{
            let maxId = 0;
            document.querySelectorAll('.notification-card').forEach(card => {{
                const idStr = card.id.replace('notif-', '');
                if (/^\\d+$/.test(idStr)) {{
                    const id = parseInt(idStr);
                    if (id > maxId) maxId = id;
                }}
            }});
            
            const seenIds = new Set();
            document.querySelectorAll('.notification-card').forEach(card => {{
                seenIds.add(card.id.replace('notif-', ''));
            }});

            const topic = '{topic}';
            let sinceParam = maxId > 0 ? maxId : 'all';
            
            async function connect() {{
                try {{
                    const response = await fetch(`/{topic}/json?since=${{sinceParam}}`);
                    if (!response.ok) throw new Error('Status ' + response.status);
                    
                    const reader = response.body.getReader();
                    const decoder = new TextDecoder();
                    let buffer = '';
                    
                    while (true) {{
                        const {{ value, done }} = await reader.read();
                        if (done) break;
                        
                        buffer += decoder.decode(value, {{ stream: true }});
                        const lines = buffer.split('\\n');
                        
                        buffer = lines.pop();
                        
                        for (const line of lines) {{
                            if (line.trim()) {{
                                try {{
                                    const data = JSON.parse(line);
                                    if (data.event === 'message') {{
                                        handleLiveMessage(data);
                                    }}
                                }} catch (e) {{
                                    console.error('Error parsing line:', e);
                                }}
                            }}
                        }}
                    }}
                }} catch (err) {{
                    console.warn('Live update connection lost, reconnecting in 5s...', err);
                    setTimeout(connect, 5000);
                }}
            }}
            
            function handleLiveMessage(msg) {{
                if (seenIds.has(msg.id)) return;
                seenIds.add(msg.id);
                
                if (/^\\d+$/.test(msg.id)) {{
                    const numId = parseInt(msg.id);
                    if (numId > sinceParam) {{
                        sinceParam = numId;
                    }}
                }}
                
                const emptyState = document.querySelector('.empty-state');
                if (emptyState) {{
                    emptyState.remove();
                }}
                
                const card = document.createElement('div');
                card.className = 'notification-card new-card';
                card.id = 'notif-' + msg.id;
                
                const priority = msg.priority || 3;
                
                const date = new Date(msg.time * 1000);
                const dtStr = date.getFullYear() + '-' + 
                    String(date.getMonth() + 1).padStart(2, '0') + '-' + 
                    String(date.getDate()).padStart(2, '0') + ' ' + 
                    String(date.getHours()).padStart(2, '0') + ':' + 
                    String(date.getMinutes()).padStart(2, '0') + ':' + 
                    String(date.getSeconds()).padStart(2, '0');
                    
                const priorityEmoji = getPriorityEmoji(priority);
                
                const linkify = (text) => {{
                    if (!text) return '';
                    const urlRegex = /(\\b(https?|ftp|file):\\/\\/[-A-Z0-9+&@#\\/%?=~_|!:,.;]*[-A-Z0-9+&@#\\/%=~_|])/ig;
                    return text.replace(urlRegex, function(url) {{
                        return '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + url + '</a>';
                    }});
                }};
                
                card.innerHTML = `
                    <div class="priority-indicator priority-${{priority}}"></div>
                    <div class="meta">
                        <span>${{priorityEmoji}} Priority ${{priority}}</span>
                        <span title="Server Timezone: {server_tz}">${{dtStr}}</span>
                    </div>
                    ${{msg.title ? `<span class="title">${{linkify(msg.title)}}</span>` : ''}}
                    <div class="message">${{linkify(msg.message)}}</div>
                    <span class="copy-btn" onclick="copyNotif('${{msg.id}}')" title="Copy to clipboard">📋</span>
                `;
                
                const list = document.querySelector('.notification-list');
                list.insertBefore(card, list.firstChild);
            }}
            
            function getPriorityEmoji(priority) {{
                switch(parseInt(priority)) {{
                    case 5: return '🔴';
                    case 4: return '🟠';
                    case 2: return '🔵';
                    case 1: return '⚪️';
                    default: return '🟢';
                }}
            }}
            
            connect();
        }}

        function copyText(text, btnElem) {{
            navigator.clipboard.writeText(text).then(() => {{
                const oldIcon = btnElem.innerText;
                btnElem.innerText = '✅';
                setTimeout(() => {{ btnElem.innerText = oldIcon; }}, 1500);
            }}).catch(err => {{
                console.error('Could not copy text: ', err);
            }});
        }}
        
        function copyNotif(id) {{
            const card = document.getElementById('notif-' + id);
            const titleElem = card.querySelector('.title');
            const msgElem = card.querySelector('.message');
            
            const title = titleElem ? titleElem.innerText : '';
            const message = msgElem ? msgElem.innerText : '';
            const fullText = title ? (title + '\\n' + message) : message;
            
            copyText(fullText, card.querySelector('.copy-btn'));
        }}
    </script>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="topic-path">
                <a href="{bot_url}" class="home-link">{display_host}</a>/<span class="topic-name">{topic}</span>
                <span class="copy-btn-inline" onclick="copyText('{topic_url}', this)" title="Copy URL">📋</span>
            </div>
            <div class="actions">
                <span class="copy-btn-inline" onclick="copyText('/sub {topic}', this)" title="Copy '/sub {topic}' command">📋</span>
                <a href="{subscribe_link}" class="btn-subscribe">Subscribe</a>
            </div>
        </div>

        <div class="publish-card collapsed" id="publishCard">
            <div class="publish-header" onclick="togglePublishForm()">
                <h3>✏️ Send Notification</h3>
                <span class="publish-toggle-icon" id="publishToggleIcon">▼</span>
            </div>
            <div class="publish-body">
                <form id="publishForm" onsubmit="submitPublishForm(event)">
                    <div class="form-group" style="margin-bottom: 1rem;">
                        <label for="pub-message">Message *</label>
                        <textarea class="form-control" id="pub-message" placeholder="Type your message here..." required></textarea>
                    </div>
                    
                    <div class="form-row" style="margin-bottom: 1rem;">
                        <div class="form-group">
                            <label for="pub-title">Title</label>
                            <input type="text" class="form-control" id="pub-title" placeholder="Optional title">
                        </div>
                        <div class="form-group">
                            <label>Priority</label>
                            <div class="priority-select-wrapper">
                                <button type="button" class="priority-btn" data-priority="1" onclick="setPriority(1)">Min</button>
                                <button type="button" class="priority-btn" data-priority="2" onclick="setPriority(2)">Low</button>
                                <button type="button" class="priority-btn active" data-priority="3" onclick="setPriority(3)">Default</button>
                                <button type="button" class="priority-btn" data-priority="4" onclick="setPriority(4)">High</button>
                                <button type="button" class="priority-btn" data-priority="5" onclick="setPriority(5)">Max</button>
                            </div>
                        </div>
                    </div>
                    
                    <div class="form-row" style="margin-bottom: 1rem;">
                        <div class="form-group">
                            <label for="pub-tags">Tags</label>
                            <input type="text" class="form-control" id="pub-tags" placeholder="e.g. warning, tag1, tag2">
                        </div>
                        <div class="form-group">
                            <label for="pub-click">Click URL</label>
                            <input type="url" class="form-control" id="pub-click" placeholder="https://example.com">
                        </div>
                    </div>
                    
                    {auth_field_html}
                    
                    <div id="pub-status" class="publish-status" style="margin-bottom: 1rem;"></div>
                    
                    <button type="submit" class="btn-publish-submit" id="pub-submit-btn">
                        <span>Send</span>
                    </button>
                </form>
            </div>
        </div>
        
        <div class="notification-list">
"""
    if not notifications:
        html += '<div class="empty-state">No notifications found for this topic in the last 24 hours.</div>'
    else:
        for n in notifications:
            dt = datetime.datetime.fromtimestamp(n['created_at']).strftime('%Y-%m-%d %H:%M:%S')
            priority = n['priority'] or 3
            priority_emoji = get_priority_emoji(str(priority))
            notif_id = n['id']
            
            html += f"""
            <div class="notification-card" id="notif-{notif_id}">
                <div class="priority-indicator priority-{priority}"></div>
                <div class="meta">
                    <span>{priority_emoji} Priority {priority}</span>
                    <span title="Server Timezone: {server_tz}">{dt}</span>
                </div>
                {f'<span class="title">{linkify(n["title"])}</span>' if n['title'] else ''}
                <div class="message">{linkify(n['message'])}</div>
                <span class="copy-btn" onclick="copyNotif({notif_id})" title="Copy to clipboard">📋</span>
            </div>
"""
            

    html += f"""
        </div>
    </div>
    <div class="footer">
        <a href="{bot_url}">This server</a> is running the <a href="https://github.com/mrgluek/deltachat_ntfy">Delta Chat Ntfy Bot</a>.
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

async def handle_ntfy_json(request):
    topic = request.match_info.get('topic')
    since = request.query.get('since', '')
    poll = request.query.get('poll', '0')
    
    response = web.StreamResponse(
        status=200,
        reason='OK',
        headers={
            'Content-Type': 'application/x-ndjson',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'Access-Control-Allow-Origin': '*'
        }
    )
    await response.prepare(request)
    
    # Send open event
    open_event = {
        "id": f"ntfy-{int(time.time()*1000)}",
        "time": int(time.time()),
        "event": "open",
        "topic": topic
    }
    await response.write(json.dumps(open_event).encode('utf-8') + b'\n')
    
    # Send historical messages
    if since:
        historical = database.get_messages_since(topic, since)
        for row in historical:
            msg_payload = {
                "id": str(row['id']),
                "time": row['created_at'],
                "event": "message",
                "topic": topic,
                "message": row['message']
            }
            if row['title']: msg_payload["title"] = row['title']
            if row['priority']: msg_payload["priority"] = row['priority']
            await response.write(json.dumps(msg_payload).encode('utf-8') + b'\n')
            
    if poll == '1':
        return response
        
    # Enter streaming loop
    q = asyncio.Queue()
    listeners[topic].append(q)
    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=15.0)
                await response.write(json.dumps(msg).encode('utf-8') + b'\n')
            except asyncio.TimeoutError:
                keepalive = {
                    "id": f"ntfy-{int(time.time()*1000)}",
                    "time": int(time.time()),
                    "event": "keepalive",
                    "topic": topic
                }
                await response.write(json.dumps(keepalive).encode('utf-8') + b'\n')
    except (asyncio.CancelledError, ConnectionError, aiohttp.ClientConnectionResetError):
        pass
    finally:
        if q in listeners[topic]:
            listeners[topic].remove(q)
            
    return response

async def publish_stats():
    topic = "stats"
    title = "Daily Server Stats"
    priority_int = 3
    priority_raw = "3"
    tags_raw = "bar_chart"
    
    last_24h = database.get_notifications_last_24h()
    
    message = f"📊 Ntfy Bot Statistics\n\nNotifications received in the last 24h: {last_24h}"
    
    # Save to database
    database.add_notification(topic, title, message, priority_int)

    # Pub/Sub push
    msg_payload = {
        "id": f"ntfy-{int(time.time()*1000)}",
        "time": int(time.time()),
        "event": "message",
        "topic": topic,
        "message": message,
        "title": title,
        "priority": priority_int,
        "tags": [tags_raw]
    }
    push_to_listeners(topic, msg_payload)

    # Broadcast to subscribers
    subscribers = database.get_subscribers(topic)
    if subscribers and dc_bot_instance and dc_accid is not None:
        for dc_chat_id in subscribers:
            success = False
            for acc_id in dc_bot_instance.rpc.get_all_account_ids():
                is_private = True
                try:
                    chat_info = dc_bot_instance.rpc.get_basic_chat_info(acc_id, dc_chat_id)
                    if isinstance(chat_info, dict):
                        is_private = (chat_info.get("chat_type", "") == "Single" or chat_info.get("type", 1) == 1)
                    else:
                        is_private = (getattr(chat_info, "chat_type", "") == "Single" or getattr(chat_info, "type", 1) == 1)
                except Exception:
                    continue

                try:
                    formatted_msg = format_notification(title, message, priority_raw, tags_raw, "", topic, is_private)
                    msg_data = MsgData(text=formatted_msg)
                    if not is_private:
                        msg_data.override_sender_name = f"#{topic}"
                    dc_bot_instance.rpc.send_msg(acc_id, dc_chat_id, msg_data)
                    success = True
                    break
                except Exception:
                    break

async def _stats_publisher_loop():
    while True:
        now = datetime.datetime.now()
        next_midnight = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        sleep_seconds = (next_midnight - now).total_seconds()
        
        logger.info(f"Stats publisher sleeping for {sleep_seconds} seconds until midnight.")
        await asyncio.sleep(sleep_seconds)
        
        try:
            await publish_stats()
        except Exception as e:
            logger.error(f"Failed to publish stats: {e}")

async def _run_web_server():
    global web_server_loop
    web_server_loop = asyncio.get_running_loop()
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
    app.router.add_get('/{topic}/json', handle_ntfy_json)
    app.router.add_get('/{topic}', handle_topic_view)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, '0.0.0.0', port)
    
    logger.info(f"Starting web server on 0.0.0.0:{port}...")
    await site.start()
    logger.info("Web server is UP and running.")
    
    # Start stats publisher
    asyncio.create_task(_stats_publisher_loop())
    
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

resilient_lock = threading.Lock()

def _setup_resilient_mode(bot):
    original_send_msg = bot.rpc.send_msg

    def patched_send_msg(account_id, chat_id, msg_data):
        try:
            is_resilient = database.get_config("resilient") == "1"
        except Exception:
            is_resilient = False

        if not is_resilient:
            return original_send_msg(account_id, chat_id, msg_data)

        try:
            transports = bot.rpc.list_transports(account_id)
        except Exception:
            transports = []

        if len(transports) <= 1:
            return original_send_msg(account_id, chat_id, msg_data)

        initial_addr = None
        try:
            initial_addr = bot.rpc.get_config(account_id, "configured_addr") or bot.rpc.get_config(account_id, "addr")
        except Exception:
            pass

        # 1. Send the message normally via the current primary transport (non-blocking queueing)
        try:
            msg_id = original_send_msg(account_id, chat_id, msg_data)
            bot.logger.info(f"Resilient send: initial msg queued with ID {msg_id} on transport {initial_addr}.")
        except Exception as send_err:
            bot.logger.error(f"Resilient send: failed to queue initial message: {send_err}")
            return None

        # Background worker to handle resending to other transports sequentially
        def bg_resend_worker(m_id, init_addr, t_list):
            bot.logger.info(f"Resilient send: starting background sender for msg {m_id}")
            with resilient_lock:
                bot.logger.info(f"Resilient send bg: waiting for initial delivery of msg {m_id} on {init_addr}...")
                start_time = time.time()
                delivered = False
                while time.time() - start_time < 10:
                    try:
                        msg_snapshot = bot.rpc.get_message(account_id, m_id)
                        state = msg_snapshot.get('state') if isinstance(msg_snapshot, dict) else getattr(msg_snapshot, 'state', None)
                        if state in (26, 28):
                            bot.logger.info(f"Resilient send bg: initial msg {m_id} delivered successfully on {init_addr}.")
                            delivered = True
                            break
                        if state == 24:
                            bot.logger.warning(f"Resilient send bg: initial msg {m_id} failed on {init_addr}.")
                            break
                    except Exception as poll_err:
                        bot.logger.debug(f"Resilient send bg initial poll error: {poll_err}")
                    time.sleep(0.5)

                if not delivered:
                    bot.logger.warning(f"Resilient send bg: initial msg {m_id} did not deliver on {init_addr} within timeout.")

                # 2. Resend on all other transports
                for t in t_list:
                    t_addr = t.get('addr') if isinstance(t, dict) else getattr(t, 'addr', None)
                    if not t_addr or (init_addr and t_addr.lower() == init_addr.lower()):
                        continue

                    bot.logger.info(f"Resilient send bg: switching primary transport to {t_addr}")
                    try:
                        bot.rpc.set_config(account_id, "configured_addr", t_addr)
                        time.sleep(1)
                    except Exception as switch_err:
                        bot.logger.error(f"Resilient send bg: failed to switch transport to {t_addr}: {switch_err}")
                        continue

                    try:
                        bot.logger.info(f"Resilient send bg: resending msg {m_id} on transport {t_addr}...")
                        bot.rpc.resend_messages(account_id, [m_id])

                        # Wait up to 10 seconds for the resent message to be delivered/failed
                        start_time = time.time()
                        delivered = False
                        while time.time() - start_time < 10:
                            try:
                                msg_snapshot = bot.rpc.get_message(account_id, m_id)
                                state = msg_snapshot.get('state') if isinstance(msg_snapshot, dict) else getattr(msg_snapshot, 'state', None)
                                if state in (26, 28):
                                    bot.logger.info(f"Resilient send bg: msg {m_id} delivered successfully on {t_addr}.")
                                    delivered = True
                                    break
                                if state == 24:
                                    bot.logger.warning(f"Resilient send bg: msg {m_id} failed on {t_addr}.")
                                    break
                            except Exception as poll_err:
                                bot.logger.debug(f"Resilient send bg poll error: {poll_err}")
                            time.sleep(0.5)

                        if not delivered:
                            bot.logger.warning(f"Resilient send bg: msg {m_id} did not deliver on {t_addr} within timeout.")
                    except Exception as resend_err:
                        bot.logger.error(f"Resilient send bg: failed to resend message on transport {t_addr}: {resend_err}")

                # 3. Restore the initial primary transport configuration
                if init_addr:
                    try:
                        bot.logger.info(f"Resilient send bg: restoring initial primary transport to {init_addr}")
                        bot.rpc.set_config(account_id, "configured_addr", init_addr)
                    except Exception as restore_err:
                        bot.logger.error(f"Resilient send bg: failed to restore transport to {init_addr}: {restore_err}")

        # Start the background thread for resilient sending
        threading.Thread(target=bg_resend_worker, args=(msg_id, initial_addr, transports), daemon=True).start()

        return msg_id

    bot.rpc.send_msg = patched_send_msg


_message_failover_attempts = {}

@dc_cli.on(events.RawEvent(events.EventType.MSG_FAILED))
def on_msg_failed(bot, accid, event):
    """Handle message sending failures by switching to a backup transport temporarily with backoff."""
    try:
        if database.get_config("resilient") == "1":
            return
    except Exception:
        pass

    msg_id = getattr(event, 'msg_id', None)
    if not msg_id:
        return

    try:
        global _message_failover_attempts
        if len(_message_failover_attempts) > 1000:
            _message_failover_attempts.clear()

        # Retrieve or initialize tracking state for this message
        state = _message_failover_attempts.get(msg_id)
        if state is None:
            state = {'count': 0, 'transports': set()}
            _message_failover_attempts[msg_id] = state

        # Stop retrying if we reached the maximum attempt limit (e.g. 10 attempts)
        if state['count'] >= 10:
            return

        state['count'] += 1

        # Retrieve message and verify it is indeed in failed state (state 24)
        try:
            msg_snapshot = bot.rpc.get_message(accid, msg_id)
            msg_state = msg_snapshot.get('state') if isinstance(msg_snapshot, dict) else getattr(msg_snapshot, 'state', None)
            if msg_state != 24:
                return
        except Exception:
            return

        # Fetch chat details to include in logs (checking both snake_case and camelCase key fallbacks)
        chat_id = None
        if isinstance(msg_snapshot, dict):
            chat_id = msg_snapshot.get('chat_id') or msg_snapshot.get('chatId')
        else:
            chat_id = getattr(msg_snapshot, 'chat_id', getattr(msg_snapshot, 'chatId', None))
            
        chat_name = "Unknown"

        if chat_id:
            try:
                chat_info = bot.rpc.get_full_chat_by_id(accid, chat_id)
                if isinstance(chat_info, dict):
                    chat_name = chat_info.get('name', 'Unknown')
                else:
                    chat_name = getattr(chat_info, 'name', 'Unknown')
            except Exception:
                pass

        # Check if it's a permanent E2E encryption failure
        msg_error = msg_snapshot.get('error') if isinstance(msg_snapshot, dict) else getattr(msg_snapshot, 'error', None)
        if msg_error:
            msg_error_lower = msg_error.lower()
            if "encryption" in msg_error_lower or "unencrypted" in msg_error_lower or "шифр" in msg_error_lower or "зашифр" in msg_error_lower:
                bot.logger.warning(
                    f"Permanent E2E encryption failure for message {msg_id} in chat '{chat_name}' (ID: {chat_id}): {msg_error}. "
                    f"Stopping failover attempts immediately."
                )
                return

        # List all configured transports
        try:
            transports = bot.rpc.list_transports(accid)
        except Exception:
            transports = []

        if len(transports) <= 1:
            bot.logger.info(f"Message {msg_id} failed to send, but only {len(transports)} transport(s) configured. Cannot failover.")
            return

        current_addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr")
        if not current_addr:
            return

        # Find current transport index
        current_idx = -1
        for idx, t in enumerate(transports):
            t_addr = t.get('addr') if isinstance(t, dict) else getattr(t, 'addr', None)
            if t_addr and t_addr.lower() == current_addr.lower():
                current_idx = idx
                break

        if current_idx == -1:
            bot.logger.warning(f"Current transport {current_addr} not found in transports list.")
            current_idx = 0

        # Try to find the next transport
        next_idx = (current_idx + 1) % len(transports)
        next_t = transports[next_idx]
        next_addr = next_t.get('addr') if isinstance(next_t, dict) else getattr(next_t, 'addr', None)

        if not next_addr or next_addr.lower() == current_addr.lower():
            bot.logger.info("No alternative transport available for failover.")
            return

        # Check if we have already tried this transport for this message
        if next_addr.lower() in state['transports']:
            if len(state['transports']) >= len(transports):
                bot.logger.warning(f"All available transports have been tried for message {msg_id}. Stopping failover.")
                return

        state['transports'].add(current_addr.lower())

        # Calculate exponential backoff delay: 5, 10, 20, 40, 80, 160... seconds (max 5 minutes)
        delay = min(300, 5 * (2 ** (state['count'] - 1)))
        bot.logger.warning(
            f"Resilient Failover: Message {msg_id} (Chat: {chat_name}, ID: {chat_id}) failed on {current_addr} (attempt {state['count']}/10). "
            f"Scheduling resend on transport {next_addr} in {delay}s."
        )

        init_addr = current_addr

        # Schedule the resend asynchronously using a non-blocking Timer thread
        def delayed_resend():
            try:
                bot.logger.info(f"Executing scheduled resend for message {msg_id} in chat '{chat_name}' (ID: {chat_id}) on transport {next_addr}...")
                with resilient_lock:
                    # Switch configured_addr to next transport temporarily
                    bot.rpc.set_config(accid, "configured_addr", next_addr)
                    time.sleep(1) # Give core a moment to reconfigure
                    
                    bot.rpc.resend_messages(accid, [msg_id])
                    
                    # Wait up to 10 seconds for the resent message to be delivered/failed
                    start_time = time.time()
                    delivered = False
                    while time.time() - start_time < 10:
                        try:
                            raw_msg = bot.rpc.get_message(accid, msg_id)
                            if raw_msg:
                                from deltachat2 import AttrDict
                                msg_snapshot = AttrDict(raw_msg)
                                state = msg_snapshot.get('state') if isinstance(msg_snapshot, dict) else getattr(msg_snapshot, 'state', None)
                                if state in (26, 28):
                                    bot.logger.info(f"Resilient Failover bg: msg {msg_id} delivered successfully on {next_addr}.")
                                    delivered = True
                                    break
                                if state == 24:
                                    bot.logger.warning(f"Resilient Failover bg: msg {msg_id} failed on {next_addr}.")
                                    break
                        except Exception as poll_err:
                            bot.logger.debug(f"Resilient Failover bg poll error: {poll_err}")
                        time.sleep(0.5)

                    if not delivered:
                        bot.logger.warning(f"Resilient Failover bg: msg {msg_id} did not deliver on {next_addr} within timeout.")

            except Exception as resend_err:
                bot.logger.warning(f"Error executing scheduled resend for message {msg_id} in chat '{chat_name}' (ID: {chat_id}): {resend_err}")
                err_str = str(resend_err).lower()
                if "e2e encryption" in err_str or "encryption" in err_str:
                    bot.logger.warning(f"E2E encryption error detected during resend of msg {msg_id} in chat '{chat_name}'. Stopping further failovers.")
                    try:
                        _message_failover_attempts[msg_id]['count'] = 10
                    except Exception:
                        pass
            finally:
                # Always restore the initial primary transport address!
                try:
                    bot.logger.info(f"Resilient Failover bg: restoring primary transport to {init_addr}")
                    bot.rpc.set_config(accid, "configured_addr", init_addr)
                except Exception as restore_err:
                    bot.logger.error(f"Resilient Failover bg: failed to restore transport to {init_addr}: {restore_err}")

        import threading
        threading.Timer(delay, delayed_resend).start()

        # Send a warning message to the administrator about the failover (only on the first failure)
        if state['count'] == 1:
            admin_email = database.get_config("admin_dc_email")
            if admin_email:
                try:
                    contact_id = bot.rpc.create_contact(accid, admin_email, "Admin")
                    admin_chat_id = bot.rpc.create_chat_by_contact_id(accid, contact_id)
                    # Protect against infinite admin alert loops if the admin alert itself fails to send
                    if admin_chat_id and chat_id != admin_chat_id:
                        _dc_send_msg_with_stats(bot, accid, admin_chat_id, MsgData(text=f"⚠️ **Transport Failover Alert**\n\nMessage delivery failed on `{current_addr}`.\nScheduled temporary resend on `{next_addr}`."))
                except Exception as admin_err:
                    bot.logger.error(f"Failed to send failover alert to admin: {admin_err}")

    except Exception as e:
        bot.logger.error(f"Error handling message failover for message {msg_id}: {e}")


@dc_cli.on_init
def on_init(bot, args):
    """Called when the Delta Chat bot starts."""
    global dc_bot_instance, dc_accid
    bot.logger.info("Initializing Delta Chat ntfy bot...")
    
    dc_bot_instance = bot
    _setup_resilient_mode(bot)
    
    for accid in bot.rpc.get_all_account_ids():
        dc_accid = accid
        bot.rpc.set_config(accid, "displayname", "Ntfy Bot")
        bot.rpc.set_config(accid, "selfstatus", "A Delta Chat bot that emulates a ntfy.sh backend to broadcast notifications from HTTP POST requests to Delta Chat users and groups: https://github.com/mrgluek/deltachat_ntfy")
        # Auto-delete messages after 24 hours to save disk space
        bot.rpc.set_config(accid, "delete_device_after", "86400")
        try:
            bot.rpc.set_config(accid, "download_limit", "1")
            bot.logger.info("Configured auto-download limit (1 byte) in on_init.")
        except Exception as e:
            bot.logger.warning(f"Could not configure storage optimization in on_init: {e}")
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
            bot.rpc.set_config(dc_accid, "download_limit", "1")
            bot.rpc.set_config(dc_accid, "delete_device_after", "86400")
            bot.logger.info("Successfully set auto-download limit to 1 byte and delete_device_after to 24 hours to optimize storage.")
        except Exception as e:
            bot.logger.error(f"Failed to set storage optimization settings in on_start: {e}")
        
        # Show configured admin and transports
        admin_email = database.get_config("admin_dc_email")
        admin_fp = database.get_admin_fingerprint()
        if admin_email:
            fp_suffix = f" ({admin_fp[-8:].upper()})" if admin_fp else ""
            print(f"Bot Administrator: {admin_email}{fp_suffix}")
            
        try:
            transports = bot.rpc.list_transports(dc_accid)
            print("\n" + "=" * 50)
            print("Configured Bot Transports (Relays):")
            for t in transports:
                addr = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
                print(f" - {addr}")
        except Exception:
            pass

        try:
            qrdata = bot.rpc.get_chat_securejoin_qr_code(dc_accid, None)
            print("\nTo add this bot, scan the QR code or copy the link below:\n")

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

def _get_contact_fingerprint(bot, accid, contact_id, contact=None):
    """Returns the cryptographic fingerprint of a contact, trying various RPC methods and signatures."""
    self_fps = set()
    try:
        bot_addrs = []
        bot_addr = bot.rpc.get_config(accid, "addr")
        if bot_addr: bot_addrs.append(bot_addr.lower().strip())
            
        try:
            transports = bot.rpc.list_transports(accid)
            for t in transports:
                t_addr = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
                if t_addr: bot_addrs.append(t_addr.lower().strip())
        except: pass
        
        if bot_addrs:
            for args in [(accid, contact_id), (contact_id,)]:
                try:
                    enc_info_self = bot.rpc.get_contact_encryption_info(*args)
                    if enc_info_self:
                        blocks = re.split(r'\n\s*\n', enc_info_self.strip())
                        for block in blocks:
                            if any(a in block.lower() for a in bot_addrs):
                                matches = re.findall(r'[0-9a-fA-F]{32,64}', "".join(block.split()).replace(':', ''))
                                self_fps.update(m.upper() for m in matches)
                        break
                except Exception:
                    continue
        if self_fps:
            bot.logger.debug(f"Detected bot's own fingerprints from enc_info: {[f[-8:] for f in self_fps]}")
    except Exception as e:
        bot.logger.error(f"Error detecting self-fingerprint: {e}")

    # 1. Try directly from the contact object if available
    if contact:
        # The contact object from RPC is often a dict-like object
        get_val = getattr(contact, 'get', lambda k: getattr(contact, k, None))
        for attr in ['fingerprint', 'key_fingerprint', 'public_key']:
            val = get_val(attr)
            if val:
                matches = re.findall(r'[0-9a-fA-F]{32,64}', str(val).replace(' ', '').replace(':', ''))
                valid_matches = [m.upper() for m in matches if m.upper() not in self_fps]
                if valid_matches:
                    fps = ",".join(valid_matches)
                    bot.logger.debug(f"Found fingerprint(s) in contact.{attr}: {fps}")
                    return fps

    # 2. Try get_contact_config(accid, contact_id, "fp")
    try:
        fp = bot.rpc.get_contact_config(accid, contact_id, "fp")
        if fp and fp.upper().replace(' ', '') not in self_fps:
            bot.logger.debug(f"Found fingerprint in contact config 'fp': {fp}")
            return fp.upper().replace(' ', '')
    except Exception:
        pass

    for args in [(accid, contact_id), (contact_id,)]:
        try:
            # Try positional arguments first
            enc_info = bot.rpc.get_contact_encryption_info(*args)
            if enc_info:
                # Clean ALL whitespace including newlines
                cleaned_info = "".join(enc_info.split()).replace(':', '')
                # Look for hex strings between 32 and 64 characters (handles SHA-1 and Ed25519)
                matches = re.findall(r'[0-9a-fA-F]{32,64}', cleaned_info)
                # Filter out bot's own fingerprints
                valid_matches = [m.upper() for m in matches if m.upper() not in self_fps]
                if valid_matches:
                    # Return all valid matches joined by comma
                    fps = ",".join(valid_matches)
                    bot.logger.debug(f"Found fingerprint(s): {fps}")
                    return fps
        except Exception as e:
            bot.logger.debug(f"get_contact_encryption_info{args} failed: {e}")
            continue
            
    return None

def _is_dc_admin(bot, accid, contact_id):
    """Check if the given contact is the bot administrator (by email or fingerprint)."""
    try:
        contact = None
        try:
            contact = bot.rpc.get_contact(accid, contact_id)
        except Exception:
            pass
        
        # 1. Check fingerprint (strongest)
        admin_fp = database.get_admin_fingerprint()
        if admin_fp:
            c_fp = _get_contact_fingerprint(bot, accid, contact_id, contact=contact)
            bot.logger.debug(f"Admin check (fp): stored={admin_fp}, current={c_fp}")
            if c_fp:
                # c_fp might be a comma-separated list if multiple keys were found
                if admin_fp.upper() in c_fp.upper().split(','):
                    return True
            
            # If fingerprint is set but didn't match, we REJECT even if email matches (security)
            if c_fp:
                 bot.logger.warning(f"Admin fingerprint mismatch for {contact_id}")
                 return False
        
        # 2. Check email (legacy or initial setup)
        if contact:
            sender_email = contact.address
            admin_email = database.get_config("admin_dc_email")
            if admin_email and admin_email.lower() == sender_email.lower():
                bot.logger.debug(f"Admin check (email): stored={admin_email}, current={sender_email}")
                return True
            
    except Exception as e:
        bot.logger.error(f"Error during admin verification: {e}")
    return False

def get_help_text(bot, accid, from_id):
    contact = bot.rpc.get_contact(accid, from_id)
    sender_email = contact.address
    admin_email = database.get_config("admin_dc_email")
    
    bot_url = database.get_config("bot_url") or "https://ntfy.gluek.info"
    
    help_text = (
        f"👋 Hi {sender_email}!\n\n"
        f"I'm the Ntfy Bot hosted at {bot_url}\n\n"
        f"I receive HTTP POST requests and broadcast them to subscribed topics.\n\n"
        f"Try me with `/sub test` and then send message with curl:\n"
        f"`curl -d \"Hello from ntfy\" {bot_url}/test`\n\n"
        f"**Commands:**\n"
        f"/sub <topic> — Subscribe to a topic\n"
        f"/unsub <topic> — Unsubscribe from a topic\n"
        f"/send <topic> [**title**] <msg> — Post a message to a topic\n"
        f"/list — Show subscribed topics\n"
        f"/last — Show last 5 notifications\n"
        f"/stats — Show bot statistics\n"
        f"/newgroup [name] — Create a dedicated group chat\n"
        f"/donate — Support bot development ❤️\n"
        f"/help — Show this help message\n\n"
        f"💡 _You can also reply to any notification to post back to its topic._\n\n"
    )
    
        
    is_actually_admin = _is_dc_admin(bot, accid, from_id)
    if not admin_email:
        help_text += (
            f"**Initialisation Command:**\n"
            f"/initadmin — Claim bot ownership (if no admin is set)\n\n"
        )
    elif is_actually_admin:
        admin_fp = database.get_admin_fingerprint()
        fp_suffix = f" ({admin_fp[-8:].upper()})" if admin_fp else ""
        help_text += f"👑 **Admin:** `{admin_email}`{fp_suffix}\n\n"
        help_text += (
            f"**Admin Commands:**\n"
            f"/accounts — List configured bot accounts\n"
            f"/rmaccount <id> — Delete a bot account\n"
            f"/url <url> — Set the bot's public URL\n"
            f"/transports — Show configured mail relays & stats\n"
            f"/addtransport — Add a backup mail relay\n"
            f"/rmtransport <addr> — Remove a mail relay\n"
            f"/setprimary <addr> — Manually switch the primary relay\n"
            f"/resilient — Toggle resilient sending mode (all relays)\n\n"
        )
        
    help_text += f"Run your own bot: https://github.com/mrgluek/deltachat_ntfy"
    return help_text

@dc_cli.on(events.NewMessage(command="/help"))
def help_command(bot, accid, event):
    msg = event.msg
    help_text = get_help_text(bot, accid, msg.from_id)
    _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=help_text))

@dc_cli.on(events.NewMessage)
def on_new_message(bot, accid, event):
    msg = event.msg
    if msg.is_info:
        return
        
    # Track receiving stats
    try:
        # Use configured_addr (SMTP override) if set, otherwise fallback to main account addr
        addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr")
        if addr:
            database.increment_transport_received(addr)
    except Exception:
        pass

    text = (msg.text or "").strip()
    
    # 1. URL detection for subscription (e.g. https://ntfy.gluek.info/topic)
    bot_url = database.get_config("bot_url") or "https://ntfy.gluek.info"
    base_url = bot_url.rstrip('/')
    
    if text.startswith(base_url + "/"):
        topic = text[len(base_url)+1:].strip()
        # Basic validation: topic should not have spaces or further slashes
        if topic and " " not in topic and "/" not in topic:
            if database.subscribe(msg.chat_id, topic):
                bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=f"✅ Subscribed to '{topic}' via link"))
                bot.logger.info(f"Subscribed chat {msg.chat_id} to '{topic}' via URL detection")
            else:
                bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=f"ℹ️ Already subscribed to '{topic}'"))
            return

    # 2. Reply-to-notification: if user replies to a bot message with [topic] prefix, post to that topic
    try:
        quote = getattr(msg, 'quote', None)
        if quote and text and not text.startswith('/'):
            quote_text = getattr(quote, 'text', '') or ''
            # format_notification puts `[topic]` at the start for private chats
            topic_match = re.match(r'^`\[(.+?)\]`', quote_text)
            if topic_match:
                reply_topic = topic_match.group(1)
                _publish_from_chat(bot, accid, reply_topic, text, msg.chat_id)
                bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=f"✅ Sent to `{reply_topic}`"))
                return
    except Exception as e:
        bot.logger.error(f"Error in reply-to-notification check: {e}")

    # 3. Detect new users in private chats and send welcome
    try:
        chat_info = bot.rpc.get_basic_chat_info(accid, msg.chat_id)
        
        # Safe check for chat type
        is_private = False
        if isinstance(chat_info, dict):
            is_private = (chat_info.get("type") == 1)
        else:
            is_private = (getattr(chat_info, "type", 1) == 1)
            
        if is_private:
            greeted_key = f"greeted_{msg.from_id}"
            if not database.get_config(greeted_key):
                bot.logger.info(f"New user detected, sending welcome to chat {msg.chat_id}")
                help_text = get_help_text(bot, accid, msg.from_id)
                bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=help_text))
                database.set_config(greeted_key, "1")
    except Exception as e:
        bot.logger.error(f"Error in greeting check: {e}")

def _publish_from_chat(bot, accid, topic, message, sender_chat_id, title=""):
    """Publish a message to a topic from a Delta Chat command/reply.
    Saves to DB, pushes to Pub/Sub listeners, and broadcasts to all subscribers.
    """
    priority_int = 3
    priority_raw = "3"
    tags_raw = ""
    click_raw = ""

    # Save to database
    database.add_notification(topic, title, message, priority_int)

    # Pub/Sub push for JSON streaming clients
    msg_payload = {
        "id": f"ntfy-{int(time.time()*1000)}",
        "time": int(time.time()),
        "event": "message",
        "topic": topic,
        "message": message
    }
    if title:
        msg_payload["title"] = title
        
    push_to_listeners(topic, msg_payload)

    # Broadcast to all subscribers
    subscribers = database.get_subscribers(topic)
    if subscribers and dc_bot_instance and dc_accid is not None:
        for dc_chat_id in subscribers:
            # Skip the sender's chat to avoid echo
            if dc_chat_id == sender_chat_id:
                continue

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
                except Exception:
                    continue

                try:
                    formatted_msg = format_notification(title, message, priority_raw, tags_raw, click_raw, topic, is_private)
                    msg_data = MsgData(text=formatted_msg)
                    if not is_private:
                        msg_data.override_sender_name = f"#{topic}"
                    dc_bot_instance.rpc.send_msg(acc_id, dc_chat_id, msg_data)
                    
                    # Track sending stats
                    try:
                        addr = dc_bot_instance.rpc.get_config(acc_id, "configured_addr") or dc_bot_instance.rpc.get_config(acc_id, "addr")
                        if addr:
                            database.increment_transport_sent(addr)
                    except Exception:
                        pass
                        
                    success = True
                    break
                except Exception as e:
                    logger.error(f"_publish_from_chat: failed to send to {dc_chat_id} on {acc_id}: {e}")
                    break

            if not success:
                logger.warning(f"_publish_from_chat: could not find chat {dc_chat_id} on any account.")


@dc_cli.on(events.NewMessage(command="/send"))
def send_command(bot, accid, event):
    """Post a message to an ntfy topic: /send <topic> <message>"""
    msg = event.msg
    payload = event.payload.strip()

    if not payload or " " not in payload:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="Usage: /send <topic> [**title**] <message>"))
        return

    topic, rest = payload.split(" ", 1)
    rest = rest.strip()
    if not rest:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="Usage: /send <topic> [**title**] <message>"))
        return

    # Parse optional **title** at the beginning
    title = ""
    title_match = re.match(r'^\*\*(.+?)\*\*\s*(.*)', rest, re.DOTALL)
    if title_match:
        title = title_match.group(1).strip()
        message = title_match.group(2).strip()
        if not message:
            # Only title, no body — use title as message too
            message = title
    else:
        message = rest

    _publish_from_chat(bot, accid, topic, message, msg.chat_id, title=title)
    confirm = f"✅ Sent to `{topic}`"
    if title:
        confirm += f" (title: {title})"
    bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=confirm))


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
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=reply_text))
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
    _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=support_msg))


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
    _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=reply))

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
        
    bot_url = database.get_config("bot_url") or "https://ntfy.gluek.info"
    base_url = bot_url.rstrip('/')
    topics_list = "\n".join([f"- [{t}]({base_url}/{t})" for t in topics])
    _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"📋 Subscribed topics:\n{topics_list}"))

@dc_cli.on(events.NewMessage(command="/last"))
def last_command(bot, accid, event):
    msg = event.msg
    topics = database.get_subscriptions(msg.chat_id)
    
    if not topics:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="You are not subscribed to any topics, so there are no notifications."))
        return
        
    recent = database.get_recent_notifications(topics, limit=5)
    
    lines = ["🕒 **Last 5 Notifications**\n"]
    for notif in recent:
        emoji = get_priority_emoji(str(notif['priority']))
        title_str = f"**{notif['title']}** " if notif['title'] else ""
        lines.append(f"{emoji} [{notif['topic']}] {title_str}\n{notif['message']}")
        lines.append("---")
        
    # Remove last separator
    if lines and lines[-1] == "---":
        lines.pop()
        
    _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="\n".join(lines)))

def _dc_send_msg_with_stats(bot, accid, chat_id, msg_data):
    """Wrapper for bot.rpc.send_msg that tracks stats."""
    try:
        msg_id = bot.rpc.send_msg(accid, chat_id, msg_data)
        
        # Track success
        try:
            addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr") or "unknown"
            if addr != "unknown":
                database.increment_transport_sent(addr)
        except Exception:
            pass
        
        bot.logger.info(f"Successfully sent msg_id {msg_id} to chat {chat_id} on account {accid}")
        return msg_id
    except Exception as e:
        bot.logger.error(f"Failed to send DC message to chat {chat_id} on account {accid}: {e}")
        raise e

@dc_cli.on(events.NewMessage(command="/setprimary"))
def setprimary_command(bot, accid, event):
    """Set a specific transport as primary. Admin only."""
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can use /setprimary."))
        return

    addr = event.payload.strip()
    if not addr:
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="Usage: /setprimary user@example.com"))
        return

    try:
        bot.rpc.set_config(accid, "configured_addr", addr)
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"✅ Primary address (`configured_addr`) is now `{addr}`."))
    except Exception as e:
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"❌ Failed to set primary address: {e}"))
        return

@dc_cli.on(events.NewMessage(command="/resilient"))
def resilient_command(bot, accid, event):
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Only the bot administrator can use /resilient."))
        return

    arg = event.payload.strip().lower() if event.payload else ""

    try:
        current = database.get_config("resilient") == "1"
        if not arg:
            status = "enabled" if current else "disabled"
            _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"ℹ️ Resilient sending mode is currently {status}."))
            return

        if arg in ("on", "1", "true"):
            database.set_config("resilient", "1")
            _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="✅ Resilient sending mode enabled. Each outgoing message will be sent via all connected transports."))
        elif arg in ("off", "0", "false"):
            database.set_config("resilient", "0")
            _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Resilient sending mode disabled."))
        else:
            _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ Invalid argument. Use '/resilient on', '/resilient off', or '/resilient' to get status."))
    except Exception as e:
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"❌ Failed to update resilient mode: {e}"))

@dc_cli.on(events.NewMessage(command="/stats"))
def stats_command(bot, accid, event):
    msg = event.msg
    
    last_24h = database.get_notifications_last_24h()
    
    reply = f"📊 **Ntfy Bot Statistics**\n\nNotifications received in the last 24h: {last_24h}"
    _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=reply))

@dc_cli.on(events.NewMessage(command="/url"))
def url_command(bot, accid, event):
    msg = event.msg
    contact = bot.rpc.get_contact(accid, msg.from_id)
    sender_email = contact.address
    
    admin_email = database.get_config("admin_dc_email")
    if not admin_email or admin_email.lower() != sender_email.lower():
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="❌ This command is only for the administrator."))
        return
        
    url = event.payload.strip()
    if not url:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="❌ Usage: /url <https://your-bot-url.com>"))
        return
    
    database.set_config("bot_url", url)
    _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=f"✅ Bot URL updated to: {url}"))

@dc_cli.on(events.NewMessage(command="/transports"))
def transports_command(bot, accid, event):
    """Show configured transports (mail relays) and their status."""
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ This command is only for the administrator."))
        return

    try:
        transports = bot.rpc.list_transports(accid)
    except Exception as e:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=f"❌ Failed to list transports: {e}"))
        return

    if not transports:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="No transports configured."))
        return

    # Get connectivity status
    connectivity_label = "❓ Unknown"
    try:
        connectivity = bot.rpc.get_connectivity(accid)
        if connectivity >= 4000:
            connectivity_label = "🟢 Connected"
        elif connectivity >= 3000:
            connectivity_label = "🔄 Working"
        elif connectivity >= 2000:
            connectivity_label = "🟡 Connecting"
        else:
            connectivity_label = "🔴 Not connected"
    except Exception:
        pass

    # Get connectivity HTML to parse per-transport status
    connectivity_html = ""
    try:
        connectivity_html = bot.rpc.get_connectivity_html(accid)
    except Exception:
        pass

    # Get resilient sending mode status
    resilient_on = False
    try:
        resilient_on = database.get_config("resilient") == "1"
    except Exception:
        pass

    # Get per-transport statistics from database
    stats_map = {}
    for s in database.get_all_transport_stats():
        stats_map[s['addr']] = s

    active_addr = bot.rpc.get_config(accid, "configured_addr") or bot.rpc.get_config(accid, "addr")
    transport_addrs = []
    for t in transports:
        addr = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
        transport_addrs.append(addr)

    reply = f"🔌 **Mail Relays (Transports)**\n\nStatus: {connectivity_label}\n\n"

    import re
    for addr in transport_addrs:
        # Determine status label from HTML
        status_label = "❓ Unknown"
        if connectivity_html:
            domain = addr.split('@')[-1] if '@' in addr else addr
            pattern = rf'class="([^"]+)\s+dot".*?<b>{re.escape(domain)}:</b>\s*([^<]+)'
            match = re.search(pattern, connectivity_html, re.IGNORECASE)
            if match:
                color = match.group(1).lower()
                status_text = match.group(2).strip().lower()
                if "yellow" in color or "connecting" in status_text:
                    status_label = "🟡 Connecting"
                elif "green" in color:
                    status_label = "🔄 Working"
                elif "red" in color or "lost" in status_text or "error" in status_text:
                    status_label = "🔴 Not connected"

        is_used = resilient_on or (addr == active_addr)
        used_str = " ✔︎ Used for sending:" if is_used else ":"
        reply += f"**{status_label}**{used_str} `{addr}`\n"

        stats = stats_map.get(addr)
        if stats:
            reply += f"  📤 Sent: {stats['msgs_sent']}  📥 Received: {stats['msgs_received']}\n"
            if stats.get('last_sent_at'):
                import datetime
                last_sent = datetime.datetime.fromtimestamp(stats['last_sent_at']).strftime('%Y-%m-%d %H:%M')
                reply += f"  Last sent: {last_sent}\n"
            if stats.get('last_received_at'):
                import datetime
                last_recv = datetime.datetime.fromtimestamp(stats['last_received_at']).strftime('%Y-%m-%d %H:%M')
                reply += f"  Last received: {last_recv}\n"
        else:
            reply += f"  📤 Sent: 0  📥 Received: 0\n"
        reply += "\n"

    reply += f"Total transports: {len(transport_addrs)}"
    _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text=reply))

@dc_cli.on(events.NewMessage(command="/addtransport"))
def addtransport_command(bot, accid, event):
    """Add a backup mail relay (transport). Admin only."""
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ This command is only for the administrator."))
        return

    payload = event.payload.strip()
    if not payload:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(
            text="Usage:\n"
                 "/addtransport DCACCOUNT:server.example\n"
                 "/addtransport user@example.com password123"
        ))
        return

    try:
        if payload.startswith("DCACCOUNT:"):
            # QR-style chatmail URI
            bot.rpc.add_transport_from_qr(accid, payload)
            bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=f"✅ Backup transport added via chatmail URI."))
        else:
            parts = payload.split(None, 1)
            if len(parts) < 2:
                bot.rpc.send_msg(accid, msg.chat_id, MsgData(
                    text="❌ For email accounts, provide both address and password:\n"
                         "/addtransport user@example.com password123"
                ))
                return
            addr, password = parts[0], parts[1]
            bot.rpc.add_or_update_transport(accid, {"addr": addr, "password": password})
            bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=f"✅ Backup transport `{addr}` added."))
    except Exception as e:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=f"❌ Failed to add transport: {e}"))

@dc_cli.on(events.NewMessage(command="/rmtransport"))
def rmtransport_command(bot, accid, event):
    """Remove a mail relay (transport). Admin only."""
    msg = event.msg
    if not _is_dc_admin(bot, accid, msg.from_id):
        _dc_send_msg_with_stats(bot, accid, msg.chat_id, MsgData(text="❌ This command is only for the administrator."))
        return

    addr = event.payload.strip()
    if not addr:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="Usage: /rmtransport user@example.com"))
        return

    # Safety: don't allow removing the last transport
    try:
        transports = bot.rpc.list_transports(accid)
        transport_addrs = []
        for t in transports:
            a = t.get('addr', '') if isinstance(t, dict) else getattr(t, 'addr', '')
            transport_addrs.append(a)
        if len(transport_addrs) <= 1:
            bot.rpc.send_msg(accid, msg.chat_id, MsgData(text="❌ Cannot remove the last transport. Add another one first."))
            return
        if addr not in transport_addrs:
            bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=f"❌ Transport `{addr}` not found. Use /transports to see configured relays."))
            return
    except Exception as e:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=f"❌ Failed to check transports: {e}"))
        return

    try:
        bot.rpc.delete_transport(accid, addr)
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=f"✅ Transport `{addr}` removed."))
    except Exception as e:
        bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=f"❌ Failed to remove transport: {e}"))

if __name__ == "__main__":
    import sys
    # Handle 'init transport' CLI command
    if len(sys.argv) > 2 and sys.argv[1] == "init" and sys.argv[2] == "transport":
        if len(sys.argv) < 4:
            print("Usage:")
            print("  python bot.py init transport DCACCOUNT:uri")
            print("  python bot.py init transport addr password")
            sys.exit(1)
            
        # We need to manually initialize RPC to add transport without starting the bot
        from deltachat2 import Rpc, IOTransport
        from appdirs import user_config_dir
        
        config_dir = user_config_dir("ntfybot")
        accounts_dir = os.path.join(config_dir, "accounts")
        
        try:
            with IOTransport(accounts_dir=accounts_dir) as trans:
                rpc = Rpc(trans)
                accids = rpc.get_all_account_ids()
                if not accids:
                    print("Error: No accounts configured. Run 'python bot.py init addr password' first.")
                    sys.exit(1)
                accid = accids[0]
                
                payload = sys.argv[3]
                if payload.startswith("DCACCOUNT:"):
                    rpc.add_transport_from_qr(accid, payload)
                    print(f"Success: Backup transport added via chatmail URI.")
                elif len(sys.argv) >= 5:
                    addr, password = sys.argv[3], sys.argv[4]
                    rpc.add_or_update_transport(accid, {"addr": addr, "password": password})
                    print(f"Success: Backup transport {addr} added.")
                else:
                    print("Error: For email accounts, provide both address and password.")
                    sys.exit(1)
        except Exception as e:
            print(f"Error adding transport: {e}")
            sys.exit(1)
        sys.exit(0)

    # If no subcommand is provided, default to 'serve' to start the bot
    if len(sys.argv) == 1:
        sys.argv.append("serve")
    dc_cli.start()
