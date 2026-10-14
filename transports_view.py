"""/transports report: which relay the Delta Chat core actually sends through.

Since core 2.61 the relay used for sending is not `configured_addr`: the core
keeps sending through the relay that last worked (table `smtp_success`) and
moves to the next one only when it cannot connect. The JSON-RPC API only lists
the relay addresses, so the sending order, the send queue and the time of the
last received message are read from the account database (read-only; the core
keeps it in WAL mode, so this does not block it). Incoming status and mailbox
storage come from get_connectivity_html().

This file is shared verbatim between the Delta Chat bots; keep the copies in sync.
"""
import html
import logging
import re
import sqlite3
import time
from typing import Optional

logger = logging.getLogger(__name__)

_DOT = {"green": "🟢", "yellow": "🟡", "red": "🔴"}


def read_core_transport_state(rpc, accid) -> Optional[dict]:
    """Relays in the order the core tries them for sending, plus the core's send queue."""
    try:
        db_path = rpc.get_info(accid).get("database_dir")
        if not isinstance(db_path, str) or not db_path:
            return None
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            rows = conn.execute(
                """SELECT t.id, t.addr, t.add_timestamp, t.is_published, t.last_rcvd_timestamp, s.id
                   FROM transports t LEFT JOIN smtp_success s ON s.transport_id = t.id
                   ORDER BY IFNULL(s.id, 0) DESC, t.id ASC"""
            ).fetchall()
            outbox = conn.execute("SELECT COUNT(*), IFNULL(MAX(retries), 0) FROM smtp2").fetchone()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"Could not read transport state from the core database: {e}")
        return None
    transports = [
        {"id": r[0], "addr": r[1], "added": r[2] or 0, "published": bool(r[3]),
         "last_rcvd": r[4] or 0, "sending": r[5] is not None}
        for r in rows
    ]
    # Only the first one in this order is the current sending relay.
    for t in transports[1:]:
        t["sending"] = False
    return {"transports": transports, "outbox": outbox[0], "outbox_max_retries": outbox[1]}


def parse_connectivity_html(html_text: str) -> dict:
    """Per-relay incoming status/quota (in transport id order) and the outgoing status."""
    result = {"relays": [], "outgoing": None}
    if not isinstance(html_text, str) or not html_text:
        return result
    parts = html_text.split('<li class="transport">')
    tail = parts[-1]
    out_idx = tail.rfind("<h3>")
    outgoing_html = tail[out_idx:] if out_idx != -1 else ""
    if out_idx != -1:
        parts[-1] = tail[:out_idx]
    for seg in parts[1:]:
        relay = {"domain": None, "color": None, "status": None, "quota": None, "quota_pct": None}
        m = re.search(r'class="(\w+) dot"></span>\s*<b>([^<]+):</b>\s*([^<]*)', seg)
        if m:
            relay["color"] = m.group(1)
            relay["domain"] = html.unescape(m.group(2)).strip()
            relay["status"] = html.unescape(m.group(3)).strip()
        q = re.search(r'<ul class="quota-list"><li>(.*?)<div class="bar"><div class="progress \w+"[^>]*>(\d+)%', seg, re.S)
        if q:
            relay["quota"] = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", q.group(1))).split())
            relay["quota_pct"] = int(q.group(2))
        result["relays"].append(relay)
    m = re.search(r'class="(\w+) dot"></span>\s*([^<]*)', outgoing_html)
    if m:
        result["outgoing"] = {"color": m.group(1), "status": html.unescape(m.group(2)).strip()}
    return result


def _ago(ts: int) -> str:
    if not ts:
        return "never"
    delta = max(0, int(time.time()) - int(ts))
    if delta < 120:
        return "just now"
    if delta < 7200:
        return f"{delta // 60} min ago"
    if delta < 172800:
        return f"{delta // 3600} h ago"
    return f"{delta // 86400} days ago"


def build_transports_report(rpc, accid, transports: list) -> str:
    """Markdown text for /transports. `transports` is the result of rpc.list_transports()."""
    rpc_addrs = [t.get("addr", "") if isinstance(t, dict) else getattr(t, "addr", "") for t in transports]
    state = read_core_transport_state(rpc, accid)
    try:
        conn_info = parse_connectivity_html(rpc.get_connectivity_html(accid))
    except Exception:
        conn_info = {"relays": [], "outgoing": None}

    if state:
        ordered = state["transports"]
        # The connectivity page lists relays by transport id.
        by_id = sorted(ordered, key=lambda t: t["id"])
        relay_info = {t["addr"]: (conn_info["relays"][i] if i < len(conn_info["relays"]) else None)
                      for i, t in enumerate(by_id)}
    else:
        ordered = [{"addr": a, "sending": False, "published": None, "last_rcvd": 0, "added": 0} for a in rpc_addrs]
        relay_info = {}
        for t in ordered:
            domain = t["addr"].split("@")[-1]
            relay_info[t["addr"]] = next((r for r in conn_info["relays"] if r.get("domain") == domain), None)

    lines = ["🔌 **Mail Relays (Transports)**", ""]
    sending = next((t for t in ordered if t["sending"]), None)
    if sending:
        lines.append(f"📤 **Sending via:** `{sending['addr']}`")
    elif state and ordered:
        lines.append(f"📤 **Sending via:** `{ordered[0]['addr']}` (nothing sent successfully yet)")
    out = conn_info.get("outgoing")
    if out:
        lines.append(f"   {_DOT.get(out['color'], '❓')} {out['status']}")
    if state:
        if state["outbox"]:
            retry_note = (f", retried up to {state['outbox_max_retries']}× (fails after 6)"
                          if state["outbox_max_retries"] else "")
            lines.append(f"   📬 {state['outbox']} message(s) waiting in the core's send queue{retry_note}")
        else:
            lines.append("   📬 Send queue is empty")
    lines.append("")

    for idx, t in enumerate(ordered, 1):
        addr = t["addr"]
        tags = []
        if t["sending"]:
            tags.append("sending")
        if t.get("published") is False:
            tags.append("not published")
        lines.append(f"{idx}. `{addr}`" + (f" — {', '.join(tags)}" if tags else ""))
        info = relay_info.get(addr)
        if info and info.get("status"):
            lines.append(f"   📥 {_DOT.get(info['color'], '❓')} {info['status']}"
                         + (f", last message {_ago(t['last_rcvd'])}" if state else ""))
        elif state:
            lines.append(f"   📥 Last message {_ago(t['last_rcvd'])}")
        if info and info.get("quota"):
            warn = " ⚠️" if (info.get("quota_pct") or 0) >= 80 else ""
            lines.append(f"   💾 {info['quota']} ({info['quota_pct']}%){warn}")
        if t.get("added"):
            lines.append(f"   ➕ Added {time.strftime('%Y-%m-%d', time.gmtime(t['added']))}")
        lines.append("")

    lines.append("ℹ️ The core keeps sending through the relay that last worked and switches to the next one "
                 "only if it cannot connect. Relays are tried in the order above. "
                 "\"Not published\" relays are not announced to contacts.")
    if not state:
        lines.append("(Sending relay unknown: could not read the core database.)")
    return "\n".join(lines)
