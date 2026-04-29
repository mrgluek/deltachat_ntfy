import os
import sqlite3
import threading

DB_PATH = os.getenv("DB_PATH", "ntfy.db")
_lock = threading.Lock()

def init_db():
    with _lock:
        conn = sqlite3.connect(DB_PATH)
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
        
        conn.commit()
        conn.close()

def set_config(key: str, value: str):
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        conn.close()

def get_config(key: str) -> str:
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

def subscribe(dc_chat_id: int, topic: str) -> bool:
    """Subscribe a chat to a topic. Returns True if newly subscribed, False if already subscribed."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        try:
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
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM subscriptions WHERE dc_chat_id = ? AND topic = ?", (dc_chat_id, topic))
        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted

def get_subscriptions(dc_chat_id: int) -> list[str]:
    """Get all topics a chat is subscribed to."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT topic FROM subscriptions WHERE dc_chat_id = ?", (dc_chat_id,))
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows]

def get_subscribers(topic: str) -> list[int]:
    """Get all chats subscribed to a topic."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT dc_chat_id FROM subscriptions WHERE topic = ?", (topic,))
        rows = cursor.fetchall()
        conn.close()
        return [row[0] for row in rows]

def add_notification(topic: str, title: str, message: str, priority: int):
    """Save a notification to the history."""
    with _lock:
        conn = sqlite3.connect(DB_PATH)
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
        conn.close()

def get_recent_notifications(topics: list[str], limit: int = 5) -> list[dict]:
    """Get recent notifications for a list of topics."""
    if not topics:
        return []
    
    with _lock:
        conn = sqlite3.connect(DB_PATH)
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
        conn.close()
        return [dict(r) for r in rows]

init_db()
