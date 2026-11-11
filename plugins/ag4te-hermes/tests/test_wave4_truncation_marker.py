"""A cut tool reply says how much was cut and how to get the rest (wave 4)."""
import json
import re

import pytest

from test_summary import ag


def _dig(monkeypatch, name, summary="ok"):
    monkeypatch.setattr(ag, "validate_project", lambda p: p)
    monkeypatch.setattr(ag, "load_dig", lambda p: {"authors": [{"name": name}], "kinds": []})
    monkeypatch.setattr(ag, "brief", lambda d: summary)
    monkeypatch.setattr(ag, "_on_gateway", lambda: False)


def test_a_path_of_a_few_hundred_characters_is_not_cut(monkeypatch):
    path = "src/" + "a/" * 120 + "file.py"  # 250 characters: a real deep path
    _dig(monkeypatch, path)
    out = json.loads(ag._tool({"project": "demo", "detail": "full"}))
    assert out["authors"][0]["name"] == path


def test_brief_cut_names_the_count_and_how_to_get_more(monkeypatch):
    long = "A" * 3000
    _dig(monkeypatch, "x", summary=long)
    out = json.loads(ag._tool({"project": "demo"}))
    body = out["summary"]
    m = re.search(r"…\((\d+) more chars; ask with detail: full\)", body)
    assert m, body
    kept = body.split(ag.UNTRUSTED_OPEN + "\n")[1].split("…(")[0]
    assert len(kept) + int(m.group(1)) == 3000
    assert len(body) < ag.TOOL_SUMMARY_CAP + 100


def test_full_cut_does_not_point_at_detail_full(monkeypatch):
    _dig(monkeypatch, "B" * 5000)
    name = json.loads(ag._tool({"project": "demo", "detail": "full"}))["authors"][0]["name"]
    m = re.search(r"…\((\d+) more chars cut\)$", name)
    assert m and "detail" not in name
    assert len(name) <= ag.TOOL_TEXT_CAP_FULL
    assert len(name) - len(m.group(0)) + int(m.group(1)) == 5000


@pytest.mark.parametrize("cap", [60, 500])
def test_marker_stays_inside_the_cap(cap):
    for n in (cap + 1, cap + 9, cap + 99, cap + 5000):
        s = ag._untrusted("z" * n, cap)
        assert len(s) <= cap and "more chars" in s
    assert ag._untrusted("z" * cap, cap) == "z" * cap
