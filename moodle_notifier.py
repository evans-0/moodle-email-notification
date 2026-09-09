#!/usr/bin/env python3
"""
Moodle Assignment Notifier
---------------------------
Checks a Moodle calendar export (iCal) feed for new events (new assignments /
due dates) and emails a summary when it finds anything not seen before.

Built to run as a GitHub Actions workflow, configured entirely through
environment variables (GitHub Actions secrets) -- no config file, no packages
to install (standard library only).

Usage:
    python moodle_notifier.py                # normal check (what the workflow runs)
    python moodle_notifier.py --test-email    # sends a test email, ignoring Moodle
    python moodle_notifier.py --rebootstrap   # wipes local "seen" state and re-syncs silently

Requires these environment variables to be set: ICAL_URL, GMAIL_ADDRESS,
GMAIL_APP_PASSWORD, RECIPIENT_EMAIL.

State is kept in seen_events.json next to this script (committed back to the
repo by the workflow after each run). The FIRST run ever (or after
--rebootstrap) is a silent "bootstrap": it records every event currently in
the feed WITHOUT emailing, so you don't get flooded with everything that
already existed. From the next run onward, only genuinely new events trigger
an email.
"""

import json
import logging
import os
import re
import smtplib
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(SCRIPT_DIR, "seen_events.json")
LOG_PATH = os.path.join(SCRIPT_DIR, "notifier.log")

IST = timezone(timedelta(hours=5, minutes=30))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("moodle_notifier")


REQUIRED_KEYS = ["ICAL_URL", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "RECIPIENT_EMAIL"]


def load_config():
    cfg = {k: os.environ.get(k, "") for k in REQUIRED_KEYS}
    missing = [k for k in REQUIRED_KEYS if not cfg[k]]
    if missing:
        log.error("Missing required environment variable(s): %s. Set these as GitHub Actions "
                  "secrets (Settings -> Secrets and variables -> Actions).", ", ".join(missing))
        sys.exit(1)
    return cfg


def load_state():
    if not os.path.exists(STATE_PATH):
        return None  # signals "never run before" -> bootstrap
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(seen_uids):
    tmp_path = STATE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({"seen_uids": sorted(seen_uids), "updated_at": datetime.now(timezone.utc).isoformat()},
                   f, indent=2)
    os.replace(tmp_path, STATE_PATH)


def fetch_ics(url):
    req = urllib.request.Request(url, headers={"User-Agent": "MoodleNotifier/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    # Moodle serves this as UTF-8 text/calendar
    return raw.decode("utf-8", errors="replace")


def unfold_lines(ics_text):
    """RFC5545 line unfolding: a line starting with a space/tab continues the previous line."""
    raw_lines = ics_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    unfolded = []
    for line in raw_lines:
        if line[:1] in (" ", "\t") and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def unescape_ics_value(value):
    return (value.replace("\\n", "\n").replace("\\N", "\n")
                 .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def parse_events(ics_text):
    lines = unfold_lines(ics_text)
    events = []
    current = None
    for line in lines:
        stripped = line.strip()
        if stripped == "BEGIN:VEVENT":
            current = {}
            continue
        if stripped == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        key_and_params, _, value = line.partition(":")
        key = key_and_params.split(";")[0].strip().upper()
        value = unescape_ics_value(value.strip())
        # keep first occurrence of each key; also remember param string for DTSTART (VALUE=DATE detection)
        if key not in current:
            current[key] = value
            if key == "DTSTART":
                current["DTSTART_PARAMS"] = key_and_params.upper()
    return events


def parse_dt(value, params_line=""):
    """Returns (datetime, is_date_only) or (None, None) if unparseable."""
    value = (value or "").strip()
    if not value:
        return None, None
    is_date_only = "VALUE=DATE" in (params_line or "") and "VALUE=DATE-TIME" not in (params_line or "")
    try:
        if is_date_only or re.fullmatch(r"\d{8}", value):
            return datetime.strptime(value[:8], "%Y%m%d"), True
        if value.endswith("Z"):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc), False
        return datetime.strptime(value, "%Y%m%dT%H%M%S"), False
    except ValueError:
        return None, None


def format_due(value, params_line=""):
    dt, date_only = parse_dt(value, params_line)
    if dt is None:
        return value or "(no date given)"
    if date_only:
        return dt.strftime("%a, %d %b %Y")
    if dt.tzinfo is not None:
        dt = dt.astimezone(IST)
        return dt.strftime("%a, %d %b %Y, %I:%M %p IST")
    return dt.strftime("%a, %d %b %Y, %I:%M %p (server time)")


def sort_key(event):
    dt, _ = parse_dt(event.get("DTSTART", ""), event.get("DTSTART_PARAMS", ""))
    if dt is None:
        return datetime.max.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        # Treat naive (all-day / floating) datetimes as UTC purely for sorting purposes.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def send_email(cfg, subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = cfg["GMAIL_ADDRESS"]
    msg["To"] = cfg["RECIPIENT_EMAIL"]

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(cfg["GMAIL_ADDRESS"], cfg["GMAIL_APP_PASSWORD"])
        server.sendmail(cfg["GMAIL_ADDRESS"], [cfg["RECIPIENT_EMAIL"]], msg.as_string())


def build_email_body(new_events):
    lines = []
    for ev in new_events:
        title = ev.get("SUMMARY", "(untitled event)")
        course = ev.get("CATEGORIES", "").strip()
        due = format_due(ev.get("DTSTART", ""), ev.get("DTSTART_PARAMS", ""))
        url = ev.get("URL", "")
        desc = ev.get("DESCRIPTION", "").strip()
        lines.append(f"- {title}")
        if course:
            lines.append(f"  Course: {course}")
        lines.append(f"  Due: {due}")
        if url:
            lines.append(f"  Link: {url}")
        if desc:
            snippet = desc if len(desc) <= 300 else desc[:300] + "..."
            lines.append(f"  Details: {snippet}")
        lines.append("")
    lines.append("(Sent automatically by your Moodle notifier script.)")
    return "\n".join(lines)


def run_check(cfg):
    log.info("Fetching calendar feed...")
    try:
        ics_text = fetch_ics(cfg["ICAL_URL"])
    except urllib.error.URLError as e:
        log.error("Could not fetch the calendar feed: %s", e)
        sys.exit(1)

    events = parse_events(ics_text)
    log.info("Parsed %d event(s) from the feed.", len(events))

    current_uids = {ev["UID"] for ev in events if ev.get("UID")}
    state = load_state()

    if state is None:
        # First ever run: bootstrap silently.
        save_state(current_uids)
        log.info("Bootstrap complete: recorded %d existing event(s). No email sent. "
                 "Future runs will notify only about NEW events.", len(current_uids))
        return

    seen_uids = set(state.get("seen_uids", []))
    new_uids = current_uids - seen_uids
    new_events = sorted((ev for ev in events if ev.get("UID") in new_uids), key=sort_key)

    if not new_events:
        log.info("No new events since last check.")
        # Still refresh state in case some old events disappeared (course ended, etc.)
        save_state(current_uids)
        return

    log.info("Found %d new event(s). Sending email...", len(new_events))
    if len(new_events) == 1:
        ev = new_events[0]
        title = ev.get("SUMMARY", "Untitled")
        course = ev.get("CATEGORIES", "").strip()
        subject = f"Moodle: New item - {title} ({course})" if course else f"Moodle: New item - {title}"
    else:
        courses = {ev.get("CATEGORIES", "").strip() for ev in new_events if ev.get("CATEGORIES", "").strip()}
        if len(courses) == 1:
            subject = f"Moodle: {len(new_events)} new items posted ({next(iter(courses))})"
        else:
            subject = f"Moodle: {len(new_events)} new items posted"
    body = build_email_body(new_events)

    try:
        send_email(cfg, subject, body)
    except Exception as e:
        log.error("Failed to send email, will retry these events next run: %s", e)
        sys.exit(1)

    # Only mark as seen AFTER a successful send, so a failed email doesn't lose the event.
    save_state(current_uids)
    log.info("Email sent and state updated.")


def send_test_email(cfg):
    try:
        send_email(cfg, "Moodle notifier: test email",
                   "If you're reading this, your Gmail App Password and SMTP settings work.\n"
                   "The real notifier will send messages that look like this when new "
                   "assignments appear.")
        log.info("Test email sent successfully to %s.", cfg["RECIPIENT_EMAIL"])
    except Exception as e:
        log.error("Test email failed: %s", e)
        sys.exit(1)


def dump_events(cfg, n=3):
    """Diagnostic: print the raw parsed fields of the first N events so we can see
    exactly what Moodle's feed includes (e.g. where the course name lives), without
    sending any email or touching state."""
    ics_text = fetch_ics(cfg["ICAL_URL"])
    events = parse_events(ics_text)
    print(f"Total events in feed: {len(events)}\n")
    for ev in events[:n]:
        print(json.dumps(ev, indent=2, ensure_ascii=False))
        print("---")


def main():
    cfg = load_config()
    if "--test-email" in sys.argv:
        send_test_email(cfg)
        return
    if "--rebootstrap" in sys.argv:
        if os.path.exists(STATE_PATH):
            os.remove(STATE_PATH)
        log.info("Cleared local state; next run will re-bootstrap silently.")
        return
    if "--dump-events" in sys.argv:
        dump_events(cfg)
        return
    run_check(cfg)


if __name__ == "__main__":
    main()
