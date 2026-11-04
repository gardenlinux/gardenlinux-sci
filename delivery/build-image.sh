#!/usr/bin/env bash
# Assemble the SCI delivery image from the built raws, shims, and manifest.
# The image is a test artifact that carries only $IMAGE_TAG.
# Usage: bash delivery/build-image.sh [--stage-only | --from-staging <dir>] [--help]
# Contract: delivery/README.md.

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd -P)

RAW_DIR=${RAW_DIR:-$REPO_ROOT/.build/mkosi/output}
SHIM_GLOB=${SHIM_GLOB:-$REPO_ROOT/mkosi/mkosi.images/*/mkosi.extra/usr/share/sci/activation/*.service}
STAGING_OUT=${STAGING_OUT:-$REPO_ROOT/.build/delivery/staging}
CONTAINERFILE=${CONTAINERFILE:-$SCRIPT_DIR/Containerfile}
DELIVERY_SCRIPT=${DELIVERY_SCRIPT:-$SCRIPT_DIR/sci-delivery.sh}
MANIFEST=${MANIFEST:-$SCRIPT_DIR/extensions.manifest}
IMAGE_TAG=${IMAGE_TAG:-sci-delivery:latest}

die() {
    printf 'build-image: error: %s\n' "$*" >&2
    exit 1
}

log() {
    printf 'build-image: %s\n' "$*"
}

usage() {
    cat <<'EOF'
usage: build-image.sh [--stage-only | --from-staging <dir>] [--help]

Assembles <repo>/.build/delivery/staging from the built extension images,
the activation shim templates and delivery/extensions.manifest, validates it,
stages SHA256SUMS over the staged raws, and runs `docker build` with the
staging directory as the context. The image carries only $IMAGE_TAG. It is a
test artifact, not a published product.

--stage-only stops after the validated staging directory: no docker build and
no image tags, for callers that take the staged files as plain files.

--from-staging <dir> skips the assembly and builds the image from an
already-staged directory (for example an unpacked raws artifact): the staged
SHA256SUMS must verify the staged raws as shipped, and the image carries only
$IMAGE_TAG.

Environment overrides: RAW_DIR, SHIM_GLOB, STAGING_OUT, CONTAINERFILE,
DELIVERY_SCRIPT, MANIFEST, IMAGE_TAG.
EOF
}

declare -a NAMES=()

load_manifest() {
    [ -f "$MANIFEST" ] || die "missing manifest: $MANIFEST"

    NAMES=()
    local line name rest lineno=0
    while IFS= read -r line || [ -n "$line" ]; do
        lineno=$((lineno + 1))
        line=${line%$'\r'}
        if [ -z "$line" ]; then
            continue
        fi
        if [[ $line =~ ^[[:space:]]*$ ]]; then
            die "malformed manifest line $lineno (whitespace-only): $MANIFEST"
        fi
        if [[ $line =~ ^[[:space:]]*# ]]; then
            continue
        fi
        if [[ $line =~ ^[A-Z][A-Z0-9_]*=[^[:space:]]*$ ]]; then
            continue
        fi
        read -r name rest <<<"$line"
        if [[ ! $name =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
            die "malformed manifest line $lineno (invalid name '$name'): $MANIFEST"
        fi
        NAMES+=("$name")
    done <"$MANIFEST"

    [ "${#NAMES[@]}" -gt 0 ] || die "manifest has no entries: $MANIFEST"
}

shim_templates() {
    local shim
    while IFS= read -r shim; do
        [ -n "$shim" ] || continue
        printf '%s\n' "$shim"
    done < <(compgen -G "$SHIM_GLOB" || true)
}

validate_inputs() {
    [ -d "$RAW_DIR" ] \
        || die "raw output directory not found: $RAW_DIR (build the extensions first)"
    [ -f "$CONTAINERFILE" ] || die "missing Containerfile: $CONTAINERFILE"
    [ -f "$DELIVERY_SCRIPT" ] || die "missing delivery step script: $DELIVERY_SCRIPT"

    local i name
    for i in "${!NAMES[@]}"; do
        name=${NAMES[$i]}
        [ -f "$RAW_DIR/$name.raw" ] \
            || die "missing raw for manifest entry '$name': $RAW_DIR/$name.raw"
    done

    local shim base shim_name j found
    while IFS= read -r shim; do
        [ -n "$shim" ] || continue
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
    done < <(shim_templates)
}

assemble_staging() {
    case $STAGING_OUT in
        '' | /) die "refusing to use '$STAGING_OUT' as the staging directory" ;;
    esac

    rm -rf -- "$STAGING_OUT"
    mkdir -p -- "$STAGING_OUT"

    local i name shim
    for i in "${!NAMES[@]}"; do
        name=${NAMES[$i]}
        cp -- "$RAW_DIR/$name.raw" "$STAGING_OUT/$name.raw"
    done
    while IFS= read -r shim; do
        [ -n "$shim" ] || continue
        cp -- "$shim" "$STAGING_OUT/${shim##*/}"
    done < <(shim_templates)

    cp -- "$MANIFEST" "$STAGING_OUT/extensions.manifest"
    cp -- "$DELIVERY_SCRIPT" "$STAGING_OUT/sci-delivery.sh"
    chmod 0755 "$STAGING_OUT/sci-delivery.sh"

    log "staged ${#NAMES[@]} extensions in $STAGING_OUT"
}

stage_sha256sums() {
    local -a raws=()
    local i name raw
    for i in "${!NAMES[@]}"; do
        raws+=("${NAMES[$i]}.raw")
    done

    local mkosi_sums=$RAW_DIR/SHA256SUMS
    local -A mkosi_hash=()
    local sums_line_re='^([0-9a-fA-F]{64}) [ *]([^[:space:]]+)$'
    local line hash lineno=0 usable=0

    if [ -f "$mkosi_sums" ]; then
        usable=1
        while IFS= read -r line || [ -n "$line" ]; do
            lineno=$((lineno + 1))
            line=${line%$'\r'}
            [ -n "$line" ] || continue
            if [[ ! $line =~ $sums_line_re ]]; then
                log "$mkosi_sums line $lineno is not sha256sum output; not using the mkosi checksums"
                usable=0
                break
            fi
            hash=${BASH_REMATCH[1],,}
            name=${BASH_REMATCH[2]}
            mkosi_hash[$name]=$hash
        done <"$mkosi_sums"
    fi

    local actual
    if [ "$usable" -eq 1 ]; then
        for raw in "${raws[@]}"; do
            if [ -z "${mkosi_hash[$raw]+set}" ]; then
                usable=0
                break
            fi
            actual=$(sha256sum -- "$STAGING_OUT/$raw")
            actual=${actual%% *}
            if [ "$actual" != "${mkosi_hash[$raw]}" ]; then
                usable=0
                break
            fi
        done
    fi

    local source
    if [ "$usable" -eq 1 ]; then
        : >"$STAGING_OUT/SHA256SUMS"
        for raw in "${raws[@]}"; do
            printf '%s  %s\n' "${mkosi_hash[$raw]}" "$raw" >>"$STAGING_OUT/SHA256SUMS"
        done
        source="mkosi checksum output $mkosi_sums"
    else
        (
            cd -- "$STAGING_OUT"
            sha256sum -- "${raws[@]}" >SHA256SUMS
        )
        source="generated with sha256sum over the staged raws"
    fi

    (
        cd -- "$STAGING_OUT"
        sha256sum -c -- SHA256SUMS >/dev/null
    ) || die "staged SHA256SUMS does not verify the staged raws: $STAGING_OUT/SHA256SUMS"

    log "staged SHA256SUMS for ${#raws[@]} raws ($source)"
}

build_image() {
    log "docker build -t $IMAGE_TAG -f $CONTAINERFILE $STAGING_OUT"
    docker build -t "$IMAGE_TAG" -f "$CONTAINERFILE" "$STAGING_OUT"
}

build_from_staging() {
    local dir=$1
    case $dir in
        '' | /) die "refusing to use '$dir' as the staging directory" ;;
    esac
    [ -d "$dir" ] || die "--from-staging: no such directory: $dir"
    [ -f "$dir/SHA256SUMS" ] \
        || die "--from-staging: $dir carries no SHA256SUMS: a staging without checksums cannot be verified"
    [ -f "$dir/sci-delivery.sh" ] \
        || die "--from-staging: $dir carries no sci-delivery.sh: the build context must contain the delivery step script"
    [ -f "$dir/extensions.manifest" ] \
        || die "--from-staging: $dir carries no extensions.manifest"
    # The shipped checksums are verified, never regenerated: this is the
    # artifact contract, not a fresh assembly.
    (
        cd -- "$dir"
        sha256sum -c -- SHA256SUMS >/dev/null
    ) || die "--from-staging: SHA256SUMS does not verify the staged raws: $dir"
    log "verified the staged SHA256SUMS in $dir"
    log "docker build -t $IMAGE_TAG -f $CONTAINERFILE $dir"
    docker build -t "$IMAGE_TAG" -f "$CONTAINERFILE" "$dir"
}

main() {
    local stage_only=0 from_staging=""
    while [ "$#" -gt 0 ]; do
        case $1 in
            -h | --help)
                usage
                exit 0
                ;;
            --stage-only)
                stage_only=1
                ;;
            --from-staging)
                [ $# -ge 2 ] || die "--from-staging requires a directory argument (try --help)"
                from_staging=$2
                shift
                ;;
            *)
                die "unknown argument: $1 (try --help)"
                ;;
        esac
        shift
    done

    if [ -n "$from_staging" ]; then
        [ "$stage_only" -eq 0 ] \
            || die "--from-staging and --stage-only are mutually exclusive"
        build_from_staging "$from_staging"
        return 0
    fi

    load_manifest
    validate_inputs
    assemble_staging
    stage_sha256sums
    if [ "$stage_only" -eq 0 ]; then
        build_image
    fi
}

main "$@"
