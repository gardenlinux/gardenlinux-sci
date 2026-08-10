#!/usr/bin/env bash
set -Eeuo pipefail

TMPDIR_IGN=""
REGISTRY_CONTAINER=""
cleanup() {
    [[ -n "$REGISTRY_CONTAINER" ]] && podman rm -f "$REGISTRY_CONTAINER" >/dev/null 2>&1 || true
    [[ -n "$TMPDIR_IGN" ]] && rm -rf "$TMPDIR_IGN" || true
}
trap cleanup EXIT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="$REPO_DIR/.build"
QEMU_PREFIX="$(brew --prefix qemu 2>/dev/null || echo /opt/homebrew)"
OVMF_CODE="${QEMU_PREFIX}/share/qemu/edk2-x86_64-code.fd"
OVMF_VARS="${QEMU_PREFIX}/share/qemu/edk2-i386-vars.fd"
REGISTRY_PORT=5001

start_oci_registry() {
    local prefix="$1"
    local ignition_file="$2"

    local release_file="${prefix}.release"
    if [[ ! -f "$release_file" ]]; then
        echo "Error: release file not found: $release_file" >&2
        exit 1
    fi
    # shellcheck disable=SC1090
    . "$release_file"
    local dashed_version="${GARDENLINUX_VERSION//./-}"
    local oci_tag="${GARDENLINUX_VERSION}-${VARIANT_ID}-${dashed_version}-${GARDENLINUX_COMMIT_ID}"
    oci_tag="${oci_tag//_/-}"
    local oci_repo_local="localhost:${REGISTRY_PORT}/gardenlinux/gardenlinux-sci"
    local oci_repo_guest="10.0.2.2:${REGISTRY_PORT}/gardenlinux/gardenlinux-sci"
    echo "OCI tag: ${oci_tag}" >&2

    podman pull docker.io/registry:2 >/dev/null
    REGISTRY_CONTAINER="oci-registry-boot-uki"
    podman rm -f "$REGISTRY_CONTAINER" &>/dev/null || true
    podman run -d --rm --name "$REGISTRY_CONTAINER" \
        -p "${REGISTRY_PORT}:5000" \
        docker.io/registry:2 >/dev/null
    echo "Started OCI registry: $REGISTRY_CONTAINER" >&2
    if ! podman container inspect "$REGISTRY_CONTAINER" &>/dev/null; then
        echo "Error: failed to start OCI registry container '$REGISTRY_CONTAINER'" >&2
        exit 1
    fi

    oras push "${oci_repo_local}:${oci_tag}" \
        --artifact-type "application/vnd.oci.image.manifest.v1+json" \
        --disable-path-validation \
        "${prefix}.uki:application/io.gardenlinux.uki" >&2
    echo "Pushed UKI to local registry" >&2

    local merged_ign="$TMPDIR_IGN/merged.ign"
    local admin_hash
    admin_hash="$(openssl passwd -6 "admin")"
    python3 - "$ignition_file" "$oci_repo_guest" "$oci_tag" "$admin_hash" > "$merged_ign" <<'PYEOF'
import json, sys, struct, base64
from urllib.parse import quote

cfg = json.load(open(sys.argv[1]))
cfg.setdefault("storage", {}).setdefault("files", [])
cfg.setdefault("passwd", {}).setdefault("users", [])

oci_repo   = sys.argv[2]
oci_tag    = sys.argv[3]
admin_hash = sys.argv[4]

# Inject admin user if not already present
if not any(u.get("name") == "admin" for u in cfg["passwd"]["users"]):
    cfg["passwd"]["users"].append({
        "name": "admin",
        "groups": ["wheel"],
        "passwordHash": admin_hash
    })

gl_oci_conf = (
    f"OCI_REPO={oci_repo}\n"
    f"OCI_TAG={oci_tag}\n"
    "PATH=/sysroot/opt/persist:$PATH\n"
)
oras_wrapper = "#!/bin/bash\nexec /usr/bin/oras \"$@\" --plain-http\n"

# Build a PE32+ EFI application with .cmdline section for serial console.
# systemd-stub loads *.addon.efi via UEFI LoadImage and reads their
# .cmdline content to append to the kernel command line.
def build_console_addon():
    cmdline = b'console=ttyS0,115200'
    section_alignment = 4096
    file_alignment = 512
    header_size = file_alignment  # all headers fit in one block
    num_sections = 2
    opt_header_size = 240  # PE32+: 112 base + 128 data directory (16 entries)

    # File layout:
    # [0..512)      Headers (DOS + PE sig + COFF + OptHdr + Section table)
    # [512..1024)   .text section (entry point returning EFI_UNSUPPORTED)
    # [1024..1536)  .cmdline section
    text_file_offset = header_size
    cmdline_file_offset = header_size + file_alignment
    text_va = section_alignment       # 0x1000
    cmdline_va = section_alignment*2  # 0x2000
    size_of_image = section_alignment * 3
    total_size = cmdline_file_offset + file_alignment

    # Entry point code: mov rax, EFI_UNSUPPORTED (0x8000000000000003); ret
    text_code = b'\x48\xB8\x03\x00\x00\x00\x00\x00\x00\x80\xC3'

    # DOS Header (64 bytes)
    dos_hdr = bytearray(64)
    struct.pack_into('<H', dos_hdr, 0, 0x5A4D)   # e_magic = 'MZ'
    struct.pack_into('<I', dos_hdr, 60, 64)       # e_lfanew -> PE sig

    # COFF Header (20 bytes)
    coff_hdr = struct.pack('<HHIIIHH',
        0x8664, num_sections, 0, 0, 0, opt_header_size, 0x0022)

    # Optional Header PE32+ (240 bytes)
    opt_hdr = bytearray(opt_header_size)
    struct.pack_into('<H', opt_hdr, 0, 0x020B)               # Magic: PE32+
    struct.pack_into('<I', opt_hdr, 4, file_alignment)        # SizeOfCode
    struct.pack_into('<I', opt_hdr, 8, file_alignment)        # SizeOfInitializedData
    struct.pack_into('<I', opt_hdr, 16, text_va)              # AddressOfEntryPoint
    struct.pack_into('<I', opt_hdr, 20, text_va)              # BaseOfCode
    struct.pack_into('<I', opt_hdr, 32, section_alignment)    # SectionAlignment
    struct.pack_into('<I', opt_hdr, 36, file_alignment)       # FileAlignment
    struct.pack_into('<I', opt_hdr, 56, size_of_image)        # SizeOfImage
    struct.pack_into('<I', opt_hdr, 60, header_size)          # SizeOfHeaders
    struct.pack_into('<H', opt_hdr, 68, 10)                   # Subsystem: EFI_APPLICATION
    struct.pack_into('<I', opt_hdr, 108, 16)                  # NumberOfRvaAndSizes

    # Section table
    text_sec = struct.pack('<8sIIIIIIHHI',
        b'.text\x00\x00\x00', len(text_code), text_va,
        file_alignment, text_file_offset, 0, 0, 0, 0, 0x60000020)
    cmdline_sec = struct.pack('<8sIIIIIIHHI',
        b'.cmdline', len(cmdline), cmdline_va,
        file_alignment, cmdline_file_offset, 0, 0, 0, 0, 0x40000040)

    # Assemble PE
    pe = bytearray(total_size)
    pe[0:64] = dos_hdr
    pe[64:68] = b'PE\x00\x00'
    pe[68:88] = coff_hdr
    pe[88:88+opt_header_size] = opt_hdr
    pe[328:368] = text_sec
    pe[368:408] = cmdline_sec
    pe[text_file_offset:text_file_offset+len(text_code)] = text_code
    pe[cmdline_file_offset:cmdline_file_offset+len(cmdline)] = cmdline
    return base64.b64encode(bytes(pe)).decode()

console_addon_b64 = build_console_addon()

cfg["storage"]["files"] += [
    {
        "path": "/opt/persist/gl-oci.conf",
        "overwrite": True,
        "contents": {"source": "data:," + quote(gl_oci_conf)},
        "mode": 0o644
    },
    {
        "path": "/opt/persist/oras",
        "overwrite": True,
        "contents": {"source": "data:," + quote(oras_wrapper)},
        "mode": 0o755
    },
    {
        "path": "/efi/loader/addons/console.addon.efi",
        "overwrite": True,
        "contents": {"source": "data:;base64," + console_addon_b64},
        "mode": 0o755
    }
]
print(json.dumps(cfg))
PYEOF
    echo "Merged ignition config: $merged_ign" >&2
    echo "$merged_ign"
}

usage() {
    cat >&2 <<EOF
Usage: $(basename "$0") [OPTIONS] [ARTIFACT_PREFIX]

Boot a gardenlinux UKI artifact in QEMU using EFI/OVMF boot.

ARTIFACT_PREFIX  Path prefix for build artifacts, e.g.:
                   .build/metal-scibase_usi-amd64-1877.13.12-local
                 If omitted, auto-detects the most recently built .uki in .build/

OPTIONS
  --mem <size>       QEMU memory, e.g. 4096M or 8G (default: 4096M)
  --ssh              Forward host port 2222 to guest port 22
  --ignition <file>  Ignition config JSON (default: ignition.yaml); attaches as CD-ROM
                     (config-2 label) for ignition firstboot provisioning
  --disk <size>      Add a blank SATA disk; starts a local OCI registry via podman,
                     pushes the UKI, and patches the UKI cmdline with ignition firstboot
                     params (default: 10G, disk is temporary and deleted when QEMU exits)
  -h, --help         Show this help

EXAMPLES
  $(basename "$0")                          # uses test.ign + 10G disk
  $(basename "$0") .build/metal-scibase_usi-amd64-1877.13.12-local
  $(basename "$0") --ssh --mem 8192 .build/metal-scibase_usi-amd64-1877.13.12-local
  $(basename "$0") --ignition config.ign
  $(basename "$0") --ignition config.ign --disk 20G
EOF
}

MEM=4096M
SSH=0
PREFIX=""
IGNITION_FILE="${SCRIPT_DIR}/ignition.yaml"
DISK_SIZE="10G"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mem)
            if [[ $# -lt 2 ]]; then
                echo "Error: --mem requires an argument" >&2
                usage
                exit 1
            fi
            MEM="$2"; shift 2 ;;
        --ssh) SSH=1; shift ;;
        --ignition)
            if [[ $# -lt 2 ]]; then
                echo "Error: --ignition requires an argument" >&2
                usage
                exit 1
            fi
            IGNITION_FILE="$2"; shift 2 ;;
        --disk)
            if [[ $# -lt 2 ]]; then
                echo "Error: --disk requires an argument" >&2
                usage
                exit 1
            fi
            DISK_SIZE="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "Unknown option: $1" >&2; usage; exit 1 ;;
        *) PREFIX="$1"; shift ;;
    esac
done

# Validate MEM value
if [[ ! "$MEM" =~ ^[0-9]+[MGmg]?$ ]]; then
    echo "Error: --mem value '$MEM' is not a valid QEMU memory size (e.g. 4096, 4096M, 4G)" >&2
    exit 1
fi

# Validate ignition config file
if [[ ! -f "$IGNITION_FILE" || ! -r "$IGNITION_FILE" ]]; then
    echo "Error: ignition config not found or not readable: $IGNITION_FILE" >&2
    exit 1
fi

# Validate disk size if provided
if [[ -n "$DISK_SIZE" && ! "$DISK_SIZE" =~ ^[0-9]+[MGTmgt]?$ ]]; then
    echo "Error: --disk value '$DISK_SIZE' is not a valid size (e.g. 20G, 50G)" >&2
    exit 1
fi

# Validate required tools
if ! command -v hdiutil &>/dev/null; then
    echo "Error: hdiutil is required (macOS only)" >&2; exit 1
fi
if ! command -v qemu-system-x86_64 &>/dev/null; then
    echo "Error: qemu-system-x86_64 is required (brew install qemu)" >&2; exit 1
fi
if [[ ! -f "$OVMF_CODE" ]]; then
    echo "Error: OVMF firmware not found: $OVMF_CODE (brew install qemu)" >&2; exit 1
fi
if [[ ! -f "$OVMF_VARS" ]]; then
    echo "Error: OVMF vars not found: $OVMF_VARS (brew install qemu)" >&2; exit 1
fi
if [[ -n "$DISK_SIZE" ]]; then
    if ! command -v qemu-img &>/dev/null; then
        echo "Error: qemu-img is required (brew install qemu)" >&2; exit 1
    fi
    if ! command -v podman &>/dev/null; then
        echo "Error: podman is required (brew install podman)" >&2; exit 1
    fi
    _podman_info="$(podman info 2>&1)" || {
        if echo "$_podman_info" | grep -q "proxy already running"; then
            echo "Error: Podman proxy is stuck from a previous run. Fix with:" >&2
            echo "  podman machine stop && podman machine start" >&2
        else
            echo "Error: cannot connect to Podman. Run 'podman machine start' first." >&2
        fi
        exit 1
    }
    unset _podman_info
    if ! command -v oras &>/dev/null; then
        echo "Error: oras is required (brew install oras)" >&2; exit 1
    fi
    if ! command -v openssl &>/dev/null; then
        echo "Error: openssl is required" >&2; exit 1
    fi
fi

TMPDIR_IGN="$(mktemp -d)"

# Auto-detect prefix if not given: pick the most recently modified .uki in .build/
if [[ -z "$PREFIX" ]]; then
    uki_files=("$BUILD_DIR"/*.uki)
    if [[ ! -e "${uki_files[0]}" ]]; then
        echo "Error: no .uki files found in $BUILD_DIR" >&2
        echo "Either run a build first or specify ARTIFACT_PREFIX" >&2
        exit 1
    fi
    # Sort by modification time descending (newest first)
    # shellcheck disable=SC2012
    latest_uki="$(ls -t "${uki_files[@]}" | head -1)"
    PREFIX="${latest_uki%.uki}"
    echo "Auto-detected artifact: $PREFIX" >&2
fi

UKI="${PREFIX}.uki"
if [[ ! -f "$UKI" ]]; then
    echo "Error: required file not found: $UKI" >&2
    exit 1
fi

# Start local OCI registry, push UKI, and merge OCI config into ignition
if [[ -n "$DISK_SIZE" ]]; then
    IGNITION_FILE="$(start_oci_registry "$PREFIX" "$IGNITION_FILE")"
fi

# Build ignition config-drive ISO
mkdir -p "$TMPDIR_IGN/iso-root/openstack/latest"
cp "$IGNITION_FILE" "$TMPDIR_IGN/iso-root/openstack/latest/user_data"
# hdiutil makehybrid auto-appends .iso to the output filename
hdiutil makehybrid \
    -o "$TMPDIR_IGN/ignition-config-drive" \
    -iso -joliet \
    -iso-volume-name "config-2" \
    -joliet-volume-name "config-2" \
    "$TMPDIR_IGN/iso-root" >/dev/null
echo "Ignition config: $IGNITION_FILE (attached as CD-ROM with label config-2)" >&2

# Create blank SATA install disk and patch UKI cmdline for firstboot
UKI_FOR_ESP="$UKI"
if [[ -n "$DISK_SIZE" ]]; then
    qemu-img create -f qcow2 "$TMPDIR_IGN/disk.qcow2" "$DISK_SIZE" >/dev/null
    echo "Created blank disk: $DISK_SIZE" >&2

    # Patch .cmdline section in UKI to add ignition firstboot params
    # Extract current cmdline from UKI PE32+ .cmdline section
    ORIG_CMDLINE="$(python3 - "$UKI" <<'PYEOF'
import struct, sys
with open(sys.argv[1], 'rb') as f:
    f.seek(0x3c)
    pe_offset = struct.unpack('<I', f.read(4))[0]
    f.seek(pe_offset + 4)
    _machine, nsections, _ts, _sp, _sc, opt_size, _chars = struct.unpack('<HHIIIHH', f.read(20))
    section_table_offset = f.tell() + opt_size
    for i in range(nsections):
        f.seek(section_table_offset + i * 40)
        name = f.read(8).rstrip(b'\x00').decode('ascii', errors='replace')
        _vsize = struct.unpack('<I', f.read(4))[0]
        _vaddr = struct.unpack('<I', f.read(4))[0]
        raw_size = struct.unpack('<I', f.read(4))[0]
        raw_offset = struct.unpack('<I', f.read(4))[0]
        if name == '.cmdline':
            f.seek(raw_offset)
            data = f.read(raw_size).rstrip(b'\x00')
            print(data.decode('utf-8'))
            sys.exit(0)
sys.exit(1)
PYEOF
)"
    if [[ -z "$ORIG_CMDLINE" ]]; then
        echo "Error: could not extract .cmdline section from $UKI" >&2
        exit 1
    fi
    NEW_CMDLINE="$(echo "$ORIG_CMDLINE" | tr -d '\n\r') console=ttyS0,115200 ignition.firstboot=1 ignition.platform.id=openstack"
    PATCHED_UKI="$TMPDIR_IGN/uki-firstboot.efi"
    python3 - "$UKI" "$PATCHED_UKI" "$NEW_CMDLINE" <<'PYEOF'
import struct, sys, shutil

src, dst, new_cmdline = sys.argv[1], sys.argv[2], sys.argv[3].encode('utf-8')
shutil.copy2(src, dst)

with open(dst, 'r+b') as f:
    f.seek(0x3c)
    pe_offset = struct.unpack('<I', f.read(4))[0]
    f.seek(pe_offset + 4)
    _machine, nsections, _ts, _sp, _sc, opt_size, _chars = struct.unpack('<HHIIIHH', f.read(20))
    section_table_offset = f.tell() + opt_size
    for i in range(nsections):
        hdr_offset = section_table_offset + i * 40
        f.seek(hdr_offset)
        name = f.read(8).rstrip(b'\x00').decode('ascii', errors='replace')
        f.seek(hdr_offset + 8)
        _vsize = struct.unpack('<I', f.read(4))[0]
        _vaddr = struct.unpack('<I', f.read(4))[0]
        raw_size = struct.unpack('<I', f.read(4))[0]
        raw_offset = struct.unpack('<I', f.read(4))[0]
        if name == '.cmdline':
            assert len(new_cmdline) <= raw_size, \
                f"cmdline too large: {len(new_cmdline)} > {raw_size}"
            # Update VirtualSize in section header
            f.seek(hdr_offset + 8)
            f.write(struct.pack('<I', len(new_cmdline)))
            # Write new cmdline data, zero-padded to raw_size
            f.seek(raw_offset)
            f.write(new_cmdline + b'\x00' * (raw_size - len(new_cmdline)))
            sys.exit(0)
sys.stderr.write("Error: .cmdline section not found\n")
sys.exit(1)
PYEOF
    UKI_FOR_ESP="$PATCHED_UKI"
    echo "Patched UKI cmdline: $NEW_CMDLINE" >&2
fi

# Build ESP directory and create FAT32 disk image
ESP_DIR="$TMPDIR_IGN/esp-root"
mkdir -p "$ESP_DIR/EFI/BOOT"
cp "$UKI_FOR_ESP" "$ESP_DIR/EFI/BOOT/BOOTX64.EFI"
hdiutil create -srcfolder "$ESP_DIR" \
    -fs "MS-DOS FAT32" -volname BOOTVOL \
    -layout NONE \
    -o "$TMPDIR_IGN/esp" >/dev/null
hdiutil convert "$TMPDIR_IGN/esp.dmg" \
    -format UDTO \
    -o "$TMPDIR_IGN/esp-raw" >/dev/null
ESP_IMG="$TMPDIR_IGN/esp-raw.cdr"
echo "Created ESP image: $ESP_IMG" >&2

# Copy writable OVMF vars to tmpdir (OVMF writes EFI variables to it)
cp "$OVMF_VARS" "$TMPDIR_IGN/ovmf-vars.fd"

# Build QEMU options
# shellcheck disable=SC2054
QEMU_OPTS=(
    -machine q35
    -cpu qemu64
    -m "$MEM"
    -accel tcg
    -drive "if=pflash,unit=0,format=raw,readonly=on,file=$OVMF_CODE"
    -drive "if=pflash,unit=1,format=raw,file=$TMPDIR_IGN/ovmf-vars.fd"
    -drive "id=esp,if=none,format=raw,readonly=on,file=$ESP_IMG"
    -device "virtio-blk-pci,drive=esp"
    -nographic
    -device virtio-net-pci,netdev=net0
)

if ((SSH)); then
    QEMU_OPTS+=(-netdev "user,id=net0,hostfwd=tcp::2222-:22")
    echo "SSH forwarding: ssh -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null admin@localhost"
else
    QEMU_OPTS+=(-netdev "user,id=net0")
fi

QEMU_OPTS+=(-cdrom "$TMPDIR_IGN/ignition-config-drive.iso")

if [[ -n "$DISK_SIZE" ]]; then
    QEMU_OPTS+=(-drive "id=disk0,if=none,format=qcow2,file=$TMPDIR_IGN/disk.qcow2")
    QEMU_OPTS+=(-device "ide-hd,drive=disk0,bus=ide.1")
fi

echo "Starting QEMU (tcg software emulation - will be slow without KVM/HVF acceleration)..."
echo "Serial console on stdio. QEMU monitor shortcuts:"
echo "  Ctrl-A X  quit"
echo "  Ctrl-A C  switch to QEMU monitor (type 'quit' or 'system_powerdown')"
if [[ -n "$DISK_SIZE" ]]; then
    echo "  Firstboot cmdline: $NEW_CMDLINE"
fi
echo ""

qemu-system-x86_64 "${QEMU_OPTS[@]}"
