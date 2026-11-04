"""The resolved-config pin machinery: `mkosi summary --json` normalization."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


class SummaryError(RuntimeError):
    """A summary invocation or pin precondition failed (the build would too)."""


class SummaryShapeError(SummaryError):
    """The JSON shape is not what `normalize_summary` expects (jq parity)."""


def _repo_prefix(repo_root: Path | str) -> str:
    """`$repo` with one trailing "/" stripped, plus the "/" suffix."""
    repo = str(repo_root)
    if repo.endswith("/"):
        repo = repo[:-1]
    return repo + "/"


def _rewrite(value: Any, prefix: str) -> Any:
    """Recursive `walk(norm($prefix))`: values only, never dict keys."""
    if isinstance(value, dict):
        return {key: _rewrite(item, prefix) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite(item, prefix) for item in value]
    if isinstance(value, str):
        if value.startswith(prefix):
            return "@REPO/" + value[len(prefix) :]
        if value.startswith("/"):
            return "@ABS"
        return value
    return value


def _null_seeds(data: Any) -> Any:
    """`.Images |= map(.Seed = null)` with the shape errors made explicit."""
    if not isinstance(data, dict):
        raise SummaryShapeError(
            "the summary top level is not a JSON object; .Images cannot be indexed"
        )
    if "Images" not in data:
        # An absent `Images` key is a no-op, unlike jq 1.7 (documented contract).
        return data
    images = data["Images"]
    if not isinstance(images, list):
        raise SummaryShapeError(f"Images is {type(images).__name__}, not a list")
    for index, image in enumerate(images):
        if not isinstance(image, dict):
            raise SummaryShapeError(f"Images[{index}] is not an object")
        image["Seed"] = None
        # uid-dependent mkosi default (root=0, non-root=3): null it like Seed
        image["DelegateRanges"] = None
        # environment-derived via no_proxy/NO_PROXY: null it like Seed/DelegateRanges
        image["ProxyExclude"] = None
    return data


def normalize_summary_data(data: Any, repo_root: Path | str) -> bytes:
    """Normalize an already-parsed summary object into the pinned bytes."""
    prefix = _repo_prefix(repo_root)
    normalized = _null_seeds(_rewrite(data, prefix))
    text = json.dumps(normalized, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    return text.encode("utf-8")


def normalize_summary(summary_json: Path | str, repo_root: Path | str) -> bytes:
    """Normalize `<summary.json>` against `<repo_root>` (byte-exact)."""
    data = json.loads(Path(summary_json).read_text(encoding="utf-8"))
    return normalize_summary_data(data, repo_root)


def normalize_summary_text(text: str, repo_root: Path | str) -> bytes:
    """Normalize raw `mkosi summary --json` stdout."""
    return normalize_summary_data(json.loads(text), repo_root)


def normalized_digest(summary_json: Path | str, repo_root: Path | str) -> str:
    """sha256 hex of the normalized bytes."""
    return hashlib.sha256(normalize_summary(summary_json, repo_root)).hexdigest()


def summary_command(mkosi_bin: Path | str, epoch: int) -> list[str]:
    """build.sh's invocation shape; pure, therefore unit-testable."""
    return [str(mkosi_bin), f"--source-date-epoch={epoch}", "summary", "--json"]


def _abspath(path: Path | str) -> Path:
    """Absolute paths pass through; a relative path resolves against the cwd."""
    p = Path(path)
    return p if p.is_absolute() else (Path.cwd() / p)


def _release_suite(config_dir: Path) -> str:
    """The one `^Release=` value of `<config_dir>/mkosi.conf`."""
    conf = config_dir / "mkosi.conf"
    if not conf.is_file():
        raise SummaryError(f"{conf} missing")
    values = [
        line[len("Release=") :].strip()
        for line in conf.read_text(encoding="utf-8").splitlines()
        if line.startswith("Release=")
    ]
    if len(values) != 1:
        raise SummaryError(f"{conf} carries {len(values)} Release= lines (expected one)")
    if not values[0]:
        raise SummaryError(f"{conf} has a Release= line but no suite value")
    return values[0]


def _tail(text: str, n: int = 20) -> str:
    return "\n".join(text.splitlines()[-n:])


def run_summary(
    mkosi_bin: Path | str,
    config_dir: Path | str,
    garden_version: Path | str,
    *,
    repo_root: Path | str,
    timeout: int | None = 600,
) -> str:
    """Return the raw `--json` summary on stdout."""
    mkosi_path = _abspath(mkosi_bin)
    config_path = _abspath(config_dir)
    version_path = _abspath(garden_version)
    _ = str(repo_root)  # kept for API symmetry; the invocation does not use it

    suite = _release_suite(config_path)
    if not (version_path.is_file() and os.access(version_path, os.X_OK)):
        raise SummaryError(
            f"{version_path} missing or not executable (git submodule update "
            "--init gardenlinux)"
        )

    try:
        epoch_res = subprocess.run(
            [str(version_path), "--epoch", suite],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SummaryError(f"cannot derive the epoch from Release={suite}: {exc}") from exc
    epoch = epoch_res.stdout.strip()
    if epoch_res.returncode != 0:
        raise SummaryError(
            f"cannot derive the epoch from Release={suite}:\n"
            f"{_tail(epoch_res.stderr, 5)}"
        )
    if not re.fullmatch(r"[0-9]+", epoch):
        raise SummaryError(f"'{version_path} --epoch {suite}' is no timestamp: '{epoch}'")

    try:
        res = subprocess.run(
            summary_command(mkosi_path, int(epoch)),
            cwd=config_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        tail = _tail(exc.stderr) if isinstance(exc.stderr, str) else ""
        raise SummaryError(f"timeout: mkosi summary took over {timeout}s\n{tail}") from exc
    except OSError as exc:
        raise SummaryError(f"cannot execute {mkosi_path}: {exc}") from exc
    if res.returncode != 0:
        raise SummaryError(
            f"mkosi summary exited {res.returncode}\n{_tail(res.stderr)}"
        )
    return res.stdout


def resolved_config_digest(
    mkosi_bin: Path | str,
    config_dir: Path | str,
    garden_version: Path | str,
    repo_root: Path | str,
    *,
    runs: int = 2,
) -> str:
    """Digest of the normalized resolved config, stable across repeated runs."""
    digests: list[str] = []
    for index in range(1, max(runs, 1) + 1):
        raw = run_summary(
            mkosi_bin, config_dir, garden_version, repo_root=repo_root
        )
        try:
            normalized = normalize_summary_text(raw, repo_root)
        except ValueError as exc:  # json.JSONDecodeError: the shape moved
            raise SummaryShapeError(
                f"normalization of the mkosi summary failed: {exc}"
            ) from exc
        digests.append(hashlib.sha256(normalized).hexdigest())
    if len(set(digests)) > 1:
        raise SummaryError(
            "mkosi summary is NOT stable run-to-run after normalization "
            f"(digests: {', '.join(digests)}): a field is volatile, not skipped"
        )
    return digests[0]


USAGE = "usage: python3 extensions/tests/summary.py <summary.json> <repo-root>"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or not Path(args[0]).is_file():
        print(USAGE, file=sys.stderr)
        return 2
    try:
        normalized = normalize_summary(args[0], args[1])
    except SummaryError as exc:
        print(f"summary-normalize: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(f"summary-normalize: cannot parse {args[0]}: {exc}", file=sys.stderr)
        return 1
    # bytes, not text: stdout must be byte-identical to jq's output
    sys.stdout.buffer.write(normalized)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
