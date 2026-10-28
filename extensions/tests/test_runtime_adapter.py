"""Contract for extensions/tests/runtime_adapter.py.

- stage_distribution() creates destination, returns the run_tests launcher,
  and stages the build_dist-shaped tree: byte-exact run_tests heredoc,
  runtime/ = runtime tar contents, byte-exact tests/conftest.py, the closed
  plugin set, empty handlers/, guest tests under tests/integration/sci/.
  Safe in-runtime links must be staged verbatim (the real upstream runtime
  .build/sysext-runtime/runtime.tar.gz has 2096, e.g. x86_64/bin/python ->
  python3.14); symlinks and hardlinks escaping the runtime,
  path-traversing members, and destinations resolving into the gardenlinux
  submodule are refused.
- parse_guest_result() accepts exactly one SCI-RESULT:<id>:<scenario>:<b64>
  line per console (id == invocation, scenario == scenario); payload is
  base64 JSON {"rc": int, "junit": b64 JUnit XML}; rc == 0, JUnit has named
  testcases, none skipped/failed/errored; record lines over MAX_RECORD_LINE
  are rejected; returns rc, tests, failures, errors, skipped, and names
  (the JUnit testcase names in order, for the caller's explicit-name check).
- Rejections raise ValueError ("invalid evidence:" parse, "invalid input:"
  staging); CLI `parse` prints the dict as JSON, exits 0, else exits nonzero
  with the error text on stderr.
"""

import base64
import importlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

MAX_RECORD_LINE = 65536
CLOSED_PLUGINS = ("booted", "docstring", "file", "modify", "parse", "shell", "systemd")
ADAPTER_FILE = Path(__file__).resolve().parents[0] / "runtime_adapter.py"
INVOCATION, SCENARIO = "./run_tests -m sysext", "sysext-boot"
TC_NAMES = [f"tc-{i}" for i in range(3)]
NAMELESS_JUNIT = '<?xml version="1.0"?><testsuite tests="1" failures="0" errors="0" skipped="0"><testcase/></testsuite>'


def _b64(data):
    return base64.b64encode(data.encode() if isinstance(data, str) else data).decode()


def _junit_xml(n, child=""):
    cases = [f'<testcase name="tc-{i}">{child if i == 0 else ""}</testcase>' for i in range(n)]
    f, e, s = (c in child for c in ("<failure", "<error", "<skipped"))
    head = f'tests="{n}" failures="{int(f)}" errors="{int(e)}" skipped="{int(s)}"'
    return f'<?xml version="1.0"?><testsuites><testsuite name="pytest" {head}>{"".join(cases)}</testsuite></testsuites>'


def _record(invocation, scenario, payload):
    return f"SCI-RESULT:{invocation}:{scenario}:{payload}"


def _console(*lines):
    return "boot noise line\n" + "\n".join(lines) + "\ntrailing noise\n"


def _launcher_bytes(repo_root):
    lines = (repo_root / "tests" / "util" / "build_dist.sh").read_text(encoding="utf-8").splitlines(keepends=True)
    marker = 'cat >"$tmpdir/dist/run_tests" <<\'EOF\''
    start = next(i for i, l in enumerate(lines) if marker in l)
    end = next(i for i in range(start + 1, len(lines)) if lines[i].rstrip("\n") == "EOF")
    return "".join(lines[start + 1 : end]).encode()


def _adapter():
    return importlib.import_module("runtime_adapter")  # lazy: no import at collection


def _runtime_archive(path):
    """Synthetic runtime.tar.gz with the real runtime's safe link shapes."""
    root = path / "rt"
    py = root / "x86_64" / "bin" / "python3.14"
    py.parent.mkdir(parents=True)
    py.write_bytes(b"#!/bin/sh\n# synthetic python\n")
    py.chmod(0o755)
    (root / "x86_64" / "bin" / "python").symlink_to("python3.14")
    (root / "site-packages").mkdir()
    (root / "site-packages" / "marker.txt").write_text("synthetic runtime\n")
    (root / "x86_64" / "lib" / "python3.14").mkdir(parents=True)
    (root / "x86_64" / "lib" / "python3.14" / "site-packages").symlink_to("../../../site-packages")
    with tarfile.open(path / "runtime.tar.gz", "w:gz") as tf:
        tf.add(root, arcname=".")
    return path / "runtime.tar.gz"


def _guest_tests(path):
    g = path / "guest-tests"
    g.mkdir()
    (g / "test_boot.py").write_text("def test_boot():\n    assert True\n")
    (g / "test_policy.py").write_text("def test_policy():\n    assert True\n")
    return g


def _evil_archive(path, member):
    with tarfile.open(path / "evil.tar.gz", "w:gz") as tf:
        info = tarfile.TarInfo(member)
        info.size = 5
        tf.addfile(info, io.BytesIO(b"evil\n"))
    return path / "evil.tar.gz"


def _link_archive(path, typ, target):
    with tarfile.open(path / "esc.tar.gz", "w:gz") as tf:
        info = tarfile.TarInfo("bin/link")
        info.type, info.linkname = tarfile.SYMTYPE if typ == "sym" else tarfile.LNKTYPE, target
        tf.addfile(info)
    return path / "esc.tar.gz"


BIG_RECORD = _record(INVOCATION, SCENARIO, _b64(_junit_xml(2500)))
GOOD_RECORD = _record(INVOCATION, SCENARIO, _b64(json.dumps({"rc": 0, "junit": _b64(_junit_xml(3))})))

REJECTIONS = [
    ("missing-record", "boot noise without any result line\n", INVOCATION, SCENARIO),
    ("duplicate-record", _console(GOOD_RECORD, GOOD_RECORD), INVOCATION, SCENARIO),
    ("three-fields", f"SCI-RESULT:{INVOCATION}:{SCENARIO}\n", INVOCATION, SCENARIO),
    ("bad-base64", f"SCI-RESULT:{INVOCATION}:{SCENARIO}:####\n", INVOCATION, SCENARIO),
    ("bad-json", _record(INVOCATION, SCENARIO, _b64("not json")), INVOCATION, SCENARIO),
    ("nonzero-rc", _record(INVOCATION, SCENARIO, _b64(json.dumps({"rc": 1, "junit": _b64(_junit_xml(3))}))), INVOCATION, SCENARIO),
    ("empty-junit", _record(INVOCATION, SCENARIO, _b64(_junit_xml(0))), INVOCATION, SCENARIO),
    *[(f"{t}-case", _record(INVOCATION, SCENARIO, _b64(json.dumps({"rc": 0, "junit": _b64(_junit_xml(3, f"<{t}/>"))}))), INVOCATION, SCENARIO) for t in ("skipped", "failure", "error")],
    ("bad-junit-base64", _record(INVOCATION, SCENARIO, _b64(json.dumps({"rc": 0, "junit": "####"}))), INVOCATION, SCENARIO),
    ("oversized-record", BIG_RECORD + "\n", INVOCATION, SCENARIO),
    ("nameless-case", _record(INVOCATION, SCENARIO, _b64(json.dumps({"rc": 0, "junit": _b64(NAMELESS_JUNIT)}))), INVOCATION, SCENARIO),
]

ESCAPES = [("escape-symlink", "sym", "../../../outside.txt"), ("absolute-symlink", "sym", "/nonexistent-r4-abs"),
           ("escape-hardlink", "lnk", "../../../outside.txt"), ("absolute-hardlink", "lnk", "/nonexistent-r4-abs")]


@pytest.mark.parametrize("kind,console,inv,scen", REJECTIONS, ids=[k for k, *_ in REJECTIONS])
def test_parse_guest_result_rejects(kind, console, inv, scen):
    if kind == "oversized-record":
        assert len(console) > MAX_RECORD_LINE
    with pytest.raises(ValueError, match="invalid evidence"):
        _adapter().parse_guest_result(console, inv, scen)


@pytest.mark.parametrize("inv,scen", [(INVOCATION + "-other", SCENARIO), (INVOCATION, SCENARIO + "-other")], ids=["invocation", "scenario"])
def test_parse_guest_result_mismatches(inv, scen):
    with pytest.raises(ValueError, match="invalid evidence"):
        _adapter().parse_guest_result(_console(GOOD_RECORD), inv, scen)


def test_parse_guest_result_accepts():
    result = _adapter().parse_guest_result(_console("noise", GOOD_RECORD, "noise"), INVOCATION, SCENARIO)
    assert (result["rc"], result["tests"], result["failures"], result["errors"], result["skipped"], result["names"]) == (0, 3, 0, 0, 0, TC_NAMES)


def test_stage_distribution_happy(repo_root, tmp_path):
    guest = _guest_tests(tmp_path)
    launcher = _adapter().stage_distribution(_runtime_archive(tmp_path), tmp_path / "dist", repo_root, guest)
    assert launcher == tmp_path / "dist" / "run_tests" and launcher.is_file() and os.access(launcher, os.X_OK)
    assert launcher.read_bytes() == _launcher_bytes(repo_root)
    tests = launcher.parent / "tests"
    assert (tests / "conftest.py").read_bytes() == (repo_root / "tests" / "conftest.py").read_bytes()
    assert sorted(p.name for p in (tests / "plugins").glob("*.py")) == sorted(["__init__.py", *[f"{n}.py" for n in CLOSED_PLUGINS]])
    assert all((tests / "plugins" / f"{n}.py").read_bytes() == (repo_root / "tests" / "plugins" / f"{n}.py").read_bytes() for n in CLOSED_PLUGINS)
    assert not list((tests / "handlers").glob("*.py"))
    assert all((tests / "integration" / "sci" / f.name).read_bytes() == f.read_bytes() for f in guest.iterdir())
    py = launcher.parent / "runtime" / "x86_64" / "bin" / "python"
    assert py.is_symlink() and os.readlink(py) == "python3.14" and py.read_bytes() == b"#!/bin/sh\n# synthetic python\n" and os.access(py, os.X_OK)
    sp = launcher.parent / "runtime" / "x86_64" / "lib" / "python3.14" / "site-packages"
    assert sp.is_symlink() and os.readlink(sp) == "../../../site-packages" and (sp / "marker.txt").read_text() == "synthetic runtime\n"


@pytest.mark.parametrize("kind,typ,target", ESCAPES, ids=[k for k, *_ in ESCAPES])
def test_stage_distribution_rejects_escaping_links(repo_root, tmp_path, kind, typ, target):
    with pytest.raises(ValueError, match="invalid input"):
        _adapter().stage_distribution(_link_archive(tmp_path, typ, target), tmp_path / "dist", repo_root, _guest_tests(tmp_path))
    assert not (tmp_path / "outside.txt" if target.startswith("..") else Path(target)).exists() and not (tmp_path / "dist").exists()


@pytest.mark.parametrize("kind", ["traversal", "dest-in-submodule", "dest-symlink-into-submodule"])
def test_stage_distribution_rejects(repo_root, tmp_path, kind):
    dest = {"traversal": tmp_path / "dist", "dest-in-submodule": repo_root / "gardenlinux" / "staged",
            "dest-symlink-into-submodule": repo_root / "tests" / "staged"}[kind]
    arc = _evil_archive(tmp_path, "../../evil.txt") if kind == "traversal" else _runtime_archive(tmp_path)
    with pytest.raises(ValueError, match="invalid input"):
        _adapter().stage_distribution(arc, dest, repo_root, _guest_tests(tmp_path))
    assert kind not in {"traversal", "dest-in-submodule"} or not {"traversal": tmp_path / "evil.txt",
                                                                   "dest-in-submodule": repo_root / "gardenlinux" / "staged"}[kind].exists()


@pytest.mark.parametrize("console,ok", [(_console(GOOD_RECORD), True), ("no result in this console", False)], ids=["valid", "invalid"])
def test_cli_parse(console, ok):
    proc = subprocess.run([sys.executable, str(ADAPTER_FILE), "parse", console, INVOCATION, SCENARIO],
                          capture_output=True, text=True)
    assert (proc.returncode == 0) == ok, proc.stderr
    if ok:
        out = json.loads(proc.stdout)
        assert (out["rc"], out["tests"], out["failures"], out["errors"], out["skipped"], out["names"]) == (0, 3, 0, 0, 0, TC_NAMES)
    else:
        assert "invalid evidence" in proc.stderr
