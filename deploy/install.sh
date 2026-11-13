#!/usr/bin/env bash
#
# Install the GujRERA daily delta job on a Debian/Ubuntu VPS.
#
#   sudo APP_DIR=/opt/gujrera ./deploy/install.sh
#
# Idempotent: re-run it after changing units or pulling new code. It never
# touches .env (so credentials survive an upgrade) and never enables the
# backfill timer unless you ask with ENABLE_BACKFILL=1.
#
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/gujrera}"
APP_USER="${APP_USER:-gujrera}"
GIT_URL="${GIT_URL:-}"
ENABLE_BACKFILL="${ENABLE_BACKFILL:-0}"
PY_MIN_MINOR=10

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run this with sudo"

# ------------------------------------------------------------------ python check ---
command -v python3 >/dev/null || die "python3 is not installed (apt install python3 python3-venv)"
python3 - <<'EOF' || die "Python 3.10+ is required"
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
EOF
action=""

if ! python3 -c "import venv" 2>/dev/null; then
    die "python3-venv is missing:  apt install python3-venv"
fi

# --------------------------------------------------------------------- user/dir ---
if ! id "$APP_USER" >/dev/null 2>&1; then
    say "creating service user $APP_USER"
    useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
fi

if [ ! -d "$APP_DIR" ]; then
    [ -n "$GIT_URL" ] || die "$APP_DIR does not exist and GIT_URL is unset.
  Either:  sudo GIT_URL=<repo> $0
  Or:      put the code at $APP_DIR first."
    say "cloning $GIT_URL into $APP_DIR"
    command -v git >/dev/null || die "git is not installed"
    git clone --depth 1 "$GIT_URL" "$APP_DIR"
fi

cd "$APP_DIR"
[ -f run.py ] || die "$APP_DIR does not look like the scraper (run.py missing)"

# ---------------------------------------------------------------------- venv ---
say "installing Python dependencies into $APP_DIR/.venv"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

# ------------------------------------------------------------------ credentials ---
if [ ! -f .env ]; then
    say "creating .env from deploy/env.example — fill in the R2 values"
    install -m 600 -o "$APP_USER" -g "$APP_USER" deploy/env.example .env
    NEEDS_EDIT=1
else
    chown "$APP_USER:$APP_USER" .env
    chmod 600 .env
fi

# ------------------------------------------------------------------- directories ---
install -d -o "$APP_USER" -g "$APP_USER" "$APP_DIR/state" "$APP_DIR/output"

# ------------------------------------------------------------------------ units ---
say "installing systemd units"
for unit in gujrera-weekly.service gujrera-weekly.timer \
            gujrera-backfill.service gujrera-backfill.timer; do
    sed -e "s|@@APP_DIR@@|$APP_DIR|g" -e "s|@@APP_USER@@|$APP_USER|g" \
        "deploy/$unit" > "/etc/systemd/system/$unit"
done

systemctl daemon-reload

say "enabling the weekly timer (Monday 02:30, Persistent=true)"
systemctl enable --now gujrera-weekly.timer

# Any timer left over from an earlier install would double every run.
for stale in gujrera-daily.timer gujrera-monthly.timer; do
    if systemctl list-unit-files "$stale" >/dev/null 2>&1 && \
       systemctl is-enabled --quiet "$stale" 2>/dev/null; then
        say "disabling superseded $stale"
        systemctl disable --now "$stale"
    fi
done

if [ "$ENABLE_BACKFILL" = "1" ]; then
    say "enabling the backfill timer (every 20 min until the backlog drains)"
    systemctl enable --now gujrera-backfill.timer
fi

# ------------------------------------------------------------------------- smoke ---
say "checking the CLI starts"
sudo -u "$APP_USER" env PYTHONIOENCODING=utf-8 "$APP_DIR/.venv/bin/python" run.py --help \
    >/dev/null && echo "ok"

cat <<EOF

$(printf '\033[1m')Installed.$(printf '\033[0m')

  next run    : $(systemctl show -p NextElapseUSecRealtime --value gujrera-weekly.timer 2>/dev/null || echo 'Mon 02:30')
  run now     : systemctl start gujrera-weekly
  follow logs : journalctl -u gujrera-weekly -f
  run history : $APP_DIR/.venv/bin/python run.py --runs
  state db    : $APP_DIR/state/gujrera.db

EOF

if [ "${NEEDS_EDIT:-0}" = "1" ]; then
    cat <<EOF
$(printf '\033[33m')ACTION REQUIRED: fill in the R2 credentials before the first run.$(printf '\033[0m')

  sudo nano $APP_DIR/.env          # R2_ACCOUNT_ID, R2_ACCESS_KEY_ID/SECRET,
                                   # R2_BUCKET, R2_PDF_PREFIX, R2_JSON_PREFIX
  cd $APP_DIR
  sudo -u $APP_USER .venv/bin/python run.py --restore-state   # pull state from R2
  sudo -u $APP_USER .venv/bin/python run.py --coverage        # must show ~2,500 projects
  sudo -u $APP_USER .venv/bin/python run.py --proxy-test

EOF
fi

cat <<'EOF'
Attach the custom domain to the bucket first (R2 → bucket → Settings → Custom
Domains). `r2.dev` is rate-limited and not intended for production traffic.

Before the first run, the state db must come from R2 (the systemd units do this
on their own via ExecStartPre; it is a no-op once state/gujrera.db exists).
Without it every captured project counts as new and ~190 GB re-downloads.

Remaining crawl: ~4,900 registered projects have never been captured.

  sudo systemctl start gujrera-backfill          # one 250-project slice
  sudo ENABLE_BACKFILL=1 ./deploy/install.sh     # …or run slices every 20 min

Watch progress with `run.py --runs`; when it stops deferring, turn the
backfill timer off again.

EOF
