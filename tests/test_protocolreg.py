"""Tests for djcrate:// protocol registration (fake winreg — no real HKCU)."""
import cratebuilder.protocolreg as pr


class FakeKey:
    def __init__(self, store, path):
        self.store, self.path = store, path


class FakeWinreg:
    """Dict-backed winreg: {key_path: {value_name: value}}."""
    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.keys = {}

    def CreateKey(self, root, path):
        self.keys.setdefault(path, {})
        return FakeKey(self.keys, path)

    def OpenKey(self, root, path, reserved=0, access=0):
        if path not in self.keys:
            raise FileNotFoundError(path)
        return FakeKey(self.keys, path)

    def SetValueEx(self, key, name, reserved, vtype, value):
        self.keys[key.path][name] = value

    def QueryValueEx(self, key, name):
        try:
            return (self.keys[key.path][name], self.REG_SZ)
        except KeyError:
            raise FileNotFoundError(name)

    def DeleteKey(self, root, path):
        if path not in self.keys:
            raise FileNotFoundError(path)
        if any(k != path and k.startswith(path + "\\") for k in self.keys):
            raise OSError("subkeys exist")
        del self.keys[path]

    def CloseKey(self, key):
        pass


def _with_fake(monkeypatch):
    fake = FakeWinreg()
    monkeypatch.setattr(pr, "winreg", fake)
    return fake


def test_register_writes_the_class_tree(monkeypatch):
    fake = _with_fake(monkeypatch)
    assert pr.register_protocol() is True
    root = fake.keys[r"Software\Classes\djcrate"]
    assert root[""] == "URL:DJ-CrateBuilder Protocol"
    assert root["URL Protocol"] == ""
    cmd = fake.keys[r"Software\Classes\djcrate\shell\open\command"][""]
    assert cmd.startswith('"')
    assert cmd.endswith('"%1"')


def test_is_registered_reflects_state(monkeypatch):
    _with_fake(monkeypatch)
    assert pr.protocol_is_registered() is False
    pr.register_protocol()
    assert pr.protocol_is_registered() is True


def test_is_registered_is_false_when_the_stored_command_is_stale(monkeypatch):
    """After the app moves, is reinstalled, or switches source↔frozen, the key
    is still there but points at a path that no longer launches anything. The
    Settings toggle must read "off" then, not "on"."""
    fake = _with_fake(monkeypatch)
    pr.register_protocol()
    assert pr.protocol_is_registered() is True         # matches: really on
    fake.keys[r"Software\Classes\djcrate\shell\open\command"][""] = \
        r'"C:\Old\Install\pythonw.exe" "C:\Old\Install\web_window.py" "%1"'
    assert pr.protocol_is_registered() is False
    pr.register_protocol()                             # re-register repairs it
    assert pr.protocol_is_registered() is True


def test_unregister_removes_everything(monkeypatch):
    fake = _with_fake(monkeypatch)
    pr.register_protocol()
    assert pr.unregister_protocol() is True
    assert r"Software\Classes\djcrate" not in fake.keys
    assert pr.protocol_is_registered() is False


def test_unregister_when_absent_is_ok(monkeypatch):
    _with_fake(monkeypatch)
    assert pr.unregister_protocol() is True


def test_everything_degrades_off_windows(monkeypatch):
    monkeypatch.setattr(pr, "winreg", None)
    assert pr.protocol_is_registered() is False
    assert pr.register_protocol() is False
    assert pr.unregister_protocol() is False


def test_source_command_names_the_entry_script(monkeypatch):
    """From source the handler must launch web_window.py (the shipped entry
    point), not whatever python happens to be on PATH."""
    monkeypatch.setattr(pr.sys, "frozen", False, raising=False)
    monkeypatch.setattr(pr.sys, "argv", [r"C:\src\web_window.py"])
    cmd = pr._handler_command()
    assert cmd.endswith(r'web_window.py" "%1"')
