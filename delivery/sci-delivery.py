#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
import glob, hashlib, os, re, shutil, signal, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path
from types import SimpleNamespace
SCRIPT_DIR = Path(__file__).resolve().parent; REPO_ROOT = SCRIPT_DIR.parent; MANIFEST_SOURCE = SCRIPT_DIR / "extensions.manifest"
SHIM_GLOB = str(REPO_ROOT / "mkosi/mkosi.images/*/mkosi.extra/usr/share/sci/activation/*.service"); PENDING = ".sci-delivery-pending"
DIRECTIVE_RE = re.compile(r"([A-Z][A-Z0-9_]*)=([^\s]*)"); NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$"); DIGITS_RE = re.compile(r"[0-9]+")
SUMS_RE = re.compile(r"^([0-9a-fA-F]{64}) [ *]([^\s]+)$"); FETCH_SUMS_RE = re.compile(r"^([0-9a-f]{64}) [ *]([A-Za-z0-9][A-Za-z0-9._-]*)$")
HOST_ROOT = Path(os.environ.get("HOST_ROOT", "/host")); STAGING_EXPLICIT = os.environ.get("STAGING_DIR")
STAGING_DIR = Path(STAGING_EXPLICIT or "/opt/sci"); INTERVAL = os.environ.get("INTERVAL", "60")
NSENTER = os.environ.get("NSENTER", "1"); FETCH_DIR = Path(os.environ.get("FETCH_DIR", "/var/lib/sci-sysext"))
FETCH_URL = os.environ.get("FETCH_URL", ""); FETCH_PUBKEY = os.environ.get("FETCH_PUBKEY", ""); FETCH_SUMS_DIGEST = os.environ.get("FETCH_SUMS_DIGEST", "")
def log(message): print(f"[sci-delivery] {message}", flush=True)
def warn(message): print(f"[sci-delivery] warning: {message}", file=sys.stderr, flush=True)
def die(message): print(f"[sci-delivery] error: {message}", file=sys.stderr); sys.exit(1)
def digest(path): return hashlib.file_digest(open(path, "rb"), "sha256").hexdigest()
def files_equal(first, second): first, second = Path(first), Path(second); return first.is_file() and second.is_file() and first.stat().st_size == second.stat().st_size and digest(first) == digest(second)
def write_atomic(source, destination):
    destination = Path(destination); destination.parent.mkdir(parents=True, exist_ok=True); before = destination.parent.stat()
    temporary = Path(f"{destination}.tmp"); shutil.copy2(source, temporary); os.replace(temporary, destination)
    os.utime(destination.parent, ns=(before.st_atime_ns, before.st_mtime_ns))
def run_host(args, check=True):
    command = (["nsenter", "-t", "1", "-m", "--"] if NSENTER == "1" else []) + list(args); log("running: " + " ".join(command))
    code = subprocess.run(command).returncode
    if check and code: die(f"command failed ({code}): {' '.join(command)}")
    return code
def parse_manifest(path):
    if not Path(path).is_file(): die(f"missing manifest: {path}")
    path = Path(path); target, removes, entries, units = "", [], [], {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.rstrip("\r")
        if not line.strip():
            if line: die(f"malformed manifest line {lineno} (whitespace-only): {path}")
            continue
        if line.lstrip().startswith("#"): continue
        if found := DIRECTIVE_RE.fullmatch(line):
            key, value = found.groups()
            if key == "TARGET_BASE" and (target or not DIGITS_RE.fullmatch(value)): die(f"invalid or duplicate TARGET_BASE='{value}' at line {lineno} (one digits-only line required): {path}")
            elif key == "REMOVE" and (not NAME_RE.fullmatch(value) or value in removes): die(f"invalid or duplicate REMOVE='{value}' at line {lineno}: {path}")
            elif key not in ("TARGET_BASE", "REMOVE"): die(f"unknown manifest directive '{key}' at line {lineno} (TARGET_BASE and REMOVE only): {path}")
            elif key == "TARGET_BASE": target = value
            else: removes.append(value)
            continue
        parts = line.split()
        if not NAME_RE.fullmatch(parts[0]): die(f"malformed manifest line {lineno} (invalid name '{parts[0]}'): {path}")
        entries.append(parts[0]); units[parts[0]] = parts[1:]
    if not target: die(f"manifest {path} carries no TARGET_BASE=<major> directive (the base-version gate needs one)")
    if set(removes) & set(entries): die(f"a REMOVE= name is also a manifest entry: {path}")
    return SimpleNamespace(target_base=target, removes=removes, entries=entries, units=units)
def parse_sums(path, line_re=SUMS_RE, prefix=""):
    table = {}
    for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.rstrip("\r")
        if not line: continue
        if not (found := line_re.fullmatch(line)): die(f"{prefix}invalid SHA256SUMS line {lineno}: {line}")
        checksum, filename = found.groups()
        if filename in table: die(f"{prefix}duplicate SHA256SUMS line for '{filename}' at line {lineno}")
        table[filename] = checksum.lower()
    return table
def load_manifest():
    manifest = parse_manifest(STAGING_DIR / "extensions.manifest"); entries = set(manifest.entries)
    for name in manifest.entries:
        if not (STAGING_DIR / f"{name}.raw").is_file(): die(f"manifest entry '{name}' has no staging raw: {STAGING_DIR / f'{name}.raw'}")
    for shim in STAGING_DIR.glob("*-activation.service"):
        if shim.name[: -len("-activation.service")] not in entries: die(f"staging shim '{shim.name}' has no manifest entry")
    if not (sums := STAGING_DIR / "SHA256SUMS").is_file(): die(f"staged-raw integrity: missing SHA256SUMS: {sums} (the raws cannot be verified)")
    table = parse_sums(sums)
    for name in manifest.entries:
        if table.get(f"{name}.raw") != digest(STAGING_DIR / f"{name}.raw"): die(f"staged-raw integrity: {name}.raw does not match SHA256SUMS: {sums}")
    log(f"staged-raw integrity: SHA256SUMS verifies {len(manifest.entries)} staged raw(s)")
    return manifest
def check_base_gate(manifest):
    os_release = HOST_ROOT / "etc/os-release"
    if not os_release.is_file(): die(f"base-version gate: cannot read {os_release}; refusing TARGET_BASE={manifest.target_base}")
    text = os_release.read_text(encoding="utf-8")
    version = next((l[14:].strip().strip('"').strip("'") for l in text.splitlines() if l.startswith("IMAGE_VERSION=")), "") or next((l[20:].strip().strip('"').strip("'") for l in text.splitlines() if l.startswith("GARDENLINUX_VERSION=")), "")
    if not version: die(f"base-version gate: {os_release} carries neither IMAGE_VERSION nor GARDENLINUX_VERSION")
    if version.split(".")[0] != manifest.target_base: die(f"base-version gate: host Garden Linux line {version.split('.')[0]} (version '{version}') does not match TARGET_BASE={manifest.target_base}")
    log(f"base-version gate: host Garden Linux line {manifest.target_base} matches TARGET_BASE={manifest.target_base}")
def run_pass(manifest):
    ext, sysd = HOST_ROOT / "var/lib/extensions", HOST_ROOT / "etc/systemd/system"
    wants = sysd / "multi-user.target.wants"; marker = ext / PENDING; pending = marker.exists()
    new = {n: not (ext / f"{n}.raw").is_file() for n in manifest.entries}
    paths = lambda n: (ext / f"{n}.raw", sysd / f"{n}-activation.service", wants / f"{n}-activation.service")
    if any(any(p.exists() or p.is_symlink() for p in paths(n)) for n in manifest.removes): ext.mkdir(parents=True, exist_ok=True); marker.write_text("")
    changed = False
    for n in manifest.removes:
        present = [p for p in paths(n) if p.exists() or p.is_symlink()]
        for p in present: p.unlink()
        if present: changed = True; log(f"removed {n}")
    raws, shims, all_shims = set(), set(), set()
    for n in manifest.entries:
        source, shim = STAGING_DIR / f"{n}.raw", STAGING_DIR / f"{n}-activation.service"; destination = sysd / f"{n}-activation.service"
        if new[n] or not files_equal(source, ext / f"{n}.raw"): write_atomic(source, ext / f"{n}.raw"); raws.add(n); changed = True; log(f"deployed {n}.raw")
        if shim.is_file():
            all_shims.add(n)
            if not files_equal(shim, destination): write_atomic(shim, destination); shims.add(n); changed = True; log(f"deployed {n}-activation.service")
    if not changed and not pending: log("no changes; nothing to do"); return 0
    if pending: log("pending activation found; replaying the activation flow"); raws, shims = set(manifest.entries), set(all_shims)
    ext.mkdir(parents=True, exist_ok=True); marker.write_text("")
    # The refresh exposes the extension units, so daemon-reload follows it; shims create users/tmpfiles/modules before unit restarts.
    run_host(["systemd-sysext", "refresh"]); run_host(["systemctl", "daemon-reload"]); run_host(["systemctl", "reload", "dbus.service"], check=False)
    for n in manifest.entries:
        if n in all_shims:
            run_host(["systemctl", "enable", f"{n}-activation.service"])
            if n in shims or n in raws or new[n]: run_host(["systemctl", "restart", f"{n}-activation.service"])
    failed = False
    for n in manifest.entries:
        if n in raws and manifest.units.get(n) and run_host(["systemctl", "restart", *manifest.units[n]], check=False) != 0:
            failed = True; warn(f"extension {n}: restart failed for {' '.join(manifest.units[n])} (continuing)")
    marker.unlink(missing_ok=True)
    if failed: warn("activation flow completed, but at least one extension unit restart failed"); return 1
    return 0
def fetch_file(url, destination, timeout=30, retries=2):
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response, open(destination, "wb") as handle: shutil.copyfileobj(response, handle); return
        except (OSError, ValueError) as error:
            die(f"fetch: cannot download {url}: {error}") if attempt == retries else time.sleep(1 + attempt)
def fetch_staging():
    global STAGING_DIR
    if not FETCH_URL: return
    if not STAGING_EXPLICIT: STAGING_DIR = FETCH_DIR / "staging"
    pubkey = Path(FETCH_PUBKEY)
    if not FETCH_PUBKEY or not pubkey.is_file() or not os.access(pubkey, os.R_OK): die(f"fetch: FETCH_PUBKEY must be a readable file: {pubkey}")
    if FETCH_SUMS_DIGEST and not re.fullmatch(r"[0-9a-f]{64}", FETCH_SUMS_DIGEST): die(f"fetch: FETCH_SUMS_DIGEST must be 64 lowercase hex characters: {FETCH_SUMS_DIGEST}")
    FETCH_DIR.mkdir(parents=True, exist_ok=True); STAGING_DIR.mkdir(parents=True, exist_ok=True)
    base = FETCH_URL.rstrip("/"); temporary = Path(tempfile.mkdtemp(prefix=".fetch.", dir=FETCH_DIR))
    try:
        checksums, signature = temporary / "SHA256SUMS", temporary / "SHA256SUMS.sig"
        fetch_file(f"{base}/SHA256SUMS", checksums); fetch_file(f"{base}/SHA256SUMS.sig", signature)
        if FETCH_SUMS_DIGEST and digest(checksums) != FETCH_SUMS_DIGEST: die(f"fetch: SHA256SUMS digest mismatch: expected {FETCH_SUMS_DIGEST}, got {digest(checksums)}")
        verified = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(pubkey), "-signature", str(signature), str(checksums)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if verified.returncode != 0: die("fetch: SHA256SUMS signature verification failed")
        log("fetch: verified SHA256SUMS signature")
        table = parse_sums(checksums, FETCH_SUMS_RE, "fetch: "); shutil.copyfile(checksums, STAGING_DIR / "SHA256SUMS")
        for filename, expected in table.items():
            destination = STAGING_DIR / filename
            if destination.is_file() and digest(destination) == expected: log(f"fetch: up to date {filename}"); continue
            partial = Path(f"{destination}.part"); fetch_file(f"{base}/{filename}", partial, timeout=600)
            if digest(partial) != expected: die(f"fetch: hash mismatch for {filename}")
            os.replace(partial, destination); log(f"fetch: downloaded {filename}")
        if subprocess.run(["sha256sum", "-c", "--", "SHA256SUMS"], cwd=STAGING_DIR, stdout=subprocess.DEVNULL).returncode: die("fetch: staged set is incomplete or corrupt")
        log(f"fetch: staging verified ({len(table)} files)"); pruned = 0
        for path in sorted(STAGING_DIR.iterdir()):
            stale = path.name.endswith(".part") or (path.name.endswith((".raw", "-activation.service")) and path.name not in table)
            if path.is_file() and not path.name.startswith(".") and stale: path.unlink(); pruned += 1; log(f"fetch: pruned {path.name}")
        if pruned: log(f"fetch: pruned {pruned} stale file(s)")
    finally: shutil.rmtree(temporary, ignore_errors=True)
def stage(raw_dir, out_arg):
    raw_dir, out_dir = Path(raw_dir), Path(out_arg).resolve()
    if not raw_dir.is_dir(): die(f"raw output directory not found: {raw_dir} (build the extensions first)")
    if not out_arg.strip() or out_dir in (Path("/"), Path.cwd().resolve(), REPO_ROOT.resolve()): die(f"refusing to use '{out_arg}' as the staging directory")
    manifest = parse_manifest(MANIFEST_SOURCE); entries = set(manifest.entries)
    for name in manifest.entries:
        if not (raw_dir / f"{name}.raw").is_file(): die(f"missing raw for manifest entry '{name}': {raw_dir / f'{name}.raw'}")
    shims = sorted(Path(path) for path in glob.glob(SHIM_GLOB))
    for shim in shims:
        if shim.name[: -len("-activation.service")] not in entries: die(f"staging shim '{shim.name}' has no manifest entry")
    if out_dir.exists(): shutil.rmtree(out_dir) if out_dir.is_dir() else out_dir.unlink()
    out_dir.mkdir(parents=True)
    for name in manifest.entries: shutil.copy2(raw_dir / f"{name}.raw", out_dir / f"{name}.raw")
    for shim in shims: shutil.copy2(shim, out_dir / shim.name)
    shutil.copy2(MANIFEST_SOURCE, out_dir / "extensions.manifest"); delivery = out_dir / "sci-delivery.py"
    shutil.copy2(SCRIPT_DIR / "sci-delivery.py", delivery); delivery.chmod(0o755)
    lines = [f"{digest(out_dir / f'{name}.raw')}  {name}.raw\n" for name in manifest.entries]; (out_dir / "SHA256SUMS").write_text("".join(lines), encoding="utf-8")
    for checksum, filename in (line.rstrip("\n").split("  ", 1) for line in lines):
        if digest(out_dir / filename) != checksum: die(f"staged SHA256SUMS does not verify the staged raws: {out_dir / 'SHA256SUMS'}")
    log(f"staged {len(manifest.entries)} extensions in {out_dir}")
def usage():
    print("""usage: sci-delivery.py [--help] | sci-delivery.py --stage <raw-dir> <out-dir>
Reconcile STAGING_DIR into HOST_ROOT.  Environment: HOST_ROOT=/host, STAGING_DIR=/opt/sci (or $FETCH_DIR/staging), INTERVAL=60 (0 runs one pass), NSENTER=1 (nsenter -t 1 -m).  Fetch mode: FETCH_URL, FETCH_PUBKEY (required), FETCH_SUMS_DIGEST, FETCH_DIR=/var/lib/sci-sysext.  Manifest: TARGET_BASE=<major> is required; REMOVE=<name> deletes a deployed extension.  --stage assembles and checksums <out-dir> from <raw-dir>, the manifest, the activation shims, and this script.""")
def main(argv):
    if argv and argv[0] in ("-h", "--help"): usage(); return 0
    if argv and argv[0] == "--stage": (stage(argv[1], argv[2]) if len(argv) == 3 else die("usage: sci-delivery.py --stage <raw-dir> <out-dir>")); return 0
    if argv: die(f"unknown argument: {argv[0]} (try --help)")
    if not DIGITS_RE.fullmatch(INTERVAL): die(f"INTERVAL must be a non-negative integer: {INTERVAL}")
    while True:
        if not HOST_ROOT.is_dir(): die(f"HOST_ROOT is not an existing directory: {HOST_ROOT}")
        fetch_staging(); manifest = load_manifest(); check_base_gate(manifest); run_pass(manifest)
        if int(INTERVAL) == 0: break
        time.sleep(int(INTERVAL))
    return 0
if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    sys.exit(main(sys.argv[1:]))
