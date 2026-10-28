"""Stage the upstream test distribution and parse its framed result records.

The tree mirrors tests/util/build_dist.sh: runtime/, the upstream run_tests
launcher and conftest.py, the closed plugin set, empty handlers/, and the SCI
guest tests under tests/integration/sci/.
"""

import base64
import binascii
import json
import shutil
import sys
import tarfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

MAX_RECORD_LINE = 65536
CLOSED_PLUGINS = ("booted", "docstring", "file", "modify", "parse", "shell", "systemd")
LAUNCHER_MARKER = 'cat >"$tmpdir/dist/run_tests" <<\'EOF\''
RESULT_PREFIX = "SCI-RESULT:"


def _fail(message: str) -> ValueError:
    return ValueError(f"invalid evidence: {message}")


def _decode_record(line: str, invocation: str, scenario: str):
    try:
        record_invocation, record_scenario, payload = line[len(RESULT_PREFIX) :].rsplit(":", 2)
    except ValueError:
        raise _fail("malformed result record") from None
    if record_invocation != invocation or record_scenario != scenario:
        raise _fail(f"result record is for {record_invocation!r}/{record_scenario!r}, "
                    f"expected {invocation!r}/{scenario!r}")
    try:
        data = json.loads(base64.b64decode(payload, validate=True))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise _fail("malformed result payload") from exc
    if not isinstance(data, dict) or type(data.get("rc")) is not int or not isinstance(data.get("junit"), str):
        raise _fail("result payload must contain integer rc and base64 junit")
    if data["rc"] != 0:
        raise _fail(f"guest tests exited with rc {data['rc']}")
    try:
        junit = base64.b64decode(data["junit"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _fail("malformed JUnit base64") from exc
    return _junit_counts(junit)


def _junit_counts(payload: bytes):
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise _fail("malformed JUnit XML") from exc
    if root.tag not in ("testsuite", "testsuites"):
        raise _fail(f"unexpected JUnit root {root.tag!r}")
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    if not suites:
        raise _fail("JUnit has no testsuite")
    cases = [case for suite in suites for case in suite.findall("testcase")]
    if not cases:
        raise _fail("JUnit has no testcases")
    names = [case.get("name") for case in cases]
    if any(not name or not name.strip() for name in names):
        raise _fail("JUnit testcase has a missing or empty name")
    counts = {tag: sum(case.find(tag) is not None for case in cases)
              for tag in ("failure", "error", "skipped")}
    if any(counts.values()):
        raise _fail(f"JUnit reports {counts}")
    return 0, len(cases), counts["failure"], counts["error"], counts["skipped"], names


def parse_guest_result(console: str, invocation: str, scenario: str) -> dict[str, object]:
    """Parse exactly one bounded, matching SCI-RESULT record from a console."""
    if not isinstance(console, str):
        raise _fail("console must be text")
    record_lines = [line for line in console.splitlines() if line.startswith(RESULT_PREFIX)]
    if any(len(line) > MAX_RECORD_LINE for line in record_lines):
        raise _fail(f"result record exceeds {MAX_RECORD_LINE} characters")
    records = [_decode_record(line, invocation, scenario) for line in record_lines]
    if len(records) != 1:
        raise _fail(f"expected exactly one result record, found {len(records)}")
    return dict(zip(("rc", "tests", "failures", "errors", "skipped", "names"), records[0]))


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"invalid input: cannot read {path}: {exc}") from exc


def _launcher(repo_root: Path) -> bytes:
    script = repo_root / "tests" / "util" / "build_dist.sh"
    try:
        lines = script.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as exc:
        raise ValueError(f"invalid input: cannot read {script}: {exc}") from exc
    try:
        start = next(i for i, line in enumerate(lines) if LAUNCHER_MARKER in line)
        end = next(i for i in range(start + 1, len(lines)) if lines[i].rstrip("\n") == "EOF")
    except StopIteration:
        raise ValueError(f"invalid input: no run_tests heredoc in {script}") from None
    return "".join(lines[start + 1 : end]).encode()


def _check_destination(destination: Path, repo_root: Path) -> None:
    submodule, resolved = (repo_root / "gardenlinux").resolve(), destination.resolve()
    if resolved == submodule or submodule in resolved.parents:
        raise ValueError(f"invalid input: destination {destination} is inside the submodule")
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"invalid input: destination {destination} already exists")


def _check_members(members: list[tarfile.TarInfo], runtime_root: Path) -> None:
    if not members:
        raise ValueError("invalid input: runtime archive is empty")
    for member in members:
        name = PurePosixPath(member.name)
        if not member.name or name.is_absolute() or ".." in name.parts:
            raise ValueError(f"invalid input: unsafe archive member {member.name!r}")
        try:
            tarfile.data_filter(member, runtime_root)
        except (tarfile.FilterError, OSError, ValueError) as exc:
            raise ValueError(f"invalid input: unsafe archive member {member.name!r}: {exc}") from exc


def stage_distribution(runtime_archive: Path, destination: Path, repo_root: Path, guest_tests: Path) -> Path:
    """Build the upstream-shaped test tree; return its run_tests launcher."""
    runtime_archive, destination, repo_root, guest_tests = map(
        Path, (runtime_archive, destination, repo_root, guest_tests))
    _check_destination(destination, repo_root)
    runtime_root = destination / "runtime"
    if not guest_tests.is_dir():
        raise ValueError(f"invalid input: guest tests {guest_tests} is not a directory")
    launcher = _launcher(repo_root)
    conftest = _read(repo_root / "tests" / "conftest.py")
    plugins = {name: _read(repo_root / "tests" / "plugins" / name)
               for name in ("__init__.py", *(f"{plugin}.py" for plugin in CLOSED_PLUGINS))}
    if not runtime_archive.is_file():
        raise ValueError(f"invalid input: runtime archive {runtime_archive} is missing")
    try:
        archive = tarfile.open(runtime_archive, "r:*")
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ValueError(f"invalid input: cannot read {runtime_archive}: {exc}") from exc
    with archive:
        try:
            members = archive.getmembers()
            _check_members(members, runtime_root)
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise ValueError(f"invalid input: cannot read {runtime_archive}: {exc}") from exc
        try:
            destination.mkdir(parents=True)
            tests = destination / "tests"
            (tests / "plugins").mkdir(parents=True)
            (tests / "handlers").mkdir()
            (destination / "run_tests").write_bytes(launcher)
            (destination / "run_tests").chmod(0o755)
            (tests / "conftest.py").write_bytes(conftest)
            for name, data in plugins.items():
                (tests / "plugins" / name).write_bytes(data)
            shutil.copytree(guest_tests, tests / "integration" / "sci")
            runtime_root.mkdir()
            archive.extractall(runtime_root, members=members, filter="data")
        except (OSError, shutil.Error, tarfile.TarError, EOFError) as exc:
            shutil.rmtree(destination, ignore_errors=True)
            raise ValueError(f"invalid input: cannot stage distribution: {exc}") from exc
    return destination / "run_tests"


def _main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[0] != "parse":
        print("invalid evidence: usage: runtime_adapter.py parse <console> <invocation> <scenario>", file=sys.stderr)
        return 2
    try:
        result = parse_guest_result(argv[1], argv[2], argv[3])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
