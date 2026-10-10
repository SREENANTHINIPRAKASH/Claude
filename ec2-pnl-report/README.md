# SP bots daily P&L to Telegram

Sends one message a day at 22:15 IST with every bot's P&L, so you don't need a Claude session to check them.

For each bot the message shows:
- today's closed trades, wins and losses, net P&L, and R where the bot records risk;
- best and worst trade, plus 7-day and month-to-date totals;
- whether the systemd service ran, is running, failed or restarted;
- ERROR and CRITICAL lines from the bot's log today, with the last one quoted;
- a stale or degraded health file (XAUT `health.json`).

It only reads the bots' files. It places no orders, touches no broker API, and uses the Python standard library only.

```
📊 SP Bots · daily P&L · Fri 09 Oct 2026
⚠️ Needs a look: XAUT OPT (paper)

🟢 SP Intraday Scalp · Engine A (dry run) · ran today, stopped normally
Today 4 trades · 2W 2L (50%) · +₹300.75
Best +₹312.40 · worst −₹96.15
7 days +₹440.75 (6) · month +₹440.75 (6)

🟢 XAUT OPT (paper) · running
Today 2 trades · 1W 1L (50%) · +$1.89 · −0.09R
Best +$3.99 · worst −$2.10
7 days +$3.39 (3) · month +$3.39 (3)
⚠️ 1 error in log · last: settlement price missing after 2 h; using index TWAP

Total today +₹300.75 · +$1.89
7 days +₹440.75 · +$3.39
```

XAUT P&L is at paper size (`pnl × size`), the same as the bot's own `--report` "$ at size" column. R is per unit, as the bot reports it, so the two can have different signs when trade sizes differ.

## Install on the EC2 box

1. Copy this folder to the box (scp, SSM, or `git clone` this repo), then:
   ```bash
   cd ec2-pnl-report
   sudo bash install.sh
   ```
   It installs to `/opt/spbots/deploy/pnl-report/`, enables the `sp-pnl-report.timer`, runs the self-test, and prints today's report without sending it.
2. Check the preview. A bot marked "no trade file" or "Not found on this box" has the wrong path in `bots.json`. To find the real path:
   ```bash
   sudo -u spbot python3 /opt/spbots/deploy/pnl-report/sp_pnl_report.py --discover
   ```
   This lists the `sp-*` services and every trade, ledger or P&L file changed in the last 14 days, with its column names. Edit `/opt/spbots/deploy/pnl-report/bots.json`, then re-check with `--dry-run`.
3. Send one real report: `sudo systemctl start sp-pnl-report.service`. If nothing arrives, look in `/opt/spbots/data/pnl_report/pnl_report.err`.

Telegram uses the bots' own `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`, from `/run/spbots/.env` (via `fetch-secrets.sh`) or `/opt/spbots/.env`. To send this report to a different chat, add `PNL_REPORT_CHAT_ID=...` to that file.

## Which bots are configured

| Bot | Trade file | Status |
|---|---|---|
| SP Intraday Scalp (Engine A) | `/opt/spbots/data/scalping/csv/trades_{date}.csv`, column `net` | From the bot code |
| XAUT OPT (paper) | `/opt/spbots/data/scalping/opt/state/paper_ledger.json`, `closed` list | From the bot code and deploy guide |
| SP Intraday (Engine B) | `/opt/spbots/data/intraday/csv/trades_{date}.csv` | **Guessed**; confirm with `--discover` |
| SP Stocks Grid Scalp | `/opt/spbots/data/grid/csv/trades_{date}.csv` | **Guessed**; confirm with `--discover` |
| Gold S&R cash | `gold_final_trades.jsonl`, field `pnl_usd` | Off until you set where it runs |

## Adding a bot (ALT coins, Nifty options, ...)

Add one entry to `bots.json`:

```json
{
  "name": "ALT coins (paper)",
  "service": "sp-altcoins.service",
  "currency": "$",
  "trades": {"format": "csv", "path": "/opt/spbots/data/altcoins/trades_{date}.csv",
             "pnl": "net_pnl", "time": "exit_time"},
  "log": "/opt/spbots/data/altcoins/logs/bot.log"
}
```

| Key | Meaning |
|---|---|
| `trades.format` | `csv`, `json` (with `list_key`, e.g. `"closed"`) or `jsonl`; defaults to the file extension |
| `trades.path` | the file; `{date}` stands for `YYYY-MM-DD` when the bot writes one file per day |
| `trades.pnl` | P&L field; if left out, the first of `net`, `net_pnl`, `pnl_usd`, `pnl`, ... found is used |
| `trades.time` | exit-time field (epoch seconds or ms, or ISO); rows without a P&L value are treated as open and skipped |
| `trades.size`, `trades.risk` | optional: P&L is multiplied by `size`; R is `pnl / risk` |
| `trades.time_tz` | time zone for timestamps without one (default: the config `timezone`) |
| `service` | systemd unit to check (optional) |
| `log` | log to scan for ERROR/CRITICAL lines starting with the day's date (optional, `{date}` allowed) |
| `heartbeat`, `heartbeat_max_age_min` | a file the bot rewrites while healthy; warns when it goes stale (optional) |
| `enabled` | `false` leaves the bot out |

The service runs as `spbot`. If a bot runs as another user and its files are not readable by `spbot`, the report says "cannot read ...". Fix the file permissions; don't run the report as root.

## Commands

```bash
python3 sp_pnl_report.py --dry-run            # print today's report, send nothing
python3 sp_pnl_report.py --date 2026-10-09    # report an earlier day
python3 sp_pnl_report.py --discover           # find services and trade files
python3 sp_pnl_report.py --selftest           # offline checks
```

Each report is also saved as plain text in `/opt/spbots/data/pnl_report/pnl_YYYY-MM-DD.txt`. When you do want Claude to look at results, paste that file instead of asking it to read logs and ledgers. It costs far less of your usage limit.
