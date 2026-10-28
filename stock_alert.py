"""
Telegram price alerts for US stocks (APLD, IREN, MU, TE).
Runs on GitHub Actions every ~5 minutes during US market hours - your PC and Chrome can be off.

Secrets (GitHub -> Settings -> Secrets and variables -> Actions):
  TELEGRAM_BOT_TOKEN  the token @BotFather gave you
  TELEGRAM_CHAT_ID    your numeric Telegram user ID (from @userinfobot)
Never put either value in this file or in the config file.
"""
import csv
import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

REARM_GAP = 0.01           # price must move 1% back past a level before that alert can fire again
STATE_FILE = "stock_state.json"
UA = {"User-Agent": "Mozilla/5.0 (price-alert)"}


def http_get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode()


def quote_yahoo(sym):
    data = json.loads(http_get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?interval=1d&range=1d"))
    meta = data["chart"]["result"][0]["meta"]
    prev = meta.get("previousClose") or meta.get("chartPreviousClose")
    return float(meta["regularMarketPrice"]), (float(prev) if prev else None)


def quote_stooq(sym):
    text = http_get(f"https://stooq.com/q/l/?s={sym.lower()}.us&f=sd2t2ohlc&h&e=csv")
    row = next(csv.DictReader(io.StringIO(text)))
    return float(row["Close"]), None  # no previous close from this source


def fetch_quote(sym):
    try:
        return (*quote_yahoo(sym), "Yahoo")
    except Exception as e:
        print(f"{sym}: Yahoo unavailable ({type(e).__name__}), trying Stooq")
        return (*quote_stooq(sym), "Stooq")


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID secrets")
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            r.read()
        print("Telegram message sent")
    except urllib.error.HTTPError as e:
        # Print Telegram's own reason (it never contains the token); never print the URL
        try:
            reason = json.loads(e.read().decode()).get("description", "")
        except Exception:
            reason = ""
        print(f"Telegram send failed: HTTP {e.code} {reason}")
        hints = {401: "-> TELEGRAM_BOT_TOKEN is wrong. Re-copy it from BotFather.",
                 400: "-> TELEGRAM_CHAT_ID is wrong, or you haven't pressed Start in your bot's chat.",
                 403: "-> You haven't pressed Start in your bot's chat, or you blocked the bot.",
                 404: "-> TELEGRAM_BOT_TOKEN is wrong or incomplete. Re-copy it from BotFather."}
        print(hints.get(e.code, ""))
    except Exception as e:
        print(f"Telegram send failed: {type(e).__name__}")


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def fmt(p):
    return f"${p:,.2f}"


def main():
    config = load("stock_config.json", {})
    state = load(STATE_FILE, {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    messages, quotes = [], []

    for sym, rules in config.items():
        try:
            price, prev, src = fetch_quote(sym)
        except Exception as e:
            print(f"{sym}: no price ({type(e).__name__})")
            continue
        chg = (price / prev - 1) * 100 if prev else None
        quotes.append(f"{sym}: {fmt(price)}" + (f" ({chg:+.1f}%)" if chg is not None else ""))
        st = state.setdefault(sym, {})
        st.setdefault("above_fired", False)
        st.setdefault("below_fired", False)
        if st.get("move_day") != today:
            st["move_day"], st["move_fired"] = today, False

        above = rules.get("above")
        if above is not None:
            if price >= above and not st["above_fired"]:
                messages.append(f"{sym} UP at {fmt(price)} - crossed above {fmt(above)}")
                st["above_fired"] = True
            elif price < above * (1 - REARM_GAP):
                st["above_fired"] = False

        below = rules.get("below")
        if below is not None:
            if price <= below and not st["below_fired"]:
                messages.append(f"{sym} DOWN at {fmt(price)} - crossed below {fmt(below)}")
                st["below_fired"] = True
            elif price > below * (1 + REARM_GAP):
                st["below_fired"] = False

        move = rules.get("move_pct")
        if move is not None and chg is not None and abs(chg) >= move and not st["move_fired"]:
            messages.append(f"{sym} big move today: {chg:+.1f}% at {fmt(price)}")
            st["move_fired"] = True

    print("Quotes:", "; ".join(quotes) or "none")

    if os.environ.get("ALERT_TEST") == "1":
        send_telegram("Stock alert test - setup works\n" + "\n".join(quotes))

    if messages:
        send_telegram("Stock alert\n" + "\n".join(messages))
    else:
        print("No alerts triggered")

    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


if __name__ == "__main__":
    main()
