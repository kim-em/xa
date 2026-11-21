from xa.monitors import lib


def test_state_round_trips_per_monitor(tmp_path, monkeypatch):
    monkeypatch.setenv("XA_STATE", str(tmp_path))
    monkeypatch.setenv("XA_MONITOR", "one")
    assert lib.load_state({"cursor": None}) == {"cursor": None}
    lib.save_state({"cursor": "2026-11-21T00:00:00+00:00"})
    assert lib.load_state() == {"cursor": "2026-11-21T00:00:00+00:00"}
    monkeypatch.setenv("XA_MONITOR", "two")
    assert lib.load_state() is None
    assert sorted(p.name for p in (tmp_path / "monitors").iterdir()) == ["one.json"]


def test_unreadable_state_is_a_first_run(tmp_path, monkeypatch):
    monkeypatch.setenv("XA_STATE", str(tmp_path))
    monkeypatch.setenv("XA_MONITOR", "m")
    (tmp_path / "monitors").mkdir()
    (tmp_path / "monitors" / "m.json").write_text("{trunc")
    assert lib.load_state([]) == []
