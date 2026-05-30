from cratebuilder import startup

class _FakeReg:
    """Minimal in-memory stand-in for winreg."""
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1; KEY_SET_VALUE = 2; REG_SZ = 1
    def __init__(self): self.store = {}
    def OpenKey(self, root, path, res=0, access=0): return ("k", path)
    def QueryValueEx(self, key, name):
        if name in self.store: return (self.store[name], self.REG_SZ)
        raise FileNotFoundError(name)
    def SetValueEx(self, key, name, r, t, val): self.store[name] = val
    def DeleteValue(self, key, name): self.store.pop(name, None)
    def CloseKey(self, key): pass

def test_set_and_check_startup(monkeypatch):
    fake = _FakeReg()
    monkeypatch.setattr(startup, "winreg", fake, raising=False)
    monkeypatch.setattr(startup, "_startup_command", lambda: '"C:/app.exe"')
    assert startup.startup_is_enabled() is False
    startup.set_startup(True)
    assert startup.startup_is_enabled() is True
    startup.set_startup(False)
    assert startup.startup_is_enabled() is False


def test_degrades_when_winreg_unavailable(monkeypatch):
    # Off-Windows (or winreg import failed): every call is a safe no-op.
    monkeypatch.setattr(startup, "winreg", None, raising=False)
    assert startup.startup_is_enabled() is False
    assert startup.set_startup(True) is False
    assert startup.set_startup(False) is False


def test_set_startup_reports_failure_on_oserror(monkeypatch):
    # A registry write failure must surface as False so the UI can revert.
    class _BrokenReg(_FakeReg):
        def OpenKey(self, *a, **k):
            raise OSError("access denied")
    monkeypatch.setattr(startup, "winreg", _BrokenReg(), raising=False)
    assert startup.set_startup(True) is False
    assert startup.startup_is_enabled() is False


def test_startup_command_from_source_quotes_script(monkeypatch):
    # Non-frozen: command must quote the runner and include the script path.
    monkeypatch.setattr(startup.sys, "frozen", False, raising=False)
    monkeypatch.setattr(startup.sys, "executable", r"C:\Py\python.exe")
    monkeypatch.setattr(startup.sys, "argv", [r"C:\app\DJ-CrateBuilder_v1.3.py"])
    cmd = startup._startup_command()
    assert cmd.startswith('"')
    assert "DJ-CrateBuilder_v1.3.py" in cmd
