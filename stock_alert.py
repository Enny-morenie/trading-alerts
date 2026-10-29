"""
Telegram stock alerts + rule-based swing-trade entry signals (long and short).
Runs on GitHub Actions - your PC and Chrome can be off.

Modes (set by the workflow):
  intraday  every 5 min in US hours: price levels, big moves, entry triggers, T1/T2/stop updates
  scan      once after the US close: finds new long/short setups on daily candles, sends the plans
  test      sends current prices + any setups found right now, to check everything works

Secrets (GitHub -> Settings -> Secrets and variables -> Actions):
  TELEGRAM_BOT_TOKEN  the token @BotFather gave you
  TELEGRAM_CHAT_ID    your numeric Telegram user ID (from @userinfobot)
Never put either value in this file or in the config file.

Signals are mechanical rules, not advice. Always check the chart before entering.
"""
import csv
import io
import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

CONFIG_FILE = "stock_config.json"
STATE_FILE = "stock_state.json"
REARM_GAP = 0.01        # price-level alerts re-arm after a 1% move back
SETUP_VALID_DAYS = 3    # untriggered setups expire after this many sessions
TRADE_MAX_DAYS = 20     # time stop for tracked trades
UA = {"User-Agent": "Mozilla/5.0 (price-alert)"}


# ---------------------------------------------------------------- data

def http_get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode()


def quote(sym):
    """Latest price and previous close."""
    try:
        data = json.loads(http_get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?interval=1d&range=1d"))
        meta = data["chart"]["result"][0]["meta"]
        prev = meta.get("previousClose") or meta.get("chartPreviousClose")
        return float(meta["regularMarketPrice"]), (float(prev) if prev else None)
    except Exception as e:
        print(f"{sym}: Yahoo quote unavailable ({type(e).__name__}), trying Stooq")
        row = next(csv.DictReader(io.StringIO(
            http_get(f"https://stooq.com/q/l/?s={sym.lower()}.us&f=sd2t2ohlc&h&e=csv"))))
        return float(row["Close"]), None


def daily_bars(sym):
    """Completed daily candles only (drops today's candle while the US market is still open)."""
    bars = _daily_bars_raw(sym)
    try:
        from zoneinfo import ZoneInfo
        ny = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        ny = None
    if bars and ny and bars[-1]["date"] == ny.strftime("%Y-%m-%d") and (ny.hour, ny.minute) < (16, 15):
        bars = bars[:-1]
    return bars


def _daily_bars_raw(sym):
    """About one year of daily candles: list of dicts date/o/h/l/c/v, oldest first."""
    try:
        data = json.loads(http_get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?interval=1d&range=1y"))
        res = data["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        bars = []
        for i, ts in enumerate(res["timestamp"]):
            o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
            if None in (o, h, l, c):
                continue
            bars.append({"date": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"),
                         "o": o, "h": h, "l": l, "c": c, "v": v or 0})
        return bars
    except Exception as e:
        print(f"{sym}: Yahoo history unavailable ({type(e).__name__}), trying Stooq")
        rows = list(csv.DictReader(io.StringIO(http_get(f"https://stooq.com/q/d/l/?s={sym.lower()}.us&i=d"))))
        return [{"date": r["Date"], "o": float(r["Open"]), "h": float(r["High"]), "l": float(r["Low"]),
                 "c": float(r["Close"]), "v": float(r.get("Volume") or 0)} for r in rows[-260:]]


# ---------------------------------------------------------------- indicators

def sma(vals, n):
    out = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        out[i] = sum(vals[i - n + 1:i + 1]) / n
    return out


def ema(vals, n):
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    k = 2 / (n + 1)
    out[n - 1] = sum(vals[:n]) / n
    for i in range(n, len(vals)):
        out[i] = vals[i] * k + out[i - 1] * (1 - k)
    return out


def rsi(closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(closes)):
        if i > n:
            ag = (ag * (n - 1) + gains[i - 1]) / n
            al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr(bars, n=14):
    trs = [bars[0]["h"] - bars[0]["l"]]
    for i in range(1, len(bars)):
        pc = bars[i - 1]["c"]
        trs.append(max(bars[i]["h"] - bars[i]["l"], abs(bars[i]["h"] - pc), abs(bars[i]["l"] - pc)))
    out = [None] * len(bars)
    if len(bars) < n:
        return out
    out[n - 1] = sum(trs[:n]) / n
    for i in range(n, len(bars)):
        out[i] = (out[i - 1] * (n - 1) + trs[i]) / n
    return out


# ---------------------------------------------------------------- candles

def body(b):
    return abs(b["c"] - b["o"])


def is_hammer(b):
    rng = b["h"] - b["l"]
    lower = min(b["o"], b["c"]) - b["l"]
    upper = b["h"] - max(b["o"], b["c"])
    return rng > 0 and lower >= 2 * max(body(b), rng * 0.05) and upper <= rng * 0.25


def is_shooting_star(b):
    rng = b["h"] - b["l"]
    lower = min(b["o"], b["c"]) - b["l"]
    upper = b["h"] - max(b["o"], b["c"])
    return rng > 0 and upper >= 2 * max(body(b), rng * 0.05) and lower <= rng * 0.25


def bull_engulf(p, b):
    return p["c"] < p["o"] and b["c"] > b["o"] and b["c"] >= p["o"] and b["o"] <= p["c"]


def bear_engulf(p, b):
    return p["c"] > p["o"] and b["c"] < b["o"] and b["c"] <= p["c"] and b["o"] >= p["o"]


# ---------------------------------------------------------------- setup detection

def find_setups(bars, market_up=None):
    """Check the LAST bar for setups. Returns a list of setup dicts (may be empty)."""
    if len(bars) < 60:
        return []
    closes = [b["c"] for b in bars]
    vols = [b["v"] for b in bars]
    s50, s200, e20 = sma(closes, 50), sma(closes, 200), ema(closes, 20)
    r, a, v20 = rsi(closes), atr(bars), sma(vols, 20)
    i = len(bars) - 1
    b, p = bars[i], bars[i - 1]
    A = a[i]
    if not A or not e20[i] or not s50[i] or r[i] is None or r[i - 1] is None:
        return []
    hi20 = max(x["h"] for x in bars[i - 20:i])
    lo20 = min(x["l"] for x in bars[i - 20:i])
    vol_ratio = (b["v"] / v20[i]) if v20[i] else 0
    above200 = s200[i] is not None and b["c"] > s200[i]
    below200 = s200[i] is not None and b["c"] < s200[i]
    found = []

    def add(direction, name, why, base):
        score = base
        notes = list(why)
        if vol_ratio >= 1.5:
            score += 1
            notes.append(f"volume {vol_ratio:.1f}x average")
        if direction == "LONG" and above200:
            score += 1
            notes.append("above 200-day average")
        if direction == "SHORT" and below200:
            score += 1
            notes.append("below 200-day average")
        if market_up is not None:
            if (direction == "LONG") == market_up:
                score += 1
                notes.append("S&P 500 trend agrees")
            else:
                score -= 1
                notes.append("against S&P 500 trend")
        found.append({"direction": direction, "name": name, "score": score, "why": notes})

    # LONG setups
    if b["c"] > hi20 and vol_ratio >= 1.5 and b["c"] > s50[i]:
        add("LONG", "Breakout", [f"closed above 20-day high {hi20:.2f}"], 2)
    if (e20[i] > s50[i] and b["c"] > s50[i] and min(x["l"] for x in bars[i - 2:i + 1]) <= e20[i] * 1.005
            and min(r[i - 5:i + 1]) < 50 and r[i] > r[i - 1] and b["c"] > b["o"] and b["c"] > p["c"]):
        add("LONG", "Trend pullback", ["uptrend (20 EMA > 50 SMA)", "pulled back to 20 EMA and bounced",
                                       f"RSI turning up ({r[i]:.0f})"], 2)
    if r[i - 1] < 32 and (is_hammer(b) or bull_engulf(p, b)):
        add("LONG", "Oversold reversal", [f"RSI was oversold ({r[i - 1]:.0f})",
                                          "hammer" if is_hammer(b) else "bullish engulfing"], 1)

    # SHORT setups
    if b["c"] < lo20 and vol_ratio >= 1.5 and b["c"] < s50[i]:
        add("SHORT", "Breakdown", [f"closed below 20-day low {lo20:.2f}"], 2)
    if (e20[i] < s50[i] and b["c"] < s50[i] and max(x["h"] for x in bars[i - 2:i + 1]) >= e20[i] * 0.995
            and max(r[i - 5:i + 1]) > 50 and r[i] < r[i - 1] and b["c"] < b["o"] and b["c"] < p["c"]):
        add("SHORT", "Trend rally short", ["downtrend (20 EMA < 50 SMA)", "rallied to 20 EMA and rejected",
                                           f"RSI turning down ({r[i]:.0f})"], 2)
    if r[i - 1] > 70 and (is_shooting_star(b) or bear_engulf(p, b)):
        add("SHORT", "Overbought rejection", [f"RSI was overbought ({r[i - 1]:.0f})",
                                              "shooting star" if is_shooting_star(b) else "bearish engulfing"], 1)

    # Build the trade plan for each setup
    plans = []
    for s in found:
        if s["direction"] == "LONG":
            trigger = b["h"] + max(0.01, 0.1 * A)
            struct = min(x["l"] for x in bars[i - 2:i + 1]) - 0.25 * A
            risk = min(max(trigger - struct, 1.0 * A), 3.0 * A)
            stop = trigger - risk
            t1, t2 = trigger + 2 * risk, trigger + 3 * risk
        else:
            trigger = b["l"] - max(0.01, 0.1 * A)
            struct = max(x["h"] for x in bars[i - 2:i + 1]) + 0.25 * A
            risk = min(max(struct - trigger, 1.0 * A), 3.0 * A)
            stop = trigger + risk
            t1, t2 = trigger - 2 * risk, trigger - 3 * risk
        if stop <= 0 or t2 <= 0:
            continue
        conf = "High" if s["score"] >= 5 else "Medium" if s["score"] >= 3 else "Low"
        plans.append({**s, "confidence": conf, "trigger": round(trigger, 2), "stop": round(stop, 2),
                      "t1": round(t1, 2), "t2": round(t2, 2), "atr": round(A, 2), "bar_date": b["date"]})
    return plans


# ---------------------------------------------------------------- helpers

def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID secrets")
    for i in range(0, len(text), 3900):  # Telegram limit is 4096 characters per message
        body_ = urllib.parse.urlencode({"chat_id": chat_id, "text": text[i:i + 3900],
                                        "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body_)
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                r.read()
            print("Telegram message sent")
        except urllib.error.HTTPError as e:
            # Print Telegram's own reason (never contains the token); never print the URL
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


_FX = {}


def usd_per(currency):
    """How many USD one unit of the account currency buys (1.0 for USD)."""
    currency = (currency or "USD").upper()
    if currency == "USD":
        return 1.0
    if currency not in _FX:
        try:
            _FX[currency] = quote(f"{currency}USD=X")[0]
        except Exception as e:
            print(f"FX rate {currency}USD unavailable ({type(e).__name__})")
            _FX[currency] = None
    return _FX[currency]


def size_line(cfg, plan):
    acct = cfg.get("account", {})
    cur = (acct.get("currency") or "USD").upper()
    size = acct.get("size") or acct.get("size_usd")
    if not size:
        return "Size: add your account size in stock_config.json to get share counts"
    fx = usd_per(cur)
    if not fx:
        return "Size: currency rate unavailable right now, size manually (risk 1% of account)"
    sym = {"EUR": "€", "GBP": "£", "USD": "$"}.get(cur, cur + " ")
    size_usd = size * fx
    risk_pct = acct.get("risk_pct", 1)
    risk_usd = size_usd * risk_pct / 100
    per_share = abs(plan["trigger"] - plan["stop"])
    if per_share <= 0:
        return ""
    raw = min(risk_usd / per_share, size_usd * acct.get("max_position_pct", 20) / 100 / plan["trigger"])
    shares = math.floor(raw * 100) / 100 if acct.get("fractional", True) else math.floor(raw)
    if shares <= 0:
        return "Size: too small for whole shares at this price. Skip, or use fractional shares."
    pos = shares * plan["trigger"] / fx
    risk = shares * per_share / fx
    return (f"Size: {shares:g} shares ≈ {sym}{pos:,.0f} position, "
            f"risk ≈ {sym}{risk:,.2f} ({risk_pct}% of account)")


def plan_text(sym, p, cfg):
    arrow = "🟢 LONG" if p["direction"] == "LONG" else "🔴 SHORT"
    cond = "rises above" if p["direction"] == "LONG" else "falls below"
    lines = [f"{arrow} {sym}: {p['name']} ({p['confidence']} confidence)",
             f"Why: {'; '.join(p['why'])}",
             f"Entry: only if price {cond} {fmt(p['trigger'])}",
             f"Stop: {fmt(p['stop'])}",
             f"Target 1: {fmt(p['t1'])} (2R)   Target 2: {fmt(p['t2'])} (3R)",
             size_line(cfg, p),
             f"Valid for {SETUP_VALID_DAYS} sessions. Check the chart and earnings dates first."]
    return "\n".join(lines)


def market_trend():
    try:
        closes = [b["c"] for b in daily_bars("SPY")]
        s50 = sma(closes, 50)[-1]
        return closes[-1] > s50 if s50 else None
    except Exception as e:
        print(f"SPY trend unavailable ({type(e).__name__})")
        return None


RANK = {"Low": 0, "Medium": 1, "High": 2}


def wanted(cfg, p):
    sig = cfg.get("signals", {})
    if not sig.get("enabled", True):
        return False
    if p["direction"] == "LONG" and not sig.get("longs", True):
        return False
    if p["direction"] == "SHORT" and not sig.get("shorts", True):
        return False
    return RANK[p["confidence"]] >= RANK.get(sig.get("min_confidence", "Medium"), 1)


# ---------------------------------------------------------------- modes

def run_scan(cfg, state, test=False):
    tickers = cfg.get("tickers", {})
    mkt = market_trend()
    msgs, scanned = [], None
    for sym in tickers:
        st = state.setdefault(sym, {})
        try:
            bars = daily_bars(sym)
        except Exception as e:
            print(f"{sym}: no history ({type(e).__name__})")
            continue
        if not bars:
            continue
        last_date = bars[-1]["date"]
        new_session = st.get("last_scan_bar") != last_date
        if new_session and not test:
            st["last_scan_bar"] = last_date
            scanned = last_date
            # age pending setups and open trades by one session
            st["setups"] = [s for s in st.get("setups", []) if s.get("age", 0) + 1 <= SETUP_VALID_DAYS]
            for s in st["setups"]:
                s["age"] = s.get("age", 0) + 1
            for t in st.get("trades", []):
                t["age"] = t.get("age", 0) + 1
            expired = [t for t in st.get("trades", []) if t["age"] >= TRADE_MAX_DAYS]
            for t in expired:
                msgs.append(f"⏱ {sym} {t['direction']} ({t['name']}): {TRADE_MAX_DAYS}-day time stop. "
                            f"Consider closing if still open.")
            st["trades"] = [t for t in st.get("trades", []) if t["age"] < TRADE_MAX_DAYS]
        if not new_session and not test:
            continue
        for p in find_setups(bars, mkt):
            print(f"{sym}: {p['direction']} {p['name']} score={p['score']} conf={p['confidence']}")
            if not wanted(cfg, p):
                continue
            msgs.append(plan_text(sym, p, cfg))
            if not test:
                p["age"] = 0
                st.setdefault("setups", []).append(p)
    if test:
        return msgs, mkt
    return msgs, mkt, scanned


def run_intraday(cfg, state):
    tickers = cfg.get("tickers", {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    msgs, quotes = [], []
    for sym, rules in tickers.items():
        try:
            price, prev = quote(sym)
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

        # price levels
        above, below, move = rules.get("above"), rules.get("below"), rules.get("move_pct")
        if above is not None:
            if price >= above and not st["above_fired"]:
                msgs.append(f"📈 {sym} UP at {fmt(price)}, crossed above {fmt(above)}")
                st["above_fired"] = True
            elif price < above * (1 - REARM_GAP):
                st["above_fired"] = False
        if below is not None:
            if price <= below and not st["below_fired"]:
                msgs.append(f"📉 {sym} DOWN at {fmt(price)}, crossed below {fmt(below)}")
                st["below_fired"] = True
            elif price > below * (1 + REARM_GAP):
                st["below_fired"] = False
        if move is not None and chg is not None and abs(chg) >= move and not st["move_fired"]:
            msgs.append(f"⚡ {sym} big move today: {chg:+.1f}% at {fmt(price)}")
            st["move_fired"] = True

        # pending setups -> triggered or invalidated
        keep = []
        for s in st.get("setups", []):
            long_ = s["direction"] == "LONG"
            if (long_ and price >= s["trigger"]) or (not long_ and price <= s["trigger"]):
                msgs.append(f"🚨 ENTRY TRIGGERED: {sym} {s['direction']} ({s['name']}) at {fmt(price)}\n"
                            f"Stop {fmt(s['stop'])} · T1 {fmt(s['t1'])} · T2 {fmt(s['t2'])}\n"
                            f"{size_line(cfg, s)}")
                st.setdefault("trades", []).append({**s, "entry": price, "t1_hit": False, "age": 0})
            elif (long_ and price <= s["stop"]) or (not long_ and price >= s["stop"]):
                msgs.append(f"✖️ {sym} {s['direction']} setup cancelled: hit {fmt(s['stop'])} before triggering")
            else:
                keep.append(s)
        st["setups"] = keep

        # open trades -> T1 / T2 / stop
        open_ = []
        for t in st.get("trades", []):
            long_ = t["direction"] == "LONG"
            stop_hit = price <= t["stop"] if long_ else price >= t["stop"]
            t1_hit = price >= t["t1"] if long_ else price <= t["t1"]
            t2_hit = price >= t["t2"] if long_ else price <= t["t2"]
            if stop_hit:
                label = "break-even stop" if t["t1_hit"] else "stop"
                msgs.append(f"🛑 {sym} {t['direction']}: {label} hit at {fmt(price)}. Trade closed.")
                continue
            if t2_hit:
                msgs.append(f"🏁 {sym} {t['direction']}: Target 2 hit at {fmt(price)}. Take profit or trail the rest.")
                continue
            if t1_hit and not t["t1_hit"]:
                t["t1_hit"] = True
                t["stop"] = t["entry"]
                msgs.append(f"🎯 {sym} {t['direction']}: Target 1 hit at {fmt(price)}. "
                            f"Consider taking partial profit and moving the stop to break-even ({fmt(t['entry'])}).")
            open_.append(t)
        st["trades"] = open_
    return msgs, quotes


def main():
    cfg = load(CONFIG_FILE, {})
    if "tickers" not in cfg:  # old flat config format
        cfg = {"tickers": cfg}
    state = load(STATE_FILE, {})
    mode = os.environ.get("MODE", "intraday")
    print("Mode:", mode)

    if mode == "test":
        _, quotes = run_intraday(cfg, {})
        setups, mkt = run_scan(cfg, {}, test=True)
        trend = {True: "up", False: "down", None: "unknown"}[mkt]
        send_telegram("✅ Test: setup works\n" + "\n".join(quotes) + f"\nS&P 500 trend: {trend}\n\n" +
                      ("Setups on the latest daily candle:\n\n" + "\n\n".join(setups) if setups
                       else "No setups on the latest daily candle right now."))
    else:
        # Daily scan: runs on whichever run first sees a newly completed daily candle,
        # so a skipped evening run is caught up on the next run.
        msgs, mkt, scanned = run_scan(cfg, state)
        if scanned:
            pending = sum(len(state.get(t, {}).get("setups", [])) for t in cfg.get("tickers", {}))
            open_ = sum(len(state.get(t, {}).get("trades", [])) for t in cfg.get("tickers", {}))
            trend = {True: "up", False: "down", None: "unknown"}[mkt]
            head = f"📊 Daily scan ({scanned} close) · S&P 500 trend: {trend}\n"
            foot = f"\n\nWatching {pending} setup(s), {open_} open trade(s). Rule-based signals, not financial advice."
            if msgs:
                send_telegram(head + "\n" + "\n\n".join(msgs) + foot)
            else:
                send_telegram(head + "No new setups today." + foot)
        else:
            print("Daily scan already done for the latest candle")

        alerts, quotes = run_intraday(cfg, state)
        print("Quotes:", "; ".join(quotes) or "none")
        if alerts:
            send_telegram("\n\n".join(alerts))
        else:
            print("No intraday alerts")

    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


if __name__ == "__main__":
    main()
