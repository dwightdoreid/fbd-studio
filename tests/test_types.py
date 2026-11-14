import pytest

from fbdstudio import types as T


@pytest.mark.parametrize("text,ms", [
    ("T#1s", 1000), ("t#1m30s", 90_000), ("TIME#250ms", 250), ("T#1.5s", 1500),
    ("T#1h2m3s4ms", 3_723_004), ("T#-2s", -2000), ("750", 750), (42, 42), ("T#1_000ms", 1000),
])
def test_parse_time(text, ms):
    assert T.parse_time(text) == ms


@pytest.mark.parametrize("bad", ["T#", "T#5x", "T#s", "abc", ""])
def test_parse_time_invalid(bad):
    with pytest.raises(ValueError):
        T.parse_time(bad)


def test_format_time_roundtrip():
    for ms in (0, 1, 999, 1000, 90_500, 3_723_004):
        assert T.parse_time(T.format_time(ms)) == ms


def test_literals():
    assert T.c_literal("BOOL", "TRUE") == "true"
    assert T.c_literal("REAL", 2) == "2.0f"
    assert T.c_literal("TIME", "T#2s") == "2000"
    with pytest.raises(ValueError):
        T.c_literal("INT", 40000)
    with pytest.raises(ValueError):
        T.c_literal("INT", 1.5)


def test_compatibility():
    assert T.compatible("INT", "REAL")
    assert not T.compatible("REAL", "INT")
    assert not T.compatible("BOOL", "INT")
    assert not T.compatible("TIME", "DINT")
