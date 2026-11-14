#!/usr/bin/env bash
# Assemble the SCI delivery image from the built raws, shims, and manifest.
# The image is a test artifact that carries only $IMAGE_TAG.
# Usage: bash delivery/build-image.sh [--stage-only | --from-staging <dir>] [--help]
# Contract: delivery/README.md.

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd -P)

RAW_DIR=${RAW_DIR:-$REPO_ROOT/.build/mkosi/output}
STAGING_OUT=${STAGING_OUT:-$REPO_ROOT/.build/delivery/staging}
CONTAINERFILE=${CONTAINERFILE:-$SCRIPT_DIR/Containerfile}
DELIVERY_SCRIPT=${DELIVERY_SCRIPT:-$SCRIPT_DIR/sci-delivery.py}
IMAGE_TAG=${IMAGE_TAG:-sci-delivery:latest}
PYTHON=${PYTHON:-python3}

die() { printf 'build-image: error: %s\n' "$*" >&2; exit 1; }
log() { printf 'build-image: %s\n' "$*"; }

usage() {
    cat <<'EOF'
usage: build-image.sh [--stage-only | --from-staging <dir>] [--help]

--stage-only stops after sci-delivery.py --stage has written and verified the
staging directory: no docker build and no image tags.

--from-staging <dir> skips assembly and builds the image from an already
staged directory: its shipped SHA256SUMS must verify the staged raws.

Environment overrides: RAW_DIR, STAGING_OUT, CONTAINERFILE, DELIVERY_SCRIPT,
IMAGE_TAG, PYTHON.
EOF
}

build_image() { # <context-dir>
    log "docker build -t $IMAGE_TAG -f $CONTAINERFILE $1"
    docker build -t "$IMAGE_TAG" -f "$CONTAINERFILE" "$1"
}

build_from_staging() { # <dir>
    local dir=$1
    case $dir in
        '' | /) die "refusing to use '$dir' as the staging directory" ;;
    esac
    [ -d "$dir" ] || die "--from-staging: no such directory: $dir"
    for file in SHA256SUMS sci-delivery.py extensions.manifest; do
        [ -f "$dir/$file" ] || die "--from-staging: $dir carries no $file"
    done
    # The shipped checksums are verified, never regenerated: this is the
    # artifact contract, not a fresh assembly.
    ( cd -- "$dir" && sha256sum -c -- SHA256SUMS >/dev/null ) \
        || die "--from-staging: SHA256SUMS does not verify the staged raws: $dir"
    log "verified the staged SHA256SUMS in $dir"
    build_image "$dir"
}

main() {
    local stage_only=0 from_staging=""
    while [ "$#" -gt 0 ]; do
        case $1 in
            -h | --help) usage; exit 0 ;;
            --stage-only) stage_only=1 ;;
            --from-staging)
                [ $# -ge 2 ] || die "--from-staging requires a directory argument (try --help)"
                from_staging=$2
                shift
                ;;
            *) die "unknown argument: $1 (try --help)" ;;
        esac
        shift
    done
    [ -f "$CONTAINERFILE" ] || die "missing Containerfile: $CONTAINERFILE"
    [ -f "$DELIVERY_SCRIPT" ] || die "missing delivery step script: $DELIVERY_SCRIPT"

    if [ -n "$from_staging" ]; then
        [ "$stage_only" -eq 0 ] || die "--from-staging and --stage-only are mutually exclusive"
        build_from_staging "$from_staging"
        return 0
    fi

    "$PYTHON" "$DELIVERY_SCRIPT" --stage "$RAW_DIR" "$STAGING_OUT"
    if [ "$stage_only" -eq 0 ]; then
        build_image "$STAGING_OUT"
    fi
}

main "$@"
