#!/usr/bin/env python3
"""
Qhawaq Ticket Watcher
=====================

Watches a Joinnus event page with a real headless browser (Playwright) and
sends a Telegram alert the moment a ticket tier or add-on benefit changes
state - for example, when a sold-out fan-meeting benefit (signed photo,
fan sign, polaroid) is restocked, when a brand-new tier appears, or when
something sells out.

Joinnus serves the ticket list inside server-rendered HTML and protects the
site against naive HTTP clients, so a plain `requests` call gets blocked.
A real browser is used instead to render and read the page like a human would.

This tool only reads a public page and notifies the user. It does NOT bypass
queues, solve CAPTCHAs, or purchase anything - the user buys through the normal
flow from the link in the alert. Polling is deliberately gentle.

Configuration is read from environment variables (see .env.example).
"""

import os
import re
import time
import random
from datetime import datetime

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

# ----------------------------------------------------------------------
# Configuration (from environment variables / .env file)
# ----------------------------------------------------------------------
load_dotenv()

# The event page to watch. Required - set it in .env or as an env var.
EVENT_URL = os.environ.get("EVENT_URL", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Seconds between checks. 120s is polite to the server.
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "120"))

# Run the browser without a visible window. Set HEADLESS=false to watch it.
HEADLESS = os.environ.get("HEADLESS", "true").lower() != "false"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# Chromium flags that keep it stable on small/low-RAM servers and inside
# Docker (where it runs as root): no sandbox, don't rely on /dev/shm, no GPU.
BROWSER_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]


# ----------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------
def send_telegram(text: str) -> None:
    """Send a message to the configured Telegram chat."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=15,
        )
        print(f"[telegram] {r.status_code}: {r.text[:200]}")
    except requests.RequestException as e:
        print(f"[!] Could not reach Telegram: {e}")


# ----------------------------------------------------------------------
# Page parsing
# ----------------------------------------------------------------------
def parse_status(text: str) -> dict:
    """
    Turn the ticket-section text into {name: 'available' | 'sold_out'}.

    On the page each row reads: NAME, (optionally 'Detalles'), 'S/ price',
    then the state: a number (a quantity selector = available) or 'AGOTADAS'.
    ('Detalles' and 'agotad' are the site's literal Spanish labels, matched
    verbatim - translating them would break detection.)
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    status = {}
    for i, ln in enumerate(lines):
        if re.match(r"^S/\s*[\d.,]+$", ln):  # this line is a price
            # the name is the previous meaningful line (skipping 'Detalles')
            name = None
            for j in range(i - 1, -1, -1):
                prev = lines[j]
                if prev.lower() == "detalles" or re.match(r"^S/\s*[\d.,]+$", prev):
                    continue
                name = prev
                break
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if name:
                sold_out = "agotad" in nxt.lower()
                status[name] = "sold_out" if sold_out else "available"
    return status


def read_page(page) -> dict:
    """Load the event page and return the parsed status of every row."""
    # Wait for the DOM, not for full network silence: sites like Joinnus keep
    # background connections (analytics, etc.) open, so "networkidle" can time
    # out. We instead wait for the ticket prices to actually appear.
    page.goto(EVENT_URL, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_selector(r"text=/S\/\s*\d/", timeout=30000)
    except Exception:
        pass
    return parse_status(page.inner_text("main"))


def get_event_title(page) -> str:
    """Read the event name from the page title, kept generic across sites.

    The browser tab title usually looks like 'EVENT NAME | SiteName', so we
    drop the site suffix and tidy the casing.
    """
    try:
        raw = (page.title() or "").strip()
    except Exception:
        return ""
    # Drop a trailing " | Site" or " - Site" suffix.
    title = re.split(r"\s[|\-–]\s", raw)[0].strip()
    return title.title() if title.isupper() else title


def describe_changes(before: dict, now: dict) -> str:
    """Build a human-readable summary of what changed between two checks."""
    parts = []
    restocked = [n for n in now
                 if now[n] == "available" and before.get(n) == "sold_out"]
    new = [n for n in now if n not in before]
    sold_out = [n for n in now
                if now[n] == "sold_out" and before.get(n) == "available"]

    if restocked:
        parts.append("✅ <b>YA DISPONIBLE(S):</b>\n- " + "\n- ".join(restocked))
    if new:
        parts.append("🆕 Nueva(s) categoría(s):\n- "
                     + "\n- ".join(f"{n} ({now[n]})" for n in new))
    if sold_out:
        parts.append("⛔ Se agotaron:\n- " + "\n- ".join(sold_out))
    return "\n\n".join(parts)


# ----------------------------------------------------------------------
# Main loop
# ----------------------------------------------------------------------
def main() -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not EVENT_URL:
        print("[!] Missing EVENT_URL, TELEGRAM_TOKEN or TELEGRAM_CHAT_ID. "
              "Set them in a .env file or as environment variables.")
        return

    print(f"[i] Watching: {EVENT_URL}")
    print(f"[i] Token loaded: {TELEGRAM_TOKEN[:12]}...  Chat: {TELEGRAM_CHAT_ID}")
    print(f"[i] Checking every {POLL_INTERVAL}s. Press Ctrl+C to stop.\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, args=BROWSER_ARGS)
        ctx = browser.new_context(
            user_agent=USER_AGENT,
            locale="es-PE",
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()

        # Initial read
        try:
            previous = read_page(page)
            event_title = get_event_title(page)
        except Exception as e:
            print(f"[!] Failed on startup: {e}")
            browser.close()
            return

        title_line = f"<b>{event_title}</b>\n" if event_title else ""

        if not previous:
            send_telegram("🟡 El monitor arrancó pero no pudo leer las entradas "
                          "(la página podría estar bloqueando). Reintentando...")
            print("[!] No tickets read on startup.")
        else:
            summary = "\n".join(
                f"• {n}: {'disponible' if s == 'available' else 'AGOTADA'}"
                for n, s in previous.items()
            )
            send_telegram(f"🟢 <b>Monitor activo</b>\n{title_line}\n"
                          f"Estado actual:\n{summary}\n\n"
                          "Te aviso en cuanto algo cambie.")
            print(f"[{datetime.now():%H:%M:%S}] Initial state:")
            for n, s in previous.items():
                print(f"    {n}: {s}")

        # Polling loop
        while True:
            time.sleep(POLL_INTERVAL + random.randint(-10, 10))
            try:
                current = read_page(page)
            except Exception as e:
                print(f"[{datetime.now():%H:%M:%S}] Read error: {e}")
                continue

            if not current:
                print(f"[{datetime.now():%H:%M:%S}] No data (possible block).")
                continue

            changes = describe_changes(previous, current)
            if changes:
                send_telegram(f"🔔 <b>CAMBIO EN EL EVENTO</b>\n{title_line}\n"
                              f"{changes}\n\n👉 Entra ya:\n{EVENT_URL}")
                print(f"[{datetime.now():%H:%M:%S}] Change alerted:\n{changes}")
                previous = current
            else:
                print(f"[{datetime.now():%H:%M:%S}] No changes.")


if __name__ == "__main__":
    main()
