/* web/api.js — the only file that knows which transport is live.
   Local mount binds to the pywebview bridge; remote binds to HTTP + WebSocket.
   No screen may import anything else to reach the host. */

(function (global) {
  'use strict';

  const listeners = new Map();          // event name -> Set<handler>
  let socket = null;

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
      global.addEventListener('pywebviewready', () => {
        clearInterval(tick); resolve(true);
      }, { once: true });
    });
  }

  function unwrap(envelope) {
    if (envelope && envelope.ok === false) {
      const err = new Error(envelope.error || 'The host refused that action.');
      err.userFacing = true;
      throw err;
    }
    return envelope ? envelope.result : null;
  }

  const cbApi = {
    transport: null,          // 'local' | 'remote', set by connect()

    async connect() {
      const local = await waitForLocal(4000);
      this.transport = local ? 'local' : 'remote';
      if (!local) this._openSocket();
      emit('host.status', { online: true, transport: this.transport });
      return this.transport;
    },

    async call(method, params) {
      if (this.transport === 'local') {
        return unwrap(await global.pywebview.api.call(method, params || {}));
      }
      const res = await fetch('/rpc', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ method, params: params || {} }),
      });
      if (!res.ok) {
        const err = new Error('The host is unreachable.');
        err.userFacing = true;
        throw err;
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
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      try {
        socket = new WebSocket(`${proto}//${location.host}/ws`);
      } catch (_) { return; }
      socket.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          emit(msg.type, msg.payload);
        } catch (_) { /* ignore malformed frames */ }
      };
      socket.onclose = () => {
        emit('host.status', { online: false, transport: 'remote' });
        setTimeout(() => this._openSocket(), 3000);
      };
    },
  };

  global.cbApi = cbApi;
})(window);
