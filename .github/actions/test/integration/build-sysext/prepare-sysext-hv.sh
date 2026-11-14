#!/usr/bin/env bash
# CI adapter for the sysext integration test: --render and --prepare.
# Only test adapter files and .build/ are touched. Contract: docs/sysext-virt.md.
set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../../../../.." && pwd -P)
BASE_IMAGE_DIR=$REPO_ROOT/.build/mkosi/base-image
BASE_KERNEL=$BASE_IMAGE_DIR/current-kernel
TEMPLATE=$SCRIPT_DIR/hv-sysext.xml
HV_DISK_SIZE=${HV_DISK_SIZE:-8G}
HV_ROOT_SIZE=${HV_ROOT_SIZE:-7G}
SSH_KEY=${SSH_KEY:-/opt/ssh_host_ed25519_key}
ROOT_PARTITION_TYPE=4f68bce3-e8cd-4db1-96e7-fbcaf984b709
WORK_DIR=

die() { printf 'prepare-sysext-hv: error: %s\n' "$*" >&2; exit 2; }
cleanup() { [ -z "$WORK_DIR" ] || rm -rf -- "$WORK_DIR"; }
trap cleanup EXIT
usage() {
    cat <<'EOF'
usage: prepare-sysext-hv.sh --render <partition-table.json> <out-dir> <kernel> <disk-prefix>
       prepare-sysext-hv.sh --prepare <raws-dir> [<out-dir>]
Env: HV_DISK_SIZE=8G HV_ROOT_SIZE=7G HV_NETWORK=default (or SCI_HV_NETWORK)
EOF
}

render() {
    [ "$#" -eq 4 ] || { usage >&2; exit 2; }
    local table=$1 out=$2 kernel=$3 prefix=$4 network=${HV_NETWORK:-${SCI_HV_NETWORK:-default}} root_uuid export_dir hv mac
    [ -f "$TEMPLATE" ] || die "missing XML template: $TEMPLATE"
    # Exactly one root partition of the Garden Linux GPT type, with a usable PARTUUID.
    root_uuid=$(python3 - "$table" "$ROOT_PARTITION_TYPE" <<'PY'
import json, re, sys
want = sys.argv[2].lower()
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        parts = json.load(handle)["partitiontable"]["partitions"]
except (OSError, ValueError, KeyError, TypeError) as exc:
    sys.exit(f"prepare-sysext-hv: error: invalid JSON partition table {sys.argv[1]}: {exc}")
roots = [p for p in parts if isinstance(p, dict) and str(p.get("type", "")).lower() == want]
if len(roots) != 1:
    sys.exit(f"prepare-sysext-hv: error: expected exactly one root partition (GPT type {want}), found {len(roots)}")
uuid = str(roots[0].get("uuid", "")).strip()
if not re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", uuid, re.I):
    sys.exit(f"prepare-sysext-hv: error: root partition uuid is not a valid UUID: {uuid!r}")
print(uuid)
PY
    ) || die "cannot use the partition table $table"
    # The virtiofs export is the sibling of the disk directory that --prepare writes.
    export_dir=$(cd -- "$(dirname -- "$prefix")" && pwd -P)/extensions
    mkdir -p -- "$out"
    for hv in HV1 HV2; do
        case $hv in HV1) mac=52:54:00:12:34:56 ;; HV2) mac=52:54:00:65:43:21 ;; esac
        HV_NAME=$hv MAC=$mac KERNEL=$kernel ROOT_UUID=$root_uuid DISK_PREFIX=$prefix \
            NETWORK=$network EXPORT_DIR=$export_dir \
            envsubst '${HV_NAME} ${MAC} ${KERNEL} ${ROOT_UUID} ${DISK_PREFIX} ${NETWORK} ${EXPORT_DIR}' <"$TEMPLATE" >"$out/$hv.xml"
    done
    printf 'prepare-sysext-hv: rendered HV1, HV2 in %s\n' "$out"
}

# The in-band guard runs the artifact's delivery step from the read-only share,
# then proves that the signed raws merged exactly as the manifest declares.
print_verify_script() {
    cat <<'GUARD'
#!/usr/bin/env bash
set -Eeuo pipefail
share=/opt/sci
manifest=$share/extensions.manifest
die() { printf 'sci-verify-sysext: error: %s\n' "$*" >&2; exit 1; }
fstype=$(findmnt -n -o FSTYPE -- "$share" || true)
[ "$fstype" = virtiofs ] || die "the sysext share at $share is not virtiofs (found '${fstype:-none}')"
opts=$(findmnt -n -o OPTIONS -- "$share" || true)
case ",$opts," in *,ro,*) ;; *) die "the sysext share at $share is not mounted read-only (found '${opts:-none}')" ;; esac
[ -r "$manifest" ] || die "the sysext share carries no readable extensions.manifest: $manifest"
expected=$(awk '/^[[:space:]]*#/ { next } /^[[:space:]]*$/ { next } /^[A-Z][A-Z0-9_]*=/ { next } { print $1 }' "$manifest" | sort -u)
[ -n "$expected" ] || die "the sysext manifest lists no extensions: $manifest"
# The merged /usr needs its tmpfiles entries before the delivery step refreshes.
systemd-tmpfiles --create --prefix=/usr
HOST_ROOT=/ STAGING_DIR=/opt/sci INTERVAL=0 NSENTER=0 python3 /opt/sci/sci-delivery.py || die "the delivery step failed"
merged=$(systemd-sysext status --json=short | grep -o '"extensions":\[[^]]*\]' | sed -e 's/^"extensions":\[//' -e 's/\]$//' | tr ',' '\n' | tr -d '"' | awk 'NF' | sort -u || true)
[ -n "$merged" ] || die "systemd-sysext merged no extensions from $share"
if [ "$merged" != "$expected" ]; then
    printf 'the merged extension set does not match %s\nexpected:\n%s\nmerged:\n%s\n' "$manifest" "$expected" "$merged" >&2
    exit 1
fi
systemctl is-active --quiet libvirtd.service || die "libvirtd.service is not active; the 40-libvirt extension did not start it"
echo "the extension set matches $manifest"
GUARD
}

prepare() {
    [ "$#" -ge 1 ] && [ "$#" -le 2 ] || { usage >&2; exit 2; }
    local raws_dir=$1 out=${2:-/opt/hv} base_tree manifest sums pubkey root_uuid root_image raw root_sectors guard hv rootfs
    [ -d "$raws_dir" ] || die "raws staging directory not found: $raws_dir"
    [ -n "$out" ] && [ "$out" != / ] || die "refusing to use '$out' as the output directory"
    # The guest build action owns key generation; refuse before creating output.
    [ -s "$SSH_KEY" ] || die "the shared test key $SSH_KEY is missing; the guest build action must run before --prepare"
    [ -s "$SSH_KEY.pub" ] || die "the shared test public key $SSH_KEY.pub is missing"
    pubkey=$(cat -- "$SSH_KEY.pub")
    base_tree=$(bash "$REPO_ROOT/mkosi/base-image.sh" ensure) || die "cannot prepare the published base tree"
    [ -n "$base_tree" ] && [ -d "$base_tree" ] || die "base-image.sh ensure did not report a usable base tree"
    [ -r "$base_tree/etc/os-release" ] || die "cannot read the published base os-release"
    # Prove the downloaded raws before touching the output.
    manifest=$raws_dir/extensions.manifest sums=$raws_dir/SHA256SUMS
    [ -f "$manifest" ] || die "raws staging directory carries no extensions.manifest: $manifest"
    [ -f "$sums" ] || die "raws staging directory carries no SHA256SUMS: $sums"
    [ -f "$raws_dir/sci-delivery.py" ] || die "raws staging directory carries no sci-delivery.py: $raws_dir"
    ( cd -- "$raws_dir" && sha256sum -c -- SHA256SUMS >/dev/null ) || die "SHA256SUMS does not verify the raws in $raws_dir"
    mkdir -p -- "$(dirname -- "$out")"; out=$(cd -- "$(dirname -- "$out")" && pwd -P)/$(basename -- "$out")
    [ ! -e "$out/extensions" ] && [ ! -L "$out/extensions" ] || die "refusing to reuse $out/extensions; use a fresh out directory"
    mkdir -p -- "$out/extensions" "$out/image" "$out/disks"
    cp -- "$raws_dir"/*.raw "$out/extensions/"
    cp -- "$raws_dir"/*-activation.service "$out/extensions/"
    cp -- "$manifest" "$out/extensions/extensions.manifest"; cp -- "$sums" "$out/extensions/SHA256SUMS"
    cp -- "$raws_dir/sci-delivery.py" "$out/extensions/sci-delivery.py"; chmod 0755 "$out/extensions/sci-delivery.py"
    # Build the guest root filesystem as a plain tree: no second mkosi bootstrap.
    WORK_DIR=$(mktemp -d "$out/.hv-work.XXXXXX"); rootfs=$WORK_DIR/rootfs
    mkdir -p -- "$rootfs"; cp -a -- "$base_tree/." "$rootfs/"
    mkdir -p -- "$rootfs/etc/ssh/sshd_config.d" "$rootfs/etc/systemd/system/multi-user.target.wants" \
        "$rootfs/etc/systemd/system/ssh.service.d" "$rootfs/etc/systemd/system-preset" \
        "$rootfs/etc/cloud" "$rootfs/usr/lib/verity.d" "$rootfs/etc/systemd" "$rootfs/usr/local/sbin"
    printf '%s\n' "$pubkey" >"$rootfs/etc/ssh/authorized_keys"; chmod 600 -- "$rootfs/etc/ssh/authorized_keys"
    printf 'PasswordAuthentication no\nUsePAM no\n' >"$rootfs/etc/ssh/sshd_config.d/10-sci-ci.conf"
    sed 's/^PermitRootLogin .*/PermitRootLogin yes/' "$base_tree/etc/ssh/sshd_config" >"$rootfs/etc/ssh/sshd_config"
    grep -q '^AuthorizedKeysFile' "$rootfs/etc/ssh/sshd_config" || printf '\nAuthorizedKeysFile /etc/ssh/authorized_keys\n' >>"$rootfs/etc/ssh/sshd_config"
    printf 'enable ssh.service\ndisable ssh.socket\nenable sci-verify-sysext.service\n' >"$rootfs/etc/systemd/system-preset/00-00-sci-ci-enable.preset"
    ln -sfn /usr/lib/systemd/system/ssh.service "$rootfs/etc/systemd/system/multi-user.target.wants/ssh.service"
    : >"$rootfs/etc/cloud/cloud-init.disabled"; ln -sfn /dev/null "$rootfs/etc/systemd/system/tgt.service"
    install -m 0644 -- "$REPO_ROOT/mkosi/verity.crt" "$rootfs/usr/lib/verity.d/verity.crt"
    printf '[SysExt]\nImagePolicy=root=signed\n' >"$rootfs/etc/systemd/sysext.conf"
    cp -- "$base_tree/etc/hosts" "$rootfs/etc/hosts"; printf '192.168.122.2 hv1\n192.168.122.3 hv2\n' >>"$rootfs/etc/hosts"
    cp -- "$base_tree/etc/fstab" "$rootfs/etc/fstab"; printf 'sci-sysext-raws /opt/sci virtiofs ro 0 0\n' >>"$rootfs/etc/fstab"
    guard=$rootfs/usr/local/sbin/sci-verify-sysext; print_verify_script >"$guard"; chmod 0755 -- "$guard"
    cat >"$rootfs/etc/systemd/system/sci-verify-sysext.service" <<'EOF'
[Unit]
Description=Verify the merged SCI system extensions
After=local-fs.target network-online.target
Wants=network-online.target
Before=ssh.service multi-user.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/sci-verify-sysext

[Install]
WantedBy=multi-user.target
EOF
    ln -sfn /etc/systemd/system/sci-verify-sysext.service "$rootfs/etc/systemd/system/multi-user.target.wants/sci-verify-sysext.service"
    printf '[Unit]\nRequires=sci-verify-sysext.service\nAfter=sci-verify-sysext.service\n' >"$rootfs/etc/systemd/system/ssh.service.d/10-require-sysext-verify.conf"
    # Assemble the GPT disk directly, unprivileged: mkfs.ext4 -d runs inside a
    # user namespace so every file is owned by 0:0, sfdisk writes the table
    # with the exact root.ext4 size, and dd copies the filesystem at 1 MiB.
    command -v mkfs.ext4 >/dev/null 2>&1 || die "mkfs.ext4 is required (e2fsprogs)"
    command -v sfdisk >/dev/null 2>&1 || die "sfdisk is required (util-linux)"
    command -v qemu-img >/dev/null 2>&1 || die "qemu-img is required (dependencies action)"
    root_uuid=$(python3 -c 'import uuid; print(uuid.uuid5(uuid.NAMESPACE_DNS, "sci-sysext-hv-root"))')
    root_image=$out/image/root.ext4 raw=$out/image/sysext-hv-base.raw
    truncate -s "$HV_ROOT_SIZE" "$root_image"
    unshare -r /sbin/mkfs.ext4 -q -F -L root -U "$root_uuid" -d "$rootfs" "$root_image" "$HV_ROOT_SIZE" || die "cannot format the HV root filesystem"
    root_sectors=$(( $(stat -c '%s' "$root_image") / 512 ))
    truncate -s "$HV_DISK_SIZE" "$raw"
    sfdisk "$raw" <<EOF
label: gpt
start=2048, size=$root_sectors, type=$ROOT_PARTITION_TYPE, uuid=$root_uuid, name=root
EOF
    dd if="$root_image" of="$raw" bs=1M seek=1 conv=notrunc status=none || die "cannot copy the root filesystem into the disk image"
    install -m 0644 -- "$(readlink -f -- "$BASE_KERNEL")" "$out/vmlinuz"
    sfdisk --json "$raw" >"$out/partition-table.json" || die "cannot read the partition table of $raw"
    "$SCRIPT_DIR/prepare-sysext-hv.sh" --render "$out/partition-table.json" "$out" "$out/vmlinuz" "$out/disks"
    rm -f -- "$out/partition-table.json"
    for hv in HV1 HV2; do
        qemu-img convert -O qcow2 "$raw" "$out/disks/$hv.qcow2" || die "cannot convert the HV root image to $hv.qcow2"
        chmod 666 "$out/disks/$hv.qcow2"
    done
    rm -rf -- "$WORK_DIR"; WORK_DIR=
    printf 'prepare-sysext-hv: prepared HV1/HV2 under %s (kernel %s)\n' "$out" "$out/vmlinuz"
    printf 'prepare-sysext-hv: public test key: %s\n' "$pubkey"
}

case "${1:-}" in
    --render)  shift; render "$@" ;;
    --prepare) shift; prepare "$@" ;;
    -h|--help) usage; exit 0 ;;
    *)         usage >&2; exit 2 ;;
esac
