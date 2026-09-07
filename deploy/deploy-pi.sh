#!/usr/bin/env bash
# Put the rig on a Raspberry Pi and keep it there.
#
#   ./deploy/deploy-pi.sh operator@rig.local [token]
#   ./deploy/deploy-pi.sh --package-only /tmp/osmo-rig.tgz
#
# Idempotent: run it again after any change and it stages, proves, then atomically
# switches to a versioned release. The token is generated once and reused on later
# runs so the URL an operator has bookmarked keeps working.
#
# Why a Pi at all: a laptop on a stand is a thing to trip over on set, and the
# camera's access point has no internet, so whatever joins it loses its own
# network for the duration. Better that be a fifty pound board strapped to the
# tripod than the machine everything else is on.
#
# To undo everything:
#   systemctl --user disable --now osmo-rig
#   rm ~/.config/systemd/user/osmo-rig.service ; rm -rf ~/osmo-rig

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

create_archive() {
    local archive="$1"
    # Ship only OsmoDesk. OsmoPalm firmware is independently released.
    # Contract fixtures are required by the offline candidate tests.
    tar --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='.pio' --exclude='*.pio/**' --exclude='*.log' --exclude='moves' \
        -czf "$archive" \
        -C "$HERE" driver web tests deploy run.py server.py requirements.txt contracts
}

if [ "${1:-}" = "--package-only" ]; then
    OUTPUT="${2:-}"
    if [ -z "$OUTPUT" ]; then
        echo "usage: $0 --package-only /path/to/osmo-rig.tgz" >&2
        exit 2
    fi
    create_archive "$OUTPUT"
    exit 0
fi

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
    echo "usage: $0 user@host [token]" >&2
    exit 2
fi
REMOTE_DIR="osmo-rig"

# --- token ------------------------------------------------------------------
# Reused from the running install when one is not given, so a redeploy does not
# invalidate a bookmarked URL mid-shoot.
TOKEN="${2:-}"
if [ -z "$TOKEN" ]; then
    TOKEN="$(ssh -o BatchMode=yes "$TARGET" "grep -oP '(?<=--token )\\S+' ~/.config/systemd/user/osmo-rig.service 2>/dev/null || true")"
fi
if [ -z "$TOKEN" ]; then
    if command -v python3 >/dev/null 2>&1; then
        TOKEN="$(python3 -c "import secrets; print(secrets.token_urlsafe(9))")"
    else
        TOKEN="$(head -c 12 /dev/urandom | base64 | tr -d '=+/')"
    fi
    echo "generated a new access token"
fi

# --- ship -------------------------------------------------------------------
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
create_archive "$STAGE/rig.tgz"
ARCHIVE_SHA="$(sha256sum "$STAGE/rig.tgz" | awk '{print $1}')"
# Content-addressed releases make a repeated deployment of unchanged source a
# no-op on the Pi, including two invocations in the same second.
RELEASE_ID="release-$ARCHIVE_SHA"

scp -q -o BatchMode=yes "$STAGE/rig.tgz" "$TARGET:/tmp/osmo-rig-$ARCHIVE_SHA.tgz"

# --- install ----------------------------------------------------------------
ssh -o BatchMode=yes "$TARGET" TOKEN="$TOKEN" REMOTE_DIR="$REMOTE_DIR" RELEASE_ID="$RELEASE_ID" ARCHIVE_SHA="$ARCHIVE_SHA" 'bash -s' <<'REMOTE'
set -euo pipefail

APP_LINK="$HOME/$REMOTE_DIR"
STATE_DIR="$HOME/.local/share/$REMOTE_DIR"
RELEASES_DIR="$STATE_DIR/releases"
RELEASE_DIR="$RELEASES_DIR/$RELEASE_ID"
PREVIOUS_RELEASE="$(readlink -f "$APP_LINK" 2>/dev/null || true)"
ARCHIVE_PATH="/tmp/osmo-rig-$ARCHIVE_SHA.tgz"
PERSISTENT_MOVES="$STATE_DIR/moves"
UNIT_PATH="$HOME/.config/systemd/user/osmo-rig.service"
UNIT_BACKUP="$STATE_DIR/osmo-rig.service.before-$RELEASE_ID"
ACTIVATED=0
LEGACY_MOVED=0
UNIT_REPLACED=0
UNIT_HAD_PREVIOUS=0

rollback_activation() {
    local original_status="${1:-$?}"
    trap - ERR
    if { [ "$ACTIVATED" -eq 1 ] || [ "$LEGACY_MOVED" -eq 1 ]; } \
        && [ -n "$PREVIOUS_RELEASE" ] && [ -d "$PREVIOUS_RELEASE" ]; then
        echo "activation failed; restoring previous release" >&2
        ln -sfn "$PREVIOUS_RELEASE" "$APP_LINK.rollback"
        mv -Tf "$APP_LINK.rollback" "$APP_LINK"
        if [ "$UNIT_REPLACED" -eq 1 ]; then
            if [ "$UNIT_HAD_PREVIOUS" -eq 1 ]; then
                cp -p "$UNIT_BACKUP" "$UNIT_PATH.rollback" || true
                mv -Tf "$UNIT_PATH.rollback" "$UNIT_PATH" || true
            else
                rm -f "$UNIT_PATH"
            fi
        fi
        systemctl --user daemon-reload || true
        systemctl --user restart osmo-rig || true
    fi
    exit "$original_status"
}
trap 'rollback_activation $?' ERR

mkdir -p "$RELEASES_DIR"
if [ "$PREVIOUS_RELEASE" = "$RELEASE_DIR" ]; then
    echo "release already active: $RELEASE_ID"
    rm -f "$ARCHIVE_PATH"
    exit 0
fi

if [ ! -d "$RELEASE_DIR" ]; then
    INCOMING_DIR="$(mktemp -d "$RELEASES_DIR/.incoming.XXXXXX")"
    printf '%s  %s\n' "$ARCHIVE_SHA" "$ARCHIVE_PATH" | sha256sum -c -
    tar xzf "$ARCHIVE_PATH" -C "$INCOMING_DIR"
    find "$INCOMING_DIR" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
    mv "$INCOMING_DIR" "$RELEASE_DIR"
fi
rm -f "$ARCHIVE_PATH"
cd "$RELEASE_DIR"

# Operator-authored moves and timelapse progress outlive source releases. Seed
# the state directory without overwriting it, including a pre-release install.
mkdir -p "$PERSISTENT_MOVES"
if [ -n "$PREVIOUS_RELEASE" ] && [ -d "$PREVIOUS_RELEASE/moves" ] \
    && [ "$(readlink -f "$PREVIOUS_RELEASE/moves")" != "$PERSISTENT_MOVES" ]; then
    cp -a -n "$PREVIOUS_RELEASE/moves/." "$PERSISTENT_MOVES/"
fi
if [ -e "$RELEASE_DIR/moves" ] || [ -L "$RELEASE_DIR/moves" ]; then
    [ "$(readlink -f "$RELEASE_DIR/moves")" = "$PERSISTENT_MOVES" ] \
        || { echo "refusing to replace release-local moves" >&2; exit 1; }
else
    ln -s "$PERSISTENT_MOVES" "$RELEASE_DIR/moves"
fi

python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt

# Prove it before installing it. A service that starts and then fails its own
# tests is worse than one that never started.
./.venv/bin/python -m unittest discover -s tests -q 2>&1 | tail -3
# A reused content-addressed release was prepared earlier; record this
# activation so it remains among the newest rollback candidates later.
touch -m "$RELEASE_DIR"

# An older in-place installation cannot be atomically overwritten. Move it
# only after the candidate has proved itself, keeping the live service intact
# throughout preparation.
if [ -e "$APP_LINK" ] && [ ! -L "$APP_LINK" ]; then
    LEGACY_DIR="$STATE_DIR/legacy-$(date -u +%Y%m%dT%H%M%SZ)"
    mv "$APP_LINK" "$LEGACY_DIR"
    PREVIOUS_RELEASE="$LEGACY_DIR"
    LEGACY_MOVED=1
fi

# Keep the service's existing WorkingDirectory and executable path stable while
# changing both together. Renaming a symlink is atomic on the Pi's filesystem.
ln -sfn "$RELEASE_DIR" "$APP_LINK.next"
mv -Tf "$APP_LINK.next" "$APP_LINK"
ACTIVATED=1

mkdir -p "$HOME/.config/systemd/user"
if [ -f "$UNIT_PATH" ]; then
    cp -p "$UNIT_PATH" "$UNIT_BACKUP"
    UNIT_HAD_PREVIOUS=1
fi
TOKEN_ESCAPED="$(printf '%s' "$TOKEN" | sed 's/[&|\\]/\\&/g')"
sed "s|__TOKEN__|$TOKEN_ESCAPED|" deploy/osmo-rig.service > "$UNIT_PATH.next"
mv -Tf "$UNIT_PATH.next" "$UNIT_PATH"
UNIT_REPLACED=1

# Without lingering, a user service stops at logout -- which on a headless rig
# means it stops as soon as the SSH session that started it ends.
loginctl enable-linger "$USER" 2>/dev/null || true

systemctl --user daemon-reload
systemctl --user enable osmo-rig >/dev/null
systemctl --user reset-failed osmo-rig 2>/dev/null || true
systemctl --user restart osmo-rig

# Poll rather than sleep: a fixed wait reports "activating" on a slow boot and
# "active" on a fast one, so it says nothing useful either way.
for _ in $(seq 20); do
    state=$(systemctl --user is-active osmo-rig || true)
    [ "$state" = "active" ] && break
    [ "$state" = "failed" ] && break
    sleep 0.5
done
echo "service: $state"
if [ "$state" != "active" ]; then
    systemctl --user status osmo-rig --no-pager | head -15
    rollback_activation 1
fi
trap - ERR

# Retain the active release exactly, plus the two most recently activated
# *other* releases. Hash-based names have no chronological ordering.
ACTIVE_RELEASE="$(readlink -f "$APP_LINK")"
kept_other=0
while IFS= read -r -d '' release_record; do
    candidate_release="${release_record#*:}"
    [ "$(readlink -f "$candidate_release")" = "$ACTIVE_RELEASE" ] && continue
    kept_other=$((kept_other + 1))
    [ "$kept_other" -le 2 ] && continue
    rm -rf -- "$candidate_release" || true
done < <(find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d -name 'release-*' -printf '%T@:%p\0' | sort -z -nr)
REMOTE

HOST="${TARGET#*@}"
echo
echo "rig is up:  http://$HOST:8722?t=$TOKEN"
echo "logs:       ssh $TARGET journalctl --user -u osmo-rig -f"
echo "stop:       ssh $TARGET systemctl --user stop osmo-rig"
