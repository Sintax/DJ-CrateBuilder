"""Remote mount for the web frontend: uvicorn over cratebuilder.server.

Serves the same `web/` bundle the pywebview window loads, to browsers on the
LAN. Start it with `python web_server.py`; the tkinter app and the desktop
window are unaffected and can run beside it.

Binds 127.0.0.1 unless `--lan` is given AND remote access has been switched on
in the host's own settings — nothing here ever turns that on for you. Plain
HTTP is LAN-only by design (HANDOFF §8.2): put it behind Caddy or a Cloudflare
Tunnel for anything wider.
"""

import argparse
import os
import sys

import uvicorn

from cratebuilder.remoteauth import REMOTE_FILE_NAME
from cratebuilder.server import bind_host, create_app, uvicorn_kwargs
from cratebuilder.service import (ACTIVITY_LOG, DB_NAME, DEBUG_LOG, REMOTE,
                                  CrateBuilderService, app_dir)
from cratebuilder.settings import Settings

DEFAULT_PORT = 8770

# Console copy stays inside cp1252: a Windows terminal at the default code page
# raises UnicodeEncodeError on print(), which would take the whole process down
# at startup. The pretty typography lives in the messages that travel as JSON
# (remoteauth.DISABLED_REASON), which are UTF-8 all the way to the browser.
LAN_REFUSED = (
    "Remote access is switched off, so --lan has nothing to bind.\n"
    "Turn on 'Allow remote control over the internet' in Settings > Remote "
    "Access on the host, then start this again.")

DISABLED_NOTE = (
    "  NOTE         : remote access is OFF - every route refuses until\n"
    "                 'Allow remote control over the internet' is switched on\n"
    "                 in Settings > Remote Access on the host.")


def build_service(data_dir=None):
    """The service and its remote state, both pointed at *data_dir*.

    *data_dir* exists so this can be run against a throwaway config, database
    and token store — never the user's real library — without editing anything.
    """
    if not data_dir:
        return CrateBuilderService(transport=REMOTE)
    data_dir = os.path.abspath(os.path.expanduser(data_dir))
    os.makedirs(data_dir, exist_ok=True)
    settings = Settings(path=os.path.join(data_dir, "config.json"))
    return CrateBuilderService(
        transport=REMOTE,
        settings=settings,
        db_path=os.path.join(data_dir, DB_NAME),
        log_path=os.path.join(data_dir, ACTIVITY_LOG),
        debug_log_path=os.path.join(data_dir, DEBUG_LOG))


def announce_pairing(state, host, port, force=False):
    """Print a pairing code when there is no other way to get one.

    The desktop window's Remote Access card is the designed source of the code;
    this covers the headless case — a host with no paired device yet and no
    window open would otherwise be unreachable forever. Printed to the host's
    own console only, and never written to a log.
    """
    if not force and state.device_count():
        return None
    issued = state.begin_pairing()
    shown = f"{issued['code'][:3]} {issued['code'][3:]}"
    print(f"\n  Pairing code: {shown}    (valid for "
          f"{issued['ttl'] // 60} minutes)")
    print(f"  Open http://{host}:{port}/ on the device and enter it.\n")
    return issued


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Serve the DJ-CrateBuilder web UI to remote browsers.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"TCP port to listen on (default {DEFAULT_PORT})")
    parser.add_argument("--lan", action="store_true",
                        help="bind 0.0.0.0 instead of loopback — only honoured "
                             "when remote access is enabled in settings")
    parser.add_argument("--pair", action="store_true",
                        help="print a fresh pairing code at startup")
    parser.add_argument("--data-dir", default=None,
                        help="run against a throwaway config/database/token "
                             "store in this folder instead of the app dir")
    args = parser.parse_args(argv)

    service = build_service(args.data_dir)
    state = service.remote_state

    host = bind_host(state, lan=args.lan)
    if host is None:
        sys.exit(LAN_REFUSED)

    app = create_app(service, state)
    where = args.data_dir or app_dir()
    print(f"DJ-CrateBuilder remote mount  ·  http://{host}:{args.port}/")
    print(f"  data dir     : {where}")
    print(f"  token store  : {os.path.join(where, REMOTE_FILE_NAME)}")
    print(f"  paired       : {state.device_count()} device(s)")
    print(f"  read-only    : {'on' if state.get_flag('read_only') else 'off'}")
    if not state.get_flag("enabled"):
        print(DISABLED_NOTE)
    announce_pairing(state, host, args.port, force=args.pair)
    uvicorn.Server(uvicorn.Config(app, host=host, port=args.port,
                                  log_level="info", **uvicorn_kwargs())).run()


if __name__ == "__main__":
    main()
