#!/usr/bin/env bash
# Prepare and verify the published Garden Linux base tree used as the diff baseline.
# Usage: bash mkosi/base-image.sh [ensure|extract <dest-dir>|digest <tree-dir>]
# Contract: docs/sysext-virt.md; behaviour pinned by extensions/tests/test_host_contracts.py.

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd -P)

PIN=${BASE_IMAGE_PIN:-$SCRIPT_DIR/base-image.pin}
CACHE_ROOT=${BASE_IMAGE_DIR:-$REPO_ROOT/.build/mkosi/base-image}
MKOSI_CONF=$SCRIPT_DIR/mkosi.conf

OFFLINE=0
case ${BASE_IMAGE_OFFLINE:-0} in
    ''|0|no|false) ;;
    *) OFFLINE=1 ;;
esac

TMP_DIRS=()
cleanup() {
    local d
    for d in "${TMP_DIRS[@]}"; do
        rm -rf -- "$d"
    done
}
trap cleanup EXIT

die() {
    printf 'base-image: error: %s\n' "$*" >&2
    exit 1
}

log() {
    printf 'base-image: %s\n' "$*" >&2
}

usage() {
    cat <<'EOF'
usage: base-image.sh [ensure]
       base-image.sh extract <dest-dir>
       base-image.sh digest <tree-dir>

Environment overrides: BASE_IMAGE_PIN, BASE_IMAGE_DIR, BASE_IMAGE_TARBALL,
BASE_IMAGE_URL, BASE_IMAGE_OFFLINE=1
EOF
}

# Last KEY=VALUE field of a pin file (empty when absent).
pin_field() { # <pin> <field>
    sed -n "s/^$2[[:space:]]*=[[:space:]]*//p" -- "$1" | tail -n 1
}

validate_pin() {
    [ -f "$PIN" ] || die "missing pin $PIN (the committed pin of the published base image)"
    local field count line
    for field in FLAVOR ARCH VERSION COMMIT TARBALL_SHA256 ROOTFS_TAR_SHA256 URL; do
        count=$(grep -cE "^${field}[[:space:]]*=" -- "$PIN") || count=0
        [ "$count" -eq 1 ] \
            || die "$PIN: expected exactly one $field= line, found $count"
    done
    while IFS= read -r line; do
        if [[ $line =~ ^[[:space:]]*(#|$) ]]; then
            continue
        fi
        if [[ ! $line =~ ^(FLAVOR|ARCH|VERSION|COMMIT|TARBALL_SHA256|ROOTFS_TAR_SHA256|URL)[[:space:]]*= ]]; then
            die "$PIN: unrecognized line '$line'"
        fi
    done <"$PIN"

    PIN_FLAVOR=$(pin_field "$PIN" FLAVOR)
    PIN_ARCH=$(pin_field "$PIN" ARCH)
    PIN_VERSION=$(pin_field "$PIN" VERSION)
    PIN_COMMIT=$(pin_field "$PIN" COMMIT)
    PIN_OUTER_SHA=$(pin_field "$PIN" TARBALL_SHA256)
    PIN_INNER_SHA=$(pin_field "$PIN" ROOTFS_TAR_SHA256)
    PIN_URL=$(pin_field "$PIN" URL)

    [[ $PIN_FLAVOR =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] \
        || die "$PIN: FLAVOR='$PIN_FLAVOR' is malformed"
    [[ $PIN_ARCH =~ ^[A-Za-z0-9][A-Za-z0-9-]*$ ]] \
        || die "$PIN: ARCH='$PIN_ARCH' is malformed"
    [[ $PIN_VERSION =~ ^[0-9]+(\.[0-9]+)+$ ]] \
        || die "$PIN: VERSION='$PIN_VERSION' is malformed (expected e.g. 2150.9.0)"
    [[ $PIN_COMMIT =~ ^[0-9a-f]{40}$ ]] \
        || die "$PIN: COMMIT='$PIN_COMMIT' is not a full 40-hex-digit commit"
    [[ $PIN_OUTER_SHA =~ ^[0-9a-f]{64}$ ]] \
        || die "$PIN: TARBALL_SHA256='$PIN_OUTER_SHA' is not a 64-hex-digit sha256"
    [[ $PIN_INNER_SHA =~ ^[0-9a-f]{64}$ ]] \
        || die "$PIN: ROOTFS_TAR_SHA256='$PIN_INNER_SHA' is not a 64-hex-digit sha256"

    [ -f "$MKOSI_CONF" ] || die "missing $MKOSI_CONF (the Release= source of truth)"
    local release_count release
    release_count=$(grep -c '^Release=' -- "$MKOSI_CONF") || release_count=0
    [ "$release_count" -eq 1 ] \
        || die "$MKOSI_CONF carries $release_count Release= lines; expected exactly one"
    release=$(sed -n 's/^Release=//p' -- "$MKOSI_CONF" | head -n 1)
    [ "$PIN_VERSION" = "$release" ] \
        || die "$PIN: VERSION=$PIN_VERSION does not match Release=$release in $MKOSI_CONF (the pin must name the suite the extensions are built for)"

    local derived
    derived="https://github.com/gardenlinux/gardenlinux/releases/download/$PIN_VERSION/$PIN_FLAVOR-$PIN_ARCH-$PIN_VERSION-${PIN_COMMIT:0:8}.tar.xz"
    [ "$PIN_URL" = "$derived" ] \
        || die "$PIN: URL='$PIN_URL' does not match the URL derived from the pin's fields ('$derived')"
}

cache_key() {
    printf '%s-%s-%s-%s' "$PIN_FLAVOR" "$PIN_ARCH" "$PIN_VERSION" "${PIN_COMMIT:0:8}"
}

inner_member() {
    printf '%s.tar' "$(cache_key)"
}

tree_digest() { # <tree>
    tar -C "$1" -cf - --sort=name --owner=0 --group=0 --numeric-owner --mtime='@0' . \
        | sha256sum | awk '{print $1}'
}

verify_sha256() { # <file> <expected> <label>
    local file=$1 expected=$2 label=$3 actual
    [ -f "$file" ] || die "missing $label ($file)"
    actual=$(sha256sum -- "$file" | awk '{print $1}')
    if [ "$actual" != "$expected" ]; then
        die "sha256 mismatch for $label: expected $expected, got $actual ($file)"
    fi
}

fetch_tarball() { # <dest>
    local dest=$1 url=${BASE_IMAGE_URL:-$PIN_URL}
    if [ "$OFFLINE" -eq 1 ]; then
        die "offline: BASE_IMAGE_OFFLINE=1 forbids fetching the published base image, and no ready cache exists ($url)"
    fi
    log "fetching $url"
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 3 --retry-delay 5 -o "$dest" "$url" \
            || die "download failed: $url"
    elif command -v wget >/dev/null 2>&1; then
        wget -O "$dest" "$url" || die "download failed: $url"
    else
        die "neither curl nor wget is available to fetch $url (supply BASE_IMAGE_TARBALL)"
    fi
}

obtain_tarball() { # <workdir>
    local work=$1 tarball
    if [ -n "${BASE_IMAGE_TARBALL:-}" ]; then
        tarball=$BASE_IMAGE_TARBALL
        [ -r "$tarball" ] || die "BASE_IMAGE_TARBALL=$tarball is not readable"
        log "using supplied tarball $tarball"
    else
        tarball=$work/outer.tar.xz
        fetch_tarball "$tarball"
    fi
    log "verifying the outer tarball sha256"
    verify_sha256 "$tarball" "$PIN_OUTER_SHA" "outer tarball"
    printf '%s' "$tarball"
}

trim() { # <value>
    local v=$1
    v=${v#"${v%%[![:space:]]*}"}
    v=${v%"${v##*[![:space:]]}"}
    printf '%s' "$v"
}

os_release_line() { # <file> <key=value>
    local f=$1 want=$2 key=${2%%=*} value count
    count=$(grep -Ec "^[[:space:]]*$key=" -- "$f") || count=0
    [ "$count" -eq 1 ] \
        || die "$f carries $count $key= lines (expected exactly one $want)"
    value=$(grep -E "^[[:space:]]*$key=" -- "$f" | head -n 1)
    value=$(trim "$value")
    [ "$value" = "$want" ] || die "$f has $value (expected exactly $want)"
}

# Extraction mechanics and negative cases are pinned by test_host_contracts.py.
check_tree_invariants() { # <tree>
    local tree=$1 rel=$1/etc/os-release f kver found=0 mid
    [ -f "$rel" ] \
        || die "$tree has no etc/os-release (not an extracted Garden Linux tree)"
    os_release_line "$rel" "ID=gardenlinux"
    os_release_line "$rel" "ID_LIKE=debian"
    os_release_line "$rel" "IMAGE_VERSION=$PIN_VERSION"
    os_release_line "$rel" "GARDENLINUX_VERSION=$PIN_VERSION"
    os_release_line "$rel" "GARDENLINUX_COMMIT_ID_LONG=$PIN_COMMIT"
    os_release_line "$rel" "VARIANT_ID=$PIN_FLAVOR-$PIN_ARCH"
    if grep -q '^VERSION_ID=' -- "$rel"; then
        die "$rel carries VERSION_ID (the published base os-release has none)"
    fi
    if grep -q '^SYSEXT_LEVEL=' -- "$rel"; then
        die "$rel carries SYSEXT_LEVEL (the published base os-release has none)"
    fi
    mid=$(cat -- "$tree/etc/machine-id" 2>/dev/null) \
        || die "$tree/etc/machine-id missing (expected the literal 'uninitialized')"
    [ "$mid" = "uninitialized" ] \
        || die "$tree/etc/machine-id is '$mid' (expected exactly 'uninitialized')"
    [ ! -e "$tree/var/lib/random-seed" ] \
        || die "$tree/var/lib/random-seed present (the pristine base carries no random seed)"
    [ -z "$(find "$tree/etc/ssh" -name 'ssh_host_*' -print -quit 2>/dev/null)" ] \
        || die "SSH host keys in $tree/etc/ssh (the pristine base carries none)"
    [ -z "$(find "$tree/var/log" -type f -print -quit 2>/dev/null)" ] \
        || die "log files in $tree/var/log (the pristine base has none)"
    if [ -e "$tree/var/lib/extensions" ]; then
        [ -z "$(find "$tree/var/lib/extensions" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ] \
            || die "$tree/var/lib/extensions is not empty (expected absent or empty before staging)"
    fi
    for f in "$tree"/boot/vmlinuz-*; do
        [ -f "$f" ] || continue
        found=1
        kver=${f##*/vmlinuz-}
        [ -f "$tree/boot/initrd.img-$kver" ] \
            || die "no initrd.img-$kver in $tree/boot (the kernel $f has no matching initrd)"
    done
    [ "$found" -eq 1 ] || die "no kernel (boot/vmlinuz-*) in $tree"
}

# <dest> must be empty; an extraction is never merged into existing content.
extract_tree() { # <outer-tarball> <dest>
    local outer=$1 dest=$2 member work inner
    member=$(inner_member)
    work=$(mktemp -d "${TMPDIR:-/tmp}/base-image.XXXXXX")
    TMP_DIRS+=("$work")
    log "extracting the inner rootfs tar $member"
    tar -xf "$outer" -C "$work" --numeric-owner -- "$member" \
        || die "cannot extract $member from $outer (the published tarball layout changed?)"
    inner=$work/$member
    [ -f "$inner" ] || die "inner rootfs tar $member missing after extracting $outer"
    log "verifying the inner rootfs tar sha256"
    verify_sha256 "$inner" "$PIN_INNER_SHA" "inner rootfs tar"
    mkdir -p -- "$dest"
    if [ -n "$(find "$dest" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
        die "destination $dest is not empty (refusing to merge an extraction into it)"
    fi
    log "extracting the base tree into $dest"
    tar -xf "$inner" -C "$dest" --numeric-owner \
        || die "cannot extract the base tree from $inner into $dest"
    check_tree_invariants "$dest"
    rm -rf -- "$work"
}

link_to() { # <target> <link>
    local target=$1 link=$2 tmp
    if [ -L "$link" ] && [ "$(readlink -- "$link")" = "$target" ]; then
        return 0
    fi
    tmp=$link.tmp.$$
    ln -sfn -- "$target" "$tmp"
    mv -Tf -- "$tmp" "$link"
}

maintain_links() { # <cache> <key> <tree>
    local cache=$1 key=$2 tree=$3 k kname initrd
    local kernel=0 initrd_ok=0
    link_to "$key" "$cache/current"
    for k in "$tree"/boot/vmlinuz-*; do
        [ -f "$k" ] || continue
        kernel=1
        kname=${k##*/}
        link_to "$key/boot/$kname" "$cache/current-kernel"
        initrd=$tree/boot/initrd.img-${kname#vmlinuz-}
        if [ -f "$initrd" ]; then
            link_to "$key/boot/${initrd##*/}" "$cache/current-initrd"
            initrd_ok=1
        fi
        break
    done
    # Remove stale links when the tree has no kernel or no matching initrd.
    [ "$kernel" -eq 1 ] || rm -f -- "$cache/current-kernel"
    [ "$initrd_ok" -eq 1 ] || rm -f -- "$cache/current-initrd"
}

cmd_ensure() {
    validate_pin
    local key cache tree marker
    mkdir -p -- "$CACHE_ROOT"
    cache=$(realpath -m -- "$CACHE_ROOT")
    key=$(cache_key)
    tree=$cache/$key
    marker=$cache/$key.ready
    # Marker content is the expected inner sha256; a matching marker is not re-verified.
    if [ -f "$marker" ] && [ -d "$tree" ] \
        && [ "$(cat -- "$marker")" = "$PIN_INNER_SHA" ]; then
        log "using the ready cache at $tree"
        maintain_links "$cache" "$key" "$tree"
        printf '%s\n' "$tree"
        return 0
    fi
    rm -rf -- "$tree"
    rm -f -- "$marker" "$marker.tmp"
    local work outer dest
    work=$(mktemp -d "$cache/.ensure.XXXXXX")
    TMP_DIRS+=("$work")
    outer=$(obtain_tarball "$work")
    dest=$work/tree
    extract_tree "$outer" "$dest"
    mv -- "$dest" "$tree"
    printf '%s\n' "$PIN_INNER_SHA" >"$marker.tmp"
    mv -f -- "$marker.tmp" "$marker"
    rm -rf -- "$work"
    maintain_links "$cache" "$key" "$tree"
    printf '%s\n' "$tree"
}

cmd_extract() {
    [ "$#" -ge 1 ] || die "usage: $0 extract <dest-dir>"
    validate_pin
    local dest=$1 work outer
    work=$(mktemp -d "${TMPDIR:-/tmp}/base-image-extract.XXXXXX")
    TMP_DIRS+=("$work")
    outer=$(obtain_tarball "$work")
    extract_tree "$outer" "$dest"
    tree_digest "$dest"
}

cmd_digest() {
    [ "$#" -ge 1 ] || die "usage: $0 digest <tree-dir>"
    [ -d "$1" ] || die "not a directory: $1"
    tree_digest "$1"
}

cmd=${1:-ensure}
if [ "$#" -gt 0 ]; then
    shift
fi
case $cmd in
    ensure) cmd_ensure "$@" ;;
    extract) cmd_extract "$@" ;;
    digest) cmd_digest "$@" ;;
    -h|--help|help) usage ;;
    *) die "unknown subcommand '$cmd' (see --help)" ;;
esac
