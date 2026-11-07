"""Confirmed, opt-in grant emails with persistent scheduling and retry state."""
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from zoneinfo import ZoneInfo

import db
import pipeline

NC = ZoneInfo("America/New_York")
REMINDER_DAYS = (30, 14, 7, 3, 1)
MIN_FIELD_FIT = 40
DEFAULT_BASE_URL = "https://ncgrantfinder-bshjbjbgeahqhnck.westus3-01.azurewebsites.net"


def utc_now():
    return datetime.now(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def base_url():
    return os.environ.get("PUBLIC_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def mail_ready():
    return bool(os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASS")
                and os.environ.get("NOTIFICATIONS_ENABLED", "1") == "1")


def valid_email(value):
    value = str(value or "").strip().lower()
    if len(value) > 254 or not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+", value):
        raise ValueError("Enter a valid email address.")
    return value


def sync_catalog(conn, now):
    """Baseline existing records; only later additions count as newly added."""
    initialized = conn.execute("SELECT 1 FROM notification_state WHERE key='catalog_initialized'").fetchone()
    known = {r[0] for r in conn.execute("SELECT opportunity_id FROM notification_seen")}
    current = {r[0] for r in conn.execute("SELECT id FROM opportunities")}
    for office, profile in pipeline.load_agency_profiles().items():
        current.update(item['id'] for item in _other_items(conn, office, profile))
    conn.executemany("INSERT INTO notification_seen VALUES (?, ?)",
                     [(oid, stamp(now) if initialized else None) for oid in current - known])
    conn.execute("INSERT OR IGNORE INTO notification_state VALUES ('catalog_initialized', ?)", (stamp(now),))


def enqueue(conn, key, email, kind, payload, now):
    conn.execute("INSERT OR IGNORE INTO notification_outbox (id,email,kind,payload,next_attempt) VALUES (?,?,?,?,?)",
                 (key, email, kind, json.dumps(payload), stamp(now)))


def _apply_changes(conn, email, changes, now):
    for change in changes:
        if change["kind"] == "weekly":
            conn.execute("""INSERT INTO notification_preferences(email,office_id,category,created_at)
                VALUES (?,?,?,?) ON CONFLICT(email,office_id,category) DO UPDATE SET
                created_at=CASE WHEN notification_preferences.active=0 THEN excluded.created_at ELSE notification_preferences.created_at END,
                last_weekly_at=CASE WHEN notification_preferences.active=0 THEN NULL ELSE notification_preferences.last_weekly_at END, active=1""",
                (email, change["office"], change["category"], stamp(now)))
        else:
            row = conn.execute("SELECT status FROM decisions WHERE office_id=? AND opportunity_id=?",
                               (change["office"], change["grant"])).fetchone()
            if row and row["status"] == "Pursue":
                conn.execute("INSERT INTO notification_pursuits VALUES (?,?,?,1) ON CONFLICT(email,office_id,opportunity_id) DO UPDATE SET active=1",
                             (email, change["office"], change["grant"]))


def request_subscription(conn, email, office, category="all", grant=None, token="", now=None):
    now = now or utc_now()
    email = valid_email(email)
    profiles = pipeline.load_agency_profiles()
    categories = {p.get("category", "state") for p in profiles.values()} | {"all"}
    if (office != "all" and office not in profiles) or category not in categories:
        raise ValueError("Choose a supported organization or field.")
    sync_catalog(conn, now)
    change = {"kind": "pursuit" if grant else "weekly", "office": office, "category": category}
    if grant:
        change["grant"] = str(grant)
    account = conn.execute("SELECT * FROM notification_accounts WHERE email=?", (email,)).fetchone()
    if account and account["verified"] and account["active"] and token and hmac.compare_digest(account["manage_token"], token):
        _apply_changes(conn, email, [change], now)
        return {"active": True, "confirmation_required": False, "delivery_ready": mail_ready()}
    if not account:
        conn.execute("INSERT INTO notification_accounts(email,manage_token) VALUES (?,?)", (email, secrets.token_urlsafe(32)))
        account = conn.execute("SELECT * FROM notification_accounts WHERE email=?", (email,)).fetchone()
    changes = json.loads(account["pending_changes"])
    if change not in changes:
        changes.append(change)
    conn.execute("UPDATE notification_accounts SET pending_changes=? WHERE email=?", (json.dumps(changes), email))
    recent = account["confirm_hash"] and account["last_confirmation"] and datetime.fromisoformat(account["last_confirmation"]) > now - timedelta(minutes=15)
    if not recent:
        raw = secrets.token_urlsafe(32)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        conn.execute("UPDATE notification_accounts SET confirm_hash=?,confirm_expires=NULL,last_confirmation=? WHERE email=?",
                     (digest, stamp(now), email))
        conn.execute("UPDATE notification_outbox SET state='cancelled' WHERE email=? AND kind='confirmation' AND state IN ('pending','failed')", (email,))
        enqueue(conn, "confirm:" + digest, email, "confirmation", {"token": raw}, now)
    else:
        conn.execute("UPDATE notification_outbox SET state='pending',next_attempt=? WHERE email=? AND kind='confirmation' AND state='failed'",
                     (stamp(now), email))
    return {"active": False, "confirmation_required": True, "delivery_ready": mail_ready()}


def confirm(conn, raw, now=None):
    now = now or utc_now()
    digest = hashlib.sha256(str(raw).encode()).hexdigest()
    account = conn.execute("SELECT * FROM notification_accounts WHERE confirm_hash=? AND confirm_expires>=?", (digest, stamp(now))).fetchone()
    if not account:
        return None
    _apply_changes(conn, account["email"], json.loads(account["pending_changes"]), now)
    conn.execute("UPDATE notification_accounts SET verified=1,active=1,confirm_hash=NULL,confirm_expires=NULL,pending_changes='[]' WHERE email=?", (account["email"],))
    return {"email": account["email"], "token": account["manage_token"]}


def unsubscribe(conn, token):
    account = conn.execute("SELECT email FROM notification_accounts WHERE manage_token=?", (str(token),)).fetchone()
    if not account:
        return False
    email = account["email"]
    conn.execute("UPDATE notification_accounts SET active=0,pending_changes='[]',confirm_hash=NULL WHERE email=?", (email,))
    for table in ("notification_preferences", "notification_pursuits"):
        conn.execute(f"UPDATE {table} SET active=0 WHERE email=?", (email,))
    conn.execute("UPDATE notification_outbox SET state='cancelled' WHERE email=? AND state IN ('pending','failed')", (email,))
    return True


def _other_items(conn, office, profile):
    # Import lazily: the web app also imports this notification module.
    import app
    return app.other_items(conn, office, profile)


def _current_item(conn, oid, today, office=None):
    row = conn.execute("SELECT data,status,close_date FROM opportunities WHERE id=?", (oid,)).fetchone()
    if row:
        item = json.loads(row["data"])
        item.update(status=row["status"], close_date=row["close_date"])
        if db.application_available(item, today):
            return item
    if str(oid).startswith('st-'):
        # Dated NC applications live in the combined list, not the federal table.
        profiles = pipeline.load_agency_profiles()
        for office in ([office] if office in profiles else profiles):
            for item in _other_items(conn, office, profiles[office]):
                if item["id"] == oid:
                    return item
    return None


def reminder_bucket(days):
    return min((threshold for threshold in REMINDER_DAYS if 0 <= days <= threshold), default=None)


def weekly_items(conn, office, category, since, now):
    profiles = pipeline.load_agency_profiles()
    selected = [office] if office != "all" else [oid for oid, p in profiles.items() if category == "all" or p.get("category", "state") == category]
    today = now.astimezone(NC).date()
    items = {}
    for oid in selected:
        if oid not in profiles:
            continue
        for item in db.scored_for_office(conn, oid):
            if not db.application_available(item, today):
                continue
            fit = (item.get("score_breakdown") or {}).get("relevancy", 0)
            if fit < MIN_FIELD_FIT or pipeline.eligibility_fit(item, profiles[oid])[0] <= 10:
                continue
            previous = items.get(item["id"])
            if previous is None or item["total_score"] > previous["total_score"]:
                items[item["id"]] = item
        for item in _other_items(conn, oid, profiles[oid]):
            # These applications have already passed the source's eligibility checks.
            if item['total_score'] / 0.8 < MIN_FIELD_FIT or not db.application_available(item, today):
                continue
            previous = items.get(item['id'])
            if previous is None or item['total_score'] > previous['total_score']:
                items[item['id']] = item
    seen = {r["opportunity_id"]: r["discovered_at"] for r in conn.execute("SELECT * FROM notification_seen WHERE discovered_at IS NOT NULL")}
    new, due = [], []
    for item in sorted(items.values(), key=lambda it: -it["total_score"]):
        deadline = db.application_deadline(item)
        if seen.get(item["id"]) and stamp(since) <= seen[item["id"]] <= stamp(now):
            new.append(item)
        if deadline and 0 <= (deadline - today).days <= 30:
            due.append(item)
    return new, due


def _week(now):
    local = now.astimezone(NC)
    return (local - timedelta(days=local.weekday())).replace(hour=7, minute=0, second=0, microsecond=0)


def schedule(conn, now):
    today = now.astimezone(NC).date()
    sync_catalog(conn, now)
    rows = conn.execute("""SELECT p.* FROM notification_pursuits p JOIN notification_accounts a ON a.email=p.email
        JOIN decisions d ON d.office_id=p.office_id AND d.opportunity_id=p.opportunity_id
        WHERE a.verified=1 AND a.active=1 AND p.active=1 AND d.status='Pursue'""").fetchall()
    for row in rows:
        item = _current_item(conn, row["opportunity_id"], today, row["office_id"])
        deadline = db.application_deadline(item) if item else None
        bucket = reminder_bucket((deadline - today).days) if deadline else None
        if bucket is not None:
            key = hashlib.sha256(f"reminder|{row['email']}|{row['office_id']}|{row['opportunity_id']}|{deadline}|{bucket}".encode()).hexdigest()
            enqueue(conn, key, row["email"], "reminder", {"office": row["office_id"], "grant": row["opportunity_id"], "deadline": deadline.isoformat(), "bucket": bucket}, now)
    anchor = _week(now)
    if now < anchor:
        return
    preferences = conn.execute("""SELECT p.* FROM notification_preferences p JOIN notification_accounts a ON a.email=p.email
        WHERE a.verified=1 AND a.active=1 AND p.active=1""").fetchall()
    for row in preferences:
        if datetime.fromisoformat(row["created_at"]) > anchor or (row["last_weekly_at"] and datetime.fromisoformat(row["last_weekly_at"]) >= anchor):
            continue
        key = hashlib.sha256(f"weekly|{row['email']}|{row['office_id']}|{row['category']}|{anchor.date()}".encode()).hexdigest()
        enqueue(conn, key, row["email"], "weekly", {"office": row["office_id"], "category": row["category"], "week": anchor.date().isoformat(),
            "since": row["last_weekly_at"] or row["created_at"]}, now)


def build_message(conn, row, now):
    payload = json.loads(row["payload"])
    account = conn.execute("SELECT * FROM notification_accounts WHERE email=?", (row["email"],)).fetchone()
    if not account:
        return None
    if row["kind"] == "confirmation":
        digest = hashlib.sha256(payload["token"].encode()).hexdigest()
        if digest != account["confirm_hash"]:
            return None
        conn.execute("UPDATE notification_accounts SET confirm_expires=? WHERE email=?", (stamp(now + timedelta(hours=48)), row['email']))
        url = base_url() + '/notifications/confirm?token=' + payload['token']
        text = f"Confirm the grant emails you requested:\n{url}\n\nThis link expires in 48 hours. If you did not request these emails, ignore this message."
        return "Confirm your NC grant email alerts", text, '<p>' + escape(text).replace('\n', '<br>') + '</p>', None
    if not account["verified"] or not account["active"]:
        return None
    today = now.astimezone(NC).date()
    if row["kind"] == "reminder":
        pursuit = conn.execute("""SELECT 1 FROM notification_pursuits p JOIN decisions d ON d.office_id=p.office_id AND d.opportunity_id=p.opportunity_id
            WHERE p.email=? AND p.office_id=? AND p.opportunity_id=? AND p.active=1 AND d.status='Pursue'""",
            (row["email"], payload["office"], payload["grant"])).fetchone()
        item = _current_item(conn, payload["grant"], today, payload["office"]) if pursuit else None
        deadline = db.application_deadline(item) if item else None
        if not deadline or deadline.isoformat() != payload["deadline"] or reminder_bucket((deadline - today).days) != payload["bucket"]:
            return None
        days = (deadline - today).days
        subject = f"Grant deadline {'today' if days == 0 else 'in ' + str(days) + ' days'}: {item['title']}"
        text = f"You marked this grant Pursue.\n\n{item['title']}\n{'Expected deadline' if item['status']=='forecasted' else 'Deadline'}: {deadline.isoformat()}\n{days} days remaining\nOfficial notice: {item.get('url') or ''}\n"
    else:
        pref = conn.execute("SELECT * FROM notification_preferences WHERE email=? AND office_id=? AND category=? AND active=1",
                            (row["email"], payload["office"], payload["category"])).fetchone()
        if not pref or payload["week"] != _week(now).date().isoformat():
            return None
        new, due = weekly_items(conn, payload["office"], payload["category"], datetime.fromisoformat(payload["since"]), now)
        profile = pipeline.load_agency_profiles().get(payload["office"], {})
        field = profile.get("name") or (payload["category"].replace('_', ' ').title() if payload["category"] != 'all' else 'All fields')
        subject = f"Weekly NC grants: {field} — {len(new)} new, {len(due)} due within 30 days"
        lines = [f"Weekly grant update for {field}", "", f"NEW GRANTS ADDED SINCE YOUR LAST UPDATE ({len(new)})"]
        for item in new:
            lines += [item["title"], "Fits: " + (item.get("matched_goal_title") or 'Your selected goals'), item.get("url") or '', '']
        lines += [f"MATCHING GRANTS DUE WITHIN 30 DAYS ({len(due)})"]
        for item in due:
            deadline = db.application_deadline(item)
            lines += [item["title"], f"{'Expected deadline' if item['status']=='forecasted' else 'Deadline'}: {deadline.isoformat()} ({(deadline-today).days} days)", item.get("url") or '', '']
        if not new and not due:
            lines += ['No new matching grants or matching deadlines within 30 days this week.']
        text = '\n'.join(lines)
    manage = base_url() + '/notifications/unsubscribe?token=' + account['manage_token']
    text += '\n\nConfirm dates and eligibility in the official notice.\nManage or stop these emails: ' + manage
    return subject, text, '<div style="font-family:Arial,sans-serif;line-height:1.5;white-space:pre-wrap">' + escape(text) + '</div>', manage


def send_email(email, subject, text, html, key, unsubscribe_url=None):
    msg = EmailMessage()
    msg['From'] = os.environ.get('SMTP_FROM') or os.environ['SMTP_USER']
    msg['To'] = email
    msg['Subject'] = subject
    msg['Message-ID'] = '<' + hashlib.sha256(key.encode()).hexdigest() + '@nc-grants-matchmaker>'
    if unsubscribe_url:
        msg['List-Unsubscribe'] = '<' + unsubscribe_url + '>'
        msg['List-Unsubscribe-Post'] = 'List-Unsubscribe=One-Click'
    msg.set_content(text)
    msg.add_alternative(html, subtype='html')
    host, port = os.environ.get('SMTP_HOST', 'smtp.gmail.com'), int(os.environ.get('SMTP_PORT', '587'))
    context = ssl.create_default_context()
    if port == 465:
        client = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
    else:
        client = smtplib.SMTP(host, port, timeout=20)
    with client as smtp:
        if port != 465:
            smtp.starttls(context=context)
        smtp.login(os.environ['SMTP_USER'], os.environ['SMTP_PASS'].replace(' ', ''))
        refused = smtp.send_message(msg)
        if refused:
            raise RuntimeError('Recipient was rejected')


def run_once(conn, now=None, sender=None, max_items=100):
    now = now or utc_now()
    schedule(conn, now)
    conn.commit()
    if sender is None and not mail_ready():
        return {"configured": False, "sent": 0, "failed": 0}
    sender = sender or send_email
    result = {"configured": True, "sent": 0, "failed": 0}
    for _ in range(max_items):
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("""SELECT * FROM notification_outbox WHERE
            ((state IN ('pending','failed') AND next_attempt<=?)
             OR (state='sending' AND claimed_at<?)) ORDER BY next_attempt LIMIT 1""",
            (stamp(now), stamp(now-timedelta(minutes=30)))).fetchone()
        if not row:
            conn.commit()
            break
        conn.execute("UPDATE notification_outbox SET state='sending',claimed_at=?,attempts=attempts+1 WHERE id=?", (stamp(now), row['id']))
        conn.commit()
        try:
            message = build_message(conn, row, now)
            if message is None:
                conn.execute("UPDATE notification_outbox SET state='cancelled' WHERE id=?", (row['id'],))
                conn.commit()
                continue
            subject, text, html, manage = message
            conn.commit()
            sender(row['email'], subject, text, html, row['id'], manage)
        except smtplib.SMTPRecipientsRefused as error:
            permanent = all(code >= 500 for code, _ in error.recipients.values())
            if permanent:
                conn.execute("UPDATE notification_accounts SET active=0 WHERE email=?", (row['email'],))
            conn.execute("UPDATE notification_outbox SET state=?,next_attempt=? WHERE id=?",
                         ('cancelled' if permanent else 'failed', stamp(now+timedelta(hours=6)), row['id']))
            result['failed'] += 1
        except Exception:
            # Do not log credentials, recipient addresses, or confirmation tokens.
            conn.execute("UPDATE notification_outbox SET state='failed',next_attempt=? WHERE id=?",
                         (stamp(now+timedelta(minutes=min(360, 5*2**min(row['attempts'], 7)))), row['id']))
            result['failed'] += 1
        else:
            conn.execute("UPDATE notification_outbox SET state='sent',sent_at=? WHERE id=?", (stamp(now), row['id']))
            if row['kind'] == 'weekly':
                payload = json.loads(row['payload'])
                conn.execute("UPDATE notification_preferences SET last_weekly_at=? WHERE email=? AND office_id=? AND category=?",
                             (stamp(now), row['email'], payload['office'], payload['category']))
            result['sent'] += 1
        conn.commit()
    return result
