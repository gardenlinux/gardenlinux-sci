#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["click"]
# ///
"""Assemble the sysext HyperVisor disks and their libvirt definitions."""

import logging
import os
import shutil
import string
import subprocess
import tempfile
import uuid
from pathlib import Path

import click

LOG = logging.getLogger("prepare-sysext-hv")

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[4]
TEMPLATE = SCRIPT_DIR / "hv-sysext.xml"
SSH_KEY = Path("/opt/ssh_host_ed25519_key.pub")
HV_DISK_SIZE = os.environ.get("HV_DISK_SIZE", "8G")
HV_ROOT_SIZE = os.environ.get("HV_ROOT_SIZE", "7G")
HV_NETWORK = os.environ.get("HV_NETWORK", os.environ.get("SCI_HV_NETWORK", "default"))
DOMAINS = (("HV1", "52:54:00:12:34:56"), ("HV2", "52:54:00:65:43:21"))
EXTENSIONS = (
    "sci-10-base",
    "sci-20-compute",
    "sci-21-network",
    "sci-22-storage",
    "sci-40-libvirt",
    "sci-50-virt-ch",
    "sci-51-virt-qemu",
)

# libvirtd.service and openvswitch-switch.service do not exist when systemd
# loads ssh.service at t=0: they arrive with the merge. Only the merge may be
# a hard requirement; the two extension units are asserted by ExecStartPre,
# which runs after the merge and after the implied After= ordering.
SSH_DROP_IN = (
    "[Unit]\n"
    "After=systemd-sysext.service libvirtd.service openvswitch-switch.service\n"
    "Requires=systemd-sysext.service\n"
    "\n"
    "[Service]\n"
    "ExecStartPre=/usr/bin/systemctl is-active --quiet systemd-sysext.service "
    "libvirtd.service openvswitch-switch.service\n"
)


def run(command, **kwargs):
    LOG.info("running: %s", " ".join(str(part) for part in command))
    subprocess.run(command, check=True, **kwargs)


def base_tree():
    command = [REPO_ROOT / "mkosi" / "build.py", "--base-only"]
    result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True)
    if result.returncode:
        raise click.ClickException(f"{command[0]} --base-only failed:\n{result.stderr.strip()}")
    lines = result.stdout.strip().splitlines()
    if not lines:
        raise click.ClickException(f"{command[0]} --base-only printed no tree")
    return Path(lines[-1])


def stage_extensions(raws_dir, out_dir):
    missing = [name for name in EXTENSIONS if not (raws_dir / f"{name}.raw").is_file()]
    if missing:
        raise click.ClickException(f"the raws carry no {', '.join(missing)}.raw in {raws_dir}")

    extensions = out_dir / "extensions"
    extensions.mkdir()
    for name in EXTENSIONS:
        shutil.copyfile(raws_dir / f"{name}.raw", extensions / f"{name}.raw")


def write_ssh_config(rootfs, base_tree, pubkey):
    authorized = rootfs / "etc/ssh/authorized_keys"
    authorized.write_text(pubkey + "\n", encoding="utf-8")
    authorized.chmod(0o600)

    (rootfs / "etc/ssh/sshd_config.d/10-sci-ci.conf").write_text(
        "PasswordAuthentication no\nUsePAM no\n", encoding="utf-8"
    )
    sshd = (base_tree / "etc/ssh/sshd_config").read_text(encoding="utf-8")
    lines = [
        "PermitRootLogin yes"
        if line.startswith("PermitRootLogin ")
        else line
        for line in sshd.splitlines()
    ]
    if not any(line.startswith("AuthorizedKeysFile") for line in lines):
        lines.append("AuthorizedKeysFile /etc/ssh/authorized_keys")
    (rootfs / "etc/ssh/sshd_config").write_text("\n".join(lines) + "\n", encoding="utf-8")


def replace_symlink(target, link):
    if link.is_symlink() or link.is_file():
        link.unlink()
    elif link.is_dir():
        shutil.rmtree(link)
    link.symlink_to(target)


def write_guest_units(rootfs):
    (rootfs / "etc/systemd/system-preset/00-00-sci-ci-enable.preset").write_text(
        "enable ssh.service\ndisable ssh.socket\n", encoding="utf-8"
    )
    wants = rootfs / "etc/systemd/system/multi-user.target.wants/ssh.service"
    replace_symlink("/usr/lib/systemd/system/ssh.service", wants)
    (rootfs / "etc/systemd/system/ssh.service.d/10-sci-sysext.conf").write_text(
        SSH_DROP_IN, encoding="utf-8"
    )
    replace_symlink(
        "/usr/lib/systemd/system/systemd-sysext.service",
        rootfs / "etc/systemd/system/sysinit.target.wants/systemd-sysext.service",
    )
    replace_symlink("/dev/null", rootfs / "etc/systemd/system/tgt.service")


def overlay_rootfs(rootfs, base_tree, pubkey, raws_dir):
    for directory in (
        "etc/ssh/sshd_config.d",
        "etc/systemd/system/multi-user.target.wants",
        "etc/systemd/system/ssh.service.d",
        "etc/systemd/system/sysinit.target.wants",
        "etc/systemd/system-preset",
        "etc/cloud",
        "etc/systemd",
        "usr/lib/verity.d",
    ):
        (rootfs / directory).mkdir(parents=True, exist_ok=True)

    write_ssh_config(rootfs, base_tree, pubkey)
    write_guest_units(rootfs)

    (rootfs / "etc/cloud/cloud-init.disabled").write_text("", encoding="utf-8")
    shutil.copyfile(raws_dir / "verity.crt", rootfs / "usr/lib/verity.d/verity.crt")
    (rootfs / "etc/systemd/sysext.conf").write_text(
        "[SysExt]\nImagePolicy=root=signed\n", encoding="utf-8"
    )

    hosts = (base_tree / "etc/hosts").read_text(encoding="utf-8")
    (rootfs / "etc/hosts").write_text(
        hosts + "192.168.122.2 hv1\n192.168.122.3 hv2\n", encoding="utf-8"
    )
    fstab = (base_tree / "etc/fstab").read_text(encoding="utf-8")
    (rootfs / "etc/fstab").write_text(
        fstab + "sci-sysext-raws /run/extensions virtiofs ro 0 0\n", encoding="utf-8"
    )


def build_disk(rootfs, out_dir):
    root_uuid = str(uuid.uuid4())
    root_image = out_dir / "image" / "root.ext4"
    raw = out_dir / "image" / "sysext-hv-base.raw"

    run(["truncate", "-s", HV_ROOT_SIZE, root_image])
    run([
        "unshare", "-r", "/sbin/mkfs.ext4", "-q", "-F", "-L", "root", "-U", root_uuid,
        "-d", rootfs, root_image,
    ])
    run(["truncate", "-s", HV_DISK_SIZE, raw])

    sectors = root_image.stat().st_size // 512
    table = (
        "label: gpt\n"
        f"start=2048, size={sectors}, type=4f68bce3-e8cd-4db1-96e7-fbcaf984b709, "
        f"uuid={root_uuid}, name=root\n"
    )
    run(["sfdisk", raw], input=table, text=True)
    run(["dd", f"if={root_image}", f"of={raw}", "bs=1M", "seek=1", "conv=notrunc",
         "status=none"])

    kernel = Path(os.path.realpath(REPO_ROOT / ".build/mkosi/base-image/current-kernel"))
    shutil.copyfile(kernel, out_dir / "vmlinuz")
    (out_dir / "vmlinuz").chmod(0o644)
    initrd = Path(os.path.realpath(REPO_ROOT / ".build/mkosi/base-image/current-initrd"))
    shutil.copyfile(initrd, out_dir / "initrd.img")
    (out_dir / "initrd.img").chmod(0o644)
    return root_uuid


def render_domains(out_dir, root_uuid):
    template = string.Template(TEMPLATE.read_text(encoding="utf-8"))
    raw = out_dir / "image" / "sysext-hv-base.raw"
    for name, mac in DOMAINS:
        values = {
            "HV_NAME": name,
            "MAC": mac,
            "KERNEL": str(out_dir / "vmlinuz"),
            "INITRD": str(out_dir / "initrd.img"),
            "ROOT_UUID": root_uuid,
            "DISK_PREFIX": str(out_dir / "disks"),
            "NETWORK": HV_NETWORK,
            "EXPORT_DIR": str(out_dir / "extensions"),
        }
        (out_dir / f"{name}.xml").write_text(template.safe_substitute(values), encoding="utf-8")
        disk = out_dir / "disks" / f"{name}.qcow2"
        run(["qemu-img", "convert", "-O", "qcow2", raw, disk])
        disk.chmod(0o666)


@click.command()
@click.option("--raws-dir", envvar="RAWS_DIR", required=True,
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Artifact directory with the signed raws and their certificate.")
@click.option("--out-dir", envvar="OUT_DIR", default="/opt/hv", show_default=True,
              type=click.Path(path_type=Path), help="Directory for the domains and the share.")
def main(raws_dir, out_dir):
    """Assemble the sysext HyperVisor disks and their libvirt definitions."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    out_dir = out_dir.resolve()
    if out_dir == Path("/"):
        raise click.ClickException("refusing to use / as the output directory")
    if (out_dir / "extensions").exists():
        raise click.ClickException(f"refusing to reuse {out_dir / 'extensions'}")
    if not SSH_KEY.is_file():
        raise click.ClickException(f"missing {SSH_KEY}, the guest action must run first")

    pubkey = SSH_KEY.read_text(encoding="utf-8").strip()
    if not pubkey:
        raise click.ClickException(f"{SSH_KEY} is empty, the guest action must run first")
    if not (raws_dir / "verity.crt").is_file():
        raise click.ClickException(f"the raws carry no verity.crt: {raws_dir}")

    tree = base_tree()
    (out_dir / "image").mkdir(parents=True)
    (out_dir / "disks").mkdir()
    stage_extensions(raws_dir, out_dir)

    work = Path(tempfile.mkdtemp(dir=out_dir, prefix=".hv-work."))
    try:
        rootfs = work / "rootfs"
        shutil.copytree(tree, rootfs, symlinks=True)
        overlay_rootfs(rootfs, tree, pubkey, raws_dir)
        root_uuid = build_disk(rootfs, out_dir)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    render_domains(out_dir, root_uuid)
    LOG.info("prepared HV1/HV2 under %s with root uuid %s", out_dir, root_uuid)


if __name__ == "__main__":
    main()
