#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["click"]
# ///
"""Build the SCI system extension raws against the pinned Garden Linux base."""

import hashlib
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

import click

LOG = logging.getLogger("sci-build")

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
MKOSI_DIR = REPO_ROOT / ".build/mkosi"
BASE_CACHE = MKOSI_DIR / "base-image"
KEYS_DIR = REPO_ROOT / ".build/keys"
VERITY_KEY = KEYS_DIR / "sysext-verity.key"
VERITY_CERT = KEYS_DIR / "sysext-verity.crt"
PIN = SCRIPT_DIR / "base-image.pin"
PIN_FIELDS = "FLAVOR ARCH VERSION COMMIT TARBALL_SHA256 ROOTFS_TAR_SHA256 URL".split()
BOOT_IMAGES = (("vmlinuz-*", "current-kernel"), ("initrd.img-*", "current-initrd"))
MKOSI_VERSION = "v27.1"


def run(command, cwd=None):
    LOG.info("running: %s", " ".join(str(part) for part in command))
    subprocess.run(command, cwd=cwd, check=True)


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def replace_symlink(target, link):
    remove(link)
    link.symlink_to(target)


def read_key_values(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator:
            raise click.ClickException(f"malformed line {line!r} in {path}")
        values[name] = value.strip().strip('"').strip("'")
    return values


def read_pin():
    fields = read_key_values(PIN)
    missing = [name for name in PIN_FIELDS if name not in fields]
    if missing:
        raise click.ClickException(f"pin {PIN} lacks {', '.join(missing)}")
    return fields


def read_release():
    config = (SCRIPT_DIR / "mkosi.conf").read_text(encoding="utf-8")
    releases = [line[len("Release="):] for line in config.splitlines()
                if line.startswith("Release=")]
    if len(releases) != 1:
        raise click.ClickException(f"{SCRIPT_DIR / 'mkosi.conf'} needs exactly one Release= line")
    return releases[0]


def check_base_tree(tree, version):
    os_release = tree / "etc/os-release"
    if not os_release.is_file():
        raise click.ClickException(f"{tree} carries no etc/os-release")
    values = read_key_values(os_release)
    for name, expected in (("ID", "gardenlinux"), ("IMAGE_VERSION", version)):
        if values.get(name) != expected:
            raise click.ClickException(f"{os_release} does not set {name}={expected}")


def extract_base_tree(pin, key, tree, marker):
    tarball = BASE_CACHE / f"{key}.tar.xz"
    if not tarball.is_file():
        LOG.info("downloading %s", pin["URL"])
        partial = tarball.with_name(f"{tarball.name}.part")
        run(["curl", "-fL", "--retry", "3", "--retry-delay", "5", "-o", partial, pin["URL"]])
        partial.replace(tarball)

    digest = sha256(tarball)
    if digest != pin["TARBALL_SHA256"]:
        raise click.ClickException(f"{tarball} has sha256 {digest}, the pin has another one")

    remove(tree)
    remove(marker)
    work = Path(tempfile.mkdtemp(dir=BASE_CACHE, prefix=".extract."))
    try:
        LOG.info("extracting the rootfs from %s", tarball.name)
        inner = work / f"{key}.tar"
        run(["tar", "-xf", tarball, "-C", work, "--numeric-owner", "--", inner.name])
        digest = sha256(inner)
        if digest != pin["ROOTFS_TAR_SHA256"]:
            raise click.ClickException(f"{inner} has sha256 {digest}, the pin has another one")
        (work / "tree").mkdir()
        run(["tar", "-xf", inner, "-C", work / "tree", "--numeric-owner"])
        check_base_tree(work / "tree", pin["VERSION"])
        shutil.move(str(work / "tree"), tree)
        marker.write_text(pin["ROOTFS_TAR_SHA256"] + "\n", encoding="utf-8")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def ensure_base_tree():
    pin = read_pin()
    suite = read_release()
    if pin["VERSION"] != suite:
        raise click.ClickException(
            f"pin VERSION={pin['VERSION']} does not match Release={suite} in mkosi.conf")

    key = "-".join((pin["FLAVOR"], pin["ARCH"], pin["VERSION"], pin["COMMIT"][:8]))
    tree = BASE_CACHE / key
    marker = BASE_CACHE / f"{key}.ready"
    BASE_CACHE.mkdir(parents=True, exist_ok=True)
    ready = marker.is_file() and marker.read_text(encoding="utf-8").strip()
    if not tree.is_dir() or ready != pin["ROOTFS_TAR_SHA256"]:
        extract_base_tree(pin, key, tree, marker)

    replace_symlink(Path(key), BASE_CACHE / "current")
    for pattern, link in BOOT_IMAGES:
        images = sorted((tree / "boot").glob(pattern))
        if not images:
            raise click.ClickException(f"{tree} carries no boot/{pattern}")
        replace_symlink(Path(key) / "boot" / images[0].name, BASE_CACHE / link)

    LOG.info("base tree %s is ready", tree)
    return tree


def ensure_keys():
    KEYS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    common = ["openssl", "req", "-x509", "-days", "1", "-subj", "/CN=sci-sysext-dev"]
    if not VERITY_KEY.is_file():
        LOG.info("creating the local signing key %s", VERITY_KEY)
        run(common + ["-newkey", "rsa:3072", "-nodes", "-keyout", VERITY_KEY,
                      "-out", VERITY_CERT])
        VERITY_KEY.chmod(0o600)
    elif not VERITY_CERT.is_file():
        LOG.info("creating the certificate %s from the existing key", VERITY_CERT)
        run(common + ["-key", VERITY_KEY, "-out", VERITY_CERT])


def ensure_mkosi():
    mkosi = MKOSI_DIR / "venv" / "bin" / "mkosi"
    if not mkosi.is_file():
        LOG.info("installing mkosi %s into %s", MKOSI_VERSION, mkosi.parent.parent)
        run(["uv", "venv", MKOSI_DIR / "venv"])
        python = MKOSI_DIR / "venv" / "bin" / "python"
        package = f"mkosi @ git+https://github.com/systemd/mkosi.git@{MKOSI_VERSION}"
        run(["uv", "pip", "install", "--python", python, package])
    return mkosi


def write_sandbox(suite):
    sandbox = MKOSI_DIR / "sandbox"
    remove(sandbox)
    sources = sandbox / "etc/apt/sources.list.d/mkosi.sources"
    sources.parent.mkdir(parents=True)
    sources.write_text(
        "Types: deb\n"
        "URIs: https://packages.gardenlinux.io/gardenlinux\n"
        f"Suites: {suite}\n"
        "Components: main\n"
        "Signed-By: /usr/share/keyrings/gardenlinux.gpg\n",
        encoding="utf-8")


def garden_epoch(suite):
    command = [REPO_ROOT / "bin" / "garden-version", "--epoch", suite]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise click.ClickException(f"{command[0]} failed:\n{result.stderr.strip()}")
    epoch = result.stdout.strip()
    if not epoch.isdigit():
        raise click.ClickException(f"garden-version --epoch {suite} printed {epoch!r}")
    return epoch


def build(image):
    if image and not (SCRIPT_DIR / "mkosi.images" / image / "mkosi.conf").is_file():
        raise click.ClickException(f"unknown image {image!r}")

    ensure_keys()
    ensure_base_tree()
    mkosi = ensure_mkosi()
    suite = read_release()
    write_sandbox(suite)

    result = MKOSI_DIR / "output"
    result.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(VERITY_CERT, result / "verity.crt")

    command = [mkosi, f"--source-date-epoch={garden_epoch(suite)}"]
    if image:
        command += ["--dependency", image]
    run(command + ["build"], cwd=SCRIPT_DIR)


@click.command()
@click.argument("image", required=False)
@click.option("--base-only", is_flag=True, help="Prepare the pinned base tree and print its path.")
def main(image, base_only):
    """Build the SCI sysext raws. IMAGE builds one image, no IMAGE builds every image."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if base_only:
        click.echo(ensure_base_tree())
        return
    build(image)


if __name__ == "__main__":
    main()
