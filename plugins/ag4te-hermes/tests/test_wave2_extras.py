"""Wave 2 extras: X-SHLEX, A-tz, A-inject, A-perf."""
from test_summary import ag, make_project

# ---- X-SHLEX
def test_windows_paths_keep_their_backslashes():
    cmd = r'"C:\Program Files\uv\uvx.exe" --from devarch'
    assert ag._split_command(cmd, windows=True) == [r"C:\Program Files\uv\uvx.exe", "--from", "devarch"]
    assert ag._split_command(r"C:\tools\dev.cmd run", windows=True) == [r"C:\tools\dev.cmd", "run"]


def test_posix_splitting_is_unchanged():
    assert ag._split_command(r"uvx --from 'a b' c\ d", windows=False) == ["uvx", "--from", "a b", "c d"]


# ---- A-tz
def test_rejected_zone_name_is_reported_as_a_warning(tmp_path):
    pdir = make_project(tmp_path)
    bad = ag.summarize(pdir, settings={"time_zone": "Mars/Olympus"})
    assert any("Mars/Olympus" in w for w in bad["manifest"]["warnings"])
    good = ag.summarize(pdir, settings={"time_zone": "UTC"})
    assert good["manifest"]["warnings"] == []
    assert ag.project_zone({"time_zone": "UTC"})[1] == "UTC"  # the old two-value contract holds
