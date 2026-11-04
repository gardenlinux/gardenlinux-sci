"""Host-safe contract tests: the real sysext-virt scripts run against tiny
fixtures with host-mutating tools stubbed. No lib imports, builds, VMs, docker
or network; negative cases pin fail-closed behavior.
"""
from __future__ import annotations
import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
import pytest
REPO = Path(__file__).resolve().parents[2]
BASE_IMAGE_SH, MKOSI_CONF = REPO / "mkosi/base-image.sh", REPO / "mkosi/mkosi.conf"
DELIVERY_SH = REPO / "delivery/sci-delivery.sh"
MANIFEST = REPO / "delivery/extensions.manifest"
FLAVOR, ARCH, COMMIT = "sysext-fixture", "amd64", "a" * 40
TRIO = ("systemctl daemon-reload", "systemd-sysext refresh", "systemctl reload dbus.service")
SHIM_TEMPLATE = ("[Unit]\nDescription=SCI {name} activation (host-contracts fixture)\nAfter=systemd-sysext.service\n"
                 "[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=/usr/bin/true\n"
                 "[Install]\nWantedBy=multi-user.target\n")
FIXTURE_MANIFEST = "TARGET_BASE=2150\nsci-alpha foo-one.service foo-two.service\nsci-beta\n"
LOG_STUB = "#!/bin/sh\nprintf 'TOOL %s\\n' \"$*\" >> \"${STUB_LOG:-/dev/null}\"\nexit 0\n"
def seq(*rest, enabled=("sci-alpha",)):
    return TRIO + rest + tuple(f"systemctl {verb} {name}-activation.service" for name in enabled for verb in ("enable", "restart"))
SEQ_FRESH = seq("systemctl restart foo-one.service foo-two.service")
SEQ_SHIM = seq()
SEQ_NEW = TRIO + ("systemctl restart gamma-one.service", "systemctl enable sci-alpha-activation.service")
SEQ_TOLERATED = seq("systemctl restart foo-one.service foo-two.service", "systemctl restart multipathd.service iscsid.service", enabled=("sci-alpha", "sci-beta"))
def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
def sh(argv, *, cwd=None, env=None, timeout=180):
    return subprocess.run([str(a) for a in argv], cwd=str(cwd or REPO), env=env, timeout=timeout, capture_output=True, text=True)
def last_line(text: str) -> str:
    return text.rstrip("\n").splitlines()[-1].strip()
def tree_digest(tree: Path) -> str:
    proc = subprocess.run(["tar", "-C", str(tree), "-cf", "-", "--sort=name", "--owner=0", "--group=0", "--numeric-owner", "--mtime=@0", "."], check=True, capture_output=True)
    return hashlib.sha256(proc.stdout).hexdigest()
def sleeping(started: Path) -> bool:
    try:
        return Path(f"/proc/{int(started.read_text(encoding='utf-8').strip())}/comm").read_text(encoding="utf-8").strip() == "sleep"
    except (OSError, ValueError):
        return False
def release() -> str:
    found = re.findall(r"^Release=(.*)$", MKOSI_CONF.read_text(encoding="utf-8"), re.M)
    assert len(found) == 1, f"mkosi/mkosi.conf must carry exactly one Release= line: {found}"
    return found[0]
def write_stub(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
def manifest_names(path: Path = MANIFEST) -> list[str]:
    names = [l.split()[0] for l in path.read_text(encoding="utf-8").splitlines() if l.strip() and not l.lstrip().startswith("#") and not re.match(r"^[A-Z][A-Z0-9_]*=\S*$", l)]
    assert names, f"{path} carries no entries"
    return names

@pytest.fixture
def base_artifact(tmp_path: Path) -> dict:
    version = release()
    rootfs = tmp_path / "rootfs"
    (rootfs / "etc").mkdir(parents=True)
    (rootfs / "boot").mkdir()
    (rootfs / "etc/os-release").write_text(f"ID=gardenlinux\nID_LIKE=debian\nIMAGE_VERSION={version}\nGARDENLINUX_VERSION={version}\nGARDENLINUX_COMMIT_ID_LONG={COMMIT}\nVARIANT_ID={FLAVOR}-{ARCH}\n", encoding="utf-8")
    (rootfs / "etc/machine-id").write_text("uninitialized\n", encoding="utf-8")
    (rootfs / "boot/vmlinuz-1.0.0").write_text("fixture kernel\n", encoding="utf-8")
    (rootfs / "boot/initrd.img-1.0.0").write_text("fixture initrd\n", encoding="utf-8")
    inner = f"{FLAVOR}-{ARCH}-{version}-{COMMIT[:8]}.tar"
    sh(["tar", "-cf", tmp_path / inner, "."], cwd=rootfs)
    outer = tmp_path / "outer.tar.xz"
    sh(["tar", "-C", tmp_path, "-cJf", outer, "--", inner])
    pin = tmp_path / "pin"
    pin.write_text(f"FLAVOR={FLAVOR}\nARCH={ARCH}\nVERSION={version}\nCOMMIT={COMMIT}\nTARBALL_SHA256={sha256_file(outer)}\nROOTFS_TAR_SHA256={sha256_file(tmp_path / inner)}\nURL=https://github.com/gardenlinux/gardenlinux/releases/download/{version}/{inner}.xz\n", encoding="utf-8")
    return {"rootfs": rootfs, "inner": inner, "outer": outer, "pin": pin}
def base_tool(pin: Path, args, cache: Path, tarball: Path | None = None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("BASE_IMAGE_")}
    env.update(BASE_IMAGE_PIN=str(pin), BASE_IMAGE_DIR=str(cache), BASE_IMAGE_OFFLINE="1")
    if tarball is not None:
        env["BASE_IMAGE_TARBALL"] = str(tarball)
    return sh(["bash", BASE_IMAGE_SH, *args], env=env)
def variant_pin(fx: dict, tmp_path: Path, tree: Path) -> tuple[Path, Path]:
    inner = tmp_path / fx["inner"]
    inner.unlink()
    sh(["tar", "-cf", inner, "."], cwd=tree)
    outer = tmp_path / "variant.tar.xz"
    sh(["tar", "-C", tmp_path, "-cJf", outer, "--", fx["inner"]])
    text = fx["pin"].read_text(encoding="utf-8")
    text = re.sub(r"^TARBALL_SHA256=.*$", f"TARBALL_SHA256={sha256_file(outer)}", text, flags=re.M)
    text = re.sub(r"^ROOTFS_TAR_SHA256=.*$", f"ROOTFS_TAR_SHA256={sha256_file(inner)}", text, flags=re.M)
    (tmp_path / "variant.pin").write_text(text, encoding="utf-8")
    return tmp_path / "variant.pin", outer
def assert_cache_links(cache: Path, tree: Path) -> None:
    assert (cache / "current").is_symlink() and os.readlink(cache / "current") == tree.name and (cache / "current").resolve() == tree, f"cache/current does not resolve to {tree}"
    for link, boot in (("current-kernel", "vmlinuz-1.0.0"), ("current-initrd", "initrd.img-1.0.0")):
        assert (cache / link).is_symlink() and os.readlink(cache / link) == f"{tree.name}/boot/{boot}" and (cache / link).resolve() == tree / "boot" / boot, f"{link} does not resolve to {tree}/boot/{boot}"
def test_base_clean_extraction(base_artifact, tmp_path: Path):
    fx = base_artifact
    cache = tmp_path / "cache"
    res = base_tool(fx["pin"], ("ensure",), cache, tarball=fx["outer"])
    tree = Path(last_line(res.stdout))
    assert res.returncode == 0 and tree.is_dir() and tree_digest(tree) == tree_digest(fx["rootfs"]), res.stderr
    assert_cache_links(cache, tree)
    dest = tmp_path / "extract"
    res = base_tool(fx["pin"], ("extract", str(dest)), tmp_path / "cache2", tarball=fx["outer"])
    assert res.returncode == 0 and last_line(res.stdout) == tree_digest(tree) == tree_digest(dest), res.stderr
    again = base_tool(fx["pin"], ("ensure",), cache)  # offline cache hit, no tarball
    assert again.returncode == 0 and last_line(again.stdout) == str(tree), again.stderr
    assert_cache_links(cache, tree)

PIN_DEFECTS = [
    ("missing-flavor", lambda t: t.replace(f"FLAVOR={FLAVOR}\n", "", 1), "exactly one FLAVOR"),
    ("duplicate-url", lambda t: t + "URL=stale\n", "exactly one URL"),
    ("bad-commit", lambda t: re.sub(r"^COMMIT=.*$", "COMMIT=abc123", t, flags=re.M), "40-hex-digit commit"),
    ("short-outer-sha", lambda t: re.sub(r"^TARBALL_SHA256=.*$", "TARBALL_SHA256=" + "0" * 63, t, flags=re.M), "64-hex-digit sha256"),
    ("url-skew", lambda t: re.sub(r"^URL=.*$", "URL=https://evil.invalid/x.tar.xz", t, flags=re.M), "URL derived"),
    ("version-skew", lambda t: re.sub(r"^VERSION=.*$", "VERSION=9999.9.9", t, flags=re.M), "does not match Release="),
]
@pytest.mark.parametrize("defect,mutate,fragment", PIN_DEFECTS, ids=[d for d, *_ in PIN_DEFECTS])
def test_base_pin_refused(base_artifact, tmp_path: Path, defect, mutate, fragment):
    fx = base_artifact
    pin = tmp_path / "pin2"
    pin.write_text(mutate(fx["pin"].read_text(encoding="utf-8")), encoding="utf-8")
    res = base_tool(pin, ("ensure",), tmp_path / "cache")  # offline, no tarball: only validation can pass
    assert res.returncode != 0 and fragment in res.stderr, res.stderr
    assert not (tmp_path / "cache").exists(), "the cache was created before the refusal (not fail-closed)"
@pytest.mark.parametrize("kind", ["outer", "inner"])
def test_base_corrupt_refused(base_artifact, tmp_path: Path, kind):
    fx = base_artifact
    if kind == "outer":
        bad, data = tmp_path / "bad.tar.xz", bytearray(fx["outer"].read_bytes())
        data[0] ^= 0xFF
        bad.write_bytes(data)
        pin, expected = fx["pin"], sha256_file(fx["outer"])
    else:
        rogue = tmp_path / "rogue"
        shutil.copytree(fx["rootfs"], rogue)
        (rogue / "etc/rogue-file").write_text("rogue\n", encoding="utf-8")
        sh(["tar", "-cf", tmp_path / fx["inner"], "."], cwd=rogue)
        sh(["tar", "-C", tmp_path, "-cJf", tmp_path / "outer2.tar.xz", "--", fx["inner"]])
        bad, pin = tmp_path / "outer2.tar.xz", tmp_path / "pin2"
        pin.write_text(re.sub(r"^TARBALL_SHA256=.*$", f"TARBALL_SHA256={sha256_file(bad)}", fx["pin"].read_text(encoding="utf-8"), flags=re.M), encoding="utf-8")
        expected = sha256_file(tmp_path / fx["inner"])
    res = base_tool(pin, ("ensure",), tmp_path / "cache", tarball=bad)
    assert res.returncode != 0 and "sha256 mismatch" in res.stderr and expected in res.stderr, res.stderr
@pytest.mark.parametrize("kind", ["offline", "nonempty"])
def test_base_refused_without_side_effects(base_artifact, tmp_path: Path, kind):
    fx = base_artifact
    if kind == "offline":
        res = base_tool(fx["pin"], ("ensure",), tmp_path / "cache")
        needle = "offline"
    else:
        dest = tmp_path / "occupied"
        dest.mkdir()
        (dest / "stray.txt").write_text("stray\n", encoding="utf-8")
        res = base_tool(fx["pin"], ("extract", str(dest)), tmp_path / "cache", tarball=fx["outer"])
        needle = "not empty"
    assert res.returncode != 0 and needle in res.stderr, res.stderr
def test_base_wrong_identity_refused(base_artifact, tmp_path: Path):
    fx = base_artifact
    rel = fx["rootfs"] / "etc/os-release"
    rel.write_text(rel.read_text(encoding="utf-8") + "VERSION_ID=99\n", encoding="utf-8")
    pin, outer = variant_pin(fx, tmp_path, fx["rootfs"])
    res = base_tool(pin, ("ensure",), tmp_path / "cache", tarball=outer)
    assert res.returncode != 0 and "carries VERSION_ID" in res.stderr, f"the VERSION_ID tree was not refused (rc={res.returncode}): {res.stderr}"
    assert not list((tmp_path / "cache").glob("*.ready")), "a rejected tree was marked ready"
def test_base_cache_marker_reextracts(base_artifact, tmp_path: Path):
    fx = base_artifact
    cache = tmp_path / "cache"
    first = base_tool(fx["pin"], ("ensure",), cache, tarball=fx["outer"])
    assert first.returncode == 0, first.stderr
    tree = Path(last_line(first.stdout))
    (fx["rootfs"] / "etc/rogue-file").write_text("rogue\n", encoding="utf-8")
    pin, outer = variant_pin(fx, tmp_path, fx["rootfs"])
    second = base_tool(pin, ("ensure",), cache, tarball=outer)
    assert second.returncode == 0 and Path(last_line(second.stdout)) == tree, second.stderr
    rogue = tree / "etc/rogue-file"
    assert rogue.is_file() and rogue.read_text(encoding="utf-8") == "rogue\n", "the stale same-key tree was reused"
    assert (cache / f"{tree.name}.ready").read_text(encoding="utf-8").strip() == sha256_file(tmp_path / fx["inner"])

# delivery/sci-delivery.sh (the reconciling delivery step)
class DeliveryEnv:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.host, self.staging, self.stubs = self.root / "host", self.root / "staging", self.root / "stubs"
        self.calls_log = self.root / "calls.log"
        for tool in ("systemctl", "systemd-sysext", "nsenter", "docker"):
            write_stub(self.stubs, tool, LOG_STUB.replace("TOOL", tool))
        write_stub(self.stubs, "sleep", "#!/bin/sh\nprintf '%s\\n' \"$$\" >\"${STUB_SLEEP_STARTED:-/dev/null}\"\nexec /bin/sleep \"$@\"\n")
        self.host.mkdir(parents=True)
        self.staging.mkdir()
        for name in ("sci-alpha", "sci-beta"):
            (self.staging / f"{name}.raw").write_text(f"{name} raw content v1\n", encoding="utf-8")
        (self.staging / "sci-alpha-activation.service").write_text(SHIM_TEMPLATE.format(name="alpha"), encoding="utf-8")
        self.set_manifest(FIXTURE_MANIFEST)
    def set_manifest(self, text: str) -> None:
        (self.staging / "extensions.manifest").write_text(text, encoding="utf-8")
        names = manifest_names(self.staging / "extensions.manifest")
        (self.staging / "SHA256SUMS").write_text("".join(f"{sha256_file(self.staging / (n + '.raw'))}  {n}.raw\n" for n in names), encoding="utf-8")
    def failing(self, guard: str) -> Path:
        directory = self.root / "failing"
        write_stub(directory, "systemctl", LOG_STUB.replace("TOOL", "systemctl").replace("exit 0\n", f"{guard}\nexit 0\n"))
        return directory
    def os_release(self, version="2150.10.3", *, quoted=False, fallback_only=False) -> None:
        value = f'"{version}"' if quoted else version
        (self.host / "etc").mkdir(parents=True, exist_ok=True)
        image = "" if fallback_only else f"IMAGE_VERSION={value}\n"
        (self.host / "etc/os-release").write_text(f"ID=gardenlinux\n{image}GARDENLINUX_VERSION={value}\n", encoding="utf-8")
    def env(self, *, interval=0, nsenter=0, extra=None, path_prefix=None) -> dict:
        env = dict(os.environ, HOST_ROOT=str(self.host), STAGING_DIR=str(self.staging), INTERVAL=str(interval), NSENTER=str(nsenter), STUB_LOG=str(self.calls_log))
        env["PATH"] = os.pathsep.join(([str(path_prefix)] if path_prefix else []) + [str(self.stubs), os.environ.get("PATH", "")])
        return env | {k: str(v) for k, v in (extra or {}).items()}
    def run(self, **kwargs):
        self.calls_log.write_text("", encoding="utf-8")
        res = sh(["bash", DELIVERY_SH], env=self.env(**kwargs))
        return res.returncode, res.stdout, res.stderr, self.calls()
    def calls(self) -> list[str]:
        return self.calls_log.read_text(encoding="utf-8").splitlines() if self.calls_log.is_file() else []
    def host_state(self) -> dict:
        return {str(p.relative_to(self.host)): (p.stat().st_mtime_ns, p.stat().st_size) for p in self.host.rglob("*") if p.is_file()}
def check(env, *, rc=0, calls=None, before=None, errors=(), changed=None, files=(), **kwargs):
    before = env.host_state() if before is None else before
    code, out, err, seen = env.run(**kwargs)
    assert code == rc, f"rc={code}, expected {rc}: {err or out}"
    assert calls is None or seen == list(calls), f"calls differ:\n  {seen}"
    assert all(needle in err for needle in errors), err
    if changed is not None:
        after = env.host_state()
        assert {p for p in set(before) | set(after) if before.get(p) != after.get(p)} == set(changed)
    for rel in files:
        assert (env.host / rel).read_bytes() == (env.staging / Path(rel).name).read_bytes(), rel
    return out, err, seen, before

def deploy(env) -> None:
    env.os_release()
    assert env.run()[0] == 0

@pytest.fixture
def delivery(tmp_path: Path) -> DeliveryEnv:
    return DeliveryEnv(tmp_path / "delivery")
@pytest.mark.parametrize("nsenter", [0, 1], ids=["direct", "nsenter"])
def test_delivery_fresh(delivery, nsenter):
    delivery.os_release()
    expected = SEQ_FRESH if not nsenter else tuple(f"nsenter -t 1 -m -- {c}" for c in SEQ_FRESH)
    check(delivery, calls=expected, nsenter=nsenter, files=("var/lib/extensions/sci-alpha.raw", "var/lib/extensions/sci-beta.raw", "etc/systemd/system/sci-alpha-activation.service"))
@pytest.mark.parametrize("kind", ["noop", "foreign", "raw", "shim", "new"])
def test_delivery_reconcile(delivery, kind):
    deploy(delivery)
    legacy = delivery.host / "var/lib/extensions/sci-legacy.raw"
    shim_dir = delivery.host / "etc/systemd/system"
    shim_mtime = shim_dir.stat().st_mtime_ns if kind == "shim" else None
    if kind == "foreign":
        legacy.write_text("legacy raw\n", encoding="utf-8")
    if kind == "new":
        (delivery.staging / "sci-gamma.raw").write_text("sci-gamma raw content v1\n", encoding="utf-8")
        delivery.set_manifest(FIXTURE_MANIFEST + "sci-gamma gamma-one.service\n")
        target, calls = "var/lib/extensions/sci-gamma.raw", SEQ_NEW
    elif kind in ("raw", "shim"):
        target = "var/lib/extensions/sci-alpha.raw" if kind == "raw" else "etc/systemd/system/sci-alpha-activation.service"
        (delivery.host / target).write_text("drifted\n", encoding="utf-8")
        calls = SEQ_FRESH if kind == "raw" else SEQ_SHIM
    else:
        target, calls = None, []
    check(delivery, calls=calls, before=delivery.host_state(), changed=(target,) if target else (), files=(target,) if target else ())
    assert kind != "foreign" or legacy.read_text(encoding="utf-8") == "legacy raw\n"
    assert shim_mtime is None or shim_dir.stat().st_mtime_ns == shim_mtime, "the shim directory mtime changed"
def stage(*, version="2150.10.3", manifest=None, edit=None, host_root=False, gate_absent=False):
    def apply(env):
        env.os_release(version)
        if manifest is not None:
            (env.staging / "extensions.manifest").write_text(manifest, encoding="utf-8")
        if edit:
            edit(env)
        return {"HOST_ROOT": str(env.root / "no-such-root")} if host_root else None
    apply.gate_absent = gate_absent
    return apply
def drop_beta_sum(env) -> None:
    sums = env.staging / "SHA256SUMS"
    sums.write_text(next(l for l in sums.read_text(encoding="utf-8").splitlines() if "sci-alpha" in l) + "\n", encoding="utf-8")

REFUSED = [
    ("whitespace", stage(manifest="   \t\n"), "whitespace-only"),
    ("unknown-directive", stage(manifest="FOO=1\n"), "unknown manifest directive"),
    ("no-target-base", stage(manifest="sci-alpha\n"), "no TARGET_BASE"),
    ("duplicate-target-base", stage(version="1877.26.1", manifest="TARGET_BASE=2150\nTARGET_BASE=1877\nsci-alpha foo-one.service foo-two.service\n"), "duplicate TARGET_BASE"),
    ("missing-raw", stage(manifest="TARGET_BASE=2150\nsci-missing\n"), "has no staging raw"),
    ("unlisted-shim", stage(edit=lambda e: (e.staging / "sci-rogue-activation.service").write_text("[Unit]\n")), "has no manifest entry"),
    ("missing-manifest", stage(edit=lambda e: (e.staging / "extensions.manifest").unlink()), "missing manifest"),
    ("bad-name", stage(manifest="TARGET_BASE=2150\nsci/alpha\n"), "invalid name"),
    ("host-root", stage(host_root=True), "not an existing directory"),
    ("missing-sums", stage(edit=lambda e: (e.staging / "SHA256SUMS").unlink()), "missing SHA256SUMS"),
    ("missing-line", stage(edit=drop_beta_sum), "no line for manifest entry"),
    ("corrupt-raw", stage(edit=lambda e: (e.staging / "sci-alpha.raw").write_text("CORRUPTED\n")), "does not match its SHA256SUMS line"),
    ("before-gate", stage(version="1877.26.1", gate_absent=True, edit=lambda e: (e.staging / "sci-alpha.raw").write_text("CORRUPTED\n")), "does not match its SHA256SUMS line"),
]
@pytest.mark.parametrize("defect,setup,needle", REFUSED, ids=[d for d, *_ in REFUSED])
def test_delivery_refused(delivery, defect, setup, needle):
    extra = setup(delivery) or {}
    _out, err, _seen, _before = check(delivery, rc=1, calls=[], before=delivery.host_state(), errors=(needle,), changed=(), extra=extra)
    assert not setup.gate_absent or "base-version gate" not in err, "the gate ran before the integrity check"
    assert not extra or not (delivery.root / "no-such-root").exists(), "the delivery step created HOST_ROOT"
@pytest.mark.parametrize("scenario", ["accept-quoted", "accept-fallback", "reject-1877", "reject-fallback-1877", "reject-missing-os-release", "reject-already-deployed"])
def test_delivery_gate(delivery, scenario):
    if scenario.startswith("accept-"):
        delivery.os_release(quoted=scenario == "accept-quoted", fallback_only=scenario == "accept-fallback")
        out = check(delivery, calls=SEQ_FRESH)[0]
        assert scenario != "accept-fallback" or "host Garden Linux line 2150 matches TARGET_BASE=2150" in out
        return
    if scenario == "reject-already-deployed":
        deploy(delivery)
        delivery.os_release("1877.26.1")
    elif scenario != "reject-missing-os-release":
        delivery.os_release("1877.26.1", fallback_only=scenario == "reject-fallback-1877")
    needles = ("2150",) if scenario == "reject-missing-os-release" else ("1877", "2150")
    err = check(delivery, rc=1, calls=[], before=delivery.host_state(), errors=needles, changed=())[1]
    assert scenario != "reject-missing-os-release" or "os-release" in err, err

@pytest.mark.parametrize("failure", ["trio", "restart"])
def test_delivery_activation_failure(delivery, failure):
    delivery.os_release()
    marker = delivery.host / "var/lib/extensions/.sci-delivery-pending"
    flag = delivery.root / "fail-daemon-reload"
    if failure == "restart":
        (delivery.staging / "sci-beta-activation.service").write_text(SHIM_TEMPLATE.format(name="beta"), encoding="utf-8")
        delivery.set_manifest("TARGET_BASE=2150\nsci-alpha foo-one.service foo-two.service\nsci-beta multipathd.service iscsid.service\n")
        guard = 'if [ "$*" = "restart multipathd.service iscsid.service" ]; then exit 1; fi'
    else:
        flag.write_text("", encoding="utf-8")
        guard = f'if [ -e "{flag}" ] && [ "$1" = daemon-reload ]; then exit 1; fi'
    expected = ["systemctl daemon-reload"] if failure == "trio" else SEQ_TOLERATED
    errors = ("sci-beta", "multipathd.service", "restart failed") if failure == "restart" else ()
    files = ("var/lib/extensions/sci-beta.raw", "etc/systemd/system/sci-beta-activation.service") if failure == "restart" else ()
    check(delivery, rc=1, calls=expected, path_prefix=delivery.failing(guard), errors=errors, files=files)
    if failure == "restart":
        assert not marker.exists(), "a completed flow left the pending-activation marker"
    else:
        assert marker.is_file(), "the interrupted pass left no pending-activation marker"
        flag.unlink()
        check(delivery, calls=SEQ_FRESH)
        assert not marker.exists(), "the reconciliation pass left the marker behind"
def test_delivery_interval_term(delivery):
    delivery.os_release()
    started = delivery.root / "sleep.started"
    check(delivery, calls=SEQ_FRESH, extra={"STUB_SLEEP_STARTED": str(started)})
    assert not started.exists(), "INTERVAL=0 still invoked sleep"
    loop = DeliveryEnv(delivery.root / "loop")
    loop.os_release()
    started = loop.root / "sleep.started"
    interval = 8  # real seconds: a foreground sleep defers the trap for ~this long
    proc = subprocess.Popen(["bash", str(DELIVERY_SH)], cwd=str(REPO), env=loop.env(interval=interval, extra={"STUB_SLEEP_STARTED": str(started)}), start_new_session=True)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not sleeping(started):
            assert proc.poll() is None, f"the delivery step exited before the interval sleep (rc={proc.returncode})"
            time.sleep(0.05)
        assert sleeping(started), "the delivery step never entered the real interval sleep"
        term_at = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        try:
            rc = proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            assert False, "the delivery step did not exit promptly after SIGTERM (a foreground sleep defers the trap)"
        elapsed = time.monotonic() - term_at
        assert rc == 0 and elapsed < 2.0 < interval, f"TERM->exit {elapsed:.2f}s (rc={rc}, interval={interval})"
        assert loop.calls() == list(SEQ_FRESH)
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if proc.poll() is None:
            proc.wait(timeout=5)

# delivery/build-image.sh (packaging)

FIXTURE_HEAD = "abc1234"  # the stubbed HEAD keeps a reintroduced versioned tag observable in the tag assertions

def _mini_repo(root: Path) -> Path:
    shutil.copytree(REPO / "delivery", root / "delivery")
    (root / "mkosi").mkdir()
    (root / "mkosi/mkosi.conf").write_text(f"Release={release()}\n", encoding="utf-8")
    output = root / ".build" / "mkosi" / "output"
    output.mkdir(parents=True)
    for name in manifest_names():
        (output / f"{name}.raw").write_text(f"fake raw {name}\n", encoding="utf-8")
        activation = root / "mkosi/mkosi.images" / name / "mkosi.extra/usr/share/sci/activation"
        activation.mkdir(parents=True)
        (activation / f"{name}-activation.service").write_text(SHIM_TEMPLATE.format(name=name), encoding="utf-8")
    return root

def _run_build_image(repo: Path, stubs: Path, log: Path,
                     args: list[str] | None = None, env_extra: dict[str, str] | None = None):
    log.write_text("", encoding="utf-8")
    write_stub(stubs, "docker", LOG_STUB.replace("TOOL", "docker"))
    # A stubbed HEAD and the fixture Release= make a versioned tag reachable, so
    # the tag assertions below pin that the image carries only $IMAGE_TAG.
    write_stub(stubs, "git", f"#!/bin/sh\ncase \"$*\" in *rev-parse*) echo {FIXTURE_HEAD};; esac\nexit 0\n")
    env = dict(os.environ, STUB_LOG=str(log), **(env_extra or {}))
    env["PATH"] = os.pathsep.join([str(stubs), os.environ.get("PATH", "")])
    return sh(["bash", "delivery/build-image.sh", *(args or [])], cwd=repo, env=env, timeout=120)

def assert_staging(context: Path, repo: Path) -> None:
    """Assert the staged delivery set: delivery step, raws, shims, manifest and checksums.

    Takes the fixture repository that produced the raws the staging is compared to.
    """
    names = manifest_names()
    delivery_step = context / "sci-delivery.sh"
    assert delivery_step.is_file() and delivery_step.read_bytes() == DELIVERY_SH.read_bytes() and delivery_step.stat().st_mode & 0o111, \
        "the staging must carry sci-delivery.sh byte-identical to delivery/sci-delivery.sh and executable"
    for name in names:
        built = repo / ".build/mkosi/output" / f"{name}.raw"
        assert (context / f"{name}.raw").read_bytes() == built.read_bytes(), \
            f"the staging must carry {name}.raw byte-identical to .build/mkosi/output/{name}.raw"
        assert (context / f"{name}-activation.service").is_file(), \
            f"the staging must carry the {name}-activation.service shim"
    assert (context / "extensions.manifest").read_bytes() == MANIFEST.read_bytes(), \
        "the staging must carry extensions.manifest byte-identical to delivery/extensions.manifest"
    sums = context / "SHA256SUMS"
    assert sums.is_file(), "the staging must carry SHA256SUMS"
    recorded = {line.split()[1] for line in sums.read_text(encoding="utf-8").splitlines() if line.split()}
    missing = [f"{name}.raw" for name in names if f"{name}.raw" not in recorded]
    assert not missing, f"SHA256SUMS must record a line for every manifest raw, missing: {missing}"
    check = sh(["sha256sum", "-c", "--", "SHA256SUMS"], cwd=context)
    assert check.returncode == 0, f"sha256sum -c SHA256SUMS must verify every staged raw: {check.stdout}{check.stderr}"

@pytest.mark.parametrize("variant", ["complete", "missing-raw"])
def test_build_image(tmp_path: Path, variant):
    names = manifest_names()
    repo = _mini_repo(tmp_path / "repo")
    if variant == "missing-raw":
        (repo / ".build/mkosi/output" / f"{names[0]}.raw").unlink()
    log = tmp_path / "docker.log"
    res = _run_build_image(repo, tmp_path / "stubs", log)
    lines = [l for l in log.read_text(encoding="utf-8").splitlines() if l.startswith("docker ")]
    if variant == "missing-raw":
        assert res.returncode != 0 and lines == [] and names[0] in res.stderr, res.stderr
        return
    assert res.returncode == 0 and len(lines) == 1, res.stderr or lines
    tokens = lines[0].split()
    tags = [tokens[i + 1] for i, token in enumerate(tokens) if token == "-t"]
    assert tags == ["sci-delivery:latest"], f"the default build must tag exactly sci-delivery:latest, found: {tags}"
    context = Path(tokens[-1])
    context = context if context.is_absolute() else repo / context
    assert_staging(context, repo)


def test_build_image_stage_only(tmp_path: Path):
    """--stage-only assembles the validated staging set without any image build.

    The raws artifact publishes the staging directory as plain files, so the
    staging must be reachable without docker: same manifest validation, same
    staged SHA256SUMS contract, no image build, no tags.
    """
    repo = _mini_repo(tmp_path / "repo")
    log = tmp_path / "docker.log"
    res = _run_build_image(repo, tmp_path / "stubs", log, args=["--stage-only"])
    assert res.returncode == 0, res.stderr
    docker_calls = [l for l in log.read_text(encoding="utf-8").splitlines()
                    if l.startswith("docker ")]
    assert docker_calls == [], "the --stage-only run must not invoke docker, expected no docker calls"
    assert_staging(repo / ".build" / "delivery" / "staging", repo)


def test_build_image_from_staging(tmp_path: Path):
    """--from-staging <dir> builds the image from an already-staged directory.

    The integration job rebuilds the delivery image from the raws artifact
    instead of downloading a produced image. The shipped staging must verify
    as-is (SHA256SUMS is checked, never regenerated) and the build carries only
    `sci-delivery:latest`: the image is a test artifact, not the product.
    """
    repo = _mini_repo(tmp_path / "repo")
    staging = repo / ".build" / "delivery" / "raws"
    staged = _run_build_image(repo, tmp_path / "stubs", tmp_path / "stage.log",
                              args=["--stage-only"], env_extra={"STAGING_OUT": str(staging)})
    assert staged.returncode == 0, staged.stderr
    assert_staging(staging, repo)

    log = tmp_path / "docker.log"
    res = _run_build_image(repo, tmp_path / "stubs", log,
                           args=["--from-staging", str(staging)])
    assert res.returncode == 0, res.stderr
    calls = [l for l in log.read_text(encoding="utf-8").splitlines() if l.startswith("docker ")]
    assert len(calls) == 1, f"--from-staging must run exactly one docker build, found: {calls}"
    tokens = calls[0].split()
    tags = [tokens[i + 1] for i, token in enumerate(tokens) if token == "-t"]
    assert tags == ["sci-delivery:latest"], \
        f"the from-staging build must tag exactly sci-delivery:latest, found: {tags}"
    assert tokens[-1] == str(staging), \
        f"the from-staging build must use the given staging directory as the context, expected {staging} (found: {tokens[-1]})"

    tampered_raw = repo / ".build/mkosi/output" / f"{manifest_names()[0]}.raw"
    tampered_raw.write_text("tampered\n", encoding="utf-8")
    # A tampered staging must fail closed: the checksums are verified, never
    # regenerated from whatever files are present.
    shutil.copy(tampered_raw, staging / f"{manifest_names()[0]}.raw")
    res = _run_build_image(repo, tmp_path / "stubs", tmp_path / "docker3.log",
                           args=["--from-staging", str(staging)])
    assert res.returncode != 0 and "SHA256SUMS" in res.stderr, res.stderr
    assert [l for l in tmp_path.joinpath("docker3.log").read_text(encoding="utf-8").splitlines()
            if l.startswith("docker ")] == [], \
        "a staging whose checksums do not verify must not reach docker build, expected no docker calls"

    (staging / "SHA256SUMS").unlink()
    res = _run_build_image(repo, tmp_path / "stubs", tmp_path / "docker4.log",
                           args=["--from-staging", str(staging)])
    assert res.returncode != 0 and "SHA256SUMS" in res.stderr, res.stderr

# delivery/Containerfile (the delivery image itself)

CONTAINERFILE = REPO / "delivery" / "Containerfile"

def _apt_install_lines(text: str) -> list[str]:
    """Every 'apt-get install' line, extended over its backslash continuation lines."""
    lines = text.splitlines()
    out, i = [], 0
    while i < len(lines):
        if "apt-get install" in lines[i]:
            out.append(lines[i])
            while lines[i].rstrip().endswith("\\") and i + 1 < len(lines):
                i += 1
                out.append(lines[i])
        i += 1
    return out

def test_containerfile_ships_the_fetch_tools():
    text = CONTAINERFILE.read_text(encoding="utf-8")
    blob = " ".join(_apt_install_lines(text))
    assert blob, "the Containerfile has no 'apt-get install' line: curl, ca-certificates and openssl are not installed"
    missing = [p for p in ("curl", "ca-certificates", "openssl") if not re.search(rf"(?<![\w-]){p}(?![\w-])", blob)]
    assert not missing, f"the Containerfile must install curl, ca-certificates and openssl for the fetch mode; missing: {missing}"
    assert any(l.lstrip().startswith("FROM") and "@sha256:" in l for l in text.splitlines()), (
        "the Containerfile FROM must stay digest-pinned ('@sha256:'): the delivery image must be reproducible")
