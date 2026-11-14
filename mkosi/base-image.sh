#!/usr/bin/env bash
# Prepare and verify the published Garden Linux base tree used as the diff baseline.
# Usage: bash mkosi/base-image.sh [ensure]
# Contract: docs/sysext-virt.md.

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
    for d in "${TMP_DIRS[@]}"; do rm -rf -- "$d"; done
}
trap cleanup EXIT
die() { printf 'base-image: error: %s\n' "$*" >&2; exit 1; }; log() { printf 'base-image: %s\n' "$*" >&2; }
usage() {
    printf 'usage: base-image.sh [ensure]\n\nEnvironment overrides: BASE_IMAGE_PIN, BASE_IMAGE_DIR, BASE_IMAGE_TARBALL, BASE_IMAGE_URL, BASE_IMAGE_OFFLINE=1\n'
}
# Last KEY=VALUE field of a pin file (empty when absent).
pin_field() { sed -n "s/^$2[[:space:]]*=[[:space:]]*//p" -- "$1" | tail -n 1; }

validate_pin() {
    [ -f "$PIN" ] || die "missing pin $PIN (the committed pin of the published base image)"
    local field
    for field in FLAVOR ARCH VERSION COMMIT TARBALL_SHA256 ROOTFS_TAR_SHA256 URL; do
        [ "$(grep -cE "^${field}[[:space:]]*=" -- "$PIN")" -eq 1 ] \
            || die "$PIN: expected exactly one $field= line"
    done
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
    local release
    release=$(sed -n 's/^Release=//p' -- "$MKOSI_CONF")
    [ "$(grep -c '^Release=' -- "$MKOSI_CONF")" -eq 1 ] \
        || die "$MKOSI_CONF must carry exactly one Release= line"
    [ "$PIN_VERSION" = "$release" ] \
        || die "$PIN: VERSION=$PIN_VERSION does not match Release=$release in $MKOSI_CONF (the pin must name the suite the extensions are built for)"
    local derived="https://github.com/gardenlinux/gardenlinux/releases/download/$PIN_VERSION/$PIN_FLAVOR-$PIN_ARCH-$PIN_VERSION-${PIN_COMMIT:0:8}.tar.xz"
    [ "$PIN_URL" = "$derived" ] \
        || die "$PIN: URL='$PIN_URL' does not match the URL derived from the pin's fields ('$derived')"
}

cache_key() { printf '%s-%s-%s-%s' "$PIN_FLAVOR" "$PIN_ARCH" "$PIN_VERSION" "${PIN_COMMIT:0:8}"; }
inner_member() { printf '%s.tar' "$(cache_key)"; }
verify_sha256() { # <file> <expected> <label>
    [ -f "$1" ] || die "missing $3 ($1)"
    local actual
    actual=$(sha256sum -- "$1" | awk '{print $1}')
    [ "$actual" = "$2" ] \
        || die "sha256 mismatch for $3: expected $2, got $actual ($1)"
}

fetch_tarball() { # <dest>
    local dest=$1 url=${BASE_IMAGE_URL:-$PIN_URL}
    [ "$OFFLINE" -eq 0 ] \
        || die "offline: BASE_IMAGE_OFFLINE=1 forbids fetching the published base image, and no ready cache exists ($url)"
    log "fetching $url"
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 3 --retry-delay 5 -o "$dest" "$url" || die "download failed: $url"
    elif command -v wget >/dev/null 2>&1; then
        wget -O "$dest" "$url" || die "download failed: $url"
    else
        die "neither curl nor wget is available to fetch $url (supply BASE_IMAGE_TARBALL)"
    fi
}

obtain_tarball() { # <workdir>
    local tarball
    if [ -n "${BASE_IMAGE_TARBALL:-}" ]; then
        tarball=$BASE_IMAGE_TARBALL
        [ -r "$tarball" ] || die "BASE_IMAGE_TARBALL=$tarball is not readable"
        log "using supplied tarball $tarball"
    else
        tarball=$1/outer.tar.xz
        fetch_tarball "$tarball"
    fi
    log "verifying the outer tarball sha256"
    verify_sha256 "$tarball" "$PIN_OUTER_SHA" "outer tarball"
    printf '%s' "$tarball"
}

os_release_value() { # <file> <key>
    sed -n "s/^$2=//p" -- "$1" | head -n 1 | tr -d '"' | tr -d "'"
}

check_tree_invariants() { # <tree>
    local rel=$1/etc/os-release
    [ -f "$rel" ] || die "$1 has no etc/os-release (not an extracted Garden Linux tree)"
    [ "$(os_release_value "$rel" ID)" = gardenlinux ] \
        || die "$rel does not set ID=gardenlinux"
    [ "$(os_release_value "$rel" IMAGE_VERSION)" = "$PIN_VERSION" ] \
        || die "$rel does not set IMAGE_VERSION=$PIN_VERSION"
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
    [ -z "$(find "$dest" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ] \
        || die "destination $dest is not empty (refusing to merge an extraction into it)"
    log "extracting the base tree into $dest"
    tar -xf "$inner" -C "$dest" --numeric-owner \
        || die "cannot extract the base tree from $inner into $dest"
    check_tree_invariants "$dest"
    rm -rf -- "$work"
}

link_to() { # <target> <link>
    local tmp
    if [ -L "$2" ] && [ "$(readlink -- "$2")" = "$1" ]; then return 0; fi
    tmp=$2.tmp.$$
    ln -sfn -- "$1" "$tmp"
    mv -Tf -- "$tmp" "$2"
}

maintain_links() { # <cache> <key> <tree>
    local cache=$1 key=$2 tree=$3 k kname initrd kernel=0 initrd_ok=0
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
    if [ -f "$marker" ] && [ -d "$tree" ] && [ "$(cat -- "$marker")" = "$PIN_INNER_SHA" ]; then
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

case ${1:-ensure} in
    ensure) cmd_ensure ;;
    -h|--help|help) usage ;;
    *) die "unknown subcommand '${1}' (see --help)" ;;
esac
