"""Tests for the Linux self-update helpers in updater_core.

Pure logic only — no tkinter, no network, no privilege escalation. The
location-based classifier and the command builder take explicit arguments so
these run identically on any platform.
"""
from cratebuilder import updater_core as uc


# ── linux_install_kind ────────────────────────────────────────────────────────
def test_install_kind_deb_for_opt_path():
    kind = uc.linux_install_kind(
        module_path="/opt/dj-cratebuilder/DJ-CrateBuilder_v1.3.py")
    assert kind == "deb"


def test_install_kind_source_for_other_path():
    kind = uc.linux_install_kind(
        module_path="/home/dj/DJ-CrateBuilder/DJ-CrateBuilder_v1.3.py")
    assert kind == "source"


def test_install_kind_source_on_commonpath_valueerror(monkeypatch):
    # commonpath raises ValueError when the two paths share no root (e.g. on
    # Windows, different drive letters). That must be treated as "source".
    def boom(_paths):
        raise ValueError("paths don't have the same drive")
    monkeypatch.setattr(uc.os.path, "commonpath", boom)
    assert uc.linux_install_kind(module_path="C:/app/x.py",
                                 deb_root="D:/opt/dj-cratebuilder") == "source"


# ── build_deb_install_cmd ─────────────────────────────────────────────────────
def test_build_deb_install_cmd_exact_list():
    assert uc.build_deb_install_cmd("/tmp/x.deb") == [
        "pkexec", "apt-get", "install", "-y", "/tmp/x.deb"]


# ── can_self_update truth table ───────────────────────────────────────────────
def test_can_self_update_when_frozen(monkeypatch):
    monkeypatch.setattr(uc, "is_frozen", lambda: True)
    monkeypatch.setattr(uc, "is_linux", lambda: False)
    assert uc.can_self_update() is True


def test_can_self_update_linux_deb(monkeypatch):
    monkeypatch.setattr(uc, "is_frozen", lambda: False)
    monkeypatch.setattr(uc, "is_linux", lambda: True)
    monkeypatch.setattr(uc, "linux_install_kind", lambda: "deb")
    assert uc.can_self_update() is True


def test_cannot_self_update_linux_source(monkeypatch):
    monkeypatch.setattr(uc, "is_frozen", lambda: False)
    monkeypatch.setattr(uc, "is_linux", lambda: True)
    monkeypatch.setattr(uc, "linux_install_kind", lambda: "source")
    assert uc.can_self_update() is False


def test_cannot_self_update_plain_source(monkeypatch):
    monkeypatch.setattr(uc, "is_frozen", lambda: False)
    monkeypatch.setattr(uc, "is_linux", lambda: False)
    assert uc.can_self_update() is False
