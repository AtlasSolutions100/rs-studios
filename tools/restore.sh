#!/usr/bin/env bash
# Restore drill for Campaignify (roadmap M1.6): rebuild the data folder from the
# newest off-site backup on a scratch path, then check that the app boots on it.
# It never touches the live data folder.
#
#   tools/restore.sh [--settings FILE] [--snapshot NAME] [--media] SCRATCH_DIR
#
#   --settings FILE   read the six backup settings from this file (e.g. the server's
#                     /opt/rs-studios/settings.txt) instead of the environment
#   --snapshot NAME   restore this snapshot (rs-YYYYMMDDTHHMMSSZ.db.gz) instead of the newest
#   --media           also download the media folders (can be many GB)
#
# Settings: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET,
# BACKUP_CRYPT_PASSWORD, BACKUP_CRYPT_SALT (and BACKUP_PREFIX if it was changed).
# Needs rclone and python3 with the app's requirements. The app image has both:
#   docker exec rs-studios-app tools/restore.sh /tmp/restore-drill
# Result: SCRATCH_DIR/data is a working data folder. Run the app on it with
# RS_DATA=SCRATCH_DIR/data. Delete SCRATCH_DIR when you are done (it holds customer data).
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RCLONE=${RCLONE_BIN:-rclone}
PY=${PYTHON:-python3}
KEYS="R2_ACCOUNT_ID R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_BUCKET BACKUP_CRYPT_PASSWORD BACKUP_CRYPT_SALT"
MEDIA_DIRS="uploads media presenters"
settings=""; snapshot=""; media=0; scratch=""

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
while [ $# -gt 0 ]; do
  case "$1" in
    --settings) settings="$2"; shift ;;
    --snapshot) snapshot="$2"; shift ;;
    --media) media=1 ;;
    -h|--help) usage ;;
    -*) echo "unknown option $1" >&2; usage ;;
    *) [ -z "$scratch" ] || usage; scratch="$1" ;;
  esac
  shift
done
[ -n "$scratch" ] || usage

if [ -n "$settings" ]; then
  [ -r "$settings" ] || { echo "can't read $settings" >&2; exit 2; }
  for k in $KEYS BACKUP_PREFIX; do
    line=$(grep -E "^${k}=" "$settings" | tail -1 || true)
    [ -n "$line" ] && export "$k=${line#*=}"
  done
fi
missing=""
for k in $KEYS; do [ -n "${!k:-}" ] || missing="$missing $k"; done
if [ -n "$missing" ]; then
  echo "Not set:$missing. Export them or pass --settings FILE." >&2
  exit 2
fi
command -v "$RCLONE" >/dev/null 2>&1 || { echo "rclone isn't installed (or set RCLONE_BIN)" >&2; exit 2; }

if [ -e "$scratch" ] && [ -n "$(ls -A "$scratch" 2>/dev/null)" ]; then
  echo "$scratch is not empty. Pick a new scratch folder (this script never overwrites anything)." >&2
  exit 2
fi
mkdir -p "$scratch/data"
scratch=$(cd "$scratch" && pwd)
live="${RS_DATA:-$ROOT/data}"
if [ -d "$live" ] && [ "$(cd "$live" && pwd)" = "$scratch/data" ]; then
  echo "refusing: $scratch/data is the live data folder" >&2; exit 2
fi

# The same two remotes backup.py uses, only in this process's environment.
prefix=${BACKUP_PREFIX:-campaignify}
export RCLONE_CONFIG_CFYBR2_TYPE=s3 RCLONE_CONFIG_CFYBR2_PROVIDER=Cloudflare \
       RCLONE_CONFIG_CFYBR2_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" \
       RCLONE_CONFIG_CFYBR2_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
       RCLONE_CONFIG_CFYBR2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com" \
       RCLONE_CONFIG_CFYBR2_REGION=auto RCLONE_CONFIG_CFYBR2_ACL=private RCLONE_CONFIG_CFYBR2_NO_CHECK_BUCKET=true
export RCLONE_CONFIG_CFYBACKUP_TYPE=crypt RCLONE_CONFIG_CFYBACKUP_REMOTE="cfybr2:${R2_BUCKET}/${prefix}" \
       RCLONE_CONFIG_CFYBACKUP_FILENAME_ENCRYPTION=standard RCLONE_CONFIG_CFYBACKUP_DIRECTORY_NAME_ENCRYPTION=true
RCLONE_CONFIG_CFYBACKUP_PASSWORD=$(printf '%s\n' "$BACKUP_CRYPT_PASSWORD" | "$RCLONE" obscure -)
RCLONE_CONFIG_CFYBACKUP_PASSWORD2=$(printf '%s\n' "$BACKUP_CRYPT_SALT" | "$RCLONE" obscure -)
export RCLONE_CONFIG_CFYBACKUP_PASSWORD RCLONE_CONFIG_CFYBACKUP_PASSWORD2
rc() { "$RCLONE" -q --retries 3 --low-level-retries 10 "$@"; }

if [ -z "$snapshot" ]; then
  snapshot=$(rc lsf --files-only cfybackup:db | grep -E '^rs-[0-9]{8}T[0-9]{6}Z\.db\.gz$' | sort | tail -1 || true)
  [ -n "$snapshot" ] || { echo "no snapshot found in ${R2_BUCKET}/${prefix}/db" >&2; exit 1; }
fi
echo "Restoring $snapshot into $scratch/data"
rc copyto "cfybackup:db/$snapshot" "$scratch/$snapshot"
gzip -dc "$scratch/$snapshot" > "$scratch/data/rs.db"
rm -f "$scratch/$snapshot"
if ! rc copyto cfybackup:keys/secret_key "$scratch/data/secret_key" 2>/dev/null; then
  echo "  (no session key in the backup: members would have to sign in again)"
fi
if [ "$media" = 1 ]; then
  for d in $MEDIA_DIRS; do
    echo "  media/$d"
    rc copy "cfybackup:media/$d" "$scratch/data/$d" || echo "  (media/$d not in the backup)"
  done
fi

smoke_args=("$scratch/data")
[ "$media" = 1 ] && smoke_args+=(--media)
cd "$ROOT"
"$PY" tools/restore_smoke.py "${smoke_args[@]}"
echo "Restore drill passed: $snapshot -> $scratch/data. Delete $scratch when done (it holds customer data)."
