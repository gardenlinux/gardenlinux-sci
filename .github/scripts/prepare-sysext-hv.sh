#!/usr/bin/env bash
# CI adapter for the sysext integration test: --render, --prepare, --check.
# Only test adapter files and .build/ are touched.
# Contract: docs/sysext-virt.md §11.
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd -P)
MKOSI_DIR=$REPO_ROOT/.build/mkosi
MKOSI_VENV=$MKOSI_DIR/venv/bin/mkosi
SANDBOX_DIR=$MKOSI_DIR/sandbox
SANDBOX_SOURCES=$SANDBOX_DIR/etc/apt/sources.list.d/mkosi.sources
TOOLS_DIR=$MKOSI_DIR/tools
TOOLS_TREE=$TOOLS_DIR/mkosi.tools
TOOLS_PIN_FILE=$TOOLS_DIR/pin
TOOLS_PIN=ubuntu/noble
TOOLS_MANIFEST=$TOOLS_DIR/mkosi.tools.manifest
TOOLS_PIN_SOURCE=$REPO_ROOT/mkosi/tools.pin
BASE_TREE=$MKOSI_DIR/base-image/current
BASE_KERNEL=$MKOSI_DIR/base-image/current-kernel
TEMPLATE=$REPO_ROOT/.github/actions/test/integration/setup/hv-sysext.xml
ROOT_GUID=4f68bce3-e8cd-4db1-96e7-fbcaf984b709
HV_DISK_SIZE=${HV_DISK_SIZE:-8G}
HV_ROOT_SIZE=${HV_ROOT_SIZE:-7G}

die() { printf 'prepare-sysext-hv: error: %s\n' "$*" >&2; exit 2; }

usage() {
    cat <<'EOF'
usage: prepare-sysext-hv.sh --render <partition-table.json> <out-dir> <kernel> <disk-prefix>
       prepare-sysext-hv.sh --prepare <artifact-dir> <out-dir>
       prepare-sysext-hv.sh --check       (preflight: tools tree, sandbox, base cache)
Env: HV_DISK_SIZE=4G HV_ROOT_SIZE=3G SCI_HV_NETWORK=default
EOF
}

render() {
    [ "$#" -eq 4 ] || { usage >&2; exit 2; }
    [ -f "$TEMPLATE" ] || die "missing XML template: $TEMPLATE"
    python3 - "$TEMPLATE" "$1" "$2" "$3" "$4" <<'PY'
import json, os, re, sys, xml.sax.saxutils as sax
TEMPLATE, TABLE, OUT, KERNEL, PREFIX = sys.argv[1:6]
GUID = "4f68bce3-e8cd-4db1-96e7-fbcaf984b709"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
MACS = {"HV1": "52:54:00:12:34:56", "HV2": "52:54:00:65:43:21"}
def fail(msg): sys.exit(f"prepare-sysext-hv: error: {msg}")
def esc(value): return sax.escape(str(value), {'"': "&quot;", "'": "&apos;"})
try:
    parts = json.load(open(TABLE, encoding="utf-8"))["partitiontable"]["partitions"]
except (OSError, ValueError, KeyError, TypeError) as exc:
    fail(f"invalid JSON partition table {TABLE}: {exc}")
roots = [p for p in parts if isinstance(p, dict)
         and str(p.get("type", "")).lower() == GUID]
if len(roots) != 1:
    fail(f"expected exactly one root partition (GPT type {GUID}), found {len(roots)}")
root = roots[0]
uuid = str(root.get("uuid", "")).strip()
if not UUID_RE.fullmatch(uuid):
    fail(f"root partition uuid is not a valid UUID: {uuid!r}")
network = os.environ.get("SCI_HV_NETWORK") or "default"
if not network.strip() or "\n" in network:
    fail(f"invalid network name: {network!r}")
template = open(TEMPLATE, encoding="utf-8").read()
os.makedirs(OUT, exist_ok=True)
for hv, mac in MACS.items():
    text = template
    for key, value in {"HV_NAME_GOES_HERE": hv, "MAC_GOES_HERE": mac,
                       "KERNEL_GOES_HERE": esc(KERNEL), "ROOT_UUID_GOES_HERE": esc(uuid),
                       "DISK_PREFIX_GOES_HERE": esc(PREFIX), "NETWORK_GOES_HERE": esc(network)}.items():
        text = text.replace(key, value)
    open(os.path.join(OUT, f"{hv}.xml"), "w", encoding="utf-8").write(text)
print(f"prepare-sysext-hv: rendered HV1, HV2 in {OUT}")
PY
}

preflight() {
    [ "$#" -eq 0 ] || { usage >&2; exit 2; }
    local -a bad=()
    need() { [ -e "$1" ] || bad+=("$1  ($2)"); }
    need_x() { [ -x "$1" ] || bad+=("$1  ($2; must be executable)"); }
    need_x "$REPO_ROOT/bin/garden-version" "gardenlinux submodule; run: git submodule update --init gardenlinux"
    need_x "$MKOSI_VENV" "mkosi 27.1 venv; create it and install the pinned mkosi (see the bootstrap step of .github/workflows/test.yml)"
    need "$SANDBOX_DIR" "assembled sandbox; run: bash mkosi/build.sh --bootstrap"
    need "$SANDBOX_SOURCES" "assembled sandbox apt source; run: bash mkosi/build.sh --bootstrap"
    need "$TOOLS_TREE" "pinned tools tree; run: bash mkosi/build.sh --bootstrap"
    need_x "$TOOLS_TREE/usr/bin/systemd-repart" "pinned tools tree; run: bash mkosi/build.sh --bootstrap"
    need "$TOOLS_MANIFEST" "tools-tree manifest; run: bash mkosi/build.sh --bootstrap"
    need "$BASE_TREE" "published base tree; run: bash mkosi/base-image.sh ensure"
    need "$BASE_KERNEL" "published base kernel; run: bash mkosi/base-image.sh ensure"
    if [ -x "$MKOSI_VENV" ] \
        && ! "$MKOSI_VENV" --version 2>/dev/null | grep -qx 'mkosi 27.1'; then
        bad+=("$MKOSI_VENV  (must report 'mkosi 27.1')")
    fi
    if [ -f "$TOOLS_MANIFEST" ]; then
        local pin
        pin=$(grep -vE '^[[:space:]]*(#|$)' -- "$TOOLS_PIN_SOURCE" | head -n 1 || true)
        pin=${pin//[[:space:]]/}
        if [[ ! $pin =~ ^[0-9a-f]{64}$ ]]; then
            bad+=("$TOOLS_PIN_SOURCE  (expected exactly one sha256)")
        elif [ "$(sha256sum -- "$TOOLS_MANIFEST" | awk '{print $1}')" != "$pin" ]; then
            bad+=("$TOOLS_MANIFEST  (sha256 does not match $TOOLS_PIN_SOURCE)")
        fi
    fi
    if [ -f "$SANDBOX_SOURCES" ]; then
        local suite sources_suite
        suite=$(sed -n 's/^Release=//p' -- "$REPO_ROOT/mkosi/mkosi.conf" | head -n 1)
        sources_suite=$(sed -n 's/^Suites:[[:space:]]*//p' -- "$SANDBOX_SOURCES" | head -n 1)
        [ "$sources_suite" = "$suite" ] \
            || bad+=("$SANDBOX_SOURCES  (Suites: ${sources_suite:-none} != Release=$suite)")
    fi
    [ "$(cat "$TOOLS_PIN_FILE" 2>/dev/null || true)" = "$TOOLS_PIN" ] \
        || bad+=("$TOOLS_PIN_FILE  (must contain '$TOOLS_PIN')")
    if [ "${#bad[@]}" -gt 0 ]; then
        printf 'prepare-sysext-hv: preflight failed; missing or invalid inputs:\n' >&2
        printf '  %s\n' "${bad[@]}" >&2
        cat >&2 <<'EOF'
Bootstrap the toolchain on this checkout (no images are built):

  bash mkosi/build.sh --bootstrap

It assembles the sandbox, bootstraps the pinned tools tree (verified against
mkosi/tools.pin) and ensures the published base tree. The mkosi venv is a
precondition (create it with python3 -m venv and install the pinned mkosi;
see the bootstrap step of .github/workflows/test.yml). It must provide:
  .build/mkosi/venv                        mkosi 27.1
  .build/mkosi/sandbox                     assembled by mkosi/build.sh
  .build/mkosi/tools/mkosi.tools           pinned tools tree
  .build/mkosi/tools/pin                   ubuntu/noble
  .build/mkosi/tools/mkosi.tools.manifest  sha256 must match mkosi/tools.pin
  .build/mkosi/base-image/current          published base tree
  .build/mkosi/base-image/current-kernel   published base kernel
EOF
        exit 2
    fi
    printf 'prepare-sysext-hv: preflight ok (mkosi 27.1, tools pin %s)\n' "$TOOLS_PIN"
}

find_one() { # <dir> <suffix>
    local count
    count=$(find "$1" -maxdepth 1 -type f -name "*.$2" | wc -l)
    [ "$count" -eq 1 ] || die "artifact directory $1 must contain exactly one *.$2 (found $count)"
    find "$1" -maxdepth 1 -type f -name "*.$2" | head -n 1
}

prepare() {
    [ "$#" -eq 2 ] || { usage >&2; exit 2; }
    local artifact_dir=$1 out=$2
    [ -d "$artifact_dir" ] || die "artifact directory not found: $artifact_dir"
    [ -n "$out" ] && [ "$out" != / ] || die "refusing to use '$out' as the output directory"
    preflight

    local cmd
    for cmd in qemu-img sfdisk e2fsck resize2fs ssh-keygen; do command -v "$cmd" >/dev/null 2>&1 || die "$cmd is required (dependencies action)"; done

    local artifact_tar artifact_ref
    artifact_tar=$(find_one "$artifact_dir" tar)
    artifact_ref=$(find_one "$artifact_dir" ref)
    [ -s "$artifact_tar" ] || die "empty delivery container archive: $artifact_tar"
    [ -s "$artifact_ref" ] || die "empty delivery container reference: $artifact_ref"
    artifact_ref=$(head -n 1 "$artifact_ref")
    [ -n "$artifact_ref" ] || die "delivery container reference is empty"
    tar -tf "$artifact_tar" >/dev/null 2>&1 || die "not a readable tar: $artifact_tar"

    local suite epoch
    suite=$(sed -n 's/^Release=//p' -- "$REPO_ROOT/mkosi/mkosi.conf" | head -n 1)
    [ -n "$suite" ] || die "no Release= in mkosi/mkosi.conf"
    epoch=$("$REPO_ROOT/bin/garden-version" --epoch "$suite") \
        || die "cannot derive the epoch for suite $suite"

    local out_parent
    out_parent=$(dirname -- "$out")
    mkdir -p -- "$out_parent"
    out=$(cd -- "$out_parent" && pwd -P)/$(basename -- "$out")
    mkdir -p -- "$out/overlay/etc/ssh/sshd_config.d" \
               "$out/overlay/etc/systemd/system/multi-user.target.wants" \
               "$out/overlay/etc/systemd/system-preset" \
               "$out/overlay/etc/cloud" "$out/overlay/usr/lib/verity.d" \
               "$out/mkosi" "$out/repart.d" "$out/disks"
    printf '%s\n' "$artifact_ref" >"$out/sci-delivery.ref"

    local key=$out/hv-test-key
    [ -f "$key" ] || ssh-keygen -q -t ed25519 -N '' -C 'sci-sysext-ci' -f "$key" \
        || die "cannot generate the test SSH key"
    chmod 600 "$key"
    local pubkey
    pubkey=$(cat "$key.pub")

    # Test-environment overlay only; the published base stays untouched.
    printf '%s\n' "$pubkey" >"$out/overlay/etc/ssh/authorized_keys"
    chmod 600 "$out/overlay/etc/ssh/authorized_keys"
    printf 'PasswordAuthentication no\nUsePAM no\n' \
        >"$out/overlay/etc/ssh/sshd_config.d/10-sci-ci.conf"
    # sshd uses the first value; patch the main config, not a drop-in.
    sed 's/^PermitRootLogin .*/PermitRootLogin yes/' \
        "$BASE_TREE/etc/ssh/sshd_config" >"$out/overlay/etc/ssh/sshd_config"
    grep -q '^AuthorizedKeysFile' "$out/overlay/etc/ssh/sshd_config" \
        || printf '\nAuthorizedKeysFile /etc/ssh/authorized_keys\n' \
            >>"$out/overlay/etc/ssh/sshd_config"
    # Presets: first match wins, so 00-* must sort before the base 00-*-disable.
    printf 'enable ssh.service\ndisable ssh.socket\nenable containerd.service\n' \
        >"$out/overlay/etc/systemd/system-preset/00-00-sci-ci-enable.preset"
    ln -sfn /usr/lib/systemd/system/ssh.service \
        "$out/overlay/etc/systemd/system/multi-user.target.wants/ssh.service"
    ln -sfn /usr/lib/systemd/system/containerd.service \
        "$out/overlay/etc/systemd/system/multi-user.target.wants/containerd.service"
    : >"$out/overlay/etc/cloud/cloud-init.disabled"
    ln -sfn /dev/null "$out/overlay/etc/systemd/system/tgt.service"
    cp -- "$REPO_ROOT/mkosi/verity.crt" "$out/overlay/usr/lib/verity.d/verity.crt"
    # libvirt derives the p2p migration address from the destination
    # hostname; the guests have no upstream DNS, so pin the names.
    cp -- "$BASE_TREE/etc/hosts" "$out/overlay/etc/hosts"
    printf '192.168.122.2 hv1\n192.168.122.3 hv2\n' >>"$out/overlay/etc/hosts"

    cat >"$out/mkosi/mkosi.conf" <<EOF
[Distribution]
Distribution=custom
[Build]
ToolsTree=$TOOLS_TREE
CacheDirectory=$out/cache
WithNetwork=no
[Output]
Format=disk
Output=sysext-hv-base
OutputDirectory=$out/image
[Content]
BaseTrees=$BASE_TREE
Packages=
ExtraTrees=$out/overlay:/
Bootable=no
EOF

    printf 'prepare-sysext-hv: building HV root image (suite %s)\n' "$suite"
    ( cd -- "$out/mkosi"; "$MKOSI_VENV" --source-date-epoch="$epoch" -f build ) \
        >"$out/build.log" 2>&1 || {
            tail -n 60 -- "$out/build.log" >&2 || true
            die "mkosi build failed; see $out/build.log"
        }
    local raw=$out/image/sysext-hv-base.raw
    [ -f "$raw" ] || die "mkosi build produced no $raw"

    # Grow GPT and ext4 offline; no loop device or root privilege needed.
    printf '[Partition]\nType=root-x86-64\nSizeMinBytes=%s\nSizeMaxBytes=%s\n' \
        "$HV_ROOT_SIZE" "$HV_ROOT_SIZE" >"$out/repart.d/80-root.conf"
    # The tools-tree repart finds its private libsystemd-shared only through
    # an absolute host RUNPATH; point the loader back into the tools tree.
    LD_LIBRARY_PATH="$TOOLS_TREE/usr/lib/x86_64-linux-gnu/systemd" \
        "$TOOLS_TREE/usr/bin/systemd-repart" --offline=yes --size="$HV_DISK_SIZE" \
        --dry-run=no --definitions="$out/repart.d" "$raw" >>"$out/build.log" 2>&1 || {
            tail -n 60 -- "$out/build.log" >&2 || true
            die "cannot grow the HV root image to $HV_DISK_SIZE"
        }

    local table start size uuid name number
    table=$(python3 - "$raw" "$ROOT_GUID" <<'PY'
import json, subprocess, sys
raw, guid = sys.argv[1], sys.argv[2].lower()
parts = json.loads(subprocess.run(["sfdisk", "--json", raw], check=True,
                                  capture_output=True, text=True).stdout)["partitiontable"]["partitions"]
roots = [p for p in parts if p["type"].lower() == guid]
if len(roots) != 1:
    raise SystemExit(f"expected one root partition, found {len(roots)}")
p = roots[0]
print(p["start"] * 512, p["size"] * 512, p["uuid"].lower(),
      p.get("name", "ROOT"), p.get("number", 1))
PY
    ) || die "cannot read the grown root partition"
    read -r start size uuid name number <<<"$table"

    local part=$out/rootfs.part
    dd if="$raw" of="$part" bs=1M iflag=skip_bytes,count_bytes skip="$start" \
        count="$size" conv=sparse status=none || die "cannot extract the root filesystem"
    e2fsck -f -y "$part" >/dev/null 2>&1 || true
    resize2fs "$part" >>"$out/build.log" 2>&1 || {
        tail -n 60 -- "$out/build.log" >&2 || true
        die "cannot grow the root filesystem"
    }
    dd if="$part" of="$raw" bs=1M oflag=seek_bytes conv=sparse,notrunc \
        seek="$start" status=none || die "cannot write the root filesystem back"
    rm -f -- "$part"

    cp -- "$BASE_KERNEL" "$out/vmlinuz"
    printf '{"partitiontable":{"label":"gpt","device":"/dev/nvme0n1","partitions":[{"number":%s,"type":"%s","name":"%s","uuid":"%s"}]}}\n' \
        "$number" "$ROOT_GUID" "$name" "$uuid" >"$out/partition-table.json"
    "$SCRIPT_DIR/prepare-sysext-hv.sh" --render "$out/partition-table.json" \
        "$out" "$out/vmlinuz" "$out/disks"
    rm -f -- "$out/partition-table.json"

    local hv
    for hv in HV1 HV2; do
        qemu-img convert -O qcow2 "$raw" "$out/disks/$hv.qcow2" \
            || die "cannot convert the HV root image to $hv.qcow2"
        # The integration libvirt runs qemu unprivileged with
        # security_driver=none: no chowning, so the disk carries raw access.
        chmod 666 "$out/disks/$hv.qcow2"
    done

    cat >"$out/metadata.env" <<EOF
HV_DIR=$out
HV_SSH_KEY=$key
HV_ARTIFACT_TAR=$artifact_tar
HV_ARTIFACT_REF=$artifact_ref
HV_KERNEL=$out/vmlinuz
EOF
    printf 'prepare-sysext-hv: prepared HV1/HV2 under %s (kernel %s)\n' \
        "$out" "$out/vmlinuz"
    printf 'prepare-sysext-hv: public test key: %s\n' "$pubkey"
}

case "${1:-}" in
    --render)    shift; render "$@" ;;
    --prepare)   shift; prepare "$@" ;;
    --check)     shift; preflight "$@" ;;
    -h|--help)   usage; exit 0 ;;
    *)           usage >&2; exit 2 ;;
esac
