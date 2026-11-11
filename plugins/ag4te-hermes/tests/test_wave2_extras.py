"""Wave 2 extras: X-SHLEX, A-tz, A-inject, A-perf."""
from test_summary import ag

# ---- X-SHLEX
def test_windows_paths_keep_their_backslashes():
    cmd = r'"C:\Program Files\uv\uvx.exe" --from devarch'
    assert ag._split_command(cmd, windows=True) == [r"C:\Program Files\uv\uvx.exe", "--from", "devarch"]
    assert ag._split_command(r"C:\tools\dev.cmd run", windows=True) == [r"C:\tools\dev.cmd", "run"]


def test_posix_splitting_is_unchanged():
    assert ag._split_command(r"uvx --from 'a b' c\ d", windows=False) == ["uvx", "--from", "a b", "c d"]
