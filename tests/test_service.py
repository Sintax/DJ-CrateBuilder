"""CrateBuilderService: transport gating, batch queue, settings and snapshots."""

import pytest

from cratebuilder.db import DownloadsDatabase
from cratebuilder.service import CBError, CrateBuilderService, version_info
from cratebuilder.settings import Settings


@pytest.fixture
def service(tmp_path):
    """A service pointed entirely at tmp_path — never the developer's config."""
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    return CrateBuilderService(settings=settings,
                               db_path=str(tmp_path / "cratebuilder.db"))


# ── transport gating ─────────────────────────────────────────────────────────

def test_remote_transport_refuses_filesystem_and_updater(tmp_path):
    remote = CrateBuilderService(
        transport="remote",
        settings=Settings(path=str(tmp_path / "c.json")),
        db_path=str(tmp_path / "db.sqlite"))
    for method in ("fs.pick_folder", "update.check", "update.apply"):
        with pytest.raises(CBError):
            remote.call(method)


def test_local_transport_advertises_both_capabilities(service):
    caps = service.snapshot()["capabilities"]
    assert caps == {"update": True, "filesystem": True}


def test_remote_snapshot_advertises_neither(tmp_path):
    remote = CrateBuilderService(
        transport="remote",
        settings=Settings(path=str(tmp_path / "c.json")),
        db_path=str(tmp_path / "db.sqlite"))
    caps = remote.snapshot()["capabilities"]
    assert caps == {"update": False, "filesystem": False}


def test_unknown_transport_is_rejected():
    with pytest.raises(ValueError):
        CrateBuilderService(transport="carrier-pigeon")


def test_unknown_method_raises_user_facing_error(service):
    with pytest.raises(CBError):
        service.call("nope.not_a_method")


# ── batch queue ──────────────────────────────────────────────────────────────

def test_batch_add_and_list(service):
    row = service.batch_add("https://youtube.com/watch?v=a", "Techno")
    assert row["state"] == "queued"
    assert [r["url"] for r in service.batch_list()] == ["https://youtube.com/watch?v=a"]


def test_batch_add_rejects_blank_url(service):
    with pytest.raises(CBError):
        service.batch_add("   ")


def test_batch_add_defaults_to_the_no_genre_value(service):
    assert service.batch_add("https://x/y")["genre"] == "(none)"


def test_batch_move_reorders(service):
    first = service.batch_add("https://a")
    service.batch_add("https://b")
    service.batch_move(first["id"], 1)
    assert [r["url"] for r in service.batch_list()] == ["https://b", "https://a"]


def test_batch_move_past_the_end_is_a_noop(service):
    row = service.batch_add("https://a")
    service.batch_add("https://b")
    service.batch_move(row["id"], -5)
    assert [r["url"] for r in service.batch_list()] == ["https://a", "https://b"]


def test_batch_remove_and_clear(service):
    row = service.batch_add("https://a")
    service.batch_add("https://b")
    assert len(service.batch_remove(row["id"])) == 1
    assert service.batch_clear() == []


def test_batch_remove_unknown_row_raises(service):
    with pytest.raises(CBError):
        service.batch_remove(4242)


def test_batch_skip_toggles(service):
    row = service.batch_add("https://a")
    assert service.batch_skip(row["id"])["state"] == "skipped"
    assert service.batch_skip(row["id"])["state"] == "queued"


# ── settings ─────────────────────────────────────────────────────────────────

def test_settings_set_echoes_stored_value(service):
    assert service.settings_set("skip_existing", False)["value"] is False
    assert service.settings_get("skip_existing")["value"] is False


def test_settings_reject_unknown_key(service):
    with pytest.raises(CBError):
        service.settings_set("not_a_key", 1)
    with pytest.raises(CBError):
        service.settings_get("not_a_key")


def test_settings_all_skips_keys_the_schema_lacks(service):
    values = service.settings_all()
    assert "skip_existing" in values
    assert "log_limit" not in values      # contract-only name, not in the schema


# ── library ──────────────────────────────────────────────────────────────────

def test_library_stats_without_a_database_creates_nothing(service, tmp_path):
    stats = service.library_stats()
    assert stats["available"] is False
    assert stats["downloads"] == 0
    assert not (tmp_path / "cratebuilder.db").exists()


def test_library_stats_counts_an_existing_database(tmp_path):
    db_path = tmp_path / "cratebuilder.db"
    DownloadsDatabase(str(db_path))
    svc = CrateBuilderService(settings=Settings(path=str(tmp_path / "c.json")),
                              db_path=str(db_path))
    stats = svc.library_stats()
    assert stats["available"] is True
    assert stats["downloads"] == 0


def test_genres_reads_the_crate_tree(service, tmp_path):
    (tmp_path / "crate" / "YouTube" / "Techno").mkdir(parents=True)
    (tmp_path / "crate" / "YouTube" / "_No Genre").mkdir(parents=True)
    assert service.genres() == ["(none)", "Techno"]


def test_genres_survives_a_missing_crate_root(service):
    assert service.genres() == []


# ── snapshot / strings ───────────────────────────────────────────────────────

def test_snapshot_carries_everything_the_shell_needs(service):
    snap = service.snapshot()
    for key in ("app", "host", "counts", "library", "batch", "watchlist",
                "settings", "settings_path", "platform", "genres", "capabilities"):
        assert key in snap


def test_ui_strings_expose_the_shared_registry(service):
    strings = service.ui_strings()
    assert strings["tooltips"]["wl.scan_all"]
    assert any(e["key"] == "skip_existing" for e in strings["settings_keys"])


def test_version_info_parses_the_monolith():
    info = version_info()
    assert info["version"] == "1.3"
    assert isinstance(info["build"], int)


def test_version_info_missing_file_is_not_fatal(tmp_path):
    assert version_info(str(tmp_path / "gone.py")) == {"version": None, "build": None}
