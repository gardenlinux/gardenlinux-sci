#!/usr/bin/env bash
# Boot-test the built SCI sysexts in the signed, tamper, and policy scenarios.
# Usage: bash mkosi/boot-test.sh [--scenario signed|tamper|policy|all] [--manifest <path>] [--runtime <archive>]
# Contract: docs/sysext-virt.md; results parsed by extensions/tests/runtime_adapter.py.

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd -P)
cd -- "$REPO_ROOT"

MKOSI_DIR=$REPO_ROOT/.build/mkosi
VENV_MKOSI=$MKOSI_DIR/venv/bin/mkosi
BASE_IMAGE_TOOL=$SCRIPT_DIR/base-image.sh
BASE_TREE=$MKOSI_DIR/base-image/current
KERNEL=$MKOSI_DIR/base-image/current-kernel
TOOLS_TREE=$MKOSI_DIR/tools/mkosi.tools
SANDBOX_TREE=$MKOSI_DIR/sandbox
CERT=$SCRIPT_DIR/verity.crt
BOOT_RUNNER=$REPO_ROOT/extensions/tests/boot_runner.py
RUNTIME=$REPO_ROOT/.build/sysext-runtime/runtime.tar.gz
DIST=$REPO_ROOT/.build/sysext-runtime/dist
SCRATCH_DIR=$REPO_ROOT/.build/test-scratch-vmboot
MANIFEST=${MANIFEST:-$REPO_ROOT/delivery/extensions.manifest}
SCENARIO=all

die() {
    printf 'boot-test: error: %s\n' "$*" >&2
    printf 'VM-BOOT-TEST-RUNNER: FAIL: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
usage: boot-test.sh [--scenario signed|tamper|policy|all] [--manifest <path>]
                    [--runtime <archive>]

Boot signed extensions through the real delivery step, or prove that a
tampered signed extension / an unsigned extension under ImagePolicy=root=signed
is refused. Default scenario: all. Default runtime:
.build/sysext-runtime/runtime.tar.gz. Final stdout is one JSON object.

Environment overrides: MANIFEST (default: delivery/extensions.manifest)
EOF
}

while [ "$#" -gt 0 ]; do
    case $1 in
        --scenario)   [ "$#" -ge 2 ] || die "--scenario needs a value"; SCENARIO=$2; shift 2 ;;
        --scenario=*) SCENARIO=${1#*=}; shift ;;
        --runtime)    [ "$#" -ge 2 ] || die "--runtime needs a path"; RUNTIME=$2; shift 2 ;;
        --runtime=*)  RUNTIME=${1#*=}; shift ;;
        --manifest)   [ "$#" -ge 2 ] || die "--manifest needs a path"; MANIFEST=$2; shift 2 ;;
        --manifest=*) MANIFEST=${1#*=}; shift ;;
        -h|--help)    usage; exit 0 ;;
        *) die "unknown argument '$1' (usage: boot-test.sh [--scenario signed|tamper|policy|all] [--manifest <path>] [--runtime <archive>])" ;;
    esac
done

case $SCENARIO in
    signed|tamper|policy|all) ;;
    *) die "unknown scenario '$SCENARIO' (choose signed, tamper, policy, or all)" ;;
esac
[ -f "$RUNTIME" ] || die "missing runtime archive: $RUNTIME"
[ -f "$MANIFEST" ] || die "missing manifest: $MANIFEST"
[ -f "$BOOT_RUNNER" ] || die "missing boot runner helper: $BOOT_RUNNER"

if ! PREFLIGHT=$(python3 "$BOOT_RUNNER" --preflight --manifest "$MANIFEST" 2>&1); then
    die "manifest preflight failed: ${PREFLIGHT#boot-runner: error: }"
fi

[ -x "$VENV_MKOSI" ] \
    || die "missing mkosi venv $MKOSI_DIR/venv (create it first: uv venv '$MKOSI_DIR/venv' && uv pip install --python '$MKOSI_DIR/venv/bin/python' 'mkosi @ git+https://github.com/systemd/mkosi.git@v27.1')"
[ -c /dev/kvm ] || die "/dev/kvm is missing: the boot image pins KVM=yes (no TCG fallback)"
[ -r /dev/kvm ] && [ -w /dev/kvm ] || die "/dev/kvm is not readable and writable by $(id -un)"
[ -f "$BASE_IMAGE_TOOL" ] || die "missing $BASE_IMAGE_TOOL"

BASE_IMAGE_OFFLINE=1 bash "$BASE_IMAGE_TOOL" ensure >/dev/null \
    || die "cannot prepare the published base tree offline (bash mkosi/base-image.sh ensure failed)"
[ -d "$BASE_TREE" ] || die "missing base tree $BASE_TREE"
[ -f "$KERNEL" ] || die "missing base-tree kernel $KERNEL"
[ -d "$TOOLS_TREE" ] || die "missing tools tree $TOOLS_TREE (run 'bash mkosi/build.sh' once)"
[ -d "$SANDBOX_TREE" ] || die "missing sandbox tree $SANDBOX_TREE (run 'bash mkosi/build.sh' first)"
[ -f "$CERT" ] || die "missing verity certificate $CERT"

exec python3 "$BOOT_RUNNER" \
    --scenario "$SCENARIO" --manifest "$MANIFEST" --runtime "$RUNTIME" \
    --repo-root "$REPO_ROOT" --scratch "$SCRATCH_DIR" --dist "$DIST" \
    --base "$BASE_TREE" --kernel "$KERNEL" --tools "$TOOLS_TREE" \
    --sandbox "$SANDBOX_TREE" --cert "$CERT" --cache "$SCRATCH_DIR/cache" \
    --mkosi "$VENV_MKOSI"
