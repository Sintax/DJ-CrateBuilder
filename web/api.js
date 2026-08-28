/* web/api.js — the only file that knows which transport is live.
   Local mount binds to the pywebview bridge; remote binds to HTTP + WebSocket.
   No screen may import anything else to reach the host. */

(function (global) {
  'use strict';

  const listeners = new Map();          // event name -> Set<handler>
  let socket = null;
  let retryTimer = null;

  /* The device token, held in localStorage so a paired browser stays paired
     across restarts (HANDOFF §8.1). Every access is guarded: a private window
     can refuse storage outright, and an unpaired session is a valid state —
     it just means the pairing screen. */
  const TOKEN_KEY = 'cb_device_token';
  const NAME_KEY = 'cb_device_name';

  /* The two close codes that are NOT authentication failures. A socket closed
     for either reason keeps its token; only 4401 drops one. The text mirrors
     what the host puts in the matching HTTP body, since the socket close frame
     carries no body of its own. */
  const DISABLED_REASON =
    'Remote access is switched off on this host. Turn on “Allow remote ' +
    'control over the internet” in Settings ▸ Remote Access on the host ' +
    'machine to reach it from another device.';
  const BAD_ORIGIN_REASON =
    'This page’s origin is not one the host answers to. If you are ' +
    'reaching it through a proxy or a tunnel, start the host with ' +
    '--host-allow <name> naming the public address.';

  function stored(key) {
    try { return localStorage.getItem(key) || ''; } catch (_) { return ''; }
  }
  function store(key, value) {
    try {
      if (value) localStorage.setItem(key, value);
      else localStorage.removeItem(key);
    } catch (_) { /* storage refused — the session stays in memory only */ }
  }

  let token = '';
  let memoryToken = '';

  function currentToken() { return memoryToken || stored(TOKEN_KEY); }

  function emit(event, payload) {
    const set = listeners.get(event);
    if (!set) return;
    for (const fn of set) {
      try { fn(payload); } catch (err) { console.error(event, err); }
    }
  }

  /* pywebview injects window.pywebview.api once the bridge is ready; it is not
     present at first paint, so wait rather than assuming either transport. */
  function localReady() {
    return !!(global.pywebview && global.pywebview.api && global.pywebview.api.call);
  }

  function waitForLocal(timeoutMs) {
    return new Promise((resolve) => {
      if (localReady()) return resolve(true);
      const started = Date.now();
      const tick = setInterval(() => {
        if (localReady()) { clearInterval(tick); resolve(true); }
        else if (Date.now() - started > timeoutMs) { clearInterval(tick); resolve(false); }
      }, 50);
      /* The event only short-circuits the poll — it must never BE the
         answer. pywebview injects `window.pywebview` and fires this event for
         every window it owns, js_api or not, so a window pointed at the
         server's own URL (which is what a host-side browser test is) would
         otherwise be told it has a local bridge it does not have, and every
         call would land on an undefined function. */
      global.addEventListener('pywebviewready', () => {
        if (localReady()) { clearInterval(tick); resolve(true); }
      }, { once: true });
    });
  }

  function fail(message, extra) {
    const err = new Error(message);
    err.userFacing = true;
    Object.assign(err, extra || {});
    return err;
  }

  function unwrap(envelope) {
    if (envelope && envelope.ok === false) {
      throw fail(envelope.error || 'The host refused that action.');
    }
    return envelope ? envelope.result : null;
  }

  const cbApi = {
    transport: null,          // 'local' | 'remote', set by connect()
    session: null,            // remote only: {can_write, read_only, holder, reason}

    async connect() {
      const local = await waitForLocal(4000);
      this.transport = local ? 'local' : 'remote';
      if (!local) {
        token = currentToken();
        if (!token) {
          /* Unpaired: no socket, no calls. The shell shows the pairing
             screen, which is the only thing an unpaired browser may reach. */
          emit('auth.required', { reason: 'unpaired' });
          return this.transport;
        }
        /* connect() runs again after pairing and after every Retry, and both
           of those have already opened the socket — reopening here would throw
           away a live connection to build the same one. */
        if (!socket) this._openSocket();
      }
      emit('host.status', { online: true, transport: this.transport });
      return this.transport;
    },

    paired() {
      return this.transport !== 'remote' || !!currentToken();
    },

    deviceName() {
      return stored(NAME_KEY);
    },

    /* The pairing screen's own two calls. Neither carries a token — they are
       the only routes an unpaired browser may reach. */
    async pairInfo() {
      try {
        const res = await fetch('/pair/info', { cache: 'no-store' });
        if (!res.ok) return { require_pairing: true };
        return await res.json();
      } catch (_) {
        return { require_pairing: true, offline: true };
      }
    },

    async pair(code, deviceName) {
      let res;
      try {
        res = await fetch('/pair', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ code: code || '', device_name: deviceName || '' }),
        });
      } catch (_) {
        throw fail('The host is unreachable. Check it is running, then retry.');
      }
      let data = {};
      try { data = await res.json(); } catch (_) { data = {}; }
      if (!res.ok) throw fail(data.detail || 'Pairing failed.');
      memoryToken = data.token;
      store(TOKEN_KEY, data.token);
      store(NAME_KEY, (data.device && data.device.name) || deviceName || '');
      token = data.token;
      this.session = data.session || null;
      this._openSocket();
      emit('host.status', { online: true, transport: 'remote' });
      return data;
    },

    forgetPairing() {
      memoryToken = '';
      token = '';
      store(TOKEN_KEY, '');
      this.session = null;
    },

    async call(method, params) {
      if (this.transport === 'local') {
        return unwrap(await global.pywebview.api.call(method, params || {}));
      }
      const active = currentToken();
      if (!active) {
        emit('auth.required', { reason: 'unpaired' });
        throw fail('Pair this device to reach the host.', { needsPairing: true });
      }
      let res;
      try {
        res = await fetch('/rpc', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CB-Token': active },
          body: JSON.stringify({ method, params: params || {} }),
        });
      } catch (_) {
        emit('host.status', { online: false, transport: 'remote' });
        throw fail('The host is unreachable.');
      }
      if (res.status === 401) {
        /* The token this browser holds is no longer one the host knows —
           revoked, or the store was reset. Drop it and ask to pair again. */
        this.forgetPairing();
        emit('auth.required', { reason: 'revoked' });
        throw fail('This device is no longer paired with the host.',
                   { needsPairing: true });
      }
      if (!res.ok) {
        /* The host answered, and said why. A 403 carries the "remote access is
           switched off" reason and a 421 the "not a name I answer to" one —
           collapsing either into "unreachable" sends the user to look at their
           network when the host is right there telling them what to change. */
        let detail = '';
        try { detail = (await res.json()).detail || ''; } catch (_) { detail = ''; }
        if (detail) emit('host.status', { online: false, transport: 'remote',
                                          reason: detail });
        throw fail(detail || 'The host is unreachable.');
      }
      return unwrap(await res.json());
    },

    on(event, handler) {
      if (!listeners.has(event)) listeners.set(event, new Set());
      listeners.get(event).add(handler);
      return () => listeners.get(event).delete(handler);
    },

    /* Called by the host from a worker thread via evaluate_js — this is what
       replaces tkinter's after() polling. Never poll from JS. */
    _push(event, payload) { emit(event, payload); },

    _openSocket() {
      const active = currentToken();
      if (!active) return;
      clearTimeout(retryTimer);
      retryTimer = null;
      /* One socket per page, ever. Two paths open one each — pair() then
         boot()'s connect(), and Retry then boot() again — and a surviving
         socket is not harmless: it subscribes on the host independently, so
         every progress frame and log line arrives (and is rendered) once per
         leaked socket. Detach onclose first, or closing this one schedules a
         reconnect for a socket we are replacing. */
      if (socket) {
        socket.onclose = null;
        socket.onmessage = null;
        try { socket.close(); } catch (_) { /* already closing */ }
        socket = null;
      }
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      /* The token rides in the query string here and nowhere else: the browser
         WebSocket API cannot set a request header, so this is the only way to
         authenticate the handshake. /rpc and /logs use X-CB-Token. */
      try {
        socket = new WebSocket(
          `${proto}//${location.host}/ws?token=${encodeURIComponent(active)}`);
      } catch (_) { return; }
      socket.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === 'host.status' && msg.payload && msg.payload.session) {
            cbApi.session = msg.payload.session;
          }
          emit(msg.type, msg.payload);
        } catch (_) { /* ignore malformed frames */ }
      };
      const mine = socket;
      socket.onclose = (ev) => {
        if (socket !== mine) return;  // superseded; its replacement owns the retry
        socket = null;
        const code = ev && ev.code;
        if (code === 4401) {
          /* The only code that means "your token is no good". Everything else
             below keeps it — a client that treats a deployment problem as
             revocation throws away a perfectly valid token and sends the user
             to a pairing screen that cannot fix anything. */
          this.forgetPairing();
          emit('auth.required', { reason: 'revoked' });
          return;                     // no point retrying with a dead token
        }
        if (code === 4421) {
          /* This page's origin is not one the host answers to — a proxy or
             tunnel whose public name has not been named with --host-allow.
             Retrying cannot change the answer, so say so and stop; the offline
             bar's Retry is still there if the host is reconfigured. */
          emit('host.status', { online: false, transport: 'remote',
                                reason: BAD_ORIGIN_REASON, retrying: false });
          return;
        }
        /* 4403 is "the host switched remote access off" — the token is still
           good, so keep it and keep trying rather than demanding a new code. */
        emit('host.status', {
          online: false, transport: 'remote', retrying: true,
          reason: code === 4403 ? DISABLED_REASON : '' });
        clearTimeout(retryTimer);
        retryTimer = setTimeout(() => this._openSocket(), 3000);
      };
    },

    /* Retry now rather than waiting out the reconnect timer — the offline
       bar's Retry button (design 3k). */
    reconnect() {
      clearTimeout(retryTimer);
      if (this.transport === 'remote') this._openSocket();
    },

    /* Fetch a host file the RPC envelope cannot carry (the log downloads).
       Goes out with the token header, so it is never a bare navigation with a
       token in the URL. */
    async fetchFile(path) {
      const active = currentToken();
      const res = await fetch(path, { headers: active ? { 'X-CB-Token': active } : {} });
      if (res.status === 401) {
        this.forgetPairing();
        emit('auth.required', { reason: 'revoked' });
        throw fail('This device is no longer paired with the host.');
      }
      if (!res.ok) {
        let detail = '';
        try { detail = (await res.json()).detail || ''; } catch (_) { detail = ''; }
        throw fail(detail || 'The host could not send that file.');
      }
      return await res.blob();
    },
  };

  global.cbApi = cbApi;
})(window);
