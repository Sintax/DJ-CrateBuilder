"""Settings ▸ Remote Access's three notification toggles.

They gate at CrateBuilderService.emit, the one place every `notification`
passes, so the bell, the toasts and every paired device hear the same thing.
Until now the three rendered disabled, blaming a host that simply had no
such keys.
"""
import pytest

from cratebuilder.service import CrateBuilderService
from cratebuilder.settings import Settings


@pytest.fixture
def host(tmp_path):
    settings = Settings(path=str(tmp_path / "config.json"))
    settings.set("base_dir", str(tmp_path / "crate"))
    service = CrateBuilderService(settings=settings,
                                  db_path=str(tmp_path / "cratebuilder.db"))
    heard = []
    service.events.subscribe(lambda type, payload: heard.append((type, payload)))
    return service, settings, heard


def note(**fields):
    return {"level": "info", "title": "T", "body": "b",
            "at": "2026-09-02T10:00:00", "job": "watchlist", **fields}


def notes(heard):
    return [p for t, p in heard if t == "notification"]


def test_all_three_are_on_by_default_and_served_to_settings(host):
    service, settings, _ = host
    for key in ("notify_scan_found", "notify_batch_done", "notify_errors"):
        assert settings.get(key) is True
        assert service.settings_all()[key] is True
        assert service.call("settings.set", {"key": key, "value": False}) == {
            "key": key, "value": False}


def test_a_scan_summary_is_dropped_when_scan_notifications_are_off(host):
    service, settings, heard = host
    service.emit("notification", note(kind="scan_found"))
    settings.set("notify_scan_found", False)
    service.emit("notification", note(kind="scan_found"))
    service.emit("notification", note())          # anything else still lands
    assert len(notes(heard)) == 2
    assert notes(heard)[0].get("kind") == "scan_found"
    assert "kind" not in notes(heard)[1]


def test_a_batch_summary_is_dropped_when_batch_notifications_are_off(host):
    service, settings, heard = host
    settings.set("notify_batch_done", False)
    service.emit("notification", note(kind="batch_done", job="batch"))
    service.emit("notification", note(kind="scan_found"))
    assert [n.get("kind") for n in notes(heard)] == ["scan_found"]


def test_errors_are_dropped_when_error_notifications_are_off(host):
    service, settings, heard = host
    settings.set("notify_errors", False)
    service.emit("notification", note(level="error", body="boom"))
    service.emit("notification", note(level="warn"))
    assert [n["level"] for n in notes(heard)] == ["warn"]


def test_other_events_are_never_gated(host):
    service, settings, heard = host
    for key in ("notify_scan_found", "notify_batch_done", "notify_errors"):
        settings.set(key, False)
    service.emit("job.finished", {"job": "batch", "ok": False, "error": "x"})
    service.emit("notification", note(title="Update available"))
    assert [t for t, _ in heard] == ["job.finished", "notification"]
