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

    
    if not topic:
        logger.warning(f"Request to {request.path} failed: Topic required")
        return web.Response(status=400, text="Topic required. Use /{topic}, Topic header or 'topic' in JSON.")

    if not message or not str(message).strip():
        return web.Response(status=400, text="Message body required")

    # Save to database
    priority_int = parse_priority(priority_raw)
    database.add_notification(topic, title, message, priority_int)

    # Pub/Sub push
    if topic in listeners:
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
        
        for q in listeners[topic]:
            try:
                q.put_nowait(msg_payload)
            except asyncio.QueueFull:
                pass

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
                # Use cached QR link if available, otherwise generate new one
                qrdata = bot_qr_cache.get(active_accid)
                if not qrdata:
                    qrdata = dc_bot_instance.rpc.get_chat_securejoin_qr_code(active_accid, None)
                    bot_qr_cache[active_accid] = qrdata
                
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
        @media (max-width: 600px) {{
            body {{ padding: 1rem; }}
            .topic-path {{ font-size: 1rem; }}
        }}
    </style>
    <script>
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
        
        <div class="notification-list">
"""
    server_tz = datetime.datetime.now().astimezone().tzname()
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
                {f'<span class="title">{n["title"]}</span>' if n['title'] else ''}
                <div class="message">{n['message']}</div>
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
    except asyncio.CancelledError:
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
    if topic in listeners:
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
        for q in listeners[topic]:
            try:
                q.put_nowait(msg_payload)
            except asyncio.QueueFull:
                pass

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
        f"/list — Show subscribed topics\n"
        f"/last — Show last 5 notifications\n"
        f"/stats — Show bot statistics\n"
        f"/newgroup [name] — Create a dedicated group chat\n"
        f"/donate — Support bot development ❤️\n"
        f"/help — Show this help message\n\n"
    )
    
    if not admin_email:
        help_text += (
            f"**Initialisation Command:**\n"
            f"/initadmin — Claim bot ownership (if no admin is set)\n\n"
        )
    elif admin_email.lower() == sender_email.lower():
        help_text += (
            f"**Admin Commands:**\n"
            f"/accounts — List configured bot accounts\n"
            f"/rmaccount <id> — Delete a bot account\n"
            f"/url <url> — Set the bot's public URL\n\n"
        )
        
    help_text += f"Run your own bot: https://github.com/mrgluek/deltachat_ntfy"
    return help_text

@dc_cli.on(events.NewMessage(command="/help"))
def help_command(bot, accid, event):
    msg = event.msg
    help_text = get_help_text(bot, accid, msg.from_id)
    bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=help_text))

@dc_cli.on(events.NewMessage)
def on_new_message(bot, accid, event):
    msg = event.msg
    if msg.is_info:
        return
        
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

    # 2. Detect new users in private chats and send welcome
    try:
        chat_info = bot.rpc.get_basic_chat_info(accid, msg.chat_id)
        
        # Safe check for chat type
        is_private = False
        if isinstance(chat_info, dict):
            is_private = (chat_info.get("type") == 1)
        else:
            is_private = (getattr(chat_info, "type", 1) == 1)
            
        if is_private:
            if not bot.rpc.get_contact_config(accid, msg.from_id, "greeted"):
                bot.logger.info(f"New user detected, sending welcome to chat {msg.chat_id}")
                help_text = get_help_text(bot, accid, msg.from_id)
                welcome_msg = f"👋 Welcome to Ntfy Bot!\n\n{help_text}"
                bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=welcome_msg))
                bot.rpc.set_contact_config(accid, msg.from_id, "greeted", "1")
    except Exception as e:
        bot.logger.error(f"Error in greeting check: {e}")

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

@dc_cli.on(events.NewMessage(command="/stats"))
def stats_command(bot, accid, event):
    msg = event.msg
    
    last_24h = database.get_notifications_last_24h()
    
    reply = f"📊 **Ntfy Bot Statistics**\n\nNotifications received in the last 24h: {last_24h}"
    bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=reply))

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
    bot.rpc.send_msg(accid, msg.chat_id, MsgData(text=f"✅ Bot URL updated to: {url}"))

if __name__ == "__main__":
    import sys
    # If no subcommand is provided, default to 'serve' to start the bot
    if len(sys.argv) == 1:
        sys.argv.append("serve")
    dc_cli.start()
