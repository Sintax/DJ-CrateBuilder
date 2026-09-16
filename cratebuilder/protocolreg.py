"""Windows djcrate:// protocol registration via HKCU Software\\Classes.

Mirrors startup.py's degrade-gracefully pattern: every function returns False
or no-ops off-Windows and on registry error. Per-user, no admin rights.
Wire contract: the extension repo's docs/specs/djcrate-uri-v1.md. Linux has
no registry — there the .desktop entry's MimeType line does this job.
"""
import os
import sys

try:
    import winreg  # Windows only
except ImportError:  # pragma: no cover
    winreg = None

_CLASS_KEY = r"Software\Classes\djcrate"
_COMMAND_KEY = _CLASS_KEY + r"\shell\open\command"


def _handler_command():
    """Quoted command Windows should run for a djcrate:// navigation.

    Frozen, sys.executable IS the app (web_window.py built onedir). From
    source, sys.argv[0] is web_window.py and pythonw keeps a console window
    from flashing up on every send."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" "%1"'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    runner = pyw if os.path.exists(pyw) else exe
    script = os.path.abspath(sys.argv[0])
    return f'"{runner}" "{script}" "%1"'


def protocol_is_registered():
    """True only when the stored command is the one THIS install would write.

    Merely checking the value exists left the Settings toggle reading "on"
    after the app moved, was reinstalled, or switched between source and
    frozen — while djcrate:// launched a path that no longer works. A
    mismatch reads as off, so flicking the toggle rewrites it.
    """
    if winreg is None:
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _COMMAND_KEY, 0,
                             winreg.KEY_READ)
        try:
            stored = winreg.QueryValueEx(key, "")[0]
        finally:
            winreg.CloseKey(key)
    except OSError:      # FileNotFoundError included — no key, no handler
        return False
    return stored == _handler_command()


def register_protocol():
    """Write the class tree. Returns True on success. Re-registering is the
    supported way to refresh a stale path after the app moves."""
    if winreg is None:
        return False
    try:
        root = winreg.CreateKey(winreg.HKEY_CURRENT_USER, _CLASS_KEY)
        winreg.SetValueEx(root, "", 0, winreg.REG_SZ,
                          "URL:DJ-CrateBuilder Protocol")
        winreg.SetValueEx(root, "URL Protocol", 0, winreg.REG_SZ, "")
        winreg.CloseKey(root)
        cmd = winreg.CreateKey(winreg.HKEY_CURRENT_USER, _COMMAND_KEY)
        winreg.SetValueEx(cmd, "", 0, winreg.REG_SZ, _handler_command())
        winreg.CloseKey(cmd)
        return True
    except OSError:
        return False


def unregister_protocol():
    """Delete the class tree leaf-first (winreg has no recursive delete).
    Returns True when gone, including when it was never there."""
    if winreg is None:
        return False
    for path in (_COMMAND_KEY,
                 _CLASS_KEY + r"\shell\open",
                 _CLASS_KEY + r"\shell",
                 _CLASS_KEY):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except FileNotFoundError:
            pass
        except OSError:
            return False
    return True
