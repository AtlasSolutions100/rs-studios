#!/usr/bin/env bash
# A stand-in for rclone in the backup and restore tests: no network, no real
# encryption. It reads the remotes from RCLONE_CONFIG_* the way rclone does,
# checks that every path goes through a crypt remote whose passwords were
# obscured and that sits on an S3 remote set up for Cloudflare R2, and keeps the
# "bucket" as plain folders under $FAKE_RCLONE_ROOT.
#
#   FAKE_RCLONE_ROOT   folder that plays the bucket store (required)
#   FAKE_RCLONE_LOG    every call is appended here, one line each
#   FAKE_RCLONE_FAIL   a subcommand (copyto, sync, lsf, ...) that fails with exit 1
#   FAKE_RCLONE_SAY    text the failure prints on stderr
#
# Subcommands: obscure -, copyto, copy, sync (--backup-dir, --exclude), lsf
# (--files-only, --dirs-only), deletefile, purge, version.
set -u

say() { [ -n "${FAKE_RCLONE_LOG:-}" ] && printf '%s\n' "$*" >> "$FAKE_RCLONE_LOG"; return 0; }
die() { echo "fake rclone: $*" >&2; exit "${2:-1}"; }

pos=(); excludes=(); backup_dir=""; only=""
while [ $# -gt 0 ]; do
  case "$1" in
    -q|-v|--fast-list) ;;
    --files-only) only=files ;;
    --dirs-only) only=dirs ;;
    --retries|--low-level-retries|--bwlimit|--transfers|--checkers) shift ;;
    --backup-dir) shift; backup_dir="$1" ;;
    --exclude) shift; excludes+=("$1") ;;
    --*) die "unknown flag $1" 2 ;;
    *) pos+=("$1") ;;
  esac
  shift
done
[ ${#pos[@]} -gt 0 ] || die "no subcommand" 2
cmd="${pos[0]}"
say "$cmd ${pos[*]:1}${backup_dir:+ --backup-dir $backup_dir}${excludes[*]:+ --exclude ${excludes[*]}}"

if [ "${FAKE_RCLONE_FAIL:-}" = "$cmd" ]; then
  echo "ERROR : ${FAKE_RCLONE_SAY:-fake failure}" >&2
  exit 1
fi

# NAME:path -> a local folder, after checking the remote's settings. Plain paths pass through.
resolve() {
  local a="$1"
  if [[ "$a" =~ ^([A-Za-z0-9_]+):(.*)$ ]]; then
    local name="${BASH_REMATCH[1]}" path="${BASH_REMATCH[2]}"
    local up; up=$(printf '%s' "$name" | tr '[:lower:]' '[:upper:]')
    local tv="RCLONE_CONFIG_${up}_TYPE"
    case "${!tv:-}" in
      crypt)
        local pv="RCLONE_CONFIG_${up}_PASSWORD" sv="RCLONE_CONFIG_${up}_PASSWORD2" rv="RCLONE_CONFIG_${up}_REMOTE"
        [[ "${!pv:-}" == obscured-* ]] || die "crypt remote $name: PASSWORD is not obscured"
        [[ "${!sv:-}" == obscured-* ]] || die "crypt remote $name: PASSWORD2 (salt) is not obscured"
        [ -n "${!rv:-}" ] || die "crypt remote $name has no REMOTE"
        say "via crypt $name -> ${!rv}"
        local inner; inner=$(resolve "${!rv}") || exit 1
        printf '%s' "${inner%/}/${path#/}" ;;
      s3)
        local prov="RCLONE_CONFIG_${up}_PROVIDER" ep="RCLONE_CONFIG_${up}_ENDPOINT" ak="RCLONE_CONFIG_${up}_ACCESS_KEY_ID"
        local sk="RCLONE_CONFIG_${up}_SECRET_ACCESS_KEY" nc="RCLONE_CONFIG_${up}_NO_CHECK_BUCKET"
        [ "${!prov:-}" = "Cloudflare" ] || die "s3 remote $name: PROVIDER is not Cloudflare"
        [[ "${!ep:-}" =~ ^https://[A-Za-z0-9]+\.r2\.cloudflarestorage\.com$ ]] || die "s3 remote $name: bad R2 endpoint"
        [ -n "${!ak:-}" ] && [ -n "${!sk:-}" ] || die "s3 remote $name: keys missing"
        [ "${!nc:-}" = "true" ] || die "s3 remote $name: NO_CHECK_BUCKET must be true for a bucket-scoped token"
        [ -n "${FAKE_RCLONE_ROOT:-}" ] || die "FAKE_RCLONE_ROOT not set"
        printf '%s' "$FAKE_RCLONE_ROOT/${path#/}" ;;
      *) die "unknown remote $name" ;;
    esac
  else
    printf '%s' "$a"
  fi
}

excluded() {
  local base; base=$(basename "$1")
  for pat in "${excludes[@]+"${excludes[@]}"}"; do
    # shellcheck disable=SC2053
    [[ "$base" == $pat ]] && return 0
  done
  return 1
}

case "$cmd" in
  version) echo "rclone v0.0.0-fake" ;;
  obscure)
    [ "${pos[1]:-}" = "-" ] || die "obscure: pass the password on stdin with -" 2
    IFS= read -r line || true
    [ -n "$line" ] || die "obscure: empty password"
    printf 'obscured-%s\n' "$(printf '%s' "$line" | base64 | tr -d '\n=' | tr '+/' '-_')" ;;
  copyto)
    src=$(resolve "${pos[1]}") || exit 1; dst=$(resolve "${pos[2]}") || exit 1
    [ -f "$src" ] || die "copyto: $src not found" 3
    mkdir -p "$(dirname "$dst")" && cp "$src" "$dst" ;;
  copy)
    src=$(resolve "${pos[1]}") || exit 1; dst=$(resolve "${pos[2]}") || exit 1
    [ -d "$src" ] || die "directory not found" 3
    mkdir -p "$dst" && cp -a "$src/." "$dst/" ;;
  sync)
    src=$(resolve "${pos[1]}") || exit 1; dst=$(resolve "${pos[2]}") || exit 1
    bdir=""; [ -n "$backup_dir" ] && { bdir=$(resolve "$backup_dir") || exit 1; }
    [ -d "$src" ] || die "directory not found" 3
    mkdir -p "$dst"
    while IFS= read -r rel; do
      excluded "$rel" && continue
      if [ ! -f "$dst/$rel" ] || ! cmp -s "$src/$rel" "$dst/$rel"; then
        if [ -f "$dst/$rel" ] && [ -n "$bdir" ]; then mkdir -p "$(dirname "$bdir/$rel")"; mv "$dst/$rel" "$bdir/$rel"; fi
        mkdir -p "$(dirname "$dst/$rel")" && cp "$src/$rel" "$dst/$rel"
      fi
    done < <(cd "$src" && find . -type f | sed 's|^\./||')
    while IFS= read -r rel; do
      excluded "$rel" && continue
      [ -f "$src/$rel" ] && continue
      if [ -n "$bdir" ]; then mkdir -p "$(dirname "$bdir/$rel")"; mv "$dst/$rel" "$bdir/$rel"; else rm -f "$dst/$rel"; fi
    done < <(cd "$dst" && find . -type f | sed 's|^\./||')
    ;;
  lsf)
    d=$(resolve "${pos[1]}") || exit 1
    [ -d "$d" ] || die "directory not found" 3
    for e in "$d"/* ; do
      [ -e "$e" ] || continue
      if [ -d "$e" ]; then [ "$only" = files ] || echo "$(basename "$e")/"
      else [ "$only" = dirs ] || basename "$e"; fi
    done ;;
  deletefile)
    f=$(resolve "${pos[1]}") || exit 1
    [ -f "$f" ] || die "object not found" 4
    rm -f "$f" ;;
  purge)
    d=$(resolve "${pos[1]}") || exit 1
    [ -d "$d" ] || die "directory not found" 3
    rm -rf "$d" ;;
  *) die "unknown command $cmd" 2 ;;
esac
