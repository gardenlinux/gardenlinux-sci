"""A refusal is success only when the guest proves the victim caused it."""
from pathlib import Path

from test_sysext import guest_expected, merged_extensions, run


def test_verity_refusal():
    expected = guest_expected({"tamper", "policy"})
    victim = expected["victim"]
    assert isinstance(victim, str) and victim, "the scenario must name its victim"
    if expected["scenario"] == "tamper":
        # The tamper fixture must name one of the raws it staged.
        assert victim in expected["extensions"], (victim, expected["extensions"])
    victim_raw = Path(f"/var/lib/extensions/{victim}.raw")
    assert victim_raw.is_file(), f"victim raw not staged: {victim_raw}"
    assert not merged_extensions(), "refusal fixture must start unmerged"
    cursor = run("journalctl", "--show-cursor", "-n", "0", "--no-pager")
    assert cursor.returncode == 0, cursor.stderr
    cursors = [line.removeprefix("-- cursor: ") for line in cursor.stdout.splitlines()
               if line.startswith("-- cursor: ")]
    assert len(cursors) == 1, cursor.stdout
    refresh = run("systemd-sysext", "refresh")
    assert refresh.returncode != 0, "the invalid extension was accepted"
    assert not merged_extensions(), "the refused scenario merged an extension"
    journal = run("journalctl", f"--after-cursor={cursors[0]}", "--no-pager", "-o", "cat")
    assert journal.returncode == 0, journal.stderr
    reasons = ("does not match image policy",) if expected["scenario"] == "policy" else (
        "failed to read metadata", "failed to mount", "failed to read data")
    # The diagnostic may be on stderr instead of the journal; victim and reason must match.
    output = journal.stdout + refresh.stderr
    assert any(victim in line and any(reason in line.lower() for reason in reasons)
               for line in output.splitlines()), output
