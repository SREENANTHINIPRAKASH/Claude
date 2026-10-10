#!/usr/bin/env bash
# install.sh — install the daily P&L report on the EC2 box. Run ON THE BOX as root,
# from this folder:   sudo bash install.sh
#
# Safe to re-run: it updates the script and units but keeps an edited bots.json.

set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
DEST=/opt/spbots/deploy/pnl-report
DATA=/opt/spbots/data/pnl_report

[ "$(id -u)" -eq 0 ] || { echo "run as root: sudo bash install.sh" >&2; exit 1; }
id spbot >/dev/null 2>&1 || { echo "user spbot not found; the bot units run as spbot" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 7))' || { echo "needs Python 3.7+ at /usr/bin/python3" >&2; exit 1; }

install -d -m 755 "$DEST"
install -m 755 "$HERE/sp_pnl_report.py" "$DEST/"
if [ -f "$DEST/bots.json" ]; then
    echo "keeping your existing $DEST/bots.json"
else
    install -m 644 "$HERE/bots.json" "$DEST/"
fi
install -d -m 750 -o spbot -g spbot "$DATA"

install -m 644 "$HERE/systemd/sp-pnl-report.service" "$HERE/systemd/sp-pnl-report.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sp-pnl-report.timer

echo
python3 "$DEST/sp_pnl_report.py" --selftest | tail -1
echo
echo "Preview of today's report (nothing sent):"
echo "─────────────────────────────────────────"
sudo -u spbot python3 "$DEST/sp_pnl_report.py" --dry-run || true
echo "─────────────────────────────────────────"
systemctl list-timers sp-pnl-report.timer --no-pager
cat <<'EOF'

Next:
  1. If a bot shows "no trade file" or is missing, run:
       sudo -u spbot python3 /opt/spbots/deploy/pnl-report/sp_pnl_report.py --discover
     and fix its "path" in /opt/spbots/deploy/pnl-report/bots.json
  2. Send one real report now:  sudo systemctl start sp-pnl-report.service
     Problems are logged in /opt/spbots/data/pnl_report/pnl_report.err
EOF
