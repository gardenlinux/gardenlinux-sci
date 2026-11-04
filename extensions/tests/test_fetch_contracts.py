"""Fetch-mode and REMOVE-directive contract tests for delivery/sci-delivery.sh.

The tests pin fetch mode (a signed SHA256SUMS inventory served from FETCH_URL, verified against FETCH_PUBKEY, optionally pinned by FETCH_SUMS_DIGEST, staged under FETCH_DIR) and the manifest's REMOVE= tombstone directive.

Harness: each fixture build is served over HTTP from a localhost
ThreadingHTTPServer that records every request path; an RSA keypair signs
the inventory; ``systemctl`` and ``systemd-sysext`` are stubs that log their
argv; ``curl``, ``openssl`` and ``sha256sum`` are the real tools. No lib
imports, builds, VMs, docker or non-loopback network.
"""
from __future__ import annotations
import contextlib
import hashlib
import http.server
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
import pytest
REPO = Path(__file__).resolve().parents[2]
DELIVERY_SH = REPO / "delivery/sci-delivery.sh"
NAMES = ("sci-50-virt-ch", "sci-51-virt-qemu")
MANIFEST = "TARGET_BASE=2150\nsci-50-virt-ch foo.service\nsci-51-virt-qemu\n"
MANIFEST_REMOVE = "TARGET_BASE=2150\nsci-50-virt-ch foo.service\nREMOVE=sci-51-virt-qemu\n"
MANIFEST_REMOVE_LISTED = "TARGET_BASE=2150\nsci-50-virt-ch foo.service\nREMOVE=sci-50-virt-ch\n"
MANIFEST_REMOVE_TRAVERSE = "TARGET_BASE=2150\nsci-50-virt-ch foo.service\nREMOVE=../../etc/passwd\n"
# Two-build drop sequence: build A deploys sci-21-network and sci-51-virt-qemu;
# build B drops sci-51-virt-qemu behind a tombstone and its signed inventory no
# longer lists the sci-51 files.
MANIFEST_DROP_A = "TARGET_BASE=2150\nsci-21-network\nsci-51-virt-qemu\n"
MANIFEST_DROP_B = "TARGET_BASE=2150\nsci-21-network\nREMOVE=sci-51-virt-qemu\n"
INVENTORY = ("extensions.manifest", "sci-50-virt-ch.raw", "sci-51-virt-qemu.raw",
             "sci-50-virt-ch-activation.service", "sci-51-virt-qemu-activation.service")
SUMS_PATHS = {"/SHA256SUMS", "/SHA256SUMS.sig"}
SHIM_TEMPLATE = ("[Unit]\nDescription=SCI {name} activation (fetch-contracts fixture)\nAfter=systemd-sysext.service\n"
                 "[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/usr/bin/true\n"
                 "[Install]\nWantedBy=multi-user.target\n")
LOG_STUB = "#!/bin/sh\nprintf 'TOOL %s\\n' \"$*\" >> \"${STUB_LOG:-/dev/null}\"\nexit 0\n"
SLEEP_STUB = "#!/bin/sh\nprintf '%s\\n' \"$$\" >\"${STUB_SLEEP_STARTED:-/dev/null}\"\nexec /bin/sleep \"$@\"\n"
# Like LOG_STUB, but also reproduces the filesystem effect of enable/disable:
# real systemctl enable creates the wants symlink the activation shim's
# [Install] section asks for (WantedBy=multi-user.target), and disable
# removes it again. $2 is the unit name on the systemctl command line.
SYSTEMCTL_ENABLE_STUB = (
    "#!/bin/sh\n"
    "printf 'systemctl %s\\n' \"$*\" >> \"${STUB_LOG:-/dev/null}\"\n"
    "case \"$1\" in\n"
    "  enable)\n"
    "    d=\"${HOST_ROOT}/etc/systemd/system/multi-user.target.wants\"\n"
    "    mkdir -p \"$d\" && ln -sf \"../$2\" \"$d/$2\"\n"
    "    ;;\n"
    "  disable)\n"
    "    rm -f \"${HOST_ROOT}/etc/systemd/system/multi-user.target.wants/$2\"\n"
    "    ;;\n"
    "esac\n"
    "exit 0\n"
)
TRIO = ("systemctl daemon-reload", "systemd-sysext refresh", "systemctl reload dbus.service")
# README activation flow for a fresh set: trio, restart units of changed raws
# (manifest order), then enable every shim and restart the changed/new ones.
SEQ_FRESH = TRIO + (
    "systemctl restart foo.service",
    "systemctl enable sci-50-virt-ch-activation.service",
    "systemctl restart sci-50-virt-ch-activation.service",
    "systemctl enable sci-51-virt-qemu-activation.service",
    "systemctl restart sci-51-virt-qemu-activation.service",
)
# Only sci-50-virt-ch's raw changed: its units and shim are re-activated;
# sci-51-virt-qemu's shim is enabled but not restarted.
SEQ_CHANGED_RAW = TRIO + (
    "systemctl restart foo.service",
    "systemctl enable sci-50-virt-ch-activation.service",
    "systemctl restart sci-50-virt-ch-activation.service",
    "systemctl enable sci-51-virt-qemu-activation.service",
)
# A removal-only change: trio plus the unconditional enable of every remaining
# shim, no restarts (no raw or shim was deployed).
SEQ_REMOVE_ONLY = TRIO + ("systemctl enable sci-50-virt-ch-activation.service",)
def sh(argv, *, cwd=None, env=None, timeout=180):
    return subprocess.run([str(a) for a in argv], cwd=str(cwd or REPO), env=env, timeout=timeout, capture_output=True, text=True)
def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def write_stub(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
def entry_names(manifest_text: str) -> list[str]:
    names = []
    for line in manifest_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or re.match(r"^[A-Z][A-Z0-9_]*=", line):
            continue
        names.append(line.split()[0])
    return names
class _Serve:
    """A ThreadingHTTPServer on 127.0.0.1 that records every request path."""
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.paths: list[str] = []
        self._lock = threading.Lock()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"
    def start(self) -> None:
        self._thread.start()
    def reset(self) -> None:
        with self._lock:
            self.paths.clear()
    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)
def _make_handler(server: _Serve):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(server.directory), **kwargs)
        def do_GET(self):
            with server._lock:
                server.paths.append(self.path)
            super().do_GET()
        def log_message(self, format, *args):
            pass
    return Handler
@contextlib.contextmanager
def serve(directory: Path):
    server = _Serve(directory)
    server.start()
    try:
        yield server
    finally:
        server.stop()
@pytest.fixture(scope="module")
def keys(tmp_path_factory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("fetch-keys")
    def pair(name: str) -> tuple[Path, Path]:
        d = root / name
        d.mkdir()
        key, pub = d / "key.pem", d / "pub.pem"
        res = sh(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(key)])
        assert res.returncode == 0, res.stderr
        res = sh(["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(pub)])
        assert res.returncode == 0, res.stderr
        return key, pub
    key, pub = pair("primary")
    other_key, other_pub = pair("other")
    return {"key": key, "pub": pub, "other_key": other_key, "other_pub": other_pub}
def sign_sums(served: Path, key: Path) -> None:
    res = sh(["openssl", "dgst", "-sha256", "-sign", str(key), "-out", "SHA256SUMS.sig", "SHA256SUMS"], cwd=served)
    assert res.returncode == 0, res.stderr
def make_build(served: Path, manifest_text: str, key: Path, *, raw_content: dict[str, str] | None = None) -> None:
    """Write a fixture build (manifest, raws, shims) and its signed SHA256SUMS inventory."""
    if served.exists():
        shutil.rmtree(served)
    served.mkdir(parents=True)
    (served / "extensions.manifest").write_text(manifest_text, encoding="utf-8")
    entries = entry_names(manifest_text)
    raw_content = raw_content or {}
    for name in entries:
        (served / f"{name}.raw").write_text(raw_content.get(name, f"{name} raw content v1\n"), encoding="utf-8")
        (served / f"{name}-activation.service").write_text(SHIM_TEMPLATE.format(name=name), encoding="utf-8")
    files = ["extensions.manifest"]
    files += [f"{name}.raw" for name in entries]
    files += [f"{name}-activation.service" for name in entries]
    (served / "SHA256SUMS").write_text("".join(f"{sha256_file(served / fname)}  {fname}\n" for fname in files), encoding="utf-8")
    sign_sums(served, key)
def rehash_and_resign(served: Path, fname: str, key: Path) -> None:
    sums = served / "SHA256SUMS"
    lines = sums.read_text(encoding="utf-8").splitlines()
    lines = [f"{sha256_file(served / fname)}  {fname}" if line.rsplit(" ", 1)[-1] == fname else line for line in lines]
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sign_sums(served, key)
class FetchEnv:
    """One delivery-step invocation environment: host root, fetch root, stubs, calls log."""
    def __init__(self, root: Path):
        self.root = Path(root)
        self.host = self.root / "host"
        self.stubs = self.root / "stubs"
        self.calls_log = self.root / "calls.log"
        self.fetch_dir = self.root / "fetch"
        self.staging = self.fetch_dir / "staging"
        for tool in ("systemctl", "systemd-sysext", "nsenter"):
            write_stub(self.stubs, tool, LOG_STUB.replace("TOOL", tool))
        write_stub(self.stubs, "sleep", SLEEP_STUB)
        self.host.mkdir(parents=True)
        (self.host / "etc").mkdir()
        (self.host / "etc/os-release").write_text("ID=gardenlinux\nIMAGE_VERSION=2150.9.0\n", encoding="utf-8")
    def delivery_env(self, *, url: str, pub: Path, extra: dict | None = None) -> dict:
        # STAGING_DIR is deliberately not set: fetch mode must default it to
        # $FETCH_DIR/staging.
        env = {k: v for k, v in os.environ.items()
               if k not in ("HOST_ROOT", "STAGING_DIR", "INTERVAL", "NSENTER", "STUB_LOG")
               and not k.startswith("FETCH_")}
        env.update(HOST_ROOT=str(self.host), INTERVAL="0", NSENTER="0",
                   STUB_LOG=str(self.calls_log),
                   FETCH_URL=url, FETCH_PUBKEY=str(pub), FETCH_DIR=str(self.fetch_dir))
        env["PATH"] = os.pathsep.join([str(self.stubs), os.environ.get("PATH", "")])
        env.update({k: str(v) for k, v in (extra or {}).items()})
        return env
    def run(self, *, url: str, pub: Path, extra: dict | None = None) -> subprocess.CompletedProcess:
        self.calls_log.write_text("", encoding="utf-8")
        return sh(["bash", DELIVERY_SH], env=self.delivery_env(url=url, pub=pub, extra=extra))
    def calls(self) -> list[str]:
        return self.calls_log.read_text(encoding="utf-8").splitlines() if self.calls_log.is_file() else []
    def deployed(self) -> dict[str, bytes]:
        """Every host file under the two deployment trees, keyed by relative path."""
        found = {}
        for sub in ("var/lib/extensions", "etc/systemd/system"):
            base = self.host / sub
            if base.is_dir():
                for path in base.rglob("*"):
                    if path.is_file():
                        found[str(path.relative_to(self.host))] = path.read_bytes()
        return found
    def host_tree(self) -> dict[str, bytes]:
        return {str(p.relative_to(self.host)): p.read_bytes() for p in self.host.rglob("*") if p.is_file()}
def preplace(env: FetchEnv, names: tuple[str, ...]) -> dict[str, bytes]:
    """Pre-place raw+shim pairs on the host as a previous build would have left them."""
    placed = {}
    for name in names:
        raw = env.host / "var/lib/extensions" / f"{name}.raw"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(f"{name} raw content v1\n", encoding="utf-8")
        shim = env.host / "etc/systemd/system" / f"{name}-activation.service"
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text(SHIM_TEMPLATE.format(name=name), encoding="utf-8")
        placed[str(raw.relative_to(env.host))] = raw.read_bytes()
        placed[str(shim.relative_to(env.host))] = shim.read_bytes()
    return placed
def assert_placed(env: FetchEnv, placed: dict[str, bytes]) -> None:
    for rel, data in placed.items():
        path = env.host / rel
        assert path.is_file() and path.read_bytes() == data, f"{rel} was deleted or modified"
def assert_no_passwd_strays(env: FetchEnv) -> None:
    strays = [str(p) for p in env.root.rglob("*") if p.name == "passwd"]
    assert not strays, f"traversal wrote files outside staging: {strays}"
    # A relative write against the delivery step's working directory (the repo) would
    # land two levels up.
    assert not (REPO.resolve().parent.parent / "etc" / "passwd").exists(), "a traversing name escaped to the filesystem"
def expect_refused(env: FetchEnv, res: subprocess.CompletedProcess, *, placed: dict[str, bytes] | None = None) -> None:
    assert res.returncode != 0, f"the pass must fail, rc=0: {res.stdout}"
    assert "error: fetch:" in res.stderr, f"expected a '[sci-delivery] error: fetch: ...' message: {res.stderr}"
    if placed is None:
        assert env.deployed() == {}, "the refused pass performed host writes"
    else:
        assert env.deployed() == placed, "the refused pass changed the pre-placed host state (deletions or writes)"
    assert env.calls() == [], "the refused pass called host tools"
def test_fetch_installs_a_signed_set(tmp_path: Path, keys: dict[str, Path]):
    env = FetchEnv(tmp_path / "env")
    served = tmp_path / "served"
    make_build(served, MANIFEST, keys["key"])
    with serve(served) as http:
        res = env.run(url=http.url, pub=keys["pub"])
        assert res.returncode == 0, res.stderr or res.stdout
    # sums, sig, then every inventory file (manifest, raws, shims).
    assert len(http.paths) == 7, f"GETs: {http.paths}"
    assert set(http.paths[:2]) == SUMS_PATHS, f"the pass must download SHA256SUMS and SHA256SUMS.sig first: {http.paths}"
    assert set(http.paths[2:]) == {f"/{fname}" for fname in INVENTORY}, f"GETs: {http.paths}"
    for fname in INVENTORY:
        assert f"fetch: downloaded {fname}" in res.stdout, res.stdout
    assert any("fetch:" in line and "signature" in line.lower() for line in res.stdout.splitlines()), \
        f"no signature-verified fetch log line in: {res.stdout}"
    assert env.deployed() == {
        "var/lib/extensions/sci-50-virt-ch.raw": (served / "sci-50-virt-ch.raw").read_bytes(),
        "var/lib/extensions/sci-51-virt-qemu.raw": (served / "sci-51-virt-qemu.raw").read_bytes(),
        "etc/systemd/system/sci-50-virt-ch-activation.service": (served / "sci-50-virt-ch-activation.service").read_bytes(),
        "etc/systemd/system/sci-51-virt-qemu-activation.service": (served / "sci-51-virt-qemu-activation.service").read_bytes(),
    }, env.deployed()
    assert env.calls() == list(SEQ_FRESH), env.calls()
    # In fetch mode the staging directory defaults to $FETCH_DIR/staging.
    assert (env.staging / "SHA256SUMS").is_file(), "fetch staging is not under $FETCH_DIR/staging"
    for fname in INVENTORY:
        assert (env.staging / fname).read_bytes() == (served / fname).read_bytes(), fname
def test_fetch_is_converged_on_second_pass(tmp_path: Path, keys: dict[str, Path]):
    env = FetchEnv(tmp_path / "env")
    served = tmp_path / "served"
    make_build(served, MANIFEST, keys["key"])
    with serve(served) as http:
        first = env.run(url=http.url, pub=keys["pub"])
        assert first.returncode == 0, first.stderr or first.stdout
        http.reset()
        second = env.run(url=http.url, pub=keys["pub"])
        assert second.returncode == 0, second.stderr or second.stdout
    assert len(http.paths) == 2 and set(http.paths) == SUMS_PATHS, \
        f"a converged pass downloads exactly SHA256SUMS and SHA256SUMS.sig, got: {http.paths}"
    assert "no changes; nothing to do" in second.stdout, second.stdout
    assert "fetch: downloaded" not in second.stdout, second.stdout
    assert env.calls() == [], env.calls()
def test_fetch_downloads_only_the_changed_raw(tmp_path: Path, keys: dict[str, Path]):
    env = FetchEnv(tmp_path / "env")
    served = tmp_path / "served"
    make_build(served, MANIFEST, keys["key"])
    with serve(served) as http:
        first = env.run(url=http.url, pub=keys["pub"])
        assert first.returncode == 0, first.stderr or first.stdout
        (served / "sci-50-virt-ch.raw").write_text("sci-50-virt-ch raw content v2\n", encoding="utf-8")
        rehash_and_resign(served, "sci-50-virt-ch.raw", keys["key"])
        http.reset()
        second = env.run(url=http.url, pub=keys["pub"])
        assert second.returncode == 0, second.stderr or second.stdout
    assert len(http.paths) == 3 and set(http.paths) == SUMS_PATHS | {"/sci-50-virt-ch.raw"}, \
        f"the second pass must fetch sums, sig and only the changed raw, got: {http.paths}"
    assert "fetch: downloaded sci-50-virt-ch.raw" in second.stdout, second.stdout
    for fname in INVENTORY:
        if fname != "sci-50-virt-ch.raw":
            assert f"fetch: up to date {fname}" in second.stdout, second.stdout
    assert (env.host / "var/lib/extensions/sci-50-virt-ch.raw").read_bytes() == b"sci-50-virt-ch raw content v2\n"
    assert (env.host / "var/lib/extensions/sci-51-virt-qemu.raw").read_bytes() == (served / "sci-51-virt-qemu.raw").read_bytes()
    assert env.calls() == list(SEQ_CHANGED_RAW), env.calls()
def test_fetch_refuses_a_bad_signature(tmp_path: Path, keys: dict[str, Path]):
    env = FetchEnv(tmp_path / "env")
    served = tmp_path / "served"
    make_build(served, MANIFEST, keys["other_key"])  # signature from a different key
    with serve(served) as http:
        res = env.run(url=http.url, pub=keys["pub"])
    expect_refused(env, res)
    assert "signature" in res.stderr.lower(), f"the error must mention the signature: {res.stderr}"
def test_fetch_refuses_a_wrong_sums_digest(tmp_path: Path, keys: dict[str, Path]):
    served = tmp_path / "served"
    make_build(served, MANIFEST, keys["key"])
    good_digest = sha256_file(served / "SHA256SUMS")
    env_ok = FetchEnv(tmp_path / "env-good")
    with serve(served) as http:
        good = env_ok.run(url=http.url, pub=keys["pub"], extra={"FETCH_SUMS_DIGEST": good_digest})
        assert good.returncode == 0, f"the correct digest must be accepted: {good.stderr or good.stdout}"
    env = FetchEnv(tmp_path / "env-bad")
    with serve(served) as http:
        res = env.run(url=http.url, pub=keys["pub"], extra={"FETCH_SUMS_DIGEST": "0" * 64})
    expect_refused(env, res)
def test_fetch_refuses_a_traversing_name(tmp_path: Path, keys: dict[str, Path]):
    env = FetchEnv(tmp_path / "env")
    served = tmp_path / "served"
    make_build(served, MANIFEST, keys["key"])
    sums = served / "SHA256SUMS"
    lines = sums.read_text(encoding="utf-8").splitlines()
    lines[2] = f"{hashlib.sha256(b'traversal fixture\n').hexdigest()}  ../../etc/passwd"
    sums.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sign_sums(served, keys["key"])  # the signature stays valid: only the name is illegal
    with serve(served) as http:
        res = env.run(url=http.url, pub=keys["pub"])
    expect_refused(env, res)
    assert_no_passwd_strays(env)
def test_fetch_refuses_a_file_that_does_not_match_its_hash(tmp_path: Path, keys: dict[str, Path]):
    env = FetchEnv(tmp_path / "env")
    served = tmp_path / "served"
    make_build(served, MANIFEST, keys["key"])
    (served / "sci-50-virt-ch.raw").write_text("served bytes differ from the summed content\n", encoding="utf-8")
    with serve(served) as http:
        res = env.run(url=http.url, pub=keys["pub"])
    expect_refused(env, res)
def test_fetch_offline_keeps_the_deployed_set(tmp_path: Path, keys: dict[str, Path]):
    env = FetchEnv(tmp_path / "env")
    served = tmp_path / "served"
    make_build(served, MANIFEST, keys["key"])
    url = None
    with serve(served) as http:
        url = http.url
        first = env.run(url=http.url, pub=keys["pub"])
        assert first.returncode == 0, first.stderr or first.stdout
        assert env.calls() == list(SEQ_FRESH), env.calls()
        snapshot = env.host_tree()
    offline = env.run(url=url, pub=keys["pub"])
    assert offline.returncode != 0, f"an offline fetch pass must fail, rc=0: {offline.stdout}"
    assert "error: fetch:" in offline.stderr, offline.stderr
    assert env.host_tree() == snapshot, "the offline pass mutated the deployed set"
    assert env.calls() == [], f"the offline pass made host calls (incl. refresh): {env.calls()}"
def test_fetch_prunes_a_dropped_extension(tmp_path: Path, keys: dict[str, Path]):
    env = FetchEnv(tmp_path / "env")
    served = tmp_path / "served"
    make_build(served, MANIFEST_DROP_A, keys["key"])
    with serve(served) as http:
        first = env.run(url=http.url, pub=keys["pub"])
        assert first.returncode == 0, first.stderr or first.stdout
        # Build B drops sci-51-virt-qemu: the manifest tombstones it and the
        # signed inventory no longer lists its raw or shim.
        make_build(served, MANIFEST_DROP_B, keys["key"])
        second = env.run(url=http.url, pub=keys["pub"])
        assert second.returncode == 0, (
            f"the pass that drops an extension must succeed, rc={second.returncode}: "
            f"{second.stderr or second.stdout}")
        assert "has no manifest entry" not in second.stderr, (
            f"the pass died on a staging shim left over from build A: {second.stderr}")
        assert "removed sci-51-virt-qemu" in second.stdout, second.stdout
        calls_second = env.calls()
        assert not any("sci-51-virt-qemu" in call for call in calls_second), (
            f"units of a removed entry must never be touched: {calls_second}")
        http.reset()
        third = env.run(url=http.url, pub=keys["pub"])
        assert third.returncode == 0, third.stderr or third.stdout
    assert len(http.paths) == 2 and set(http.paths) == SUMS_PATHS, (
        f"the third pass must be a converged no-op that downloads only the sums: {http.paths}")
    assert "no changes; nothing to do" in third.stdout, third.stdout
    assert "fetch: downloaded" not in third.stdout, third.stdout
    assert not (env.host / "var/lib/extensions/sci-51-virt-qemu.raw").exists(), \
        "the dropped extension's raw was not removed from the host"
    assert not (env.host / "etc/systemd/system/sci-51-virt-qemu-activation.service").exists(), \
        "the dropped extension's activation shim was not removed from the host"
    stale = sorted(p.name for p in env.staging.glob("sci-51-virt-qemu*"))
    assert not stale, (
        f"$FETCH_DIR/staging still holds build-A files of the dropped extension: {stale}")
    assert env.deployed() == {
        "var/lib/extensions/sci-21-network.raw": (served / "sci-21-network.raw").read_bytes(),
        "etc/systemd/system/sci-21-network-activation.service": (served / "sci-21-network-activation.service").read_bytes(),
    }, env.deployed()
    assert env.calls() == [], f"the third pass made host calls: {env.calls()}"
def test_remove_directive_unmerges_an_extension(tmp_path: Path, keys: dict[str, Path]):
    env = FetchEnv(tmp_path / "env")
    served = tmp_path / "served"
    make_build(served, MANIFEST_REMOVE, keys["key"])
    preplace(env, NAMES)  # a previous build deployed both extensions
    with serve(served) as http:
        res = env.run(url=http.url, pub=keys["pub"])
        assert res.returncode == 0, res.stderr or res.stdout
    assert not (env.host / "var/lib/extensions/sci-51-virt-qemu.raw").exists(), "the removed raw was not deleted"
    assert not (env.host / "etc/systemd/system/sci-51-virt-qemu-activation.service").exists(), "the removed shim was not deleted"
    assert "removed sci-51-virt-qemu" in res.stdout, res.stdout
    assert env.deployed() == {
        "var/lib/extensions/sci-50-virt-ch.raw": (served / "sci-50-virt-ch.raw").read_bytes(),
        "etc/systemd/system/sci-50-virt-ch-activation.service": (served / "sci-50-virt-ch-activation.service").read_bytes(),
    }, env.deployed()
    assert env.calls() == list(SEQ_REMOVE_ONLY), env.calls()
    assert not any("sci-51-virt-qemu" in call for call in env.calls()), \
        f"units of a removed entry must never be restarted: {env.calls()}"
def test_remove_is_idempotent(tmp_path: Path, keys: dict[str, Path]):
    env = FetchEnv(tmp_path / "env")
    served = tmp_path / "served"
    make_build(served, MANIFEST_REMOVE, keys["key"])
    preplace(env, NAMES)
    with serve(served) as http:
        first = env.run(url=http.url, pub=keys["pub"])
        assert first.returncode == 0, first.stderr or first.stdout
        http.reset()
        second = env.run(url=http.url, pub=keys["pub"])
        assert second.returncode == 0, second.stderr or second.stdout
    assert len(http.paths) == 2 and set(http.paths) == SUMS_PATHS, \
        f"removing an absent extension is not a change; the second pass must be converged: {http.paths}"
    assert "no changes; nothing to do" in second.stdout, second.stdout
    assert "removed" not in second.stdout, second.stdout
    assert env.calls() == [], f"the second pass made host calls: {env.calls()}"
def test_remove_drops_the_enable_symlink(tmp_path: Path, keys: dict[str, Path]):
    env = FetchEnv(tmp_path / "env")
    # Real systemctl enable creates the multi-user.target.wants symlink the
    # activation shim's [Install] section asks for; the plain logging stub
    # would not, so this test swaps in a stub that reproduces both effects
    # (enable creates the symlink, disable removes it).
    write_stub(env.stubs, "systemctl", SYSTEMCTL_ENABLE_STUB)
    served = tmp_path / "served"
    make_build(served, MANIFEST_REMOVE, keys["key"])
    preplace(env, NAMES)  # a previous build deployed both extensions
    wants = env.host / "etc/systemd/system/multi-user.target.wants" / "sci-51-virt-qemu-activation.service"
    wants.parent.mkdir(parents=True, exist_ok=True)
    wants.symlink_to("../sci-51-virt-qemu-activation.service")  # the enable symlink a prior build left
    with serve(served) as http:
        res = env.run(url=http.url, pub=keys["pub"])
        assert res.returncode == 0, res.stderr or res.stdout
    assert "removed sci-51-virt-qemu" in res.stdout, res.stdout
    assert not wants.exists() and not wants.is_symlink(), \
        f"the enable symlink of the removed extension survived the REMOVE pass: {wants}"
    assert (env.host / "etc/systemd/system/multi-user.target.wants/sci-50-virt-ch-activation.service").is_symlink(), \
        "the stub created no enable symlink on 'systemctl enable': the test cannot exercise the defect"
def test_remove_refuses_a_listed_entry(tmp_path: Path, keys: dict[str, Path]):
    env = FetchEnv(tmp_path / "env")
    served = tmp_path / "served"
    make_build(served, MANIFEST_REMOVE_LISTED, keys["key"])
    placed = preplace(env, NAMES)
    with serve(served) as http:
        res = env.run(url=http.url, pub=keys["pub"])
    expect_refused(env, res, placed=placed)
    assert_placed(env, placed)
def test_remove_refuses_a_traversing_name(tmp_path: Path, keys: dict[str, Path]):
    env = FetchEnv(tmp_path / "env")
    served = tmp_path / "served"
    make_build(served, MANIFEST_REMOVE_TRAVERSE, keys["key"])
    placed = preplace(env, NAMES)
    with serve(served) as http:
        res = env.run(url=http.url, pub=keys["pub"])
    expect_refused(env, res, placed=placed)
    assert_placed(env, placed)
    assert_no_passwd_strays(env)
def test_help_documents_fetch_and_remove():
    res = sh(["bash", DELIVERY_SH, "--help"], env=dict(os.environ))
    assert res.returncode == 0, res.stderr
    for token in ("FETCH_URL", "FETCH_PUBKEY", "FETCH_SUMS_DIGEST", "FETCH_DIR", "REMOVE="):
        assert token in res.stdout, f"--help does not document {token!r}"
