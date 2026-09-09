import os
import sqlite3
import threading
import time

DB_PATH = os.getenv("DB_PATH", "ntfy.db")
_lock = threading.Lock()

def _connect(timeout: float = 10.0) -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=timeout)
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn

def init_db():
    with _lock:
        conn = _connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("PRAGMA cache_size = -4000;")
            cursor = conn.cursor()
            
            # Config table for admin_dc_email etc.
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')
            
            # Subscriptions table: which chat is subscribed to which topic
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS subscriptions (
                    dc_chat_id INTEGER,
                    topic TEXT,
                    created_at INTEGER DEFAULT (strftime('%s','now')),
                    PRIMARY KEY (dc_chat_id, topic)
                )
            ''')
            
            # Notifications table: history of notifications for /last
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT,
                    title TEXT,
                    message TEXT,
                    priority INTEGER,
                    created_at INTEGER DEFAULT (strftime('%s','now'))
                )
            ''')
            
            # Add indexes to prevent full table scans during cleanup
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_notifications_topic ON notifications(topic)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_notifications_created_at ON notifications(created_at)')
            
            # Transport statistics: track messages sent/received per relay address
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS transport_stats (
                    addr TEXT PRIMARY KEY,
                    msgs_sent INTEGER DEFAULT 0,
                    msgs_received INTEGER DEFAULT 0,
                    last_sent_at INTEGER,
                    last_received_at INTEGER
                )
            ''')
            
            conn.commit()
        finally:
            conn.close()

def set_config(key: str, value: str):
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, str(value) if value is not None else None))
            conn.commit()
        finally:
            conn.close()

def get_config(key: str) -> str | None:
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row[0] if row else None
        finally:
            conn.close()

def subscribe(dc_chat_id: int, topic: str) -> bool:
    """Subscribe a chat to a topic. Returns True if newly subscribed, False if already subscribed."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO subscriptions (dc_chat_id, topic) VALUES (?, ?)", (dc_chat_id, topic))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()

def unsubscribe(dc_chat_id: int, topic: str) -> bool:
    """Unsubscribe a chat from a topic. Returns True if unsubscribed, False if wasn't subscribed."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM subscriptions WHERE dc_chat_id = ? AND topic = ?", (dc_chat_id, topic))
            deleted = cursor.rowcount > 0
            conn.commit()
            return deleted
        finally:
            conn.close()

def get_subscriptions(dc_chat_id: int) -> list[str]:
    """Get all topics a chat is subscribed to."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT topic FROM subscriptions WHERE dc_chat_id = ?", (dc_chat_id,))
            rows = cursor.fetchall()
            return [row[0] for row in rows]
        finally:
            conn.close()

def get_subscribers(topic: str) -> list[int]:
    """Get all chats subscribed to a topic."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT dc_chat_id FROM subscriptions WHERE topic = ?", (topic,))
            rows = cursor.fetchall()
            return [row[0] for row in rows]
        finally:
            conn.close()

def add_notification(topic: str, title: str, message: str, priority: int):
    """Save a notification to the history."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO notifications (topic, title, message, priority) VALUES (?, ?, ?, ?)",
                (topic, title, message, priority)
            )
            # Keep only notifications from the last 24 hours (86400 seconds)
            cursor.execute('''
                DELETE FROM notifications 
                WHERE created_at < CAST(strftime('%s','now') AS INTEGER) - 86400
            ''')
            # Also keep only the last 1000 notifications per topic to prevent spam growth
            cursor.execute('''
                DELETE FROM notifications 
                WHERE topic = ? AND id NOT IN (
                    SELECT id FROM notifications WHERE topic = ? ORDER BY id DESC LIMIT 1000
                )
            ''', (topic, topic))
            conn.commit()
        finally:
            conn.close()

def get_recent_notifications(topics: list[str], limit: int = 5) -> list[dict]:
    """Get recent notifications for a list of topics."""
    if not topics:
        return []
    
    with _lock:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            placeholders = ','.join(['?'] * len(topics))
            query = f"""
                SELECT * FROM notifications 
                WHERE topic IN ({placeholders}) 
                ORDER BY id DESC LIMIT ?
            """
            cursor.execute(query, (*topics, limit))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

def get_messages_since(topic: str, since: str) -> list[dict]:
    """Get messages for a topic since a specific time or ID."""
    with _lock:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            query = "SELECT * FROM notifications WHERE topic = ?"
            params = [topic]
            
            if since and since != 'all':
                if since.endswith('m') and since[:-1].isdigit():
                    mins = int(since[:-1])
                    query += " AND created_at >= (CAST(strftime('%s','now') AS INTEGER) - ?)"
                    params.append(mins * 60)
                elif since.endswith('h') and since[:-1].isdigit():
                    hours = int(since[:-1])
                    query += " AND created_at >= (CAST(strftime('%s','now') AS INTEGER) - ?)"
                    params.append(hours * 3600)
                elif since.endswith('s') and since[:-1].isdigit():
                    secs = int(since[:-1])
                    query += " AND created_at >= (CAST(strftime('%s','now') AS INTEGER) - ?)"
                    params.append(secs)
                elif since.isdigit():
                    val = int(since)
                    if val < 1000000000:
                        query += " AND id > ?"
                        params.append(val)
                    else:
                        query += " AND created_at >= ?"
                        params.append(val)
                        
            query += " ORDER BY id ASC"
            cursor.execute(query, tuple(params))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

def get_notifications_last_24h() -> int:
    """Get the number of notifications received in the last 24 hours."""
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM notifications WHERE created_at >= (CAST(strftime('%s','now') AS INTEGER) - 86400)")
            count = cursor.fetchone()[0]
            return count
        finally:
            conn.close()

# Transport statistics tracking (buffered in memory)
_transport_stats_buffer: dict[str, dict[str, int]] = {}
_transport_stats_lock = threading.Lock()
_last_transport_flush = time.time()
TRANSPORT_FLUSH_INTERVAL = 30.0  # seconds

def increment_transport_sent(addr: str):
    """Increment the sent counter for a transport address (buffered in memory)."""
    if not addr or not isinstance(addr, str) or "@" not in addr:
        return
    now = int(time.time())
    should_flush = False
    with _transport_stats_lock:
        if addr not in _transport_stats_buffer:
            _transport_stats_buffer[addr] = {"sent": 0, "recv": 0, "last_sent": 0, "last_recv": 0}
        _transport_stats_buffer[addr]["sent"] += 1
        _transport_stats_buffer[addr]["last_sent"] = now
        global _last_transport_flush
        if now - _last_transport_flush >= TRANSPORT_FLUSH_INTERVAL:
            should_flush = True
    if should_flush:
        flush_transport_stats()

def increment_transport_received(addr: str):
    """Increment the received counter for a transport address (buffered in memory)."""
    if not addr or not isinstance(addr, str) or "@" not in addr:
        return
    now = int(time.time())
    should_flush = False
    with _transport_stats_lock:
        if addr not in _transport_stats_buffer:
            _transport_stats_buffer[addr] = {"sent": 0, "recv": 0, "last_sent": 0, "last_recv": 0}
        _transport_stats_buffer[addr]["recv"] += 1
        _transport_stats_buffer[addr]["last_recv"] = now
        global _last_transport_flush
        if now - _last_transport_flush >= TRANSPORT_FLUSH_INTERVAL:
            should_flush = True
    if should_flush:
        flush_transport_stats()

def flush_transport_stats():
    """Flush buffered transport stats to the database in a single transaction."""
    global _last_transport_flush
    with _transport_stats_lock:
        if not _transport_stats_buffer:
            _last_transport_flush = time.time()
            return
        pending = dict(_transport_stats_buffer)
        _transport_stats_buffer.clear()
        _last_transport_flush = time.time()

    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            for addr, counts in pending.items():
                if not isinstance(addr, str) or "@" not in addr:
                    continue
                sent = int(counts.get("sent", 0))
                recv = int(counts.get("recv", 0))
                last_s = counts.get("last_sent") or None
                last_r = counts.get("last_recv") or None
                cursor.execute('''
                    INSERT INTO transport_stats (addr, msgs_sent, msgs_received, last_sent_at, last_received_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(addr) DO UPDATE SET
                        msgs_sent = msgs_sent + excluded.msgs_sent,
                        msgs_received = msgs_received + excluded.msgs_received,
                        last_sent_at = COALESCE(excluded.last_sent_at, transport_stats.last_sent_at),
                        last_received_at = COALESCE(excluded.last_received_at, transport_stats.last_received_at)
                ''', (addr, sent, recv, last_s, last_r))
            conn.commit()
        finally:
            conn.close()

def get_all_transport_stats() -> list[dict]:
    """Get statistics for all tracked transports."""
    flush_transport_stats()
    with _lock:
        conn = _connect()
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM transport_stats ORDER BY msgs_sent + msgs_received DESC")
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

def cleanup_old_records(retention_days: int = 1) -> dict[str, int]:
    """Clean up old notifications (older than retention_days, default 24 hours)."""
    flush_transport_stats()
    now = int(time.time())
    cutoff = now - (retention_days * 86400)
    with _lock:
        conn = _connect()
        try:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM notifications WHERE created_at < ?", (cutoff,))
            pruned = cursor.rowcount
            conn.commit()
            return {"notifications": pruned}
        finally:
            conn.close()

def get_admin_fingerprint():
    """Get the saved admin DC fingerprint."""
    return get_config("admin_dc_fingerprint")

def set_admin_fingerprint(fp):
    """Set the admin DC fingerprint."""
    set_config("admin_dc_fingerprint", fp)

init_db()
