#!/usr/bin/env bash
# Idempotent SCI systemd-sysext delivery step; contract in delivery/README.md.
# Usage: sci-delivery.sh [--help]

set -Eeuo pipefail

HOST_ROOT=${HOST_ROOT:-/host}
# Remember whether the caller chose STAGING_DIR; fetch mode may supply its own
# default only when they did not.
STAGING_DIR_EXPLICIT=${STAGING_DIR:+1}
STAGING_DIR=${STAGING_DIR:-/opt/sci}
INTERVAL=${INTERVAL:-60}
NSENTER=${NSENTER:-1}

# Fetch mode: download one signed build instead of reading a pre-staged tree.
FETCH_URL=${FETCH_URL:-}
FETCH_PUBKEY=${FETCH_PUBKEY:-}
FETCH_SUMS_DIGEST=${FETCH_SUMS_DIGEST:-}
FETCH_DIR=${FETCH_DIR:-/var/lib/sci-sysext}
FETCH_TMP=

# Dot-file replay marker; not an extension.
PENDING_MARKER=.sci-delivery-pending

log() {
    printf '[sci-delivery] %s\n' "$*"
}

warn() {
    printf '[sci-delivery] warning: %s\n' "$*" >&2
}

die() {
    printf '[sci-delivery] error: %s\n' "$*" >&2
    exit 1
}

fetch_cleanup() {
    if [ -n "$FETCH_TMP" ] && [ -d "$FETCH_TMP" ]; then
        rm -rf -- "$FETCH_TMP"
    fi
    return 0
}

trap fetch_cleanup EXIT
trap 'exit 0' TERM INT

usage() {
    cat <<'EOF'
usage: sci-delivery.sh [--help]

Reconciles the SCI systemd-sysext staging directory into the host root.
Intended to run in a privileged pod with hostPID and the host / mounted at
HOST_ROOT.

Staging layout:
  <name>.raw                        extension image
  <name>-activation.service         optional activation shim template
  extensions.manifest               directives plus "<name> <restart-unit>..."
                                    per line; TARGET_BASE=<major> (the
                                    Garden Linux base line) is required
  SHA256SUMS                        plain sha256sum output, one
                                    "<hash>  <name>.raw" line per entry

A staging set whose raws do not match SHA256SUMS, or a host os-release that
does not name the manifest's TARGET_BASE line, deploys nothing and exits
non-zero.

Environment:
  HOST_ROOT    host root mount point (default: /host)
  STAGING_DIR  extension staging directory (default: /opt/sci, or
               $FETCH_DIR/staging in fetch mode when unset)
  INTERVAL     seconds between passes; 0 runs one pass (default: 60)
  NSENTER      1 runs systemctl/systemd-sysext via nsenter -t 1 -m,
               0 runs them directly (default: 1)

Fetch mode is active when FETCH_URL is set. It downloads one build from that
base URL instead of trusting whatever is already staged:
  FETCH_URL          base URL of the build (unset selects image mode)
  FETCH_PUBKEY       PEM public key that verifies SHA256SUMS.sig (required)
  FETCH_SUMS_DIGEST  optional pinned sha256 of SHA256SUMS (64 lowercase hex)
  FETCH_DIR          writable fetch root (default: /var/lib/sci-sysext)

Manifest directives:
  TARGET_BASE=<major>  required Garden Linux base line
  REMOVE=<name>        tombstone: delete a previously deployed extension
EOF
}

# Run a host-mutating tool through PID 1's mount namespace when NSENTER=1.
run_host() {
    if [ "$NSENTER" = "1" ]; then
        nsenter -t 1 -m -- "$@"
    else
        "$@"
    fi
}

files_equal() { # <a> <b>
    [ -f "$1" ] && [ -f "$2" ] || return 1
    local a b
    a=$(md5sum -- "$1") || return 1
    b=$(md5sum -- "$2") || return 1
    [ "${a%% *}" = "${b%% *}" ]
}

# Restore the parent mtime so only the file content changes.
write_atomic() {
    local src=$1 dest=$2 dir mtime
    dir=$(dirname -- "$dest")
    mtime=$(LC_ALL=C stat -c '%.9Y' -- "$dir")
    cp -- "$src" "$dest.tmp"
    mv -- "$dest.tmp" "$dest"
    LC_ALL=C touch -m -d "@$mtime" -- "$dir"
}

declare -a NAMES=()
declare -a UNIT_LISTS=()
declare -a REMOVE_NAMES=()
TARGET_BASE=

validate_env() {
    [ -d "$HOST_ROOT" ] || die "HOST_ROOT is not an existing directory: $HOST_ROOT"
    case $INTERVAL in
        '' | *[!0-9]*) die "INTERVAL must be a non-negative integer: $INTERVAL" ;;
    esac
}

fetch_get() { # <inventory-name> <destination>
    curl -fsS --max-time 30 --retry 2 --retry-delay 1 \
        -o "$2" "$FETCH_URL/$1" \
        || die "fetch: cannot download $1 from $FETCH_URL"
}

fetch_get_file() { # <inventory-name> <destination>
    curl -fsS --connect-timeout 15 --max-time 600 \
        --retry 2 --retry-delay 1 \
        -o "$2" "$FETCH_URL/$1" \
        || die "fetch: cannot download $1 from $FETCH_URL"
}

# Populate STAGING_DIR from FETCH_URL and prove the downloaded set before any
# host mutation. Returns immediately in image mode.
fetch_staging() {
    [ -n "$FETCH_URL" ] || return 0

    if [ -z "$STAGING_DIR_EXPLICIT" ]; then
        STAGING_DIR=$FETCH_DIR/staging
    fi

    [ -n "$FETCH_PUBKEY" ] \
        || die "fetch: FETCH_PUBKEY is required when FETCH_URL is set"
    [ -f "$FETCH_PUBKEY" ] && [ -r "$FETCH_PUBKEY" ] \
        || die "fetch: FETCH_PUBKEY is not a readable file: $FETCH_PUBKEY"
    if [ -n "$FETCH_SUMS_DIGEST" ] \
        && [[ ! $FETCH_SUMS_DIGEST =~ ^[0-9a-f]{64}$ ]]; then
        die "fetch: FETCH_SUMS_DIGEST must be 64 lowercase hex characters: $FETCH_SUMS_DIGEST"
    fi

    mkdir -p -- "$FETCH_DIR/staging" \
        || die "fetch: cannot create $FETCH_DIR/staging"
    mkdir -p -- "$STAGING_DIR" \
        || die "fetch: cannot create the staging directory: $STAGING_DIR"

    FETCH_TMP=$(mktemp -d "$FETCH_DIR/.fetch.XXXXXX") \
        || die "fetch: cannot create a temporary directory under $FETCH_DIR"

    fetch_get SHA256SUMS "$FETCH_TMP/SHA256SUMS"
    fetch_get SHA256SUMS.sig "$FETCH_TMP/SHA256SUMS.sig"

    local actual
    if [ -n "$FETCH_SUMS_DIGEST" ]; then
        actual=$(sha256sum -- "$FETCH_TMP/SHA256SUMS") \
            || die "fetch: cannot hash the downloaded SHA256SUMS"
        actual=${actual%% *}
        [ "$actual" = "$FETCH_SUMS_DIGEST" ] \
            || die "fetch: SHA256SUMS digest mismatch: expected $FETCH_SUMS_DIGEST"
    fi

    if ! openssl dgst -sha256 -verify "$FETCH_PUBKEY" \
        -signature "$FETCH_TMP/SHA256SUMS.sig" "$FETCH_TMP/SHA256SUMS" \
        >/dev/null 2>&1; then
        die "fetch: SHA256SUMS signature verification failed"
    fi
    log "fetch: verified SHA256SUMS signature"

    local -a fetch_names=()
    local -A fetch_sums=()
    local -A fetch_listed=()
    local line fname sum lineno=0
    local sums_line_re='^([0-9a-f]{64}) [ *]([A-Za-z0-9][A-Za-z0-9._-]*)$'
    while IFS= read -r line || [ -n "$line" ]; do
        lineno=$((lineno + 1))
        line=${line%$'\r'}
        [ -n "$line" ] || continue
        if [[ ! $line =~ $sums_line_re ]]; then
            die "fetch: invalid SHA256SUMS line $lineno: $line"
        fi
        sum=${BASH_REMATCH[1]}
        fname=${BASH_REMATCH[2]}
        if [ -n "${fetch_sums[$fname]+set}" ]; then
            die "fetch: duplicate SHA256SUMS line for '$fname' at line $lineno"
        fi
        fetch_sums[$fname]=$sum
        fetch_listed[$fname]=1
        fetch_names+=("$fname")
    done <"$FETCH_TMP/SHA256SUMS"

    cp -- "$FETCH_TMP/SHA256SUMS" "$STAGING_DIR/SHA256SUMS" \
        || die "fetch: cannot copy SHA256SUMS into $STAGING_DIR"

    local dest part
    for fname in "${fetch_names[@]}"; do
        dest=$STAGING_DIR/$fname
        if [ -f "$dest" ]; then
            actual=$(sha256sum -- "$dest") \
                || die "fetch: cannot hash $dest"
            actual=${actual%% *}
            if [ "$actual" = "${fetch_sums[$fname]}" ]; then
                log "fetch: up to date $fname"
                continue
            fi
        fi
        part=$dest.part
        fetch_get_file "$fname" "$part"
        actual=$(sha256sum -- "$part") \
            || die "fetch: cannot hash $part"
        actual=${actual%% *}
        [ "$actual" = "${fetch_sums[$fname]}" ] \
            || die "fetch: hash mismatch for $fname"
        mv -- "$part" "$dest" \
            || die "fetch: cannot move $fname into $STAGING_DIR"
        log "fetch: downloaded $fname"
    done

    ( cd -- "$STAGING_DIR" && sha256sum -c -- SHA256SUMS >/dev/null ) \
        || die "fetch: staged set is incomplete or corrupt"
    log "fetch: staging verified (${#fetch_names[@]} files)"

    # Prune what the verified inventory no longer lists, so a dropped
    # extension does not leave a stale raw or shim behind for load_manifest.
    local -a stale=()
    local stale_path stale_base
    local prune_nullglob=0
    if shopt -q nullglob; then
        prune_nullglob=1
    else
        shopt -s nullglob
    fi
    for stale_path in "$STAGING_DIR"/*.raw \
        "$STAGING_DIR"/*-activation.service "$STAGING_DIR"/*.part; do
        [ -f "$stale_path" ] || continue
        stale_base=${stale_path##*/}
        case $stale_base in
            *.part)
                stale+=("$stale_base")
                ;;
            *)
                if [ -z "${fetch_listed[$stale_base]+set}" ]; then
                    stale+=("$stale_base")
                fi
                ;;
        esac
    done
    if [ "$prune_nullglob" -eq 0 ]; then
        shopt -u nullglob
    fi
    for stale_base in "${stale[@]}"; do
        rm -f -- "$STAGING_DIR/$stale_base"
        log "fetch: pruned $stale_base"
    done
    if [ "${#stale[@]}" -gt 0 ]; then
        log "fetch: pruned ${#stale[@]} stale file(s)"
    fi

    rm -rf -- "$FETCH_TMP"
    FETCH_TMP=
}

load_manifest() {
    local manifest=$STAGING_DIR/extensions.manifest
    [ -f "$manifest" ] || die "missing manifest: $manifest"

    NAMES=()
    UNIT_LISTS=()
    REMOVE_NAMES=()
    TARGET_BASE=
    local -A remove_seen=()
    local line name rest lineno=0
    while IFS= read -r line || [ -n "$line" ]; do
        lineno=$((lineno + 1))
        line=${line%$'\r'}
        if [ -z "$line" ]; then
            continue
        fi
        if [[ $line =~ ^[[:space:]]*$ ]]; then
            die "malformed manifest line $lineno (whitespace-only): $manifest"
        fi
        if [[ $line =~ ^[[:space:]]*# ]]; then
            continue
        fi
        if [[ $line =~ ^([A-Z][A-Z0-9_]*)=([^[:space:]]*)$ ]]; then
            case ${BASH_REMATCH[1]} in
                TARGET_BASE)
                    if [ -n "$TARGET_BASE" ]; then
                        die "duplicate TARGET_BASE directive at line $lineno (exactly one is required): $manifest"
                    fi
                    TARGET_BASE=${BASH_REMATCH[2]}
                    if [[ ! $TARGET_BASE =~ ^[0-9]+$ ]]; then
                        die "TARGET_BASE='$TARGET_BASE' at line $lineno is not digits-only (expected the Garden Linux major line, e.g. 2150): $manifest"
                    fi
                    ;;
                REMOVE)
                    name=${BASH_REMATCH[2]}
                    if [[ ! $name =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
                        die "fetch: REMOVE='$name' at line $lineno is not a valid extension name: $manifest"
                    fi
                    if [ -n "${remove_seen[$name]+set}" ]; then
                        die "fetch: duplicate REMOVE directive for '$name' at line $lineno: $manifest"
                    fi
                    remove_seen[$name]=1
                    REMOVE_NAMES+=("$name")
                    ;;
                *)
                    die "unknown manifest directive '${BASH_REMATCH[1]}' at line $lineno (TARGET_BASE and REMOVE are the only directives): $manifest"
                    ;;
            esac
            continue
        fi
        read -r name rest <<<"$line"
        if [[ ! $name =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
            die "malformed manifest line $lineno (invalid name '$name'): $manifest"
        fi
        NAMES+=("$name")
        UNIT_LISTS+=("$rest")
    done <"$manifest"

    [ -n "$TARGET_BASE" ] \
        || die "manifest $manifest carries no TARGET_BASE=<major> directive (expected exactly one; the base-version gate refuses to run without it)"

    local remove_name j
    for remove_name in "${REMOVE_NAMES[@]}"; do
        for j in "${!NAMES[@]}"; do
            if [ "${NAMES[$j]}" = "$remove_name" ]; then
                die "fetch: REMOVE='$remove_name' is also a manifest entry: $manifest"
            fi
        done
    done

    local i name_entry
    for i in "${!NAMES[@]}"; do
        name_entry=${NAMES[$i]}
        [ -f "$STAGING_DIR/$name_entry.raw" ] \
            || die "manifest entry '$name_entry' has no staging raw: $STAGING_DIR/$name_entry.raw"
    done

    local -a shims=()
    local shopt_was_set=0
    if shopt -q nullglob; then
        shopt_was_set=1
    else
        shopt -s nullglob
    fi
    shims=("$STAGING_DIR"/*-activation.service)
    if [ "$shopt_was_set" -eq 0 ]; then
        shopt -u nullglob
    fi

    local shim base shim_name j found
    for shim in "${shims[@]}"; do
        [ -f "$shim" ] || continue
        base=${shim##*/}
        shim_name=${base%-activation.service}
        found=0
        for j in "${!NAMES[@]}"; do
            if [ "${NAMES[$j]}" = "$shim_name" ]; then
                found=1
                break
            fi
        done
        if [ "$found" -eq 0 ]; then
            die "staging shim '$base' has no manifest entry (name '$shim_name')"
        fi
    done
}

verify_staging_integrity() {
    local sums=$STAGING_DIR/SHA256SUMS
    [ -f "$sums" ] \
        || die "staged-raw integrity: missing SHA256SUMS: $sums (the staged raws cannot be verified; refusing to deploy)"

    declare -A sums_by_name=()
    local line sum raw lineno=0
    local sums_line_re='^([0-9a-fA-F]{64}) [ *]([^[:space:]]+)$'
    while IFS= read -r line || [ -n "$line" ]; do
        lineno=$((lineno + 1))
        line=${line%$'\r'}
        [ -n "$line" ] || continue
        if [[ ! $line =~ $sums_line_re ]]; then
            die "staged-raw integrity: malformed SHA256SUMS line $lineno (expected '<64-hex><two spaces><name>.raw'): $sums"
        fi
        sum=${BASH_REMATCH[1],,}
        raw=${BASH_REMATCH[2]}
        if [ -n "${sums_by_name[$raw]+set}" ]; then
            die "staged-raw integrity: duplicate SHA256SUMS line for '$raw' at line $lineno: $sums"
        fi
        sums_by_name[$raw]=$sum
    done <"$sums"

    local i name expected actual
    for i in "${!NAMES[@]}"; do
        name=${NAMES[$i]}.raw
        [ -n "${sums_by_name[$name]+set}" ] \
            || die "staged-raw integrity: SHA256SUMS has no line for manifest entry '${NAMES[$i]}' ($name): $sums"
        expected=${sums_by_name[$name]}
        actual=$(sha256sum -- "$STAGING_DIR/$name") \
            || die "staged-raw integrity: cannot hash $STAGING_DIR/$name"
        actual=${actual%% *}
        [ "$actual" = "$expected" ] \
            || die "staged-raw integrity: $name does not match its SHA256SUMS line (recorded $expected, actual $actual): $sums"
    done

    log "staged-raw integrity: SHA256SUMS verifies ${#NAMES[@]} staged raw(s)"
}

check_base_gate() {
    local os_release=$HOST_ROOT/etc/os-release version host_line
    [ -r "$os_release" ] \
        || die "base-version gate: cannot read $os_release; refusing to deploy the TARGET_BASE=$TARGET_BASE extensions onto an unknown Garden Linux line"
    version=$(sed -n 's/^IMAGE_VERSION=//p' -- "$os_release" | head -n 1)
    if [ -z "$version" ]; then
        version=$(sed -n 's/^GARDENLINUX_VERSION=//p' -- "$os_release" | head -n 1)
    fi
    # os-release values may be quoted.
    version=${version#\"}
    version=${version%\"}
    version=${version#\'}
    version=${version%\'}
    [ -n "$version" ] \
        || die "base-version gate: $os_release carries neither IMAGE_VERSION nor GARDENLINUX_VERSION; cannot compare the Garden Linux line with TARGET_BASE=$TARGET_BASE"
    host_line=${version%%.*}
    if [ "$host_line" != "$TARGET_BASE" ]; then
        die "base-version gate: host Garden Linux line $host_line (version '$version' in $os_release) does not match TARGET_BASE=$TARGET_BASE; the extensions were built for another base line"
    fi
    log "base-version gate: host Garden Linux line $host_line matches TARGET_BASE=$TARGET_BASE"
}

run_pass() {
    local -a raw_deployed=()
    local -a shim_deployed=()
    local -a has_shim=()
    local -a new_entry=()
    local -a units=()
    local i name src dest shim_src shim_dest any_change=0 pending=0
    local restart_failed=0
    local marker=$HOST_ROOT/var/lib/extensions/$PENDING_MARKER

    if [ -e "$marker" ]; then
        pending=1
    fi

    # Tombstones run before the entries and never activate anything: a removed
    # extension must not have its units restarted.
    local remove_name remove_raw remove_shim remove_wants removed_any
    local removals_present=0
    for remove_name in "${REMOVE_NAMES[@]}"; do
        if [ -e "$HOST_ROOT/var/lib/extensions/$remove_name.raw" ] \
            || [ -e "$HOST_ROOT/etc/systemd/system/$remove_name-activation.service" ] \
            || [ -e "$HOST_ROOT/etc/systemd/system/multi-user.target.wants/$remove_name-activation.service" ] \
            || [ -L "$HOST_ROOT/etc/systemd/system/multi-user.target.wants/$remove_name-activation.service" ]; then
            removals_present=1
            break
        fi
    done
    if [ "$removals_present" -eq 1 ]; then
        # Bracket the deletions too: a kill between a delete and the activation
        # flow must replay on the next pass.
        mkdir -p -- "$HOST_ROOT/var/lib/extensions"
        : >"$marker"
    fi
    for remove_name in "${REMOVE_NAMES[@]}"; do
        remove_raw=$HOST_ROOT/var/lib/extensions/$remove_name.raw
        remove_shim=$HOST_ROOT/etc/systemd/system/$remove_name-activation.service
        remove_wants=$HOST_ROOT/etc/systemd/system/multi-user.target.wants/$remove_name-activation.service
        removed_any=0
        if [ -e "$remove_raw" ]; then
            rm -f -- "$remove_raw"
            removed_any=1
        fi
        if [ -e "$remove_shim" ]; then
            rm -f -- "$remove_shim"
            removed_any=1
        fi
        if [ -e "$remove_wants" ] || [ -L "$remove_wants" ]; then
            rm -f -- "$remove_wants"
            removed_any=1
        fi
        if [ "$removed_any" -eq 1 ]; then
            any_change=1
            log "removed $remove_name"
        fi
    done

    for i in "${!NAMES[@]}"; do
        raw_deployed[i]=0
        shim_deployed[i]=0
        has_shim[i]=0
        new_entry[i]=0
    done

    for i in "${!NAMES[@]}"; do
        name=${NAMES[$i]}

        src=$STAGING_DIR/$name.raw
        dest=$HOST_ROOT/var/lib/extensions/$name.raw
        if [ ! -f "$dest" ]; then
            new_entry[i]=1
        fi
        if [ "${new_entry[i]}" -eq 1 ] || ! files_equal "$src" "$dest"; then
            mkdir -p -- "$HOST_ROOT/var/lib/extensions"
            write_atomic "$src" "$dest"
            raw_deployed[i]=1
            any_change=1
            log "deployed $name.raw"
        fi

        shim_src=$STAGING_DIR/$name-activation.service
        if [ -f "$shim_src" ]; then
            has_shim[i]=1
            shim_dest=$HOST_ROOT/etc/systemd/system/$name-activation.service
            if [ ! -f "$shim_dest" ] || ! files_equal "$shim_src" "$shim_dest"; then
                mkdir -p -- "$HOST_ROOT/etc/systemd/system"
                write_atomic "$shim_src" "$shim_dest"
                shim_deployed[i]=1
                any_change=1
                log "deployed $name-activation.service"
            fi
        fi
    done

    if [ "$any_change" -eq 0 ] && [ "$pending" -eq 0 ]; then
        log "no changes; nothing to do"
        return 0
    fi

    if [ "$pending" -eq 1 ]; then
        log "pending activation found; replaying the activation flow"
        for i in "${!NAMES[@]}"; do
            raw_deployed[i]=1
            if [ "${has_shim[i]}" -eq 1 ]; then
                shim_deployed[i]=1
            fi
        done
    fi

    mkdir -p -- "$HOST_ROOT/var/lib/extensions"
    : >"$marker"

    run_host systemctl daemon-reload
    run_host systemd-sysext refresh
    run_host systemctl reload dbus.service

    for i in "${!NAMES[@]}"; do
        [ "${raw_deployed[i]}" -eq 1 ] || continue
        units=()
        read -r -a units <<<"${UNIT_LISTS[$i]}"
        if [ "${#units[@]}" -gt 0 ]; then
            if ! run_host systemctl restart "${units[@]}"; then
                restart_failed=1
                warn "extension ${NAMES[$i]}: restart failed for ${units[*]} (continuing)"
            fi
        fi
    done

    # Enable every activation shim; restart the changed/new ones.
    for i in "${!NAMES[@]}"; do
        [ "${has_shim[i]}" -eq 1 ] || continue
        name=${NAMES[$i]}
        run_host systemctl enable "$name-activation.service"
        if [ "${shim_deployed[i]}" -eq 1 ] \
            || [ "${raw_deployed[i]}" -eq 1 ] \
            || [ "${new_entry[i]}" -eq 1 ]; then
            run_host systemctl restart "$name-activation.service"
        fi
    done

    rm -f -- "$marker"

    if [ "$restart_failed" -ne 0 ]; then
        warn "activation flow completed, but at least one extension unit restart failed"
        return 1
    fi

    return 0
}

main() {
    local arg
    for arg in "$@"; do
        case $arg in
            -h | --help)
                usage
                exit 0
                ;;
            *)
                die "unknown argument: $arg (try --help)"
                ;;
        esac
    done

    while :; do
        validate_env
        fetch_staging
        load_manifest
        verify_staging_integrity
        check_base_gate
        run_pass
        if [ "$INTERVAL" -eq 0 ]; then
            break
        fi
        sleep "$INTERVAL" &
        wait $! || true
    done
}

main "$@"
