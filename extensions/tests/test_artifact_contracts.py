"""Public input and raw-image contracts. Build checks require explicit consent."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

import pytest

import summary

ROOT = Path(__file__).resolve().parents[2]
MKOSI = ROOT / "mkosi"
STATE = ROOT / ".build/mkosi"
IMAGES = ("sci-10-base", "sci-20-compute", "sci-21-network", "sci-22-storage", "sci-40-libvirt", "sci-50-virt-ch", "sci-51-virt-qemu")
TYPES = ("4f68bce3-e8cd-4db1-96e7-fbcaf984b709", "2c7357ed-ebd2-46d9-aec1-23d437ec2bf5",
         "41092b05-9fc8-4523-994f-2def0408b176")


def run(*args, cwd=ROOT):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=1800)
    assert result.returncode == 0, f"{args} in {cwd}: {result.stdout}\n{result.stderr}"
    return result.stdout


def values(path, key):
    return re.findall(rf"^{re.escape(key)}=(.*)$", path.read_text(), re.M)


def pin(path):
    lines = [line.strip() for line in path.read_text().splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    assert len(lines) == 1 and re.fullmatch(r"[0-9a-f]{64}", lines[0]), path
    return lines[0]


def epoch():
    release, = values(MKOSI / "mkosi.conf", "Release")
    return run(str(ROOT / "bin/garden-version"), "--epoch", release).strip()


@pytest.fixture(scope="session")
def evidence_root() -> Path:
    """A fresh per-run extraction root under the repo's persistent .build."""
    base = ROOT / ".build" / "sysext-tests"
    base.mkdir(parents=True, exist_ok=True)
    root = base / uuid.uuid4().hex
    root.mkdir()
    return root


def extract_root(image, dest):
    """EROFS root of <image>.raw extracted under dest/root, idempotent: an
    extraction left by an earlier test in this session is reused as-is."""
    root = dest / "root"
    if (root / "usr").exists():
        return root
    raw = STATE / "output" / f"{image}.raw"
    assert raw.is_file(), f"build artifact missing: {raw}"
    table = json.loads(run("sfdisk", "--json", str(raw)))["partitiontable"]
    assert table["label"] == "gpt" and table["sectorsize"] == 512
    partitions = table["partitions"]
    assert [p["type"].lower() for p in partitions] == list(TYPES), partitions
    assert all(p["size"] > 0 for p in partitions)
    dest.mkdir(parents=True, exist_ok=True)
    part = dest / "root.erofs"
    run("dd", f"if={raw}", f"of={part}", "bs=512", f"skip={partitions[0]['start']}",
        f"count={partitions[0]['size']}", "status=none")
    builder = os.environ.get("MKOSI_BUILDER_IMAGE")
    if not builder:
        builder, = values(ROOT / "build", "container_image")
    assert re.search(r"(?:@sha256:[0-9a-f]{64}|:[0-9a-f]{40})$", builder), builder
    root.mkdir(exist_ok=True)
    run(os.environ.get("MKOSI_CONTAINER_ENGINE", "docker"), "run", "--rm", "--pull=never",
        "--network=none", "--security-opt", "seccomp=unconfined", "--security-opt", "apparmor=unconfined",
        "--user", f"{os.getuid()}:{os.getgid()}", "--entrypoint", "fsck.erofs",
        "-v", f"{ROOT}:{ROOT}", builder, f"--extract={root}", "--force", "--preserve-perms", str(part))
    return root


def root_files(root):
    """Every file and symlink under an extracted root, as posix relative paths."""
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() or p.is_symlink()}


def overlapping_paths(a, b):
    """Paths present in both path sets: non-empty means one layer re-ships the other."""
    return a & b


def test_static_input_contract():
    release, = values(MKOSI / "mkosi.conf", "Release")
    assert release == "2150.9.0"
    assert values(MKOSI / "suite.pin", "SUITE") == [release]
    assert not (MKOSI / "mkosi.images/base").exists()
    assert not (MKOSI / "generate-base-packages.sh").exists()
    deployables = {p.parent.name for p in (MKOSI / "mkosi.images").glob("*/mkosi.conf")
                   if values(p, "Format") == ["sysext"]}
    assert deployables == set(IMAGES)
    assert not (MKOSI / "mkosi.images/sci-40-libvirt-tree").exists(), (
        "the directory-tree twin of sci-40-libvirt existed only as an Overlay dedup parent for the "
        "virt images; the decoupling removed that role")
    for image in IMAGES:
        conf = MKOSI / "mkosi.images" / image / "mkosi.conf"
        # The seed derives from the directory name, so a rename recomputes it.
        digest = hashlib.sha256(f"sci:{release}:{image}".encode()).hexdigest()[:32]
        seed = "-".join(digest[a:b] for a, b in ((0, 8), (8, 12), (12, 16), (16, 20), (20, 32)))
        assert values(conf, "Seed") == [seed], image
        assert values(conf, "Overlay") == ["yes"]
        base, = values(conf, "BaseTrees")
        parents = base.split()
        assert (conf.parent / parents[0]).resolve() == (STATE / "base-image/current").resolve()
        assert len(parents) == 1, (
            image, "every extension must build standalone on the published base: a second "
            "base-tree entry re-introduces a build-time parent that couples rebuilds")
        assert values(conf, "Dependencies") == [], (
            image, "inter-extension build dependencies couple rebuilds: a package change must "
            "rebuild exactly one extension; runtime merge order is handled by the deployment "
            "naming, not by build topology")
        assert "usr/lib/verity.d" in values(conf, "RemoveFiles")
        assert "usr/lib/modules/*/vmlinuz" in values(conf, "RemoveFiles")
        if image in {"sci-50-virt-ch", "sci-51-virt-qemu"}:
            # apt installs the full libvirt set; explicit globs keep it out of the delta.
            globs = values(conf, "RemoveFiles")
            for path in ("usr/share/libvirt/cpu_map", "usr/share/libvirt/schemas",
                         "usr/lib/sysusers.d/libvirt.conf"):
                assert any(path in g for g in globs), (
                    image, path, "the driver packages' Depends pulls the libvirt set into the "
                    "virt builds; without explicit removal the delta double-ships the "
                    "libvirt-owned paths (420 in ch, 265 in qemu) and the lexical overlay merge "
                    "silently shadows one copy")
    for name in ("tools.pin", "summary.pin"):
        pin(MKOSI / name)
    assert values(MKOSI / "base-image.pin", "VERSION") == [release]
    for field in ("TARBALL_SHA256", "ROOTFS_TAR_SHA256"):
        digest, = values(MKOSI / "base-image.pin", field)
        assert re.fullmatch(r"[0-9a-f]{64}", digest)


def test_qemu_vnc_range_carrier():
    extra = ROOT / "mkosi/mkosi.images/sci-51-virt-qemu/mkosi.extra"
    rules = [line.strip() for line in
             (extra / "usr/lib/tmpfiles.d/sci-virt-qemu.conf").read_text().splitlines()]
    payload = extra / "usr/share/sci/etc/libvirt/qemu.conf"
    assert payload.is_file(), f"the qemu VNC range payload must exist at {payload}"
    lines = [line.strip() for line in payload.read_text().splitlines()]
    assert any(re.fullmatch(r"d\s+/etc/libvirt\s+0755\s+root\s+root\s+-", line) for line in rules), (
        "the qemu fragment must create /etc/libvirt before the C rule copies into it")
    assert any(re.fullmatch(
        r"C\s+/etc/libvirt/qemu\.conf\s+0644\s+root\s+root\s+-\s+/usr/share/sci/etc/libvirt/qemu\.conf",
        line) for line in rules), (
        "the qemu fragment must materialize /etc/libvirt/qemu.conf from the /usr payload with type C")
    assert sum("remote_display_port" in line for line in lines) == 2, (
        "the payload must declare exactly the two VNC port settings")
    assert sum(bool(re.fullmatch(r"remote_display_port_min\s*=\s*32200", line)) for line in lines) == 1, (
        "the payload must set remote_display_port_min = 32200 for the NodePort range")
    assert sum(bool(re.fullmatch(r"remote_display_port_max\s*=\s*32299", line)) for line in lines) == 1, (
        "the payload must set remote_display_port_max = 32299 for the NodePort range")
    assert not [line for line in lines if line.startswith("#") and "remote_display_port" in line], (
        "the payload must be a partial conffile with no commented-out port settings")


@pytest.mark.build
def test_tools_manifest_pin():
    manifest = STATE / "tools/mkosi.tools.manifest"
    assert manifest.is_file(), f"pinned tools manifest missing: {manifest}"
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == pin(MKOSI / "tools.pin")


@pytest.mark.build
def test_resolved_summary_pin():
    binary = STATE / "venv/bin/mkosi"
    assert binary.is_file(), f"mkosi venv missing: {binary}"
    digest = summary.resolved_config_digest(binary, MKOSI, ROOT / "bin/garden-version", ROOT)
    assert digest == pin(MKOSI / "summary.pin")


@pytest.mark.build
def test_virt_deltas_do_not_reship_libvirt(evidence_root):
    libvirt = root_files(extract_root("sci-40-libvirt", evidence_root / "extracted" / "sci-40-libvirt"))
    for image in ("sci-50-virt-ch", "sci-51-virt-qemu"):
        virt = root_files(extract_root(image, evidence_root / "extracted" / image))
        overlap = overlapping_paths(virt, libvirt)
        assert not overlap, (
            image, "the virt deltas must not re-ship any sci-40-libvirt-owned path: with the "
            "parent-tree dedup gone, RemoveFiles= is the only guard, and a package update that "
            "adds a file outside the globs must fail here rather than silently shadow in the "
            "overlay merge; first offenders: " + ", ".join(sorted(overlap)[:10]))


@pytest.mark.build
@pytest.mark.build_state_mutating
def test_reproducible():
    run("bash", str(MKOSI / "build.sh"))
    def hashes():
        return {image: hashlib.sha256((STATE / "output" / f"{image}.raw").read_bytes()).hexdigest()
                for image in IMAGES}
    first = hashes()
    run(str(STATE / "venv/bin/mkosi"), f"--source-date-epoch={epoch()}", "-f", "build", cwd=MKOSI)
    second = hashes()
    assert second == first, f"rebuild changed the output hashes: {first} != {second}"
    for image in IMAGES:
        print(f"build1 {image} {first[image]}")
        print(f"build2 {image} {second[image]}")
