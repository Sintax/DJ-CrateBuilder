"""Windows 'run at login' via the per-user Run registry key.

All functions degrade gracefully (return False / no-op) off-Windows or on error.
"""
import os
import sys

try:
    import winreg  # Windows only
except ImportError:  # pragma: no cover
    winreg = None

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "DJ-CrateBuilder"


def _startup_command():
    """Quoted command Windows should run at login."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --startup'
    # Running from source: prefer pythonw.exe (no console window).
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    runner = pyw if os.path.exists(pyw) else exe
    script = os.path.abspath(sys.argv[0])
    return f'"{runner}" "{script}" --startup'


def startup_is_enabled():
    if winreg is None:
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, _VALUE_NAME)
            return True
        finally:
            winreg.CloseKey(key)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_startup(enabled):
    """Add or remove the Run entry. Returns True on success."""
    if winreg is None:
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                             winreg.KEY_SET_VALUE)
        try:
            if enabled:
                winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ,
                                  _startup_command())
            else:
                try:
                    winreg.DeleteValue(key, _VALUE_NAME)
                except FileNotFoundError:
                    pass
            return True
        finally:
            winreg.CloseKey(key)
    except OSError:
        return False
