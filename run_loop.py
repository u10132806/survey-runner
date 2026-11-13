"""Run fill_survey2.py in a loop until interrupted.

Local:  python run_loop.py
Render: same command; it also serves /health on $PORT so Render's free
        web service accepts it.

If DATABASE_URL is set (Neon connection string), every new row that
fill_survey2.py appends to submissions.csv is copied into the Neon
`submissions` table. Without DATABASE_URL it behaves like before.

One-off import of the existing CSV into Neon:
    python run_loop.py --import-csv
"""
import csv
import io
import os
import random
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "submissions.csv")

# ---------- disabled ----------
# The survey runner is switched off. It only reports "closed": on Render
# (PORT set) it serves "closed" over HTTP so the web service stays up;
# locally it prints "closed" and exits.
if __name__ == "__main__":
    print("closed", flush=True)
    _port = os.environ.get("PORT")
    if _port:
        class _Closed(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"closed\n")

            def do_HEAD(self):
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        HTTPServer(("0.0.0.0", int(_port)), _Closed).serve_forever()
    sys.exit(0)

import re

from faker import Faker

fake = Faker(["zu_ZA", "en_US"])


def _clean(part: str) -> str:
    """Lowercase and keep only a-z (drops spaces, apostrophes, hyphens, accents)."""
    return re.sub(r"[^a-z]", "", part.lower())


def generate_random_email() -> str:
    """
    Generates a random email address in the format:
    {firstname}.{surname}@stitchs.co.za
    """
    first_name = _clean(fake.first_name())
    surname = _clean(fake.last_name())

    return f"{first_name}.{surname}@stitchs.co.za"

def load_dotenv(path=os.path.join(HERE, ".env")):
    """Load KEY=VALUE lines from a local .env (not committed) into os.environ."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip('"'))
    except OSError:
        pass


load_dotenv()

PHONE = os.environ.get("SURVEY_PHONE")
EMAIL_USER = os.environ.get("SURVEY_EMAIL_USER")
if not PHONE or not EMAIL_USER:
    sys.exit("Set SURVEY_PHONE and SURVEY_EMAIL_USER (in .env locally, or env vars on Render).")
EMAIL_DOMAIN = os.environ.get("SURVEY_EMAIL_DOMAIN", "gmail.com")
DELAY_SECONDS = float(os.environ.get("DELAY_SECONDS", "1"))
DATABASE_URL = os.environ.get("DATABASE_URL")
BACKOFF_BASE_SECONDS = float(os.environ.get("BACKOFF_BASE_SECONDS", "5"))
BACKOFF_MAX_SECONDS = float(os.environ.get("BACKOFF_MAX_SECONDS", "900"))

state = {
    "runs": 0, "ok": 0, "failed": 0,
    "last_email": None, "last_exit": None, "last_status": None, "saved": 0,
}


def run_succeeded(rows, exit_code):
    """A run counts as OK if fill_survey2.py logged status 'ok' for it.
    If it logged nothing, fall back to the exit code."""
    if rows:
        return all((r[4] or "").strip().lower() == "ok" for r in rows)
    return exit_code == 0


def is_unauthorized(status):
    """True if the logged status for a run looks like an HTTP 401."""
    s = (status or "").lower()
    return "401" in s or "unauthorized" in s


def backoff_delay(attempt):
    """Exponential backoff: base * 2**(attempt-1), capped, with jitter in [ceiling/2, ceiling]."""
    ceiling = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))
    return random.uniform(ceiling / 2, ceiling)


def stats_line():
    runs = state["runs"]
    rate = f"{state['ok'] / runs * 100:.1f}%" if runs else "n/a"
    return f"runs={runs} ok={state['ok']} failed={state['failed']} success_rate={rate}"


csv_totals = {"ok": 0, "failed": 0, "unknown": 0, "reasons": {}}


def csv_count(rows):
    """Add rows (from submissions.csv) into the all-time CSV totals."""
    for r in rows:
        status = (r[4] or "").strip()
        if not status:
            csv_totals["unknown"] += 1
        elif status.lower() == "ok":
            csv_totals["ok"] += 1
        else:
            csv_totals["failed"] += 1
            csv_totals["reasons"][status] = csv_totals["reasons"].get(status, 0) + 1


def csv_stats_line():
    t = csv_totals
    judged = t["ok"] + t["failed"]
    rate = f"{t['ok'] / judged * 100:.1f}%" if judged else "n/a"
    extra = f" no_status={t['unknown']}" if t["unknown"] else ""
    return f"CSV all-time: total={judged} ok={t['ok']} failed={t['failed']} success_rate={rate}{extra}"


def csv_report():
    lines = [csv_stats_line()]
    reasons = sorted(csv_totals["reasons"].items(), key=lambda kv: -kv[1])
    if reasons:
        lines.append("  failure reasons:")
        lines += [f"    {n:>5}  {reason}" for reason, n in reasons]
    return "\n".join(lines)


def db_all_time_stats():
    """All-time totals from Neon (every row ever saved), or None."""
    if not DATABASE_URL:
        return None
    try:
        with db_connect() as conn:
            ok, failed = conn.execute(
                "SELECT count(*) FILTER (WHERE lower(status) = 'ok'), "
                "       count(*) FILTER (WHERE lower(coalesce(status, '')) <> 'ok') "
                "FROM submissions"
            ).fetchone()
        return f"all_time_ok={ok} all_time_failed={failed}"
    except Exception as e:
        return f"all_time=unavailable ({e})"

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS submissions (
    id         BIGSERIAL PRIMARY KEY,
    name       TEXT,
    surname    TEXT,
    email      TEXT,
    phone      TEXT,
    status     TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


# ---------- Neon ----------

def db_connect():
    import psycopg  # only needed when DATABASE_URL is set
    return psycopg.connect(DATABASE_URL, connect_timeout=15)


def db_init():
    with db_connect() as conn:
        conn.execute(CREATE_TABLE)


def parse_rows(text):
    rows = []
    for fields in csv.reader(io.StringIO(text)):
        if not fields or fields[0].lstrip("﻿").lower() == "name":
            continue  # blank line or header
        fields = (fields + [None] * 5)[:5]
        rows.append(tuple(fields))
    return rows


def db_insert(rows):
    if not rows:
        return
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO submissions (name, surname, email, phone, status) "
                "VALUES (%s, %s, %s, %s, %s)",
                rows,
            )
    state["saved"] += len(rows)


def csv_size():
    try:
        return os.path.getsize(CSV_PATH)
    except OSError:
        return 0


def read_csv_from(offset):
    try:
        with open(CSV_PATH, "rb") as f:
            f.seek(offset)
            return f.read().decode("utf-8-sig", errors="replace")
    except OSError:
        return ""


# ---------- health endpoint for Render ----------

class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        lines = [
            "this session: " + stats_line(),
            f"last: email={state['last_email']} exit={state['last_exit']} "
            f"status={state['last_status']}",
            f"saved_to_neon={state['saved']}",
        ]
        if self.path.startswith("/stats"):
            all_time = db_all_time_stats()
            if all_time:
                lines.append("neon: " + all_time)
        body = ("\n".join(lines) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass  # keep logs quiet


def start_health_server():
    port = os.environ.get("PORT")
    if not port:
        return
    server = HTTPServer(("0.0.0.0", int(port)), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"health server on port {port}", flush=True)


# ---------- main loop ----------

def main():
    if "--import-csv" in sys.argv:
        if not DATABASE_URL:
            sys.exit("Set DATABASE_URL first.")
        db_init()
        rows = parse_rows(read_csv_from(0))
        db_insert(rows)
        print(f"Imported {len(rows)} row(s) into Neon.")
        return

    csv_count(parse_rows(read_csv_from(0)))
    if "--stats" in sys.argv:
        print(csv_report())
        return
    print(csv_report(), flush=True)

    start_health_server()
    if DATABASE_URL:
        db_init()
        print("Neon storage enabled", flush=True)

    consecutive_401s = 0
    try:
        while True:
            state["runs"] += 1
            run = state["runs"]
            #email = f"{EMAIL_USER}+{random.randint(100000, 999999)}@{EMAIL_DOMAIN}"
            email = f"{generate_random_email()}"
            state["last_email"] = email
            print(f"[run {run}] {email}", flush=True)

            before = csv_size()
            result = subprocess.run(
                [sys.executable, "fill_survey2.py", email, PHONE], cwd=HERE
            )
            state["last_exit"] = result.returncode
            rows = parse_rows(read_csv_from(before))
            csv_count(rows)

            if run_succeeded(rows, result.returncode):
                state["ok"] += 1
                outcome = "OK"
            else:
                state["failed"] += 1
                outcome = "FAILED"
            state["last_status"] = (rows[-1][4] if rows else None) or f"exit {result.returncode}"
            print(f"[run {run}] {outcome} ({state['last_status']})", flush=True)
            print(f"    session: {stats_line()}", flush=True)
            print(f"    {csv_stats_line()}", flush=True)

            if DATABASE_URL:
                try:
                    db_insert(rows)
                    if rows:
                        print(f"[run {run}] saved {len(rows)} row(s) to Neon", flush=True)
                except Exception as e:  # don't let a DB hiccup kill the loop
                    print(f"[run {run}] Neon insert failed: {e}", flush=True)

            if is_unauthorized(state["last_status"]):
                consecutive_401s += 1
                delay = backoff_delay(consecutive_401s)
                print(f"[run {run}] 401 #{consecutive_401s} in a row, "
                      f"backing off {delay:.1f}s", flush=True)
            else:
                if consecutive_401s:
                    print(f"[run {run}] recovered after {consecutive_401s} 401(s)", flush=True)
                consecutive_401s = 0
                delay = DELAY_SECONDS
            time.sleep(delay)
    except KeyboardInterrupt:
        print(f"\nStopped. session: {stats_line()}")
        print(csv_report())


if __name__ == "__main__":
    main()
