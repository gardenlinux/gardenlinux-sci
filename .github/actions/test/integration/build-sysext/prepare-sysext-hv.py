#!/usr/bin/env python3
"""Prepare the sysext HyperVisor disks and their libvirt definitions."""

import argparse
import html
import logging
import os
import shutil
import string
import subprocess
from pathlib import Path

LOG = logging.getLogger("prepare-sysext-hv")

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[4]
TEMPLATE = SCRIPT_DIR / "hv-sysext.xml"
SSH_KEY = Path("/opt/ssh_host_ed25519_key.pub")
HV_NETWORK = os.environ.get("HV_NETWORK", os.environ.get("SCI_HV_NETWORK", "default"))
DOMAINS = (("HV1", "52:54:00:12:34:56"), ("HV2", "52:54:00:65:43:21"))


def run(command, env=None):
    LOG.info("running: %s", " ".join(str(part) for part in command))
    subprocess.run(command, cwd=REPO_ROOT, check=True, env=env)


def prepare(raws_dir, out_dir):
    if (out_dir / "extensions").exists():
        raise ValueError(f"refusing to reuse {out_dir / 'extensions'}")
    if not SSH_KEY.is_file():
        raise ValueError(f"missing {SSH_KEY}, the guest action must run first")
    pubkey = SSH_KEY.read_text(encoding="utf-8").strip()
    if len(pubkey.splitlines()) != 1:
        raise ValueError(f"{SSH_KEY} must contain one SSH public key")
    selected = sorted(raws_dir.rglob("sci-*.raw"))
    cert = next((path for path in raws_dir.rglob("*.crt") if path.is_file()), None)
    if not selected or cert is None:
        raise ValueError(f"missing sci-*.raw or *.crt in {raws_dir}")

    run(["bash", REPO_ROOT / "mkosi/mkosi.sync"], env=os.environ | {"SRCDIR": str(REPO_ROOT / "mkosi")})
    base_disk = REPO_ROOT / "mkosi/base-image/base.qcow2"
    extensions = out_dir / "extensions"
    extensions.mkdir(parents=True)
    (out_dir / "disks").mkdir()
    for path in selected:
        shutil.copyfile(path, extensions / path.name)
    shutil.copyfile(cert, extensions / "verity.crt")

    template = string.Template(TEMPLATE.read_text(encoding="utf-8"))
    for name, mac in DOMAINS:
        values = {
            "HV_NAME": name, "MAC": mac, "DISK_PREFIX": str(out_dir / "disks"),
            "NETWORK": HV_NETWORK, "EXPORT_DIR": str(extensions),
            "SSH_PUBLIC_KEY": pubkey,
        }
        escaped = {key: html.escape(value, quote=True) for key, value in values.items()}
        (out_dir / f"{name}.xml").write_text(template.substitute(escaped), encoding="utf-8")
        disk = out_dir / "disks" / f"{name}.qcow2"
        run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", base_disk, disk])
        disk.chmod(0o666)
    LOG.info("prepared HV1/HV2 under %s", out_dir)


def main():
    """Prepare the sysext HyperVisor disks and their libvirt definitions."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    raws_default = os.environ.get("RAWS_DIR")
    parser.add_argument("--raws-dir", type=Path,
                        default=Path(raws_default) if raws_default else None,
                        required=raws_default is None,
                        help="Artifact directory with the signed raws and their certificate.")
    parser.add_argument("--out-dir", type=Path,
                        default=Path(os.environ.get("OUT_DIR", "/opt/hv")),
                        help="Directory for the domains and the share.")
    args = parser.parse_args()
    out_dir = args.out_dir.resolve()
    if out_dir == Path("/"):
        parser.error("refusing to use / as the output directory")
    try:
        prepare(args.raws_dir.resolve(), out_dir)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        details = getattr(error, "stderr", None)
        parser.error(f"{error}{': ' + details.strip() if details else ''}")


if __name__ == "__main__":
    main()
