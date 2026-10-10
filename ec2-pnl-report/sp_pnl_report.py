#!/usr/bin/env python3
"""SP bots: one daily P&L summary for every bot on this EC2 box, sent to Telegram.

Reads each bot's own trade files (read-only), checks its systemd service and its log for
errors, and sends one message. Standard library only, Python 3.7+. Places no orders and
talks to no broker: the only network call is the Telegram send.

    python3 sp_pnl_report.py --dry-run             # print today's report, send nothing
    python3 sp_pnl_report.py                       # send today's report
    python3 sp_pnl_report.py --date 2026-10-09     # any earlier day
    python3 sp_pnl_report.py --discover            # list sp-* services and trade files on this box
    python3 sp_pnl_report.py --selftest            # offline checks, no network

Config: bots.json next to this file (or --config / PNL_REPORT_CONFIG). Each bot names its
trade file, the P&L field and the exit-time field; see README.md.

Telegram: TELEGRAM_TOKEN (or TELEGRAM_BOT_TOKEN) and TELEGRAM_CHAT_ID, read from the
environment, then SP_ENV_PATH, then /run/spbots/.env, then /opt/spbots/.env.
PNL_REPORT_CHAT_ID, when set, sends the report to a different chat.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

VERSION = "1.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "bots.json")
ENV_FILES = ("/run/spbots/.env", "/opt/spbots/.env")
DISCOVER_ROOTS = ("/opt/spbots", "/home/ubuntu", "/home/ec2-user", "/home/spbot")
TG_LIMIT = 4000                      # Telegram rejects messages over 4096 characters

# Tried in order when a bot's config does not name the field. "net" comes before "pnl" so a
# file with gross, cost and net columns reports net.
PNL_KEYS = ("net", "net_pnl", "pnl_net", "net_pnl_usd", "realised_pnl", "realized_pnl",
            "pnl_usd", "pnl", "profit", "pl")
TIME_KEYS = ("exit_ts", "exit_time", "closed", "closed_ist", "closed_at", "close_time",
             "exit_at", "ts", "timestamp", "time", "date")
ERROR_RE = re.compile(r"\b(ERROR|CRITICAL)\b")
TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")


# ─────────────────────────────────────────────────────────────────────────────
#  Time zones (zoneinfo is 3.9+; IST and UTC have no DST, so a fixed offset is exact)
# ─────────────────────────────────────────────────────────────────────────────

def get_tz(name: str) -> dt.tzinfo:
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except ImportError:
        fixed = {"Asia/Kolkata": dt.timezone(dt.timedelta(hours=5, minutes=30), "IST"),
                 "UTC": dt.timezone.utc}
        if name in fixed:
            return fixed[name]
        raise ValueError(f"timezone {name!r} needs Python 3.9+ (zoneinfo)")


# ─────────────────────────────────────────────────────────────────────────────
#  Reading trades
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    day: dt.date
    pnl: float                       # in the bot's currency, after size
    r: Optional[float] = None        # P&L / risk, when the bot records risk


def num(v) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
    else:
        try:
            x = float(str(v).strip().replace(",", "").replace("₹", "").replace("$", ""))
        except ValueError:
            return None
    return None if x != x else x     # NaN is "no value", not a trade


def parse_time(v, tz: dt.tzinfo) -> Optional[dt.datetime]:
    """Epoch seconds or ms, or an ISO string; a naive string is read in tz."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    s = str(v).strip()
    if isinstance(v, (int, float)) or re.fullmatch(r"\d{9,13}(\.\d+)?", s):
        x = float(v)
        if x > 1e12:
            x /= 1000.0
        return dt.datetime.fromtimestamp(x, dt.timezone.utc)
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=tz)


def read_rows(path: str, fmt: str, list_key: str = "") -> list:
    if fmt == "csv":
        with open(path, newline="", encoding="utf-8-sig") as fh:
            return list(csv.DictReader(fh))
    if fmt == "jsonl":
        rows = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        pass         # a half-written last line must not drop the whole day
        return [r for r in rows if isinstance(r, dict)]
    if fmt == "json":
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        for key in filter(None, list_key.split(".")):
            data = data.get(key, []) if isinstance(data, dict) else []
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
    raise ValueError(f"unknown format {fmt!r} (use csv, json or jsonl)")


def file_format(src: dict, path: str) -> str:
    return (src.get("format") or os.path.splitext(path)[1].lstrip(".")).lower()


def any_file(pattern: str) -> bool:
    return bool(glob.glob(pattern.replace("{date}", "*")))


def load_trades(src: dict, start: dt.date, end: dt.date, tz: dt.tzinfo) -> tuple:
    """Closed trades whose exit date (in tz) is within [start, end]. Returns (trades, problems)."""
    pattern = src["path"]
    ttz = get_tz(src["time_tz"]) if src.get("time_tz") else tz
    files = []
    if "{date}" in pattern:          # one file per day, e.g. trades_2026-10-09.csv
        d = start
        while d <= end:
            files.extend((p, d) for p in sorted(glob.glob(pattern.replace("{date}", d.isoformat()))))
            d += dt.timedelta(days=1)
    else:
        files = [(p, None) for p in sorted(glob.glob(pattern))]

    trades, problems = [], []
    for path, file_day in files:
        name = os.path.basename(path)
        try:
            rows = read_rows(path, file_format(src, path), src.get("list_key", ""))
        except (OSError, ValueError) as e:
            problems.append(f"cannot read {name}: {e}")
            continue
        if not rows:
            continue
        pk = src.get("pnl") or next((k for k in PNL_KEYS if k in rows[0]), None)
        tk = src.get("time") or next((k for k in TIME_KEYS if k in rows[0]), None)
        if pk is None:
            problems.append(f"no P&L field in {name}; set \"pnl\" in bots.json")
            continue
        if tk is None and file_day is None:
            problems.append(f"no exit-time field in {name}; set \"time\" in bots.json")
            continue
        for row in rows:
            pnl = num(row.get(pk))
            if pnl is None:
                continue             # still open, or not a trade row
            t = parse_time(row.get(tk), ttz) if tk else None
            day = t.astimezone(tz).date() if t else file_day
            if day is None or not start <= day <= end:
                continue
            size = num(row.get(src["size"])) if src.get("size") else None
            risk = num(row.get(src["risk"])) if src.get("risk") else None
            trades.append(Trade(day, pnl * (size if size is not None else 1.0),
                                pnl / risk if risk else None))
    return trades, problems


def stats(trades: list) -> dict:
    pnls = [t.pnl for t in trades]
    rs = [t.r for t in trades if t.r is not None]
    return {"n": len(pnls), "net": sum(pnls),
            "wins": sum(1 for x in pnls if x > 0), "losses": sum(1 for x in pnls if x < 0),
            "best": max(pnls, default=0.0), "worst": min(pnls, default=0.0),
            "R": sum(rs) if rs else None}


# ─────────────────────────────────────────────────────────────────────────────
#  Service, log and heartbeat checks
# ─────────────────────────────────────────────────────────────────────────────

def service_status(unit: str, day: dt.date, tz: dt.tzinfo) -> tuple:
    """(text, is_problem) for a systemd unit, as of now."""
    if not unit:
        return "", False
    if not shutil.which("systemctl"):
        return "status unknown (no systemctl)", False
    try:
        out = subprocess.run(
            ["systemctl", "show", unit, "--no-pager",
             "--property=LoadState,ActiveState,Result,NRestarts,ExecMainStartTimestamp"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True,
            timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return "status unknown", False
    p = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if p.get("LoadState") == "not-found":
        return "service not installed", True
    active, result = p.get("ActiveState", ""), p.get("Result", "")
    restarts = int(p.get("NRestarts") or 0)
    m = TS_RE.search(p.get("ExecMainStartTimestamp", ""))
    # systemctl prints the box's local time; a naive astimezone() reads it as exactly that.
    started = (dt.datetime.strptime(" ".join(m.groups()), "%Y-%m-%d %H:%M:%S").astimezone(tz).date()
               if m else None)
    extra = f" · restarted {restarts}×" if restarts else ""
    if active == "failed" or result not in ("", "success"):
        return f"FAILED ({result or active}){extra}", True
    if active in ("active", "reloading"):
        return "running" + extra, restarts > 0
    if active == "activating":
        return "starting" + extra, False
    if started == day:
        return "ran today, stopped normally" + extra, restarts > 0
    return "did not run today", False


def log_errors(pattern: str, day: dt.date, max_bytes: int = 4_000_000) -> tuple:
    """(count, last message) of ERROR/CRITICAL lines stamped with day, from the log's tail."""
    paths = sorted(glob.glob(pattern.replace("{date}", day.isoformat())), key=os.path.getmtime)
    if not paths:
        return 0, ""
    try:
        with open(paths[-1], "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - max_bytes))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return 0, ""
    key, n, last = day.isoformat(), 0, ""
    for line in text.splitlines():
        if line.startswith(key) and ERROR_RE.search(line):
            n, last = n + 1, line
    msg = last.split("|", 1)[-1] if "|" in last else ERROR_RE.split(last, 1)[-1]
    return n, msg.strip()[:160]


def heartbeat_problem(path: str, max_age_min: float) -> str:
    if not path or not os.path.exists(path):
        return ""
    age = (time.time() - os.path.getmtime(path)) / 60
    if age > max_age_min:
        return f"{os.path.basename(path)} not updated for {age:.0f} min"
    try:
        with open(path, encoding="utf-8") as fh:
            h = json.load(fh)
    except (OSError, ValueError):
        return ""
    if isinstance(h, dict) and (h.get("degraded") is True or h.get("ok") is False):
        return f"{os.path.basename(path)} says degraded"
    return ""


# ─────────────────────────────────────────────────────────────────────────────
#  The report
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BotReport:
    name: str
    currency: str
    found: bool
    today: dict
    week: dict
    month: dict
    status: str = ""
    status_bad: bool = False
    warnings: list = field(default_factory=list)


def money(v: float, cur: str) -> str:
    sign = "+" if v > 0.005 else ("−" if v < -0.005 else "")
    return f"{sign}{cur}{abs(v):,.2f}"


def check_bot(bot: dict, day: dt.date, tz: dt.tzinfo, is_today: bool) -> BotReport:
    src = bot["trades"]
    start = min(day - dt.timedelta(days=6), day.replace(day=1))
    trades, problems = load_trades(src, start, day, tz)
    week_start = day - dt.timedelta(days=6)
    r = BotReport(
        name=bot["name"], currency=bot.get("currency", ""), found=any_file(src["path"]),
        today=stats([t for t in trades if t.day == day]),
        week=stats([t for t in trades if t.day >= week_start]),
        month=stats([t for t in trades if t.day >= day.replace(day=1)]),
        warnings=list(problems))
    if not r.found:
        r.warnings.append(f"no trade file matches {src['path']}; fix \"path\" in bots.json")
    if is_today:
        r.status, r.status_bad = service_status(bot.get("service", ""), day, tz)
        if r.status.startswith("running"):
            hb = heartbeat_problem(bot.get("heartbeat", ""), float(bot.get("heartbeat_max_age_min", 30)))
            if hb:
                r.warnings.append(hb)
    if bot.get("log"):
        n, last = log_errors(bot["log"], day)
        if n:
            r.warnings.append(f"{n} error{'s' if n > 1 else ''} in log · last: {last}")
    return r


def format_bot(r: BotReport) -> str:
    t, c = r.today, r.currency
    dot = "🟢" if t["net"] > 0.005 else ("🔴" if t["net"] < -0.005 else "⚪")
    head = f"{dot} <b>{html.escape(r.name)}</b>"
    if r.status:
        head += f" · {'⚠️ ' if r.status_bad else ''}{html.escape(r.status)}"
    lines = [head]
    if t["n"]:
        rate = 100.0 * t["wins"] / t["n"]
        rtxt = f" · {t['R']:+.2f}R".replace("-", "−") if t["R"] is not None else ""
        lines.append(f"Today {t['n']} trade{'s' if t['n'] > 1 else ''} · {t['wins']}W {t['losses']}L "
                     f"({rate:.0f}%) · <b>{money(t['net'], c)}</b>{rtxt}")
        lines.append(f"Best {money(t['best'], c)} · worst {money(t['worst'], c)}")
    else:
        lines.append("Today: no closed trades")
    if r.week["n"] or r.month["n"]:
        lines.append(f"7 days {money(r.week['net'], c)} ({r.week['n']}) · "
                     f"month {money(r.month['net'], c)} ({r.month['n']})")
    lines += [f"⚠️ {html.escape(w)}" for w in r.warnings]
    return "\n".join(lines)


def totals_line(label: str, reports: list, key: str) -> str:
    by_cur = {}
    for r in reports:
        by_cur[r.currency] = by_cur.get(r.currency, 0.0) + r.__dict__[key]["net"]
    return f"<b>{label}</b> " + " · ".join(money(v, c) for c, v in by_cur.items())


def build_report(cfg: dict, day: dt.date, tz: dt.tzinfo, is_today: bool) -> str:
    bots = [b for b in cfg["bots"] if b.get("enabled", True)]
    reports = [check_bot(b, day, tz, is_today) for b in bots]
    shown = [r for r in reports if r.found or r.today["n"] or r.status not in ("", "service not installed")]
    missing = [r.name for r in reports if r not in shown]

    blocks = [f"📊 <b>{html.escape(cfg.get('title', 'SP Bots'))} · daily P&amp;L</b> · {day:%a %d %b %Y}"]
    attention = [r.name for r in shown if r.warnings or r.status_bad]
    if attention:
        blocks[0] += "\n⚠️ Needs a look: " + html.escape(", ".join(attention))
    blocks += [format_bot(r) for r in shown]
    if shown:
        blocks.append(totals_line("Total today", shown, "today") + "\n"
                      + totals_line("7 days", shown, "week"))
    else:
        blocks.append("No bot trade files found. Run: sp_pnl_report.py --discover")
    if missing:
        blocks.append("Not found on this box: " + html.escape(", ".join(missing)))
    return "\n\n".join(blocks)


def html_to_plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def split_message(text: str, limit: int = TG_LIMIT) -> list:
    """Split on blank lines between bots so no bot's block is cut in half."""
    chunks, cur = [], ""
    for block in text.split("\n\n"):
        while len(block) > limit:                 # one oversize block: hard-cut it
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(block[:limit])
            block = block[limit:]
        cand = f"{cur}\n\n{block}" if cur else block
        if len(cand) > limit:
            chunks.append(cur)
            cur = block
        else:
            cur = cand
    if cur:
        chunks.append(cur)
    return chunks


# ─────────────────────────────────────────────────────────────────────────────
#  Telegram
# ─────────────────────────────────────────────────────────────────────────────

def load_env_file(path: str) -> dict:
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k.startswith("export "):
                    k = k[len("export "):].strip()
                out[k] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def telegram_creds() -> tuple:
    env = dict(os.environ)
    for path in (os.environ.get("SP_ENV_PATH"),) + ENV_FILES:
        if path and os.path.exists(path):
            for k, v in load_env_file(path).items():
                env.setdefault(k, v)
    token = env.get("TELEGRAM_TOKEN") or env.get("TELEGRAM_BOT_TOKEN")
    chat = env.get("PNL_REPORT_CHAT_ID") or env.get("TELEGRAM_CHAT_ID")
    return token, chat


def send_telegram(text: str, token: str, chat: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk in split_message(text):
        fields = {"chat_id": chat, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": "true"}
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode())
                with urllib.request.urlopen(req, timeout=20) as resp:
                    if json.load(resp).get("ok"):
                        break
                raise RuntimeError("Telegram answered ok=false")
            except urllib.error.HTTPError as e:
                if e.code == 400 and "parse_mode" in fields:
                    # Telegram could not parse the HTML: resend the same text plain, once.
                    fields = {"chat_id": chat, "text": html_to_plain(chunk)}
                    continue
                err = f"HTTP {e.code}"
            except (urllib.error.URLError, OSError, ValueError, RuntimeError) as e:
                err = str(e)
            if attempt == 2:
                raise RuntimeError("Telegram send failed: " + err.replace(token, "***"))
            time.sleep(3 * (attempt + 1))
        time.sleep(1)                              # stay well under Telegram's per-chat rate


# ─────────────────────────────────────────────────────────────────────────────
#  Config, archive, discover
# ─────────────────────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg.get("bots"), list) or not cfg["bots"]:
        raise ValueError(f"{path}: \"bots\" must be a non-empty list")
    for i, b in enumerate(cfg["bots"]):
        if not b.get("name") or not isinstance(b.get("trades"), dict) or not b["trades"].get("path"):
            raise ValueError(f"{path}: bot #{i + 1} needs \"name\" and \"trades\": {{\"path\": ...}}")
    return cfg


def archive(cfg: dict, day: dt.date, text: str) -> None:
    d = cfg.get("archive_dir")
    if not d:
        return
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"pnl_{day.isoformat()}.txt"), "w", encoding="utf-8") as fh:
            fh.write(html_to_plain(text) + "\n")
    except OSError as e:
        print(f"warning: could not archive the report in {d}: {e}", file=sys.stderr)


def preview_fields(path: str) -> str:
    try:
        if path.endswith(".csv"):
            with open(path, newline="", encoding="utf-8-sig") as fh:
                return ", ".join(next(csv.reader(fh), []))
        if path.endswith(".jsonl"):
            with open(path, encoding="utf-8") as fh:
                return ", ".join(json.loads(fh.readline() or "{}"))
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):
            return "list of: " + ", ".join(data[0]) if data and isinstance(data[0], dict) else "list"
        parts = []
        for k, v in data.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                parts.append(f"{k}[{len(v)}]: {', '.join(list(v[0])[:12])}")
            else:
                parts.append(k)
        return "; ".join(parts)
    except (OSError, ValueError, StopIteration, AttributeError) as e:
        return f"(unreadable: {e})"


def discover(roots: list) -> int:
    if shutil.which("systemctl"):
        print("sp-* systemd units:")
        out = subprocess.run(["systemctl", "list-units", "sp-*", "--all", "--no-legend", "--no-pager"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True).stdout
        print(out.rstrip() or "  (none)")
    print("\nTrade-like files changed in the last 14 days ({date} = any YYYY-MM-DD):")
    name_re = re.compile(r"trade|ledger|pnl|fill|order", re.I)
    skip = {".venv", "venv", "__pycache__", ".git", "node_modules", "depth", "tape", "features", "backups"}
    cutoff, groups = time.time() - 14 * 86400, {}
    for root in roots:
        for dirpath, dirnames, files in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip]
            for f in files:
                if not f.endswith((".csv", ".json", ".jsonl")) or not name_re.search(f):
                    continue
                p = os.path.join(dirpath, f)
                try:
                    if os.path.getmtime(p) < cutoff:
                        continue
                except OSError:
                    continue
                groups.setdefault(re.sub(r"\d{4}-\d{2}-\d{2}", "{date}", p), []).append(p)
    if not groups:
        print("  (none found under " + ", ".join(roots) + ")")
    for pattern, paths in sorted(groups.items()):
        newest = max(paths, key=os.path.getmtime)
        when = dt.datetime.fromtimestamp(os.path.getmtime(newest)).strftime("%Y-%m-%d %H:%M")
        print(f"  {pattern}   ({len(paths)} file(s), newest {when})")
        print(f"      fields: {preview_fields(newest)[:300]}")
    print("\nCopy the right paths into bots.json, then check with: sp_pnl_report.py --dry-run")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
#  Self-test (offline, temporary files only)
# ─────────────────────────────────────────────────────────────────────────────

def selftest() -> int:
    fails = []

    def check(name: str, cond: bool) -> None:
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)

    ist = get_tz("Asia/Kolkata")
    day = dt.date(2026, 10, 9)
    epoch = lambda y, mo, d, h, mi: int(dt.datetime(y, mo, d, h, mi, tzinfo=dt.timezone.utc).timestamp())
    with tempfile.TemporaryDirectory() as tmp:
        # Scalp bot: one CSV per IST day, net column, exit_ts with +05:30.
        cols = ["date", "entry_ts", "exit_ts", "symbol", "side", "qty", "gross", "cost", "net"]
        for d, rows in ((day, [("10:01", 400.0), ("11:15", -150.0), ("14:40", 250.5)]),
                        (day - dt.timedelta(days=1), [("13:00", -100.0)])):
            with open(os.path.join(tmp, f"trades_{d}.csv"), "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(cols)
                for hm, net in rows:
                    w.writerow([d, f"{d}T09:30:00+05:30", f"{d}T{hm}:00+05:30", "X", "BUY", 1, net + 20, 20, net])
                w.writerow([d, f"{d}T15:00:00+05:30", "", "Y", "BUY", 1, "", "", ""])   # still open
        # XAUT ledger: closed positions, epoch seconds, P&L per XAUT, size and risk.
        ledger = {"version": "1.5.4", "open": [], "closed": [
            {"strat": "S3", "opened": epoch(2026, 10, 8, 22, 0), "closed": epoch(2026, 10, 9, 16, 0),
             "pnl": 10.0, "size": 0.5, "risk": 20.0},
            {"strat": "W1", "opened": epoch(2026, 10, 9, 1, 0), "closed": epoch(2026, 10, 9, 19, 0),
             "pnl": -4.0, "size": 1.0, "risk": 8.0},       # 00:30 IST on the 10th: next day
            {"strat": "S4a", "opened": epoch(2026, 9, 30, 1, 0), "closed": epoch(2026, 9, 30, 16, 0),
             "pnl": 99.0, "size": 1.0, "risk": 50.0}]}     # last month: not in month-to-date
        with open(os.path.join(tmp, "paper_ledger.json"), "w") as fh:
            json.dump(ledger, fh)
        # Gold bot: JSONL, float epoch, pnl_usd, plus a torn last line.
        with open(os.path.join(tmp, "gold_final_trades.jsonl"), "w") as fh:
            fh.write(json.dumps({"closed": epoch(2026, 10, 9, 16, 1) + 0.25, "pnl_usd": -0.42, "reason": "EXPIRY"}) + "\n")
            fh.write('{"closed": 17916')
        with open(os.path.join(tmp, "bot.log"), "w") as fh:
            fh.write("2026-10-09 10:00:00 INFO    bot | started\n"
                     "2026-10-09 11:00:00 ERROR   bot | quote feed dropped\n"
                     "2026-10-08 11:00:00 ERROR   bot | yesterday, not counted\n"
                     "2026-10-09 12:00:00 CRITICAL bot | token expired <soon>\n")
        cfg = {"title": "Test", "bots": [
            {"name": "Scalp", "currency": "₹", "log": os.path.join(tmp, "bot.log"),
             "trades": {"path": os.path.join(tmp, "trades_{date}.csv")}},
            {"name": "XAUT", "currency": "$",
             "trades": {"format": "json", "path": os.path.join(tmp, "paper_ledger.json"), "list_key": "closed",
                        "pnl": "pnl", "size": "size", "risk": "risk", "time": "closed"}},
            {"name": "Gold", "currency": "$",
             "trades": {"path": os.path.join(tmp, "gold_final_trades.jsonl"), "pnl": "pnl_usd", "time": "closed"}},
            {"name": "Ghost", "currency": "₹", "trades": {"path": os.path.join(tmp, "nope_{date}.csv")}},
            {"name": "Off", "enabled": False, "trades": {"path": "/nonexistent"}}]}

        scalp = check_bot(cfg["bots"][0], day, ist, False)
        check("scalp: 3 closed trades today, open row skipped", scalp.today["n"] == 3)
        check("scalp: net 500.50 = 400 - 150 + 250.50", abs(scalp.today["net"] - 500.5) < 1e-9)
        check("scalp: 2 wins 1 loss, best 400, worst -150",
              (scalp.today["wins"], scalp.today["losses"], scalp.today["best"], scalp.today["worst"]) == (2, 1, 400.0, -150.0))
        check("scalp: 7 days includes yesterday (400.50 over 4)", scalp.week["n"] == 4 and abs(scalp.week["net"] - 400.5) < 1e-9)
        check("scalp: 2 log errors today, last one kept", scalp.warnings and scalp.warnings[0].startswith("2 errors")
              and "token expired" in scalp.warnings[0])
        xaut = check_bot(cfg["bots"][1], day, ist, False)
        check("xaut: only the 21:30 IST close counts today", xaut.today["n"] == 1)
        check("xaut: P&L is pnl x size = 5.00", abs(xaut.today["net"] - 5.0) < 1e-9)
        check("xaut: R = pnl / risk = +0.50", abs(xaut.today["R"] - 0.5) < 1e-9)
        check("xaut: September trade is outside month-to-date", xaut.month["n"] == 1)
        gold = check_bot(cfg["bots"][2], day, ist, False)
        check("gold: float epoch read, torn line ignored", gold.today["n"] == 1 and abs(gold.today["net"] + 0.42) < 1e-9)

        text = build_report(cfg, day, ist, False)
        plain = html_to_plain(text)
        check("report: per-currency totals", "Total today +₹500.50 · +$4.58" in plain)
        check("report: missing bot listed once at the end", plain.rstrip().endswith("Not found on this box: Ghost"))
        check("report: disabled bot left out", "Off" not in plain)
        check("report: log text is HTML-escaped", "&lt;soon&gt;" in text and "<soon>" not in text)
        check("report: flags bots that need a look", "Needs a look: Scalp" in plain)

    check("parse_time: ms epoch", parse_time(1791600000000, ist) == dt.datetime(2026, 10, 10, 2, 40, tzinfo=dt.timezone.utc))
    check("parse_time: naive string read in tz", parse_time("2026-10-09 03:30", ist).utcoffset() == dt.timedelta(hours=5, minutes=30))
    check("parse_time: Z suffix", parse_time("2026-10-09T10:00:00Z", ist).hour == 10)
    check("num: rupee string with commas", num("₹1,240.50") == 1240.5 and num("nan") is None and num("") is None)
    long = "\n\n".join("x" * 1500 for _ in range(5))
    parts = split_message(long)
    check("split: under the Telegram limit, nothing lost",
          all(len(p) <= TG_LIMIT for p in parts) and sum(p.count("x") for p in parts) == 7500)
    check("money: signs", (money(12.3, "$"), money(-5, "₹"), money(0, "$")) == ("+$12.30", "−₹5.00", "$0.00"))
    env_path = os.path.join(tempfile.gettempdir(), f"pnl_selftest_{os.getpid()}.env")
    try:
        with open(env_path, "w") as fh:
            fh.write('# comment\nexport TELEGRAM_TOKEN="abc:123"\nTELEGRAM_CHAT_ID=42\n')
        check("env file: export prefix and quotes", load_env_file(env_path) == {"TELEGRAM_TOKEN": "abc:123", "TELEGRAM_CHAT_ID": "42"})
    finally:
        os.remove(env_path)

    print(f"\n{'ALL PASS' if not fails else f'{len(fails)} FAILED'} · sp_pnl_report {VERSION}")
    return 1 if fails else 0


# ─────────────────────────────────────────────────────────────────────────────

def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Daily P&L summary of every SP bot, sent to Telegram.")
    ap.add_argument("--config", default=os.environ.get("PNL_REPORT_CONFIG", DEFAULT_CONFIG))
    ap.add_argument("--date", help="report day, YYYY-MM-DD (default: today in the config timezone)")
    ap.add_argument("--dry-run", action="store_true", help="print the report, send nothing")
    ap.add_argument("--discover", action="store_true", help="list sp-* services and trade files on this box")
    ap.add_argument("--root", action="append", help="folder for --discover to search (repeatable)")
    ap.add_argument("--selftest", action="store_true", help="offline checks")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()
    if a.discover:
        return discover(a.root or [r for r in DISCOVER_ROOTS if os.path.isdir(r)])
    try:
        cfg = load_config(a.config)
        tz = get_tz(cfg.get("timezone", "Asia/Kolkata"))
        today = dt.datetime.now(tz).date()
        day = dt.date.fromisoformat(a.date) if a.date else today
    except (OSError, ValueError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    text = build_report(cfg, day, tz, is_today=(day == today))
    archive(cfg, day, text)
    if a.dry_run:
        print(html_to_plain(text))
        return 0
    token, chat = telegram_creds()
    if not token or not chat:
        print("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID not found (environment, SP_ENV_PATH, "
              + ", ".join(ENV_FILES) + "). Report not sent:\n", file=sys.stderr)
        print(html_to_plain(text))
        return 1
    try:
        send_telegram(text, token, chat)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    print(f"{dt.datetime.now(tz):%Y-%m-%d %H:%M} sent the {day} report ({len(text)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
