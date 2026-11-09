#!/usr/bin/env bash
set -euo pipefail

# Review of Legends - Service Management Script
# Usage: ./service.sh <command> [args]
#
# Commands:
#   status      Show LaunchAgent status and recent logs
#   start       Start the service
#   stop        Stop the service
#   restart     Restart the service
#   install     Install/update LaunchAgent and start (daemon + dashboard)
#   uninstall   Remove LaunchAgent (daemon + dashboard; data preserved)
#   logs        View logs [out|err|stdout|stderr|follow|all] [lines]
#   doctor      Run service doctor command
#   dashboard   Pass a subcommand to scripts/install-dashboard.py (verify, ...)
#   labels      Print the launchd labels this tree installs (from config/deploy.env)

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

# Deployment settings: config/deploy.env (KEY=VALUE lines, optional) names the
# launchd labels and the dashboard's access policy for this host. It is read as
# data, never sourced: only the ROL_* keys below are taken, values are literal.
# A variable set (non-empty) in the environment wins over the file; a key set
# twice in the file is refused.
DEPLOY_ENV="$ROOT/config/deploy.env"
DEPLOY_KEYS="ROL_SERVICE_LABEL ROL_DASHBOARD_LABEL ROL_DASHBOARD_OWNER ROL_DASHBOARD_VIEWER_DOMAINS ROL_DASHBOARD_PUBLIC_URL"
load_deploy_env() {
    [[ -f "$DEPLOY_ENV" ]] || return 0
    local line key value seen=" "
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
        if [[ ! "$line" =~ ^([A-Z_][A-Z0-9_]*)=(.*)$ ]]; then
            echo "[ERROR] $DEPLOY_ENV: not a KEY=VALUE line: $line" >&2
            exit 1
        fi
        key="${BASH_REMATCH[1]}"
        value="${BASH_REMATCH[2]}"
        case " $DEPLOY_KEYS " in
            *" $key "*) ;;
            *) echo "[ERROR] $DEPLOY_ENV: unknown key $key" >&2; exit 1 ;;
        esac
        case "$seen" in
            *" $key "*) echo "[ERROR] $DEPLOY_ENV: $key is set twice" >&2; exit 1 ;;
        esac
        seen="$seen$key "
        if [[ -z "${!key:-}" ]]; then
            export "$key=$value"
        fi
    done < "$DEPLOY_ENV"
}
load_deploy_env

LABEL="${ROL_SERVICE_LABEL:-ai.2lab.review-of-legends}"
# The admin dashboard web app is a second launchd job: it needs a venv with
# Starlette/Uvicorn while this daemon must stay stdlib-only on
# /usr/bin/python3, so they cannot share one process. Lifecycle stays coupled
# here so no deploy or uninstall path can forget it.
DASHBOARD_LABEL="${ROL_DASHBOARD_LABEL:-$LABEL.dashboard}"
for _label in "$LABEL" "$DASHBOARD_LABEL"; do
    if [[ ! "$_label" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
        echo "[ERROR] launchd label '$_label' may hold only letters, digits, '.', '_' and '-'" >&2
        exit 1
    fi
done
if [[ "$LABEL" == "$DASHBOARD_LABEL" ]]; then
    # One plist for both jobs: installing the dashboard would replace the daemon.
    echo "[ERROR] ROL_SERVICE_LABEL and ROL_DASHBOARD_LABEL must differ (both are $LABEL)" >&2
    exit 1
fi
export ROL_SERVICE_LABEL="$LABEL" ROL_DASHBOARD_LABEL="$DASHBOARD_LABEL"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST="$LAUNCH_AGENTS_DIR/$LABEL.plist"
DASHBOARD_PLIST="$LAUNCH_AGENTS_DIR/$DASHBOARD_LABEL.plist"
DASHBOARD_SCRIPT="$ROOT/scripts/install-dashboard.py"
DATA_DIR="$ROOT/data"
WORK_DIR="$DATA_DIR/work"
LOG_DIR="$ROOT/logs"
OUT_LOG="$LOG_DIR/service.out.log"
ERR_LOG="$LOG_DIR/service.err.log"
STATE="$DATA_DIR/state.json"
DOMAIN="gui/$(id -u)"

COMMAND="${1:-}"
shift 2>/dev/null || true

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info() { echo -e "${BLUE}[INFO]${NC} $1"; }
ok() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARNING]${NC} $1"; }
err() { echo -e "${RED}[ERROR]${NC} $1"; }

ensure_dirs() {
    mkdir -p "$LAUNCH_AGENTS_DIR" "$LOG_DIR" "$WORK_DIR"
    chmod 700 "$DATA_DIR" "$WORK_DIR" "$LOG_DIR" 2>/dev/null || true

    if [[ -f "$STATE" ]]; then
        chmod 600 "$STATE" 2>/dev/null || true
    fi
}

is_loaded() {
    launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1
}

is_running() {
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -qE '^[[:space:]]*state = running$'
}

get_pid() {
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | awk '/^[[:space:]]*pid = / {print $3; exit}'
}

dashboard_is_running() {
    launchctl print "$DOMAIN/$DASHBOARD_LABEL" 2>/dev/null | grep -qE '^[[:space:]]*state = running$'
}

# /usr/bin/python3 on purpose: the installer is the thing that *creates* the
# newer interpreter's venv, so it must run on the stock one.
dashboard_cmd() {
    if [[ ! -f "$DASHBOARD_SCRIPT" ]]; then
        err "dashboard installer missing: $DASHBOARD_SCRIPT"
        return 1
    fi
    /usr/bin/python3 "$DASHBOARD_SCRIPT" --root "$ROOT" "$@"
}

generate_plist() {
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>-m</string>
    <string>code_review_service</string>
    <string>--config</string>
    <string>$ROOT/config/reviewer.json</string>
    <string>daemon</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$ROOT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONPATH</key>
    <string>$ROOT</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$OUT_LOG</string>
  <key>StandardErrorPath</key>
  <string>$ERR_LOG</string>
  <key>ThrottleInterval</key>
  <integer>10</integer>
</dict>
</plist>
EOF
}

cmd_status() {
    echo "=================================="
    echo "Review of Legends - Status"
    echo "=================================="

    if is_running; then
        pid="$(get_pid)"
        ok "Service is RUNNING${pid:+ (PID: $pid)}"
        if [[ -n "${pid:-}" ]]; then
            started="$(ps -p "$pid" -o lstart= 2>/dev/null || true)"
            [[ -n "$started" ]] && echo "  Started: $started"
        fi
    elif is_loaded; then
        warn "Service is LOADED but not running"
    else
        warn "Service is STOPPED"
    fi

    if dashboard_is_running; then
        ok "Dashboard is RUNNING (http://127.0.0.1:8840)"
    elif [[ -f "$DASHBOARD_PLIST" ]]; then
        warn "Dashboard is INSTALLED but not running (logs/dashboard.err.log)"
    else
        warn "Dashboard is NOT INSTALLED"
    fi

    echo ""
    echo "Service: $LABEL"
    echo "Web:     $DASHBOARD_LABEL"
    echo "Project: $ROOT"
    echo "Plist:   $PLIST"
    echo "State:   $STATE"
    echo "Work:    $WORK_DIR"
    echo "Logs:    $LOG_DIR"
    [[ -f "$PLIST" ]] && echo "Plist file: EXISTS" || warn "Plist file: NOT FOUND"

    echo ""
    echo "Recent stderr (last 10 lines):"
    echo "---"
    tail -n 10 "$ERR_LOG" 2>/dev/null || echo "  (no stderr log)"
}

# Two daemons on one tree would poll the same repositories and share one
# state file, so reviews would be posted twice. Refuse to install when any other
# LaunchAgent already runs from this tree (for example after the labels in
# config/deploy.env changed): remove that job first, then install.
refuse_other_jobs_for_root() {
    local plist dir other root_physical
    [[ -x /usr/bin/plutil ]] || return 0
    root_physical="$(cd "$ROOT" && pwd -P)"
    for plist in "$LAUNCH_AGENTS_DIR"/*.plist; do
        [[ -f "$plist" ]] || continue
        [[ "$plist" == "$PLIST" || "$plist" == "$DASHBOARD_PLIST" ]] && continue
        dir="$(/usr/bin/plutil -extract WorkingDirectory raw -o - "$plist" 2>/dev/null || true)"
        [[ -n "$dir" && -d "$dir" ]] || continue
        # Compare physical paths: /tmp and /private/tmp (or a symlinked tree)
        # name the same directory.
        [[ "$(cd "$dir" && pwd -P)" == "$root_physical" ]] || continue
        other="$(/usr/bin/plutil -extract Label raw -o - "$plist" 2>/dev/null || basename "$plist" .plist)"
        err "another LaunchAgent ($other, $plist) already runs from $ROOT; uninstall it before installing $LABEL"
        return 1
    done
}

cmd_install() {
    info "Installing $LABEL as LaunchAgent..."
    ensure_dirs
    refuse_other_jobs_for_root
    generate_plist > "$PLIST"
    chmod 600 "$PLIST"

    launchctl bootout "$DOMAIN" "$PLIST" >/dev/null 2>&1 || true

    launchctl bootstrap "$DOMAIN" "$PLIST"
    launchctl kickstart -k "$DOMAIN/$LABEL"
    sleep 2

    if is_running; then
        ok "Service installed and started (PID: $(get_pid))"
    else
        warn "Service installed but not running. Check: $ERR_LOG"
        echo "Logs:  $OUT_LOG and $ERR_LOG"
        echo "State: $STATE"
        return 1
    fi
    echo "Logs:  $OUT_LOG and $ERR_LOG"
    echo "State: $STATE"

    # Every install of the daemon installs the dashboard too, and a dashboard
    # failure fails the install: "deployed together" has to be enforced here,
    # not left to whoever edits the workflow next.
    info "Installing $DASHBOARD_LABEL (admin dashboard web)..."
    dashboard_cmd install "$@"
}

cmd_uninstall() {
    info "Uninstalling $LABEL..."
    dashboard_cmd uninstall || warn "dashboard uninstall reported a problem; continuing"
    launchctl bootout "$DOMAIN" "$PLIST" >/dev/null 2>&1 || true
    rm -f "$PLIST"
    ok "Service uninstalled"
    # Nothing under data/ is removed: state.json is the idempotency ledger and
    # data/dashboard.sqlite3 holds the review attempt history. The dashboard
    # settings file (with the OAuth client secret) lives outside this tree and
    # is left untouched as well.
    info "State, review history and logs preserved under: $ROOT"
}

# The web app is its own launchd job, so it can be down while the daemon is
# fine (crash, manual stop). Every start path repairs it, not just a cold one.
start_dashboard() {
    if [[ -f "$DASHBOARD_PLIST" ]]; then
        dashboard_cmd start
    else
        warn "Dashboard not installed yet (no $DASHBOARD_PLIST). Run './service.sh install'."
    fi
}

cmd_start() {
    info "Starting $LABEL..."
    ensure_dirs
    if is_running; then
        warn "Service is already running"
        start_dashboard
        return 0
    fi
    if [[ ! -f "$PLIST" ]]; then
        err "Plist not found. Run './service.sh install' first."
        return 1
    fi
    if ! is_loaded; then
        launchctl bootstrap "$DOMAIN" "$PLIST"
    fi
    launchctl kickstart -k "$DOMAIN/$LABEL"
    sleep 2
    if is_running; then
        ok "Service started (PID: $(get_pid))"
    else
        err "Failed to start. Check: $ERR_LOG"
        return 1
    fi
    start_dashboard
}

cmd_stop() {
    info "Stopping $DASHBOARD_LABEL..."
    if [[ -f "$DASHBOARD_PLIST" ]]; then
        dashboard_cmd stop || warn "dashboard stop reported a problem; continuing"
    fi
    info "Stopping $LABEL..."
    if ! is_loaded; then
        warn "Service is not loaded"
        return 0
    fi
    launchctl bootout "$DOMAIN" "$PLIST" >/dev/null 2>&1 || true
    sleep 2
    if ! is_loaded; then
        ok "Service stopped"
    else
        err "Failed to stop service"
        return 1
    fi
}

cmd_restart() {
    # Propagate stop failures: if launchctl refuses to unload the job,
    # cmd_start would see "already running" and report success while the old
    # process keeps serving stale code/config.
    if is_loaded; then
        cmd_stop
    fi
    sleep 1
    cmd_start
}

cmd_logs() {
    log_type="${1:-err}"
    lines="${2:-50}"
    case "$log_type" in
        out|stdout)
            echo "=== $OUT_LOG (last $lines lines) ==="
            tail -n "$lines" "$OUT_LOG" 2>/dev/null || echo "  (no stdout log)"
            ;;
        err|stderr)
            echo "=== $ERR_LOG (last $lines lines) ==="
            tail -n "$lines" "$ERR_LOG" 2>/dev/null || echo "  (no stderr log)"
            ;;
        follow|f)
            echo "=== Following $ERR_LOG (Ctrl+C to stop) ==="
            tail -f "$ERR_LOG"
            ;;
        all)
            echo "=== $OUT_LOG (last $lines lines) ==="
            tail -n "$lines" "$OUT_LOG" 2>/dev/null || echo "  (no stdout log)"
            echo ""
            echo "=== $ERR_LOG (last $lines lines) ==="
            tail -n "$lines" "$ERR_LOG" 2>/dev/null || echo "  (no stderr log)"
            ;;
        *)
            echo "Usage: ./service.sh logs [out|err|follow|all] [lines]"
            return 1
            ;;
    esac
}

cmd_doctor() {
    "$ROOT/bin/code-review-service" doctor
}

case "$COMMAND" in
    status) cmd_status ;;
    start) cmd_start ;;
    stop) cmd_stop ;;
    restart) cmd_restart ;;
    install) cmd_install "$@" ;;
    uninstall) cmd_uninstall ;;
    logs) cmd_logs "$@" ;;
    doctor) cmd_doctor ;;
    dashboard) dashboard_cmd "$@" ;;
    labels) printf 'service=%s\ndashboard=%s\n' "$LABEL" "$DASHBOARD_LABEL" ;;
    *)
        echo "Review of Legends - Service Manager"
        echo ""
        echo "Usage: ./service.sh <command> [args]"
        echo ""
        echo "Commands:"
        echo "  status      Show service status"
        echo "  start       Start the service"
        echo "  stop        Stop the service"
        echo "  restart     Restart the service"
        echo "  install     Install/update LaunchAgent (daemon + dashboard)"
        echo "  uninstall   Remove LaunchAgent (data preserved)"
        echo "  logs        View logs [out|err|follow|all] [lines]"
        echo "  doctor      Run service doctor"
        echo "  dashboard   Dashboard subcommand (preflight|install|verify|start|stop|uninstall)"
        echo "  labels      Print the launchd labels (config/deploy.env)"
        echo ""
        echo "Examples:"
        echo "  ./service.sh status"
        echo "  ./service.sh logs all 100"
        echo "  ./service.sh logs follow"
        echo "  ./service.sh dashboard verify"
        ;;
esac
