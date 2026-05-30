import sys, types, pytest
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
