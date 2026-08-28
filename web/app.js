/* web/app.js — screens, wiring and the tooltip engine.
   Every host call goes through cbApi; nothing here knows the transport. */

(function () {
  'use strict';

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  let TOOLTIPS = {};
  let SETTINGS_KEYS = [];
  let state = null;

  /* ── batch runtime state ───────────────────────────────────────────────────
     Everything here is push-driven (events), never polled, and lives
     separately from `state` (the last state.snapshot) because it changes many
     times a second while `state` only changes on a snapshot/patch. */
  const dl = {
    running: false,
    paused: false,
    rows: {},        // queue row id -> {state, title, detail} from queue.row
    current: null,    // last progress.current payload
    overall: null,    // last progress.overall payload
  };

  const DL_MARK = { done: '✓', active: '▶', skipped: '⊘', error: '✗', queued: '·' };
  const DL_LOG_CLASS = { done: 'downloaded', skipped: 'skipped', error: 'error', queued: 'default' };
  const DL_MARK_COLOR = { done: 'var(--cb-ok)', skipped: 'var(--cb-warn)', error: 'var(--cb-err)', active: 'var(--cb-accent)' };

  /* ── tooltips ───────────────────────────────────────────────────────────
     theme.css styles a hover-only mockup; the contract requires focus,
     Escape and aria-describedby, so the live behaviour is driven here. */
  const tip = { el: null, timer: null, host: null, described: null };

  function showTip(host, text) {
    if (!text) return;
    hideTip();
    const el = document.createElement('div');
    el.className = 'cb-tip';
    el.setAttribute('role', 'tooltip');
    el.id = 'cb-tip-live';
    el.textContent = text;
    document.body.appendChild(el);

    const r = host.getBoundingClientRect();
    const box = el.getBoundingClientRect();
    let left = r.left;
    let top = r.bottom + 7;
    if (left + box.width > innerWidth - 10) left = innerWidth - box.width - 10;
    if (top + box.height > innerHeight - 10) top = r.top - box.height - 7;
    el.style.left = Math.max(10, left) + 'px';
    el.style.top = Math.max(10, top) + 'px';

    /* A disabled control already points aria-describedby at its own reason
       node (see describeReason); the live bubble borrows the attribute while
       it is up and hands it back, rather than silently unwiring it. */
    tip.described = host.getAttribute('aria-describedby');
    host.setAttribute('aria-describedby', el.id);
    tip.el = el;
    tip.host = host;
  }

  function hideTip() {
    if (tip.el) tip.el.remove();
    if (tip.host) {
      if (tip.described) tip.host.setAttribute('aria-describedby', tip.described);
      else tip.host.removeAttribute('aria-describedby');
    }
    tip.el = null;
    tip.host = null;
    tip.described = null;
  }

  /* The bubble is a snapshot of the text at the moment it opened, and a
     control's reason can change underneath a pointer that never moved — a
     batch starts, Cancel is re-enabled, and the bubble would go on saying "No
     download is running". Whoever rewrites the text says so here. */
  function refreshTip(host) {
    if (!tip.el || tip.host !== host) return;
    const text = tipText(host);
    if (text) tip.el.textContent = text;
    else hideTip();
  }

  function tipText(host) {
    const key = host.getAttribute('data-tt');
    return host.getAttribute('data-tt-text') || (key ? TOOLTIPS[key] : '') || '';
  }

  /* The hover half of the engine is ONE document-level tracker, not a listener
     per control, and that is the whole fix for the disabled-reason tooltips.
     A disabled form control dispatches no mouse events at all — not to itself
     and not to its ancestors — so a `mouseenter` bound to it can never fire,
     and every "why is this off" explainer in the bundle was unreachable.
     Hit-testing is a separate mechanism and is NOT suppressed by `disabled`,
     so asking what is under the pointer finds disabled controls exactly like
     enabled ones. (`pointer-events: none` does remove an element from hit
     testing — which is why the offline sweep in app.css, where every control
     is deliberately inert, stays tooltip-free.)

     Focus/blur stay per-element in bindTips: a disabled control cannot be
     focused in the first place, so there is nothing there for them to miss —
     what covers a disabled control for a screen reader is describeReason's
     aria-describedby node, not this. */
  let tipUnder = null;      // the carrier the last hit test found
  let tipAt = null;         // where that hit test was taken, [x, y]
  let tipPending = null;    // the newest coordinates awaiting a frame
  let tipFrame = 0;

  /* How far the pointer must travel before the answer could plausibly have
     changed. Controls are at least 26px tall, so a few pixels cannot cross
     from one carrier to another. */
  const TIP_SLOP = 4;

  function tipHostAt(x, y) {
    const el = document.elementFromPoint(x, y);
    return el ? el.closest('[data-tt],[data-tt-text]') : null;
  }

  /* Everything that invalidates the last hit test rather than replacing it:
     the page scrolled under a still pointer, the pointer left the window, a
     click landed, the screen changed. Clearing the COORDINATES as well as the
     carrier is the point — leaving them behind would let the slop guard below
     early-out at the same position and never re-test. */
  function tipForget() {
    tipUnder = null;
    tipAt = null;
    clearTimeout(tip.timer);
  }

  function tipTrack(x, y) {
    /* Two guards before the hit test, in order of cost. elementFromPoint
       forces a style/layout flush, and this runs at pointer-event rate over a
       DOM that can be 200 table rows while progress bars are being written —
       a read-after-write thrash pattern if it is not held down. A pointer
       resting on a control costs nothing at all. */
    if (tipAt && Math.abs(x - tipAt[0]) < TIP_SLOP
               && Math.abs(y - tipAt[1]) < TIP_SLOP) return;
    tipAt = [x, y];
    const host = tipHostAt(x, y);
    if (host === tipUnder) return;
    tipUnder = host;
    clearTimeout(tip.timer);
    if (tip.host && tip.host !== host) hideTip();
    if (!host) return;
    tip.timer = setTimeout(() => {
      if (tipUnder === host) showTip(host, tipText(host));
    }, 350);
  }

  /* Coalesced to one hit test per frame: pointermove fires far faster than
     the page repaints, and the answer cannot change between two frames. */
  document.addEventListener('pointermove', (e) => {
    if (e.pointerType === 'touch') return;      // touch is a long-press, not a hover
    tipPending = [e.clientX, e.clientY];
    if (tipFrame) return;
    tipFrame = requestAnimationFrame(() => {
      tipFrame = 0;
      const at = tipPending;
      tipPending = null;
      if (at) tipTrack(at[0], at[1]);
    });
  }, true);
  /* Leaving the window is not "moved to another control" — nothing else
     reports it, so the last tracked host would otherwise stay armed. */
  document.addEventListener('pointerout', (e) => {
    if (e.relatedTarget) return;
    tipForget();
    hideTip();
  }, true);
  document.addEventListener('pointerdown', () => {
    tipForget();
    hideTip();
  }, true);

  function bindTips(root) {
    $$('[data-tt],[data-tt-text]', root).forEach((host) => {
      if (host.__tipBound) return;
      host.__tipBound = true;
      host.addEventListener('focus', () => showTip(host, tipText(host)));
      host.addEventListener('blur', hideTip);
    });
  }

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    hideTip();
    closeNotifications();
  });
  /* Scrolling moves the page under a pointer that never moved, so the last
     hit test is answering about coordinates that now hold something else —
     and a tracker left holding the control the cursor is still resting on
     goes mute until the pointer leaves it and comes back. */
  addEventListener('scroll', () => { tipForget(); hideTip(); }, true);

  /* ── toast ─────────────────────────────────────────────────────────────── */
  let toastTimer = null;
  function toast(message, isError) {
    const el = $('#toast');
    el.textContent = message;
    el.classList.toggle('is-err', !!isError);
    el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 4200);
  }

  async function call(method, params) {
    try {
      return await cbApi.call(method, params);
    } catch (err) {
      toast(err.userFacing ? err.message : 'The host could not complete that.', true);
      throw err;
    }
  }

  /* ── remote session: read-only mode and the single-writer lock ────────────
     The local window is always a writer and never contends for the lock
     (HANDOFF §2 gives it precedence), so this is remote-only. The host answers
     with the reason in one sentence — a disabled control's explainer is
     exactly what that sentence is for, so nothing here paraphrases it. */

  let session = null;             // remote only: the host's own verdict

  function writeBlocked() {
    if (!state || state.host.transport !== 'remote') return '';
    if (!session) return '';
    return session.can_write ? '' : (session.reason ||
      'This device cannot control the host right now.');
  }

  /* Ask the host what this device may do. Cheap, and re-asked whenever the
     lock changes — `control.holder` is pushed to every socket, so a client
     that just lost control finds out without polling. */
  async function refreshSession() {
    if (!cbApi || cbApi.transport !== 'remote') { session = null; return null; }
    try {
      session = await cbApi.call('remote.session');
      cbApi.session = session;
    } catch (_) { session = cbApi.session || null; }
    return session;
  }

  /* ── navigation ────────────────────────────────────────────────────────── */
  const SCREENS = ['overview', 'downloads', 'watchlist', 'settings',
                    'activity-log', 'debug-log', 'database', 'about'];
  /* The log screens and the database viewer aren't nav items (they open
     from Settings, per the contract's shell.not_in_nav) — while any of them
     is open, Settings stays the highlighted nav entry, per
     shell.active_item_rule. About (3n) is in the same class: the contract's
     nav is exactly four items, so About opens from the panel footer's build
     line and from Settings, carries the same `‹ Settings` breadcrumb the
     other four do, and leaves Settings highlighted. */
  const NAV_ALIAS = { 'activity-log': 'settings', 'debug-log': 'settings',
                      'database': 'settings', 'about': 'settings' };
  const LOG_KIND_BY_SCREEN = { 'activity-log': 'activity', 'debug-log': 'debug' };
  let currentScreen = null;

  function show(name) {
    if (!SCREENS.includes(name)) name = 'overview';
    const previous = currentScreen;
    currentScreen = name;
    /* The control a tooltip is anchored to is about to be hidden, and nothing
       else will report the pointer leaving it — a bubble left behind floats
       over the new screen explaining a control that is no longer on it. */
    hideTip();
    tipUnder = null;
    $$('.cb-screen').forEach((s) => s.classList.toggle('is-on', s.id === 'screen-' + name));
    const navName = NAV_ALIAS[name] || name;
    $$('.cb-nav').forEach((a) => a.classList.toggle('is-on', a.dataset.screen === navName));
    $('.cb-main').scrollTop = 0;
    if (location.hash.slice(1) !== name) location.hash = name;

    const leavingKind = LOG_KIND_BY_SCREEN[previous];
    if (leavingKind && leavingKind !== LOG_KIND_BY_SCREEN[name]) logClose(leavingKind);
    const enteringKind = LOG_KIND_BY_SCREEN[name];
    if (enteringKind) logOpen(enteringKind);
    if (name === 'database' && previous !== 'database') dbOpen();
    if (name === 'about') aboutOpen();
    /* Every other screen repaints on entry; the Overview aggregates all of
       them, so it is the one that goes stale fastest — a setting changed on
       Settings, or a batch paused on Downloads, has to be on it when you
       arrive rather than at the next snapshot. */
    if (name === 'overview' && state) renderOverview();
    if (name !== 'overview') closeNotifications();
  }

  /* The nav is real anchors, so routing is the hash — that keeps deep links
     working and means the back gesture behaves in both mounts. */
  addEventListener('hashchange', () => show(location.hash.slice(1)));

  /* ── rendering ─────────────────────────────────────────────────────────── */
  const num = (n) => Number(n || 0).toLocaleString();

  /* Scan timestamps are epoch seconds on the host; show the same relative
     phrasing the desktop cards use rather than a raw number. */
  function fmtWhen(ts) {
    const secs = Number(ts);
    if (!secs) return 'never';
    const ago = Math.floor(Date.now() / 1000 - secs);
    if (ago < 60) return 'just now';
    if (ago < 3600) return `${Math.floor(ago / 60)} min ago`;
    if (ago < 86400) return `${Math.floor(ago / 3600)} h ago`;
    return new Date(secs * 1000).toLocaleDateString();
  }

  /* The one count the contract lets the nav carry (shell.nav), and the only
     place it is written. Called from the snapshot, from every state.patch, and
     from a watchlist.card replacement — a badge that only tracked snapshots
     would go stale the moment a scan reported its first channel. */
  function renderNavCount() {
    const pending = wl.cards.length ? wlPending()
      : ((state && state.counts && state.counts.pending_new) || 0);
    const badge = $('#nav-count');
    badge.textContent = num(pending);
    badge.hidden = pending === 0;
    return pending;
  }

  /* The last reason `host.status` gave for being offline — remote access
     switched off, a name the host does not answer to. Kept because the footer
     repaints from a snapshot too, where that reason is not in scope. */
  let hostReason = '';

  /* The footer's dot and label are the `host.status` event made visible: its
     `online` drives the colour, and the reason it carries is what the label's
     tooltip says, on top of the registry's own sentence. */
  function renderHostFooter() {
    const host = state.host;
    const dot = $('#host-dot');
    dot.classList.toggle('is-on', !!host.online);
    dot.classList.toggle('is-off', !host.online);
    $('#host-label').textContent = host.online
      ? (host.transport === 'local' ? 'host · this machine' : 'host · paired')
      : 'host offline';
    const row = $('#host-status');
    const base = TOOLTIPS['remote.host_status'] || '';
    if (hostReason) row.setAttribute('data-tt-text', base ? base + '\n\n' + hostReason
                                                          : hostReason);
    else row.removeAttribute('data-tt-text');
  }

  function renderShell() {
    const app = state.app;
    const host = state.host;
    $('#mount-tag').textContent = host.transport === 'local' ? 'Local' : 'Remote';
    renderHostFooter();
    $('#host-version').textContent = app.version
      ? `v${app.version} · build ${app.build}` : 'About';

    renderNavCount();
    renderSessionBar();
  }

  /* The single-writer lock, made visible (HANDOFF §2). A remote client that
     cannot write says why in one bar, and — unless the host is read-only,
     where there is nothing to take — offers to take control. */
  function renderSessionBar() {
    const blocked = writeBlocked();
    let bar = $('#cb-session-bar');
    if (!blocked) { if (bar) bar.remove(); return; }
    if (!bar) {
      bar = document.createElement('div');
      bar.className = 'cb-session-bar';
      bar.id = 'cb-session-bar';
      const main = $('.cb-main');
      main.insertBefore(bar, main.firstChild);
    }
    bar.innerHTML = '';
    const tag = tagNode(session && session.read_only ? 'Read-only' : 'Watching',
                        'cb-tag--grey');
    const text = document.createElement('span');
    text.textContent = blocked;
    bar.append(tag, text);
    if (session && !session.read_only) {
      const take = document.createElement('button');
      take.className = 'cb-btn cb-btn--sm';
      take.style.marginLeft = 'auto';
      take.dataset.readOk = '1';
      take.textContent = 'Take control';
      take.setAttribute('data-tt-text',
        'Claims the single-writer lock so this device can start, cancel and ' +
        'change things. Only one device can drive the host at a time; the app ' +
        'window on the host machine always has precedence.');
      take.addEventListener('click', async () => {
        try {
          session = await cbApi.call('remote.claim_control');
          cbApi.session = session;
          renderDownloads();
          renderWatchlist();
          renderSettings();
          renderSessionBar();
          toast('This device now has control.');
        } catch (ex) {
          toast(ex.userFacing ? ex.message : 'Could not take control.', true);
        }
      });
      bar.appendChild(take);
    }
    bindTips(bar);
  }

  /* ── host offline (3k) ────────────────────────────────────────────────────
     A dropped socket must never empty the screen: the last state stays put,
     every control goes inert (theme.css's .cb-offline), and one bar says what
     happened and offers the way back. A remote surface that blanks itself on a
     dropped socket reads as data loss — the design says so in as many words,
     and the acceptance criteria demand no blank page. */

  let offlineSince = null;

  /* When the host says WHY it is refusing — remote access switched off, or a
     name it does not answer to — the bar says that instead of "offline". A
     Retry against a host that is up and refusing can never succeed, and a bar
     that hides the reason sends the user to look at their network. */
  function setHostOffline(offline, reason) {
    document.body.classList.toggle('cb-offline', !!offline);
    let bar = $('#cb-offline-bar');
    if (!offline) {
      offlineSince = null;
      if (bar) bar.remove();
      return;
    }
    if (!offlineSince) offlineSince = new Date();
    if (bar) {
      const why = $('#cb-offline-why', bar);
      if (why) why.textContent = reason || '';
      return;
    }
    bar = document.createElement('div');
    bar.className = 'cb-offline-bar';
    bar.id = 'cb-offline-bar';
    const dot = document.createElement('span');
    dot.style.cssText = 'width:8px;height:8px;border-radius:50%;background:#B4B9C3;flex:none';
    const text = document.createElement('span');
    text.innerHTML = 'Host offline — showing the last state received ' +
      '<span class="cb-mono" id="cb-offline-at"></span>';
    /* What "offline" means is the registry's to say — remote.host_status is
       written for exactly this state. */
    text.setAttribute('data-tt', 'remote.host_status');
    text.tabIndex = 0;
    const retry = document.createElement('button');
    retry.className = 'cb-btn cb-btn--sm';
    retry.id = 'cb-offline-retry';
    retry.textContent = 'Retry';
    retry.setAttribute('data-tt-text',
      'Try the host again now instead of waiting for the next automatic ' +
      'reconnect. Nothing you pressed while offline was queued.');
    retry.addEventListener('click', async () => {
      retry.disabled = true;
      retry.textContent = 'Retrying…';
      cbApi.reconnect();
      try {
        await boot();               // re-runs the whole connect, once wired
      } catch (_) {
        retry.disabled = false;
        retry.textContent = 'Retry';
      }
    });
    const why = document.createElement('span');
    why.id = 'cb-offline-why';
    why.style.cssText = 'opacity:.9;min-width:0';
    why.textContent = reason || '';

    bar.append(dot, text, why, retry);
    document.body.insertBefore(bar, document.body.firstChild);
    $('#cb-offline-at').textContent =
      offlineSince.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    bindTips(bar);
  }

  /* ── pairing (3k) ─────────────────────────────────────────────────────────
     The only thing an unpaired browser may reach. Rendered over everything —
     there is no state behind it to protect, because the host refused to send
     any. */

  function showPairing(opts) {
    opts = opts || {};
    if ($('#cb-pair')) return;
    hideTip();
    const wrap = document.createElement('div');
    wrap.className = 'cb-pair';
    wrap.id = 'cb-pair';

    const card = document.createElement('div');
    card.className = 'cb-pair__card';

    const brand = document.createElement('div');
    brand.className = 'cb-row';
    brand.style.cssText = 'gap:9px;justify-content:center;margin-bottom:22px';
    brand.innerHTML = '<img src="assets/logo.png" alt="" width="26" height="26" ' +
      'style="border-radius:6px;display:block;flex:none">' +
      '<span style="font-weight:600;font-size:17px;letter-spacing:-.01em">CrateBuilder</span>' +
      '<span class="cb-tag">Remote</span>';

    const head = document.createElement('h3');
    head.className = 'cb-h';
    head.style.cssText = 'font-size:21px;margin-bottom:8px';
    head.textContent = 'Pair with your desktop';

    const lead = document.createElement('p');
    lead.className = 'cb-mut';
    lead.style.cssText = 'font-size:13.5px;max-width:350px;margin:0 auto;line-height:1.55';
    lead.textContent = opts.reason === 'revoked'
      ? 'This device is no longer paired with the host. Open Settings → ' +
        'Remote Access on the desktop app, start a new pairing code, and type it below.'
      : 'On the desktop app open Settings → Remote Access, then type the ' +
        'pairing code below.';

    const codeIn = document.createElement('input');
    codeIn.className = 'cb-in cb-mono';
    codeIn.id = 'cb-pair-code';
    codeIn.inputMode = 'numeric';
    codeIn.autocomplete = 'one-time-code';
    codeIn.maxLength = 7;
    codeIn.placeholder = '000 000';
    codeIn.style.cssText =
      'text-align:center;font-size:28px;letter-spacing:.18em;height:52px;' +
      'color:var(--cb-accent);width:100%';

    const nameIn = document.createElement('input');
    nameIn.className = 'cb-in';
    nameIn.id = 'cb-pair-name';
    nameIn.placeholder = 'This device';
    nameIn.value = cbApi.deviceName() || '';
    nameIn.style.width = '100%';

    const err = document.createElement('div');
    err.className = 'cb-merr';
    err.hidden = true;

    const go = document.createElement('button');
    go.className = 'cb-btn cb-btn--fill';
    go.style.cssText = 'justify-content:center;width:100%;height:40px';
    go.textContent = 'Pair this device';

    const note = document.createElement('p');
    note.className = 'cb-mut cb-mono';
    note.style.cssText = 'font-size:11px;margin:14px 0 0';
    note.textContent = 'Codes are good for five minutes and work once.';

    async function submit() {
      err.hidden = true;
      go.disabled = true;
      go.textContent = 'Pairing…';
      try {
        await cbApi.pair(codeIn.value.trim(), nameIn.value.trim());
        wrap.remove();
        await boot();
      } catch (ex) {
        err.textContent = ex.userFacing ? ex.message : 'Pairing failed.';
        err.hidden = false;
        go.disabled = false;
        go.textContent = 'Pair this device';
        codeIn.focus();
        codeIn.select();
      }
    }
    go.addEventListener('click', submit);
    [codeIn, nameIn].forEach((el) => el.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') submit();
    }));

    card.append(brand, head, lead,
      labelled('Pairing code', codeIn),
      labelled('Device name', nameIn, 'Shown in the host\'s paired-devices list.'),
      err, go, note);
    wrap.appendChild(card);
    document.body.appendChild(wrap);

    /* When the host does not require a code, say so rather than demanding
       one the user cannot get. Still rate-limited host-side. */
    cbApi.pairInfo().then((info) => {
      if (info && info.require_pairing === false) {
        lead.textContent = 'This host is not asking for a pairing code. Name ' +
          'this device and pair it.';
        codeIn.placeholder = 'not required';
      }
      if (info && info.offline) {
        err.textContent = 'The host is not answering. Check it is running, ' +
          'then try again.';
        err.hidden = false;
      }
    });
    codeIn.focus();
  }

  /* ── notifications (3n) ───────────────────────────────────────────────────
     Every `notification` the host pushes, kept client-side. There is no
     server-side inbox and the design does not ask for one: the host emits an
     announcement when something ends, and each device decides what it has
     already seen. Persisted per browser so a reload does not erase the
     morning's runs — the same localStorage the database viewer's column widths
     live in, and guarded the same way, since a private window can refuse it.

     No OS integration (the brief is explicit): this is the in-page bell, and
     the toast that already fires stays as it was. */

  const NOTE_LIMIT = 50;
  const NOTE_STORE = 'cb_notifications';

  const notes = { items: [], panel: null };

  function loadNotes() {
    try {
      const raw = localStorage.getItem(NOTE_STORE);
      const parsed = raw ? JSON.parse(raw) : [];
      notes.items = Array.isArray(parsed) ? parsed.slice(0, NOTE_LIMIT) : [];
    } catch (_) { notes.items = []; }
  }

  function saveNotes() {
    try {
      localStorage.setItem(NOTE_STORE, JSON.stringify(notes.items));
    } catch (_) { /* storage refused — the list stays in memory only */ }
  }

  function unreadNotes() { return notes.items.filter((n) => !n.read).length; }

  function noteLevelClass(n) {
    if (n.level === 'error') return ' is-err';
    if (n.level === 'warn' || n.level === 'warning') return ' is-warn';
    return '';
  }

  /* The host stamps `at` in ITS local time, with no zone — which is right, it
     is the host's clock the user is asking about. Anything unparseable falls
     back to the arrival time rather than printing "Invalid Date". */
  function fmtNoteWhen(at) {
    const when = at ? new Date(at) : null;
    const secs = when && !isNaN(when.getTime()) ? when.getTime() / 1000 : 0;
    return secs ? fmtWhen(secs) : '';
  }

  function pushNote(n) {
    notes.items.unshift({
      level: n.level || 'info', title: n.title || 'Host',
      body: n.body || '', at: n.at || new Date().toISOString(),
      job: n.job || '', task: n.task || '', read: false,
    });
    if (notes.items.length > NOTE_LIMIT) notes.items.length = NOTE_LIMIT;
    saveNotes();
    renderBell();
    renderOverviewRecent();
    if (notes.panel) renderNotifications();
  }

  function renderBell() {
    const badge = $('#ov-bell-count');
    if (!badge) return;
    const unread = unreadNotes();
    badge.textContent = unread > 99 ? '99+' : String(unread);
    badge.hidden = unread === 0;
  }

  function closeNotifications() {
    if (!notes.panel) return;
    notes.panel.remove();
    notes.panel = null;
    const bell = $('#ov-bell');
    if (bell) bell.setAttribute('aria-expanded', 'false');
    document.removeEventListener('pointerdown', notesOutside, true);
  }

  function notesOutside(e) {
    if (!notes.panel) return;
    if (notes.panel.contains(e.target)) return;
    if ($('#ov-bell') && $('#ov-bell').contains(e.target)) return;
    closeNotifications();
  }

  function toggleNotifications() {
    if (notes.panel) { closeNotifications(); return; }
    const panel = document.createElement('div');
    panel.className = 'cb-notif';
    panel.id = 'cb-notif';
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-label', 'Notifications');
    document.body.appendChild(panel);
    notes.panel = panel;
    const bell = $('#ov-bell');
    if (bell) {
      bell.setAttribute('aria-expanded', 'true');
      const r = bell.getBoundingClientRect();
      panel.style.top = (r.bottom + 8) + 'px';
      panel.style.left =
        Math.max(10, Math.min(r.right - panel.offsetWidth,
                              innerWidth - panel.offsetWidth - 10)) + 'px';
    }
    renderNotifications();
    document.addEventListener('pointerdown', notesOutside, true);
  }

  function noteJumpFor(n) {
    if (n.job === 'watchlist') return ['Open Watch List', 'watchlist'];
    if (n.job === 'batch') return ['Open Downloads', 'downloads'];
    return null;
  }

  function renderNotifications() {
    const panel = notes.panel;
    if (!panel) return;
    panel.innerHTML = '';

    const head = document.createElement('div');
    head.className = 'cb-row';
    const title = document.createElement('span');
    title.style.cssText = 'font-weight:600;font-size:14px;color:var(--cb-text)';
    title.textContent = 'Notifications';
    head.append(title);
    const unread = unreadNotes();
    if (unread) head.appendChild(tagNode(`${num(unread)} new`, 'cb-tag--fill'));
    const mark = document.createElement('button');
    mark.className = 'cb-notif__link';
    mark.style.marginLeft = 'auto';
    mark.textContent = 'Mark all read';
    setDisabled(mark, !unread, {
      reason: 'Nothing is unread.',
      ttText: 'Clear the count on the bell. The entries stay in the list.',
    });
    if (unread) {
      mark.addEventListener('click', () => {
        notes.items.forEach((n) => { n.read = true; });
        saveNotes();
        renderBell();
        renderNotifications();
      });
    }
    head.appendChild(mark);
    panel.append(head, divNode());

    if (!notes.items.length) {
      panel.appendChild(ovEmpty('Nothing yet — finished scans, batches and ' +
                                'errors from the host land here.'));
    }
    let seenRead = false;
    notes.items.forEach((n) => {
      if (n.read && !seenRead && notes.items.some((o) => !o.read)) {
        seenRead = true;
        panel.appendChild(divNode());
      }
      panel.appendChild(noteRow(n));
    });

    panel.appendChild(divNode());
    const foot = document.createElement('div');
    foot.className = 'cb-row';
    const settings = document.createElement('a');
    settings.href = '#settings';
    settings.style.fontSize = '11.5px';
    settings.textContent = 'Notification settings';
    settings.addEventListener('click', closeNotifications);
    const mirror = document.createElement('span');
    mirror.className = 'cb-mut';
    mirror.style.cssText = 'margin-left:auto;font-size:11px';
    mirror.textContent = 'Mirrors the tray notifications';
    foot.append(settings, mirror);
    panel.appendChild(foot);
    bindTips(panel);
  }

  function divNode() {
    const el = document.createElement('div');
    el.className = 'cb-div';
    return el;
  }

  function noteRow(n) {
    const row = document.createElement('div');
    row.className = 'cb-notif__row' + (n.read ? ' is-read' : '');
    const dot = document.createElement('span');
    dot.className = 'cb-notif__dot';
    const body = document.createElement('div');
    body.className = 'cb-notif__body';
    const text = document.createElement('div');
    text.className = 'cb-notif__text' + noteLevelClass(n);
    text.textContent = `${n.title} — ${n.body}`;
    const meta = document.createElement('div');
    meta.className = 'cb-row';
    meta.style.cssText = 'gap:8px;margin-top:4px';
    const when = document.createElement('span');
    when.className = 'cb-notif__when';
    when.textContent = fmtNoteWhen(n.at);
    meta.appendChild(when);
    const jump = noteJumpFor(n);
    if (jump) {
      const link = document.createElement('button');
      link.className = 'cb-notif__link';
      link.textContent = jump[0];
      link.addEventListener('click', () => {
        closeNotifications();
        show(jump[1]);
      });
      meta.appendChild(link);
    }
    body.append(text, meta);
    row.append(dot, body);
    return row;
  }

  /* ── Overview (3a) ────────────────────────────────────────────────────────
     Aggregates only — every number on this screen belongs to another screen,
     and the design says so: this is the "what is happening" landing a remote
     session needs, not a fifth place to change something. The one exception is
     the Now-running card, whose Pause/Cancel drive whichever job is actually
     running rather than the batch the artboard happens to draw. */

  /* One label/value row. The value is truncated rather than wrapped — these
     are paths, and a path that wraps takes the whole card with it — so the
     untruncated string goes in its tooltip. */
  function ovLine(label, value) {
    const row = document.createElement('div');
    row.className = 'cb-row cb-ov-line';
    const left = document.createElement('span');
    left.textContent = label;
    const right = document.createElement('span');
    right.className = 'cb-mono';
    right.style.cssText = 'margin-left:auto;text-align:right;min-width:0';
    right.textContent = value;
    if (String(value).length > 22) {
      right.tabIndex = 0;
      right.setAttribute('data-tt-text', String(value));
    }
    row.append(left, right);
    return row;
  }

  function ovEmpty(text) {
    const el = document.createElement('div');
    el.className = 'cb-ov-empty';
    el.textContent = text;
    return el;
  }

  /* Recent activity is the notification feed, three deep — the same entries
     the bell holds, which is what the design shows in both places. */
  function renderOverviewRecent() {
    const box = $('#ov-recent');
    if (!box) return;
    box.innerHTML = '';
    const recent = notes.items.slice(0, 3);
    if (!recent.length) {
      box.appendChild(ovEmpty('Nothing yet — finished scans, batches and ' +
                              'errors land here.'));
      return;
    }
    recent.forEach((n) => {
      const item = document.createElement('div');
      item.className = 'cb-ov-item';
      const text = document.createElement('div');
      text.className = 'cb-ov-item__text' + noteLevelClass(n);
      text.textContent = `${n.title} — ${n.body}`;
      const when = document.createElement('div');
      when.className = 'cb-ov-item__when';
      when.textContent = fmtNoteWhen(n.at);
      item.append(text, when);
      box.appendChild(item);
    });
  }

  /* What the host wants looking at, counted off the cards it already sent.
     Nothing here asks the host a question of its own: the Overview must not
     cost a database pass every time it repaints. */
  function renderOverviewAttention() {
    const box = $('#ov-attention');
    if (!box) return;
    box.innerHTML = '';
    const unresolved = wlUnresolved();
    const failing = wl.cards.filter((c) => c.status === 'error'
                                        || c.status === 'offline');
    const unscanned = wl.cards.filter((c) => !c.last_scan);
    const rows = [];
    if (unresolved.length) {
      rows.push([`${num(unresolved.length)} link${unresolved.length === 1 ? '' : 's'}`,
        unresolved.length === 1
          ? `${unresolved[0].name} is unresolved`
          : 'channels cannot be scanned until their link is fixed',
        'cb-tag--attn']);
    }
    if (failing.length) {
      rows.push([num(failing.length),
        `channel${failing.length === 1 ? '' : 's'} reported an error on the last scan`,
        'cb-tag--grey']);
    }
    if (unscanned.length) {
      rows.push([num(unscanned.length),
        `channel${unscanned.length === 1 ? '' : 's'} never scanned`, 'cb-tag--grey']);
    }
    if (!rows.length) {
      box.appendChild(ovEmpty('Nothing needs attention.'));
      return;
    }
    rows.forEach(([tag, text, cls]) => {
      const row = document.createElement('div');
      row.className = 'cb-row';
      row.style.cssText = 'gap:8px;font-size:12.5px;color:var(--cb-text)';
      const span = document.createElement('span');
      span.textContent = text;
      row.append(tagNode(tag, cls), span);
      box.appendChild(row);
    });
  }

  /* The host's own configuration, read straight off the snapshot's settings —
     the three the design names, plus where the database it is all counted
     from actually lives. */
  function renderOverviewHost() {
    const box = $('#ov-host');
    if (!box) return;
    const s = state.settings || {};
    box.innerHTML = '';
    box.appendChild(ovLine('Save directory', s.base_dir || '—'));
    box.appendChild(ovLine('Output', s.no_conversion
      ? 'Source format' : `MP3 ${s.bitrate_quality || ''}`.trim()));
    box.appendChild(ovLine('Cookies', !s.use_cookies ? 'Off'
      : (s.cookie_method === 'Cookie File' ? 'Cookie file'
                                           : (s.cookies_browser || 'Browser'))));
    box.appendChild(ovLine('Database', state.library.path || '—'));
    bindTips(box);
  }

  /* The Watch List card, fed from the same cards the nav badge is — so a
     watchlist.card event moves the number here without a snapshot. */
  function renderOverviewWatch() {
    const channels = wl.cards.length || (state.counts || {}).watchlist || 0;
    const pending = renderNavCount();
    $('#ov-new').textContent = num(pending);
    $('#ov-new-sub').textContent =
      `new tracks across ${num(channels)} channel${channels === 1 ? '' : 's'}`;
    $('#ov-dl-all').textContent = `⬇ Download All New (${num(pending)})`;
    gateWrite($('#ov-dl-all'),
      wl.running ? WL_BUSY_REASON : (pending ? '' : WL_NOTHING_PENDING),
      'wl.download_all_new');
    const interval = (state.settings || {}).auto_dl_interval;
    $('#ov-auto').textContent = !interval || interval === 'Off'
      ? 'off' : `every ${interval}`;
    const last = wl.cards.reduce(
      (a, c) => Math.max(a, Number(c.last_scan) || 0), 0);
    $('#ov-last-scan').textContent = fmtWhen(last);
  }

  function renderOverview() {
    const c = state.counts;
    const s = (n, word) => `<span class="cb-mono">${num(n)}</span> ` +
      word + (n === 1 ? '' : 's');
    $('#ov-library').innerHTML = 'Library ' + [s(c.downloads, 'track'),
      s(c.genres, 'genre'), s(c.watchlist, 'channel')].join(' · ');
    renderOverviewWatch();
    renderOverviewHost();
    renderOverviewAttention();
    renderOverviewRecent();
    renderOverviewRunning();
  }

  function renderGenres() {
    const sel = $('#dl-genre');
    const current = sel.value;
    sel.innerHTML = '';
    const list = state.genres.length ? state.genres : ['(none)'];
    list.forEach((g) => {
      const opt = document.createElement('option');
      opt.value = g;
      opt.textContent = g;
      sel.appendChild(opt);
    });
    if (current && list.includes(current)) sel.value = current;
  }

  /* ── downloads: idle/running state ────────────────────────────────────────
     One flag (dl.running) drives every treatment the design's 3b/3c artboards
     split on: the header tag, where Pause/Cancel live, whether the queue rows
     show reorder controls or run states, and the progress card's opacity. */

  /* A disabled control's reason, wired to the control with aria-describedby.
     The pointer tracker is the only way to READ it — a disabled form control
     cannot be focused — but a screen reader announces a described-by node
     whether or not the control can take focus, which is exactly the half a
     pointer-only affordance leaves out.

     Native `disabled` stays: the desktop app disables the same controls, and
     aria-disabled + tabindex="0" would put dead stops in the tab order that
     the tkinter UI does not have. The node sits next to the control (so it is
     discarded with it on a re-render) and is absolutely positioned by
     app.css's .cb-sr, so it is not a flex item and adds no gap to its row. */
  let reasonSeq = 0;

  function describeReason(el, reason) {
    let node = el.__whyNode;
    if (!reason) {
      if (node) { node.remove(); el.__whyNode = null; }
      el.removeAttribute('aria-describedby');
      return;
    }
    if (!node) {
      node = document.createElement('span');
      node.className = 'cb-sr';
      node.id = 'cb-why-' + (reasonSeq += 1);
      el.__whyNode = node;
    }
    node.textContent = reason;
    // Re-inserting an attached node MOVES it, which is what keeps the pair
    // together when a control is re-parented (placeBatchControls does that).
    el.insertAdjacentElement('afterend', node);
    el.setAttribute('aria-describedby', node.id);
  }

  function setDisabled(el, disabled, opts) {
    opts = opts || {};
    el.disabled = !!disabled;
    el.removeAttribute('data-tt');
    el.removeAttribute('data-tt-text');
    if (disabled) {
      if (opts.reason) el.setAttribute('data-tt-text', opts.reason);
    } else if (opts.ttKey) {
      el.setAttribute('data-tt', opts.ttKey);
    } else if (opts.ttText) {
      el.setAttribute('data-tt-text', opts.ttText);
    }
    describeReason(el, disabled ? (opts.reason || '') : '');
    // The live bubble may be open on this very control — a batch starting
    // re-enables Cancel under a pointer that never moved.
    refreshTip(el);
  }

  function placeBatchControls(running) {
    const header = $('#dl-header-actions');
    const bottomRow = $('#dl-actions-row');
    const cancelBtn = $('#dl-cancel');
    const pauseBtn = $('#dl-pause');
    if (running) {
      header.append(pauseBtn, cancelBtn);
      header.hidden = false;
      bottomRow.hidden = true;
    } else {
      bottomRow.append(cancelBtn, pauseBtn);
      header.hidden = true;
      bottomRow.hidden = false;
    }
  }

  function updatePauseLabel() {
    const b = $('#dl-pause');
    b.textContent = dl.paused ? '▶ Resume' : '⏸ Pause';
  }

  function renderDownloadsHeader() {
    const tag = $('#dl-state');
    tag.textContent = dl.running ? 'Batch running' : 'Idle';
    tag.className = 'cb-tag ' + (dl.running ? 'cb-tag--fill' : 'cb-tag--grey');
    placeBatchControls(dl.running);
    updatePauseLabel();
    gateWrite($('#dl-cancel'), dl.running ? '' : 'No download is running.',
      'main.cancel_batch');
    gateWrite($('#dl-pause'), dl.running ? '' : 'No download is running.',
      'main.pause_batch');
    gateWrite($('#quick-add'), '', 'main.batch_add');
    gateWrite($('#dl-add'), '', 'main.batch_add');
    $('#dl-progress').style.opacity = dl.running ? '1' : '.6';
    /* The panel's Scan-all quick action and the Watch List's own scan controls
       both close while a batch owns the host's yt-dlp session (3c). */
    renderWatchlistToolbar();
  }

  function renderCurrent() {
    const p = dl.current;
    $('#dl-cur-label').textContent = p ? (p.title || '—') : '—';
    $('#dl-cur-bar').style.width = (p && p.percent != null ? p.percent : 0) + '%';
    $('#dl-cur-bitrate').textContent = (p && p.bitrate_text) || '';
    const bits = [];
    if (p && p.speed_text) bits.push(p.speed_text);
    if (p && p.percent != null) bits.push(`${p.percent}%`);
    $('#dl-cur-speed').textContent = bits.join(' · ');
  }

  function renderOverall() {
    const p = dl.overall;
    $('#dl-all-label').innerHTML = p
      ? `<span class="cb-mono">${num(p.done)}</span> of <span class="cb-mono">${num(p.total)}</span> · ` +
        `<span class="cb-mono">${num(p.downloaded)}</span> downloaded · ` +
        `<span class="cb-mono">${num(p.skipped)}</span> skipped · ` +
        `<span class="cb-mono">${num(p.errors)}</span> error`
      : '—';
    $('#dl-all-bar').style.width = (p ? p.percent || 0 : 0) + '%';
    $('#dl-all-eta').textContent = p
      ? (p.eta_text ? p.eta_text + ' · ' : '') + `${p.percent || 0}%`
      : '';
  }

  function renderPanelBatchMini() {
    const mini = $('#panel-batch-mini');
    mini.hidden = !dl.running;
    if (!dl.running) return;
    const p = dl.overall;
    const pct = p ? p.percent || 0 : 0;
    $('#panel-batch-fill').style.width = pct + '%';
    $('#panel-batch-label').textContent = p ? `Batch ${num(p.done)} / ${num(p.total)}` : 'Batch 0 / 0';
  }

  /* Which job the Now-running card is about. The artboard draws the batch
     case, but all three job categories drive the same card — a Watch List run
     or a maintenance sweep is just as much "what is happening", and hiding the
     card for those would make the Overview lie about an idle host. Batch wins
     when two run at once: it is the one with a Pause. */
  function overviewJob() {
    if (dl.running) {
      return { key: 'batch', tag: 'Batch', current: dl.current,
               overall: dl.overall, href: '#downloads',
               link: 'Open Downloads →', pausable: true };
    }
    if (wl.running) {
      return { key: 'watchlist', tag: 'Watch List', current: wl.current,
               overall: wl.overall, href: '#watchlist',
               link: 'Open Watch List →', pausable: false,
               pauseReason: 'A Watch List run has no pause — cancel the run ' +
                 'here, or cancel a single channel from its card.' };
    }
    if (mt.running) {
      const spec = MAINT_TASKS[mt.task] || {};
      return { key: 'maintenance', tag: spec.run || 'Maintenance',
               current: mt.current, overall: mt.overall, href: '#settings',
               link: 'Open Settings →', pausable: false,
               pauseReason: 'A database maintenance job runs to the end or is ' +
                 'cancelled; there is nothing to hold it at.' };
    }
    return null;
  }

  const OV_IDLE_REASON = 'Nothing is running on the host — start a batch from ' +
    'Downloads, or scan the Watch List.';

  function renderOverviewRunning() {
    const card = $('#ov-running');
    if (!card) return;
    const job = overviewJob();
    const tag = $('#ov-run-tag');
    const open = $('#ov-open');
    card.style.opacity = job ? '1' : '.72';
    tag.textContent = job ? job.tag : 'Idle';
    tag.className = 'cb-tag ' + (job ? 'cb-tag--fill' : 'cb-tag--grey');
    open.href = job ? job.href : '#downloads';
    open.textContent = job ? job.link : 'Open Downloads →';

    const o = job && job.overall;
    const p = job && job.current;
    $('#ov-run-meta').textContent = !job ? ''
      : (o ? `${num(o.done)} / ${num(o.total)}` +
             (o.eta_text ? ` · ${o.eta_text}` : '')
           : 'starting…');
    $('#ov-run-title').textContent = !job
      ? 'Nothing is running.'
      : ((p && (p.title || p.note)) || 'starting…');
    const bits = [];
    if (p && p.percent != null) bits.push(`${p.percent}%`);
    if (p && p.speed_text) bits.push(p.speed_text);
    if (p && p.bitrate_text) bits.push(p.bitrate_text);
    $('#ov-run-stats').textContent = bits.join(' · ');
    $('#ov-run-cur').style.width =
      (p && p.percent != null ? p.percent : 0) + '%';
    $('#ov-run-all').style.width = (o ? o.percent || 0 : 0) + '%';
    $('#ov-run-pct').textContent = (o ? o.percent || 0 : 0) + '%';

    gateWrite($('#ov-pause'),
      !job ? OV_IDLE_REASON : (job.pausable ? '' : job.pauseReason),
      'main.pause_batch');
    $('#ov-pause').textContent = dl.paused && job && job.pausable
      ? '▶ Resume' : '⏸ Pause';
    gateWrite($('#ov-cancel'), job ? '' : OV_IDLE_REASON,
      job && job.key === 'maintenance' ? 'settings.maintenance_cancel'
                                       : 'main.cancel_batch');
    $('#ov-cancel').className = 'cb-btn cb-btn--sm ' +
      (job ? 'cb-btn--warn' : 'cb-btn--quiet');
  }

  function skipBtn(row, warn) {
    const b = document.createElement('button');
    b.className = 'cb-btn cb-btn--sm cb-icon ' + (warn ? 'cb-btn--warn' : 'cb-btn--quiet');
    b.textContent = warn ? '⏭ Skip' : '⏭';
    /* Three different things to say, and the registry has all three: a row
       already marked, a row waiting its turn in a running batch (`warn` is
       false only there and at rest), and the row being downloaded right now,
       where "interrupted on the spot" is the promise being made. */
    b.setAttribute('data-tt', row.state === 'skipped' ? 'main.row_skip_marked'
      : (dl.running && !warn ? 'main.row_skip_queued' : 'main.row_skip'));
    b.addEventListener('click', async () => {
      await call('batch.skip', { id: row.id });
      state.batch = await call('batch.list');
      renderBatch();
    });
    return b;
  }

  function renderBatch() {
    const rows = state.batch || [];
    const running = dl.running;
    const host = $('#dl-rows');
    host.innerHTML = '';

    let runningIdx = null;
    rows.forEach((r, i) => {
      const rt = dl.rows[r.id];
      if (rt && rt.state === 'active') runningIdx = i + 1;
    });
    $('#dl-count').textContent = `${rows.length} URL${rows.length === 1 ? '' : 's'}` +
      (running && runningIdx ? ` · running #${runningIdx}` : '');

    if (!rows.length) {
      const empty = document.createElement('div');
      empty.className = 'cb-mut cb-mono';
      empty.style.cssText = 'font-size:12px;padding:8px 0';
      empty.textContent = "No URLs in batch — paste a link above and press '+ Add to Batch'";
      host.appendChild(empty);
      setStartDisabled(true, 'Add a link to the queue before starting a download.');
      gateWrite($('#dl-clear'), running
        ? 'The queue is locked while a download is running. Cancel it first, or skip the row instead.'
        : '', 'main.batch_clear');
      renderQueueLog();
      return;
    }
    setStartDisabled(running, running ? 'A batch is already running.' : '');
    gateWrite($('#dl-clear'), running
      ? 'The queue is locked while a download is running. Cancel it first, or skip the row instead.'
      : '', 'main.batch_clear');

    rows.forEach((row, i) => {
      const rt = dl.rows[row.id];
      const st = running ? (rt ? rt.state : (row.state === 'skipped' ? 'skipped' : 'queued'))
                         : (row.state === 'skipped' ? 'skipped' : null);
      const el = document.createElement('div');
      el.className = 'cb-qrow' +
        (row.state === 'skipped' ? ' is-skipped' : '') +
        (running && (st === 'done' || st === 'error') ? ' is-past' : '') +
        (running && st === 'active' ? ' is-active' : '');

      const mark = document.createElement('span');
      mark.className = 'cb-mono cb-qrow__mark';
      if (running) {
        mark.textContent = DL_MARK[st] || '·';
        mark.style.color = DL_MARK_COLOR[st] || 'var(--cb-muted)';
      } else {
        mark.classList.add('cb-mut');
        mark.textContent = String(i + 1);
      }
      el.appendChild(mark);

      const url = document.createElement('span');
      url.className = 'cb-qrow__url';
      url.textContent = (rt && rt.title) || row.url;
      el.appendChild(url);

      const tag = document.createElement('span');
      if (running) {
        tag.className = 'cb-tag' + (st === 'done' || st === 'error' || st === 'skipped' ? ' cb-tag--grey'
                                    : st === 'active' ? ' cb-tag--fill' : '');
        tag.textContent = st === 'done' ? 'Done' : st === 'error' ? 'Error' :
                          st === 'skipped' ? 'Skipped' : st === 'active' ? 'Running' : 'Pending';
      } else {
        tag.className = 'cb-tag cb-tag--grey';
        tag.textContent = row.genre;
      }
      el.appendChild(tag);

      if (!running) {
        el.appendChild(skipBtn(row, false));
        [['▲', 'main.row_up', () => call('batch.move', { id: row.id, delta: -1 })],
         ['▼', 'main.row_down', () => call('batch.move', { id: row.id, delta: 1 })],
         ['✕', 'main.row_remove', () => call('batch.remove', { id: row.id })],
        ].forEach(([label, ttKey, action]) => {
          const b = document.createElement('button');
          b.className = 'cb-btn cb-btn--quiet cb-btn--sm cb-icon';
          b.textContent = label;
          b.setAttribute('data-tt', ttKey);
          b.addEventListener('click', async () => {
            await action();
            state.batch = await call('batch.list');
            renderBatch();
          });
          el.appendChild(b);
        });
      } else if (st === 'active') {
        el.appendChild(skipBtn(row, true));
      } else if (st === 'queued') {
        el.appendChild(skipBtn(row, false));
      }
      host.appendChild(el);
    });
    bindTips(host);
    renderQueueLog();
  }

  function renderQueueLog() {
    const rows = state.batch || [];
    const log = $('#dl-queue');
    const meta = $('#dl-queue-meta');
    log.innerHTML = '';

    if (!dl.running) {
      if (!rows.length) {
        log.textContent = 'Queue is empty — add links above, then press Start Downloads.';
        meta.textContent = 'empty';
      } else {
        log.textContent = 'Press Start Downloads to begin.';
        meta.textContent = `${rows.length} URL${rows.length === 1 ? '' : 's'} queued`;
      }
      return;
    }

    let processed = 0;
    rows.forEach((row) => {
      const rt = dl.rows[row.id];
      const st = rt ? rt.state : (row.state === 'skipped' ? 'skipped' : 'queued');
      if (st === 'done' || st === 'error' || st === 'skipped') processed += 1;

      const line = document.createElement('div');
      if (st === 'active') line.className = 'cb-log__now';

      const mark = document.createElement('span');
      mark.className = DL_LOG_CLASS[st] || '';
      mark.textContent = (DL_MARK[st] || '·') + '  ';
      line.appendChild(mark);

      const title = document.createElement('span');
      title.className = st === 'active' ? 'cb-log__title' : '';
      title.textContent = (rt && rt.title) || row.url;
      line.appendChild(title);

      const detail = document.createElement('span');
      detail.className = 'cb-mut';
      detail.style.marginLeft = '10px';
      detail.textContent = st === 'active'
        ? ((dl.current && (dl.current.speed_text || (dl.current.percent != null ? `${dl.current.percent}%` : ''))) || 'fetching…')
        : (rt && rt.detail) || (st === 'queued' ? 'queued' : '');
      line.appendChild(detail);

      log.appendChild(line);
    });
    meta.textContent = `${rows.length} track${rows.length === 1 ? '' : 's'} · ${processed} processed`;
  }

  /* Every write control funnels through here so a read-only session (or one
     without the control lock) closes them all with the host's own reason,
     rather than each caller inventing its own wording. */
  function setStartDisabled(disabled, reason) {
    const block = writeBlocked();
    setDisabled($('#dl-start'), !!(block || disabled),
      { reason: block || reason, ttKey: 'main.start_downloads' });
  }

  /* A closed control keeps saying what it does before it says why it is off —
     losing the registry half is exactly when the user is most likely to be
     asking what the control was for (the same rule wlGate follows). */
  function gateWrite(el, reason, ttKey) {
    if (!el) return;
    const block = writeBlocked();
    const why = block || reason;
    setDisabled(el, !!why, why ? { reason: tipPlus(ttKey, why) } : { ttKey });
  }

  function renderDownloads() {
    renderDownloadsHeader();
    renderCurrent();
    renderOverall();
    renderPanelBatchMini();
    renderBatch();
  }

  /* ── modal shell (3m) ─────────────────────────────────────────────────────
     One dialog at a time, by construction: opening closes whatever was open,
     which is also how Smart-Edit hands the Edit dialog off to Fix Link. Escape
     and a click on the dim close it (every dialog here has a safe no-op exit);
     Tab is trapped inside, and focus returns to whatever opened it. */

  let openDialog = null;

  function modalFocusables(root) {
    return $$('button, [href], input, select, textarea, [tabindex]', root)
      .filter((el) => !el.disabled && el.tabIndex !== -1 && el.offsetParent !== null);
  }

  function closeModal() {
    if (!openDialog) return;
    const dying = openDialog;
    openDialog = null;
    document.removeEventListener('keydown', dying.onKey, true);
    dying.dim.remove();
    hideTip();
    if (dying.restore && dying.restore.focus) {
      try { dying.restore.focus(); } catch (_) { /* element left the DOM */ }
    }
    if (dying.onClose) dying.onClose();
  }

  /* opts: {title, tag:{text,cls}, width, body(bodyEl, api), foot(footEl, api),
            onClose}. `api` is {close, error(msg), busy(flag), body, foot}. */
  function openModal(opts) {
    closeModal();
    const restore = document.activeElement;

    const dim = document.createElement('div');
    dim.className = 'cb-dim';
    const modal = document.createElement('div');
    modal.className = 'cb-modal';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    if (opts.width) modal.style.maxWidth = opts.width + 'px';

    const head = document.createElement('div');
    head.className = 'cb-mhead';
    const title = document.createElement('span');
    title.className = 'cb-mtitle';
    title.textContent = opts.title || '';
    modal.setAttribute('aria-label', opts.title || 'Dialog');
    head.appendChild(title);
    if (opts.tag) head.appendChild(tagNode(opts.tag.text, opts.tag.cls));
    const closeBtn = document.createElement('button');
    closeBtn.className = 'cb-btn cb-btn--quiet cb-btn--sm';
    closeBtn.style.cssText = 'margin-left:auto;padding:3px 8px';
    closeBtn.textContent = '✕';
    if (opts.closeTtKey) closeBtn.setAttribute('data-tt', opts.closeTtKey);
    closeBtn.addEventListener('click', closeModal);
    head.appendChild(closeBtn);

    const body = document.createElement('div');
    body.className = 'cb-mbody';
    const err = document.createElement('div');
    err.className = 'cb-merr';
    err.hidden = true;
    const foot = document.createElement('div');
    foot.className = 'cb-mfoot';

    modal.append(head, body, err, foot);
    dim.appendChild(modal);

    const api = {
      body, foot,
      close: closeModal,
      error(message) {
        err.textContent = message || '';
        err.hidden = !message;
      },
      busy(flag) {
        modal.style.opacity = flag ? '.7' : '';
        modal.style.pointerEvents = flag ? 'none' : '';
      },
    };

    function onKey(e) {
      if (e.key === 'Escape') { e.stopPropagation(); closeModal(); return; }
      if (e.key !== 'Tab') return;
      const items = modalFocusables(modal);
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      else if (!modal.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
    }
    dim.addEventListener('mousedown', (e) => { if (e.target === dim) closeModal(); });
    document.addEventListener('keydown', onKey, true);

    openDialog = { dim, onKey, restore, onClose: opts.onClose };
    document.body.appendChild(dim);
    if (opts.body) opts.body(body, api);
    if (opts.foot) opts.foot(foot, api);
    bindTips(dim);
    const wanted = (opts.focus && opts.focus()) ||
                   modalFocusables(modal)[0] || closeBtn;
    wanted.focus();
    return api;
  }

  function modalButton(label, cls, onClick, ttKey) {
    const b = document.createElement('button');
    b.className = ('cb-btn cb-btn--sm ' + (cls || '')).trim();
    b.textContent = label;
    if (ttKey) b.setAttribute('data-tt', ttKey);
    b.addEventListener('click', onClick);
    return b;
  }

  function modalNote(text) {
    const p = document.createElement('p');
    p.className = 'cb-mnote';
    p.textContent = text;
    return p;
  }

  function labelled(labelText, control, hintText) {
    const wrap = document.createElement('div');
    const lab = document.createElement('div');
    lab.className = 'cb-mlabel';
    lab.textContent = labelText;
    wrap.append(lab, control);
    if (hintText) {
      const hint = document.createElement('p');
      hint.className = 'cb-mhint';
      hint.textContent = hintText;
      wrap.appendChild(hint);
    }
    return wrap;
  }

  function tagNode(text, extra) {
    const el = document.createElement('span');
    el.className = ('cb-tag ' + (extra || '')).trim();
    el.textContent = text;
    return el;
  }

  /* ── Watch List (3d) ──────────────────────────────────────────────────────
     Cards are push-driven: watchlist.list fills them once and every
     watchlist.card event replaces exactly one of them in place, so a scan
     redrawing one channel never disturbs a button the user is on. The pinned
     scan log consumes scan.line through the same renderer the log viewers use.

     "Is a Watch List job running" is set optimistically when a start call is
     accepted and cleared by the host's `job.finished` — never by reading the
     run's own terminal DONE line, which the host emits while it still holds
     the job slot. */

  const WL_LOG_LIMIT = 500;
  /* The service stores a placeholder URL for a channel whose real link was
     never resolved; the Edit dialog must offer an empty field, not that
     sentinel. A duplicate literal rather than an import, for the same reason
     cratebuilder/db.py keeps its own copy of it. */
  const WL_UNRESOLVED_URL_PREFIX = 'unresolved://';
  const WL_BUSY_REASON = 'A Watch List scan or download is already running — ' +
    'cancel it first, or wait for it to finish.';
  /* Both Download-All-New buttons — the Watch List's own and the Overview's —
     close for the same reason, so they say it in the same words. */
  const WL_NOTHING_PENDING = 'No new tracks pending across any channels. Run ' +
    '🔍 Scan for new first.';
  /* Cancellation lands between channels — it cannot interrupt a channel
     listing already in flight — so nothing here promises an immediate stop. */
  const WL_CANCEL_ALL_NOTE = 'Cancelling — the channel in flight finishes what ' +
    'it is on, then the run stops.';
  const WL_CANCEL_ONE_NOTE = 'Cancelling — this channel stops once the work ' +
    'already in flight finishes.';
  const WL_URL_HINT = 'Paste the channel (…/@handle or …/channel/UC…) or a ' +
    'playlist URL.';
  /* 3m's Edit dialog adds the "leave it alone" half; on Add there is no
     current channel for that sentence to be about. */
  const WL_EDIT_URL_HINT = WL_URL_HINT + ' Leave as-is to keep the current channel.';
  /* 3m's warn box, verbatim — including the confirmation clause, which the
     Save path below now actually honours. */
  const WL_GENRE_WARNING = 'Saving a different genre moves the channel folder, ' +
    'updates the database rows, and sets the Genre tag inside each file to ' +
    'match. You get a confirmation naming the track count first, and a failed ' +
    'database write rolls the move back.';
  const WL_NO_HOST_ACTION = "Not wired up yet — creating and removing genre " +
    "folders arrives with the host filesystem bridge.";
  /* The DB's own status vocabulary, in the words the card should say it in.
     An unmapped status renders as-is rather than being hidden — a status the
     frontend has not met is still worth showing. */
  const WL_STATUS_TEXT = {
    idle: 'idle', found: 'new tracks found', scanning: 'scanning',
    downloading: 'downloading', needs_resolve: 'link needs fixing',
    offline: 'offline — no network', error: 'error',
  };

  const wl = {
    running: false,   // a "watchlist" job owns the host
    cards: [],        // watchlist.list, updated in place by watchlist.card
    current: null,    // last progress.current stamped job:"watchlist"
    overall: null,    // last progress.overall stamped job:"watchlist"
  };

  function wlPending() {
    return wl.cards.reduce((a, c) => a + (Number(c.new_count) || 0), 0);
  }
  function wlUnresolved() { return wl.cards.filter((c) => c.unresolved); }
  function wlUrl(row) {
    const url = row.url || '';
    return url.startsWith(WL_UNRESOLVED_URL_PREFIX) ? '' : url;
  }
  function wlBusy(row) {
    return row.status === 'downloading' || row.status === 'scanning';
  }
  function wlBusyReason(row) {
    return row.status === 'downloading'
      ? 'This channel is downloading — press ✕ Cancel on the card to stop it first.'
      : 'This channel is being scanned — press ✕ Cancel on the card to stop it first.';
  }
  function fmtDate(ts) {
    const secs = Number(ts);
    if (!secs) return '';
    return new Date(secs * 1000).toLocaleDateString();
  }

  /* Start (or join) a Watch List job. A download_new that lands on a run
     already going comes back with its queue position instead of a job id —
     the append-to-running the card's tooltip promises. */
  async function wlRun(method, params, note) {
    try {
      const res = await call(method, params || {});
      wl.running = true;
      renderWatchlist();
      if (res && res.queued_position) {
        toast(`Queued at position ${res.queued_position} in the run already going.`);
      } else if (note) toast(note);
      return res;
    } catch (_) { return null; }   /* call() already toasted the reason */
  }

  /* A disabled control keeps saying what it does, then why it is off — losing
     the registry half while a control is closed is exactly when the user is
     most likely to be asking what it was for. */
  function tipPlus(ttKey, reason) {
    return (ttKey && TOOLTIPS[ttKey] ? TOOLTIPS[ttKey] + '\n\n' : '') + reason;
  }

  function wlGate(el, reason, ttKey) {
    setDisabled(el, !!reason, { reason: tipPlus(ttKey, reason || ''), ttKey });
  }

  function wlActionButton(label, ttKey, cls, onClick, disabledReason) {
    const b = document.createElement('button');
    b.className = ('cb-btn cb-btn--sm ' + (cls || '')).trim();
    b.textContent = label;
    wlGate(b, disabledReason, ttKey);
    if (!disabledReason) b.addEventListener('click', onClick);
    return b;
  }

  function wlCardNode(row) {
    const busy = wlBusy(row);
    const downloading = row.status === 'downloading';
    const progress = row.progress || null;

    const card = document.createElement('div');
    card.className = 'cb-card cb-wlcard' + (busy ? ' is-running' : '');
    card.dataset.cid = String(row.id);

    const head = document.createElement('div');
    head.className = 'cb-row';
    head.style.gap = '9px';
    const link = dbSafeLink(wlUrl(row));
    let name;
    if (link) {
      name = document.createElement('a');
      name.href = link;
      name.target = '_blank';
      name.rel = 'noopener';
      if (TOOLTIPS['wl.card_title']) {
        name.setAttribute('data-tt-text', TOOLTIPS['wl.card_title'].replace('{url}', link));
      }
    } else {
      name = document.createElement('span');
    }
    name.className = 'cb-wlcard__name';
    name.textContent = row.name;
    head.appendChild(name);
    head.appendChild(tagNode(row.platform || '—', 'cb-tag--grey'));
    head.appendChild(tagNode(row.genre || '(none)', ''));
    if (downloading) head.appendChild(tagNode('Downloading', 'cb-tag--fill'));
    else if (row.status === 'scanning') head.appendChild(tagNode('Scanning', 'cb-tag--fill'));
    if (row.unresolved) head.appendChild(tagNode('Link unresolved', 'cb-tag--attn'));
    const count = document.createElement('span');
    count.className = 'cb-wlcard__new';
    count.textContent = `${num(row.new_count)} new` +
      (downloading && progress ? ` · ${num(progress.done)} done` : '');
    head.appendChild(count);
    card.appendChild(head);

    if (downloading) {
      const bar = document.createElement('div');
      bar.className = 'cb-bar';
      bar.style.height = '6px';
      const fill = document.createElement('div');
      fill.className = 'cb-bar__fill';
      fill.id = `wl-bar-${row.id}`;
      fill.style.width = (progress ? progress.percent || 0 : 0) + '%';
      bar.appendChild(fill);
      card.appendChild(bar);

      const line = document.createElement('div');
      line.className = 'cb-mut cb-mono cb-wlcard__meta';
      line.id = `wl-line-${row.id}`;
      line.textContent = wlCurrentLine(row);
      card.appendChild(line);
    } else {
      const meta = document.createElement('div');
      meta.className = 'cb-mut cb-mono cb-wlcard__meta';
      const bits = [];
      if (row.last_scan) bits.push(`Last scan ${fmtWhen(row.last_scan)}`);
      bits.push(`${num(row.downloaded)} downloaded`);
      if (row.date_added) bits.push(`added ${fmtDate(row.date_added)}`);
      if (row.unresolved) bits.push('folder has no canonical channel id');
      const status = row.status || 'idle';
      /* 3d's unresolved card stops at "folder has no canonical channel id" —
         repeating the needs_resolve status after it would say the same thing
         twice, in the database's words rather than the user's. */
      if (!(row.unresolved && status === 'needs_resolve')) {
        bits.push(WL_STATUS_TEXT[status] || status);
      }
      if (row.last_error && status !== 'idle') bits.push(row.last_error);
      meta.textContent = bits.join(' · ');
      card.appendChild(meta);
    }

    const actions = document.createElement('div');
    actions.className = 'cb-wlcard__actions';
    const why = writeBlocked() || (busy ? wlBusyReason(row) : '');
    actions.append(
      wlActionButton('🔍 Scan', 'wl.card_scan', 'cb-btn--quiet',
        () => wlRun('watchlist.scan', { channel_id: row.id }),
        why || (dl.running ? TOOLTIPS['main.scan_batch_conflict'] : '') ||
          (wl.running ? WL_BUSY_REASON : '')),
      wlActionButton('⚡ Force Download', 'wl.card_force', 'cb-btn--quiet',
        () => wlRun('watchlist.force_download', { channel_id: row.id }),
        why || (wl.running ? WL_BUSY_REASON : '')),
      wlActionButton(`⬇ Download New (${num(row.new_count)})`, 'wl.card_download_new', '',
        () => wlRun('watchlist.download_new', { channel_id: row.id }),
        why || (row.new_count ? '' :
          'Nothing pending for this channel — run 🔍 Scan first.')));
    if (row.unresolved) {
      actions.appendChild(wlActionButton('🛠 Fix Link', 'wl.card_fix_link', 'cb-btn--fix',
        () => openFixLink(row), why));
    }
    actions.append(
      wlActionButton('✏ Edit', 'wl.card_edit', 'cb-btn--quiet',
        () => openEditChannel(row), why),
      busy
        ? wlActionButton('✕ Cancel', 'wl.card_cancel', 'cb-btn--warn',
            async () => {
              try {
                await call('watchlist.cancel', { channel_id: row.id });
                toast(WL_CANCEL_ONE_NOTE);
              } catch (_) { /* call() already toasted the reason */ }
            }, writeBlocked())
        : wlActionButton('✕ Remove', 'wl.card_remove', 'cb-btn--quiet',
            () => openRemoveChannel(row), writeBlocked()));
    card.appendChild(actions);
    return card;
  }

  /* The design's downloading line: track — percent · channel total. The live
     progress.current frames are finer-grained than the card's own snapshot of
     them, so they win when one has arrived for this run. */
  function wlCurrentLine(row) {
    const progress = row.progress || {};
    const title = (wl.current && wl.current.title) || progress.title || '';
    const percent = wl.current && wl.current.percent != null
      ? wl.current.percent : progress.title_percent;
    const parts = [];
    parts.push(title || 'starting…');
    if (percent != null) parts[0] += ` — ${percent}%`;
    parts.push(`${num(row.downloaded)} downloaded`);
    return parts.join(' · ');
  }

  function wlPaintProgress() {
    wl.cards.forEach((row) => {
      if (row.status !== 'downloading') return;
      const fill = $(`#wl-bar-${row.id}`);
      const line = $(`#wl-line-${row.id}`);
      if (fill && wl.overall && wl.overall.percent != null) {
        fill.style.width = wl.overall.percent + '%';
      }
      if (line) line.textContent = wlCurrentLine(row);
    });
  }

  function renderWatchlistToolbar() {
    const pending = wlPending();
    const unresolved = wlUnresolved().length;
    const blocked = writeBlocked();
    const scanReason = blocked ||
      (dl.running ? TOOLTIPS['main.scan_batch_conflict']
                  : (wl.running ? WL_BUSY_REASON : ''));

    const quick = $('#quick-scan');
    if (quick) wlGate(quick, scanReason, 'wl.scan_all');
    wlGate($('#wl-scan'), scanReason, 'wl.scan_all');

    wlGate($('#wl-add'), blocked || (wl.running ? WL_BUSY_REASON : ''),
      'wl.add_channel');
    wlGate($('#wl-links'),
      blocked || (wl.running ? WL_BUSY_REASON
        : (unresolved ? ''
           : 'Every channel already resolves to a real channel id — nothing to check.')),
      'wl.check_links');
    wlGate($('#wl-dl-all'),
      blocked || (wl.running ? WL_BUSY_REASON
        : (pending ? '' : WL_NOTHING_PENDING)),
      'wl.download_all_new');
    wlGate($('#wl-cancel'),
      blocked || (wl.running ? '' : 'No Watch List scan or download is running.'),
      'wl.cancel_all');
    $('#wl-cancel').className = 'cb-btn ' + (wl.running ? 'cb-btn--warn' : 'cb-btn--quiet');
    $('#wl-dl-all').textContent = `⬇ Download All New (${num(pending)})`;
    $('#wl-summary').innerHTML =
      `<span class="cb-mono">${num(wl.cards.length)}</span> channels · ` +
      `<span class="cb-mono">${num(pending)}</span> new`;
  }

  function renderWatchlist() {
    renderWatchlistToolbar();
    // The nav badge and the Overview's copy of the same number are fed from
    // these cards, so a scan reporting one channel moves all three.
    renderOverviewWatch();
    const host = $('#wl-cards');
    host.innerHTML = '';
    if (!wl.cards.length) {
      const empty = document.createElement('div');
      empty.className = 'cb-card cb-pad';
      const text = document.createElement('span');
      text.className = 'cb-mut';
      text.textContent = 'No channels tracked yet — add one to have new uploads ' +
        'found automatically.';
      empty.appendChild(text);
      host.appendChild(empty);
      return;
    }
    wl.cards.forEach((row) => host.appendChild(wlCardNode(row)));
    bindTips(host);
  }

  /* A watchlist.card event replaces exactly one card. Rebuilding the whole
     list on every frame of a download would blow away focus and any hover the
     user is on, several times a second. */
  function wlApplyCard(card) {
    if (!card || card.id == null) return;
    const idx = wl.cards.findIndex((c) => c.id === card.id);
    if (idx === -1) { wl.cards.push(card); renderWatchlist(); return; }
    wl.cards[idx] = card;
    const host = $('#wl-cards');
    const old = host.querySelector(`[data-cid="${card.id}"]`);
    if (!old) { renderWatchlist(); return; }
    const node = wlCardNode(card);
    host.replaceChild(node, old);
    bindTips(node);
    renderWatchlistToolbar();
    renderOverviewWatch();
    renderOverviewAttention();
  }

  /* An empty pinned log reads as broken rather than idle, so it says which it
     is until the first line arrives. */
  function wlLogReset() {
    const box = $('#wl-log');
    box.innerHTML = '';
    const hint = document.createElement('div');
    hint.className = 'cb-mut cb-log__empty';
    hint.textContent = 'Scan lines appear here while a scan or a Watch List ' +
      'download is running.';
    box.appendChild(hint);
  }

  function wlLogAppend(entry) {
    const box = $('#wl-log');
    const hint = box.querySelector('.cb-log__empty');
    if (hint) hint.remove();
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
    const text = entry.text || '';
    box.appendChild(logLineNode(entry.ts ? `${entry.ts} | ${text}` : text,
                                entry.level || 'default'));
    while (box.childElementCount > WL_LOG_LIMIT) box.removeChild(box.firstElementChild);
    if (atBottom) box.scrollTop = box.scrollHeight;
  }

  /* ── Add Channel (plain dialog) ─────────────────────────────────────────── */

  function genreSelect(current) {
    const sel = document.createElement('select');
    sel.className = 'cb-sel';
    sel.style.flex = '1';
    const list = (state.genres || []).slice();
    if (!list.includes('(none)')) list.push('(none)');
    if (current && !list.includes(current)) list.unshift(current);
    list.forEach((g) => {
      const o = document.createElement('option');
      o.value = g;
      o.textContent = g;
      sel.appendChild(o);
    });
    if (current) sel.value = current;
    return sel;
  }

  /* The design's Genre row: the combo, + New and − Remove. Neither button has
     a service method behind it (no genre folder is created or deleted from the
     web frontend yet), so both render with the registry tooltip plus the
     reason, rather than as dead controls. */
  function genreRow(sel) {
    const row = document.createElement('div');
    row.className = 'cb-row';
    row.style.gap = '8px';
    const add = document.createElement('button');
    add.className = 'cb-btn cb-btn--quiet cb-btn--sm';
    add.textContent = '+ New';
    setDisabled(add, true, {
      reason: (TOOLTIPS['main.new_genre'] ? TOOLTIPS['main.new_genre'] + '\n\n' : '') +
        WL_NO_HOST_ACTION,
    });
    const drop = document.createElement('button');
    drop.className = 'cb-btn cb-btn--warn cb-btn--sm';
    drop.textContent = '− Remove';
    setDisabled(drop, true, {
      reason: (TOOLTIPS['db.genre_remove'] ? TOOLTIPS['db.genre_remove'] + '\n\n' : '') +
        WL_NO_HOST_ACTION,
    });
    row.append(sel, add, drop);
    return row;
  }

  function openAddChannel() {
    let urlEl = null;
    let genreEl = null;
    openModal({
      title: '+ Add Channel',
      body(body) {
        urlEl = document.createElement('input');
        urlEl.className = 'cb-in cb-mono';
        urlEl.style.fontSize = '12px';
        urlEl.placeholder = 'https://www.youtube.com/@…   or   https://soundcloud.com/…';
        body.appendChild(labelled('Channel / Playlist URL', urlEl, WL_URL_HINT));
        genreEl = genreSelect('(none)');
        body.appendChild(labelled('Genre', genreRow(genreEl)));
      },
      foot(foot, api) {
        foot.appendChild(modalNote(
          'The channel is looked up so it can be named; one that cannot be read ' +
          'right now is still added, under its URL.'));
        const cancel = modalButton('Cancel', 'cb-btn--quiet', closeModal);
        cancel.style.marginLeft = 'auto';
        const save = modalButton('Add Channel', 'cb-btn--fill', async () => {
          const url = urlEl.value.trim();
          if (!url) { api.error('Paste a channel URL first.'); urlEl.focus(); return; }
          api.error('');
          api.busy(true);
          save.textContent = 'Adding…';
          try {
            await cbApi.call('watchlist.add', { url, genre: genreEl.value });
            closeModal();
            toast('Channel added to the Watch List.');
            await refresh();
          } catch (err) {
            api.busy(false);
            save.textContent = 'Add Channel';
            api.error(err.userFacing ? err.message : 'The host could not add that channel.');
          }
        });
        foot.append(cancel, save);
      },
      focus: () => urlEl,
    });
  }

  /* ── Remove (plain yes/no) ──────────────────────────────────────────────── */

  function openRemoveChannel(row) {
    openModal({
      title: `Remove — ${row.name}`,
      width: 480,
      body(body) {
        body.appendChild(modalNote(TOOLTIPS['wl.card_remove'] ||
          'Removes this channel entry from the Watch List only.'));
      },
      foot(foot, api) {
        const cancel = modalButton('Cancel', 'cb-btn--quiet', closeModal);
        cancel.style.marginLeft = 'auto';
        const go = modalButton('Remove channel', 'cb-btn--warn', async () => {
          api.busy(true);
          try {
            await cbApi.call('watchlist.remove', { channel_id: row.id });
            closeModal();
            toast(`Removed ${row.name} — its files are untouched.`);
            await refresh();
          } catch (err) {
            api.busy(false);
            api.error(err.userFacing ? err.message : 'The host could not remove that channel.');
          }
        }, 'wl.card_remove');
        foot.append(cancel, go);
      },
    });
  }

  /* ── Edit Channel (3m) ─────────────────────────────────────────────────────
     No display-name field — the name comes from the resolved channel. The
     monolith's "folder moved out of band" auto-heal note is absent: that
     service method was deliberately not ported, and a note nothing verifies
     would be a claim the frontend cannot make. */

  /* The folder path, the movable-track count and the unavailable-track count
     are all host facts a card cannot carry (each costs a listdir or a COUNT,
     and a card is re-emitted many times a second during a run), so the dialog
     asks for them once, on open. */
  async function wlDetails(row) {
    try {
      const res = await cbApi.call('watchlist.details', { channel_id: row.id });
      return { folder: (res && res.folder) || '',
               tracks: Number((res && res.tracks) || 0),
               unavailable: Number((res && res.unavailable) || 0) };
    } catch (_) { return null; }
  }

  /* prefill carries the values the user had typed when a confirmation step
     took the dialog away, so backing out of that confirmation returns them to
     the dialog they left rather than to the stored row. */
  function openEditChannel(row, prefill) {
    const local = state && state.host.transport === 'local';
    const currentUrl = wlUrl(row);
    const currentGenre = row.genre || '(none)';
    let urlEl = null;
    let genreEl = null;
    let folderBtn = null;
    let forgetBtn = null;
    let folder = '';
    let movableTracks = null;   // null until watchlist.details answers

    const api = openModal({
      title: `Edit — ${row.name}`,
      body(body) {
        urlEl = document.createElement('input');
        urlEl.className = 'cb-in cb-mono';
        urlEl.style.fontSize = '12px';
        urlEl.value = (prefill && prefill.url !== undefined) ? prefill.url : currentUrl;
        urlEl.placeholder = 'https://www.youtube.com/@…';
        body.appendChild(labelled('Channel / Playlist URL', urlEl, WL_EDIT_URL_HINT));

        const tools = document.createElement('div');
        tools.className = 'cb-row';
        tools.style.cssText = 'gap:8px;flex-wrap:wrap';
        folderBtn = document.createElement('button');
        folderBtn.className = 'cb-btn cb-btn--quiet cb-btn--sm';
        folderBtn.textContent = local ? '📂 Open Folder' : '📋 Copy folder path';
        setDisabled(folderBtn, true, {
          reason: (TOOLTIPS['wl.card_open_folder'] ? TOOLTIPS['wl.card_open_folder'] + '\n\n' : '') +
            'Looking the folder up on the host…',
        });

        const openLink = document.createElement('button');
        openLink.className = 'cb-btn cb-btn--quiet cb-btn--sm';
        openLink.textContent = '🌐 Open Link';
        const safe = dbSafeLink(currentUrl);
        if (safe) {
          openLink.addEventListener('click', () => window.open(safe, '_blank', 'noopener'));
        } else {
          setDisabled(openLink, true, {
            reason: currentUrl ? 'Only http and https links can be opened.'
                               : 'This channel has no link yet — use 🛠 Smart-Edit Link.',
          });
        }

        const smart = modalButton('🛠 Smart-Edit Link', 'cb-btn--quiet', () => {
          /* Closes this dialog before Fix Link opens — the design's rule that
             two modal grabs never fight over focus. */
          closeModal();
          openFixLink(row);
        }, 'wl.card_smart_edit');
        tools.append(folderBtn, openLink, smart);
        body.appendChild(tools);

        genreEl = genreSelect((prefill && prefill.genre) || currentGenre);
        body.appendChild(labelled('Genre', genreRow(genreEl)));

        /* The count lands with watchlist.details; until then the button says
           what it does without claiming a number it does not have. */
        forgetBtn = modalButton('Forget unavailable tracks', 'cb-btn--quiet',
          async () => {
            forgetBtn.disabled = true;
            try {
              const res = await cbApi.call('watchlist.forget_unavailable',
                                            { channel_id: row.id });
              const n = (res && res.removed) || 0;
              forgetBtn.textContent = 'Forget unavailable tracks (0)';
              toast(`Forgot ${num(n)} permanently-unavailable track${n === 1 ? '' : 's'}.`);
            } catch (err) {
              forgetBtn.disabled = false;
              api.error(err.userFacing ? err.message :
                'The host could not forget this channel\'s unavailable tracks.');
            }
          }, 'wl.card_forget_unavailable');
        forgetBtn.style.alignSelf = 'flex-start';
        body.appendChild(forgetBtn);

        const warn = document.createElement('div');
        warn.className = 'cb-warnbox';
        warn.textContent = WL_GENRE_WARNING;
        body.appendChild(warn);
      },
      foot(foot, dialog) {
        const cancel = modalButton('Cancel', 'cb-btn--quiet', closeModal);
        cancel.style.marginLeft = 'auto';
        const save = modalButton('Save', 'cb-btn--fill', async () => {
          const url = urlEl.value.trim();
          const genre = genreEl.value;
          const params = { channel_id: row.id };
          if (url && url !== currentUrl) params.url = url;
          if (genre && genre !== currentGenre) params.genre = genre;
          if (params.url === undefined && params.genre === undefined) {
            closeModal();
            return;
          }
          /* 3m: "You get a confirmation naming the track count first." A genre
             change moves the folder on disk, rewrites the downloads rows and
             retags every file — it does not go through on one dropdown pick. */
          if (params.genre !== undefined) {
            closeModal();
            openGenreMoveConfirm(row, params, movableTracks,
                                 () => openEditChannel(row, { url, genre }));
            return;
          }
          dialog.error('');
          dialog.busy(true);
          save.textContent = 'Saving…';
          try {
            await cbApi.call('watchlist.edit', params);
            closeModal();
            toast('Channel saved.');
            await refresh();
          } catch (err) {
            /* The host's own wording is the whole explanation, so it is shown
               as-is rather than replaced. */
            dialog.busy(false);
            save.textContent = 'Save';
            dialog.error(err.userFacing ? err.message :
              'The host could not save that change.');
          }
        });
        foot.append(cancel, save);
      },
      focus: () => urlEl,
    });

    wlDetails(row).then((details) => {
      if (!api.body.isConnected) return;    // dialog closed while in flight
      if (details && details.unavailable && forgetBtn) {
        forgetBtn.textContent = `Forget unavailable tracks (${num(details.unavailable)})`;
      }
      movableTracks = details ? details.tracks : null;
      folder = details ? details.folder : '';
      if (!folderBtn) return;
      if (!folder) {
        setDisabled(folderBtn, true, {
          reason: (TOOLTIPS['wl.card_open_folder'] ? TOOLTIPS['wl.card_open_folder'] + '\n\n' : '') +
            'The host has no folder recorded for this channel yet.',
        });
        return;
      }
      setDisabled(folderBtn, false, { ttKey: 'wl.card_open_folder' });
      /* fs.* is refused on the remote transport server-side, so a remote
         session is handed the path to copy instead — the same split the
         Database viewer's context menu makes. The path is shown alongside it
         there (there is nothing else to tell the user what would be copied);
         locally the button opens it and 3m's dialog stays as designed. */
      folderBtn.addEventListener('click',
        () => (local ? dbReveal(folder, 'folder') : dbCopyText(folder, 'folder path')));
      if (local) return;
      const shown = document.createElement('div');
      shown.className = 'cb-mut cb-mono';
      shown.style.cssText = 'font-size:11px;overflow-wrap:anywhere';
      shown.textContent = folder;
      api.body.insertBefore(shown, folderBtn.parentElement.nextSibling);
    });
  }

  /* 3m's genre-move confirmation. A separate dialog rather than an inline
     step, because one dialog is open at a time (openModal closes the Edit one
     on the way in) — so Back re-opens Edit with what the user had typed.
     *tracks* is null when watchlist.details never answered; the copy then says
     so instead of naming a count it does not have. */
  function openGenreMoveConfirm(row, params, tracks, onBack) {
    let confirmed = false;
    openModal({
      title: `Move — ${row.name}`,
      width: 480,
      onClose() { if (!confirmed && onBack) setTimeout(onBack, 0); },
      body(body) {
        const count = tracks == null
          ? 'The tracks in this channel\'s folder'
          : `${num(tracks)} track${tracks === 1 ? '' : 's'}`;
        body.appendChild(modalNote(
          `${count} will move from “${row.genre || '(none)'}” to ` +
          `“${params.genre}”. The database rows are rewritten to match and the ` +
          `Genre tag inside each file is updated.`));
        const warn = document.createElement('div');
        warn.className = 'cb-warnbox';
        warn.textContent = 'A failed database write rolls the move back, so ' +
          'disk and database never disagree. Retagging runs in the background ' +
          'and reports into the scan log when it finishes.';
        body.appendChild(warn);
      },
      foot(foot, dialog) {
        const back = modalButton('Back', 'cb-btn--quiet', closeModal);
        back.style.marginLeft = 'auto';
        const go = modalButton('Move the folder', 'cb-btn--fill', async () => {
          dialog.error('');
          dialog.busy(true);
          go.textContent = 'Moving…';
          try {
            const res = await cbApi.call('watchlist.edit', params);
            confirmed = true;
            closeModal();
            const moved = res && res.genre;
            toast(moved && moved.moved
              ? `Moved ${num(moved.moved)} track${moved.moved === 1 ? '' : 's'} to ${params.genre}.`
              : `Channel filed under ${params.genre}.`);
            await refresh();
          } catch (err) {
            /* A genre move is refused outright while a batch or Watch List job
               runs; the host's wording is the whole explanation. */
            dialog.busy(false);
            go.textContent = 'Move the folder';
            dialog.error(err.userFacing ? err.message :
              'The host could not move that channel.');
          }
        });
        foot.append(back, go);
      },
    });
  }

  /* ── Fix Link (3m) ────────────────────────────────────────────────────────
     opts.queue = {index, total} drives the Check Links walk. opts.onNext moves
     that walk on, and fires only when the dialog was applied or explicitly
     skipped — closing it (✕, Escape, the dim) stops the walk instead, so
     backing out of a 20-channel sweep is one gesture, not twenty. */

  function wlCandidateMeta(candidate) {
    const bits = [];
    if (candidate.handle) bits.push(candidate.handle);
    if (candidate.channel_id) bits.push(candidate.channel_id);
    if (candidate.followers != null) bits.push(`${num(candidate.followers)} subscribers`);
    if (candidate.confidence != null && !candidate.channel_id) {
      bits.push(`${Math.round(candidate.confidence * 100)}% match`);
    }
    return bits.join(' · ');
  }

  function openFixLink(row, opts) {
    opts = opts || {};
    let listEl = null;
    let pasteEl = null;
    let picked = null;
    let advance = false;

    const api = openModal({
      title: `🛠 Fix Link — ${row.name}` +
             (opts.queue ? ` (${opts.queue.index} of ${opts.queue.total})` : ''),
      width: 608,
      tag: { text: 'Unresolved', cls: 'cb-tag--attn' },
      /* Deferred a tick: this fires from inside closeModal, and the next
         dialog must not open while the one that triggered it is unwinding. */
      onClose() { if (advance && opts.onNext) setTimeout(opts.onNext, 0); },
      body(body) {
        /* 3m says "a YouTube search"; a SoundCloud entry is searched on
           SoundCloud instead (watchrun.resolve_candidates), so the sentence
           names whichever one is about to run. */
        const where = row.platform === 'SoundCloud' ? 'SoundCloud' : 'YouTube';
        body.appendChild(modalNote(
          'This folder has no canonical channel id, so scans fall back to the ' +
          `folder name. Pick the channel it belongs to — the top matches from a ` +
          `${where} search for “${row.name}”.`));
        listEl = document.createElement('div');
        listEl.style.cssText = 'display:flex;flex-direction:column;gap:8px';
        const loading = document.createElement('div');
        loading.className = 'cb-mut';
        loading.style.fontSize = '12.5px';
        loading.textContent = `Searching for “${row.name}”…`;
        listEl.appendChild(loading);
        body.appendChild(listEl);

        const pasteRow = document.createElement('div');
        pasteRow.className = 'cb-row';
        pasteRow.style.gap = '9px';
        const lab = document.createElement('span');
        lab.className = 'cb-lab';
        lab.style.width = '74px';
        lab.textContent = 'Or paste:';
        pasteEl = document.createElement('input');
        pasteEl.className = 'cb-in cb-mono';
        pasteEl.style.fontSize = '12px';
        pasteEl.placeholder = 'https://www.youtube.com/@…';
        pasteEl.addEventListener('input', () => {
          if (!pasteEl.value.trim()) return;
          picked = null;
          $$('.cb-pick', listEl).forEach((el) => el.classList.remove('is-on'));
          $$('input[type="radio"]', listEl).forEach((el) => { el.checked = false; });
        });
        pasteRow.append(lab, pasteEl);
        body.appendChild(pasteRow);
      },
      foot(foot, dialog) {
        foot.appendChild(modalNote(
          'Resolving does not re-download anything — the next scan simply finds ' +
          'the right listing.'));
        const cancel = modalButton(opts.queue ? 'Skip channel' : 'Cancel',
          'cb-btn--quiet', () => { advance = !!opts.queue; closeModal(); });
        cancel.style.marginLeft = 'auto';
        const apply = modalButton('Apply link', 'cb-btn--fill', async () => {
          const pasted = pasteEl.value.trim();
          if (!picked && !pasted) {
            dialog.error('Pick a channel above, or paste its URL.');
            return;
          }
          dialog.error('');
          dialog.busy(true);
          apply.textContent = 'Applying…';
          try {
            await cbApi.call('watchlist.resolve_apply', {
              channel_id: row.id,
              resolved_url: pasted || (picked && picked.url) || '',
              resolved_channel_id: pasted ? '' : ((picked && picked.channel_id) || ''),
            });
            advance = true;
            closeModal();
            toast(`Link set for ${row.name}.`);
            await refresh();
          } catch (err) {
            dialog.busy(false);
            apply.textContent = 'Apply link';
            dialog.error(err.userFacing ? err.message :
              'The host could not save that link.');
          }
        });
        foot.append(cancel, apply);
      },
    });

    cbApi.call('watchlist.resolve_candidates', { channel_id: row.id })
      .then((candidates) => {
        if (!listEl || !listEl.isConnected) return;
        listEl.innerHTML = '';
        if (!candidates || !candidates.length) {
          const none = document.createElement('div');
          none.className = 'cb-mut';
          none.style.fontSize = '12.5px';
          none.textContent = 'No matches came back — paste the channel URL below instead.';
          listEl.appendChild(none);
          return;
        }
        candidates.forEach((candidate, index) => {
          const dupe = candidate.duplicate_of;
          const pick = document.createElement('label');
          pick.className = 'cb-pick' + (dupe ? ' is-off' : '');
          const radio = document.createElement('input');
          radio.type = 'radio';
          radio.name = 'cb-fixlink';
          radio.className = 'cb-cbx';
          radio.style.marginTop = '2px';
          if (dupe) {
            setDisabled(radio, true, {
              reason: `Already tracked as “${dupe.name}” — linking this entry to it ` +
                      'would leave you with two rows for one channel.',
            });
          } else {
            radio.addEventListener('change', () => {
              picked = candidate;
              pasteEl.value = '';
              $$('.cb-pick', listEl).forEach((el) => el.classList.remove('is-on'));
              pick.classList.add('is-on');
            });
          }
          const text = document.createElement('span');
          text.className = 'cb-pick__text';
          const name = document.createElement('span');
          name.className = 'cb-pick__name';
          name.textContent = candidate.title || row.name;
          const meta = document.createElement('span');
          meta.className = 'cb-pick__meta';
          meta.textContent = wlCandidateMeta(candidate);
          text.append(name, meta);
          if (dupe) {
            const note = document.createElement('span');
            note.className = 'cb-pick__dupe';
            note.textContent = `Already tracked as “${dupe.name}” — would duplicate`;
            text.appendChild(note);
          }
          pick.append(radio, text);
          listEl.appendChild(pick);
          if (index === 0 && !dupe) { radio.checked = true; picked = candidate; pick.classList.add('is-on'); }
        });
        bindTips(listEl);
      })
      .catch((err) => {
        if (!listEl || !listEl.isConnected) return;
        listEl.innerHTML = '';
        api.error(err.userFacing ? err.message : 'The search could not be run.');
      });
  }

  /* Check Links walks every unresolved channel through Fix Link in turn. */
  function runCheckLinks() {
    const queue = wlUnresolved();
    if (!queue.length) {
      toast('Every channel already resolves to a real channel id.');
      return;
    }
    let index = 0;
    const next = () => {
      if (index >= queue.length) { refresh(); return; }
      const row = queue[index];
      index += 1;
      openFixLink(row, { queue: { index, total: queue.length }, onNext: next });
    };
    next();
  }

  /* ── log viewers (3e Activity / 3f Debug) ─────────────────────────────────
     One component, two instances, told apart by `kind` ('activity'/'debug')
     — the id prefix in the DOM (#log-activity-… vs #log-debug-…) is the only
     thing that differs structurally. Windowed: the service never hands back
     more than LOG_WINDOW_LIMIT lines, so the DOM never holds more either.
     Filter and the live incremental highlight-as-you-type search work on
     whatever window is currently loaded; stepping through a search match
     (▲/▼) always re-asks the host for the window starting at that match's
     byte offset, so it's correct across the whole file, not just what's on
     screen — logs.tail's `start` lines up exactly with logs.search's
     `offset` because both walk the same byte-exact line splitter. */
  const LOG_WINDOW_LIMIT = 2000;
  const LOG_SEARCH_DEBOUNCE = 200;

  const LOG_KINDS = {
    activity: {
      filterMatches(line, filter) {
        if (filter === 'All' || !filter) return true;
        if (filter === 'Downloaded') return line.includes('DOWNLOADED');
        if (filter === 'Skipped') return line.includes('SKIPPED');
        if (filter === 'Errors') return line.includes('ERROR');
        return true;
      },
      lineClass(line) {
        if (line.includes('════')) return line.includes('CANCELLED') ? 'error' : 'warning';
        if (line.includes('DOWNLOADED')) return 'downloaded';
        if (line.includes('SKIPPED')) return 'skipped';
        if (line.includes('ERROR')) return 'error';
        return 'default';
      },
      stats(lines, totalLines) {
        const dl = lines.filter((l) => l.includes('DOWNLOADED')).length;
        const sk = lines.filter((l) => l.includes('SKIPPED')).length;
        const er = lines.filter((l) => l.includes('ERROR')).length;
        return [
          { text: `${num(lines.length)} lines shown (total ${num(totalLines)})` },
          { text: `✓ ${num(dl)} downloaded`, color: 'var(--cb-ok)' },
          { text: `⊘ ${num(sk)} skipped`, color: 'var(--cb-warn)' },
          { text: `✗ ${num(er)} error${er === 1 ? '' : 's'}`, color: 'var(--cb-err)' },
        ];
      },
    },
    debug: {
      filterMatches(line, filter) {
        if (filter === 'All' || !filter) return true;
        if (line.includes('═')) return true;
        return line.includes(`| ${filter}`);
      },
      lineClass(line) {
        if (line.includes('═')) return 'default';
        if (line.includes('| ERROR')) return 'error';
        if (line.includes('| WARNING') || line.includes('| WARN')) return 'warning';
        if (line.includes('| DEBUG')) return 'debug';
        return 'default';
      },
      stats(lines, totalLines) {
        const info = lines.filter((l) => l.includes('| INFO')).length;
        const dbg = lines.filter((l) => l.includes('| DEBUG')).length;
        const warn = lines.filter((l) => l.includes('| WARNING') || l.includes('| WARN')).length;
        const er = lines.filter((l) => l.includes('| ERROR')).length;
        return [
          { text: `${num(lines.length)} lines shown (total ${num(totalLines)})` },
          { text: `ℹ ${num(info)} info` },
          { text: `· ${num(dbg)} debug`, color: 'var(--cb-muted)' },
          { text: `⚠ ${num(warn)} warning${warn === 1 ? '' : 's'}`, color: 'var(--cb-warn)' },
          { text: `✗ ${num(er)} error${er === 1 ? '' : 's'}`, color: 'var(--cb-err)' },
        ];
      },
    },
  };

  const logState = {
    activity: { rawLines: [], windowStart: 0, windowEnd: 0, size: 0, totalLines: 0, path: '',
               filter: 'All', wrap: true, search: '', matches: [], matchPos: -1,
               currentMatchRawIndex: -1, searchTimer: null, loadingBefore: false,
               watching: false, open: false },
    debug: { rawLines: [], windowStart: 0, windowEnd: 0, size: 0, totalLines: 0, path: '',
            filter: 'All', wrap: true, search: '', matches: [], matchPos: -1,
            currentMatchRawIndex: -1, searchTimer: null, loadingBefore: false,
            watching: false, open: false },
  };

  function logEl(kind, suffix) { return $(`#log-${kind}-${suffix}`); }

  function logSplitTsAndRest(line) {
    const pipe = line.indexOf('|');
    if (pipe === -1) return [null, line];
    return [line.slice(0, pipe + 1), line.slice(pipe + 1)];
  }

  /* Wraps every case-insensitive occurrence of `query` inside `span` in a
     <mark>, building text/mark nodes by hand rather than an innerHTML replace
     — the log line is host-supplied text and the query is user-typed, and
     neither should ever be interpreted as markup. */
  function logMarkMatches(span, query) {
    if (!query) return;
    const text = span.textContent;
    const lower = text.toLowerCase();
    const needle = query.toLowerCase();
    let idx = lower.indexOf(needle);
    if (idx === -1) return;
    span.textContent = '';
    let cursor = 0;
    while (idx !== -1) {
      if (idx > cursor) span.appendChild(document.createTextNode(text.slice(cursor, idx)));
      const mark = document.createElement('mark');
      mark.textContent = text.slice(idx, idx + needle.length);
      span.appendChild(mark);
      cursor = idx + needle.length;
      idx = lower.indexOf(needle, cursor);
    }
    if (cursor < text.length) span.appendChild(document.createTextNode(text.slice(cursor)));
  }

  /* One rendered log line: dimmed timestamp up to the first `|`, the rest in
     the level's colour class. Shared with the Watch List's pinned scan log,
     whose scan.line events already carry this same class vocabulary — the
     level arrives on the event there instead of being read out of the text. */
  function logLineNode(line, cls) {
    const div = document.createElement('div');
    const [ts, rest] = logSplitTsAndRest(line);
    if (ts) {
      const tsSpan = document.createElement('span');
      tsSpan.className = 'ts';
      tsSpan.textContent = ts;
      div.appendChild(tsSpan);
    }
    const restSpan = document.createElement('span');
    restSpan.className = cls;
    restSpan.textContent = rest;
    div.appendChild(restSpan);
    return div;
  }

  function logRenderLine(kind, line, isCurrentMatch) {
    const div = logLineNode(line, LOG_KINDS[kind].lineClass(line));
    const restSpan = div.lastChild;
    if (logState[kind].search) {
      logMarkMatches(restSpan, logState[kind].search);
      if (isCurrentMatch) {
        const first = restSpan.querySelector('mark');
        if (first) first.classList.add('is-current');
      }
    }
    return div;
  }

  function logRenderWindow(kind) {
    const st = logState[kind];
    const cfg = LOG_KINDS[kind];
    const body = logEl(kind, 'body');
    body.innerHTML = '';
    /* Keep each rendered line's index into the raw (unfiltered) window
       alongside it, rather than its position in the filtered list — the
       "current match" is tracked by that raw index (see logGotoMatch), so a
       Filter change that reorders/removes lines around it never moves the
       highlight onto an unrelated line that merely landed at position 0. */
    const filtered = [];
    st.rawLines.forEach((line, rawIdx) => {
      if (st.filter === 'All' || cfg.filterMatches(line, st.filter)) {
        filtered.push({ line, rawIdx });
      }
    });
    if (!filtered.length) {
      const empty = document.createElement('div');
      empty.className = 'cb-mut';
      empty.textContent = st.size ? 'No lines match the current filter.' : '(log is empty)';
      body.appendChild(empty);
    } else {
      filtered.forEach(({ line, rawIdx }) => {
        body.appendChild(logRenderLine(kind, line, rawIdx === st.currentMatchRawIndex));
      });
    }
    logUpdateStatbar(kind, filtered.map((f) => f.line));
    logUpdatePathbar(kind);
  }

  function humanSize(bytes) {
    if (!bytes) return '0 KB';
    if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function logUpdatePathbar(kind) {
    const st = logState[kind];
    const cap = (state && state.settings && state.settings.log_limit) || 'Unlimited';
    logEl(kind, 'pathbar').textContent =
      `${st.path || ''} · ${humanSize(st.size)} of ${cap} limit`;
  }

  function logUpdateStatbar(kind, filtered) {
    const cfg = LOG_KINDS[kind];
    const st = logState[kind];
    const bar = logEl(kind, 'statbar');
    bar.innerHTML = '';
    cfg.stats(filtered, st.totalLines).forEach((part) => {
      const span = document.createElement('span');
      span.textContent = part.text;
      if (part.color) span.style.color = part.color;
      bar.appendChild(span);
    });
    const tail = document.createElement('span');
    tail.className = 'cb-log__tail';
    tail.textContent = st.watching && st.atBottom ? 'streaming · tail follows' : '';
    bar.appendChild(tail);
  }

  function logUpdateCounter(kind) {
    const st = logState[kind];
    const el = logEl(kind, 'count');
    if (!st.search) { el.textContent = ''; return; }
    el.textContent = st.matches.length ? `${st.matchPos + 1} of ${st.matches.length}` : 'no match';
  }

  function logScrollTo(kind, where) {
    const body = logEl(kind, 'body');
    if (where === 'top') body.scrollTop = 0;
    else if (where === 'bottom') body.scrollTop = body.scrollHeight;
  }

  async function logLoadWindow(kind, offset, before) {
    const st = logState[kind];
    const res = await call('logs.tail',
      { name: kind, offset, limit: LOG_WINDOW_LIMIT, before: !!before });
    st.rawLines = res.lines;
    st.windowStart = res.start;
    st.windowEnd = res.offset;
    st.size = res.size;
    st.totalLines = res.total_lines;
    st.path = res.path;
    st.atBottom = res.offset >= res.size;
    st.currentMatchRawIndex = -1;
  }

  /* The near-bottom scroll listener loads the next window forward by fully
     replacing it (see wireLogScreen) — that's the brief's "adjacent window"
     behaviour and keeps the DOM at ~2000 lines. This is the same operation
     in the other direction: after a search jump lands the window mid-file,
     scrolling up needs the window immediately *before* windowStart. Landing
     the scroll position at the bottom of the newly-loaded window keeps the
     seam continuous (that window's own `offset` lines up exactly with this
     window's old `windowStart`) instead of dumping the reader back at the
     very top of what they just loaded. */
  async function logLoadWindowBefore(kind) {
    const st = logState[kind];
    if (st.windowStart <= 0 || st.loadingBefore) return;
    st.loadingBefore = true;
    try {
      await logLoadWindow(kind, st.windowStart, true);
      logRenderWindow(kind);
      logScrollTo(kind, 'bottom');
    } finally {
      st.loadingBefore = false;
    }
  }

  async function logOpen(kind) {
    const st = logState[kind];
    st.open = true;
    try {
      await call('logs.watch', { name: kind, on: true });
      if (!st.open) return;   // navigated away while the call was in flight
      st.watching = true;
      await logLoadWindow(kind, null);
      if (!st.open) return;   // same race, on the tail-window fetch
      logRenderWindow(kind);
      logScrollTo(kind, 'bottom');
    } catch (_) { /* call() already toasted the reason */ }
  }

  function logClose(kind) {
    const st = logState[kind];
    if (!st.open) return;
    st.open = false;
    st.watching = false;
    call('logs.watch', { name: kind, on: false }).catch(() => {});
  }

  async function logJumpTop(kind) {
    await logLoadWindow(kind, 0);
    logRenderWindow(kind);
    logScrollTo(kind, 'top');
  }

  async function logJumpBottom(kind) {
    await logLoadWindow(kind, null);
    logRenderWindow(kind);
    logScrollTo(kind, 'bottom');
  }

  async function logRefresh(kind) {
    const st = logState[kind];
    await logLoadWindow(kind, st.atBottom ? null : st.windowStart);
    logRenderWindow(kind);
  }

  async function logGotoMatch(kind, idx) {
    const st = logState[kind];
    if (idx < 0 || idx >= st.matches.length) { logRenderWindow(kind); return; }
    const target = st.matches[idx];
    /* Always reload starting exactly at the match's own offset — logs.tail
       and logs.search walk the same byte-exact line splitter (see the module
       comment above), so res.lines[0] is guaranteed to be that match. Reusing
       an already-loaded window instead would mean tracking which rendered
       line the match landed on, for a save of one cheap local file read. */
    await logLoadWindow(kind, target.offset);
    st.currentMatchRawIndex = 0;
    logRenderWindow(kind);
    const body = logEl(kind, 'body');
    const marked = body.querySelector('mark.is-current');
    if (marked) marked.scrollIntoView({ block: 'center' });
    logUpdateCounter(kind);
  }

  function logOnSearchInput(kind) {
    const st = logState[kind];
    st.search = logEl(kind, 'search').value.trim();
    clearTimeout(st.searchTimer);
    if (!st.search) {
      st.matches = []; st.matchPos = -1;
      logRenderWindow(kind);
      logUpdateCounter(kind);
      return;
    }
    st.searchTimer = setTimeout(async () => {
      try {
        const res = await call('logs.search', { name: kind, query: st.search, regex: false });
        st.matches = res.matches;
        st.matchPos = st.matches.length ? 0 : -1;
      } catch (_) { st.matches = []; st.matchPos = -1; }
      await logGotoMatch(kind, st.matchPos);
    }, LOG_SEARCH_DEBOUNCE);
  }

  async function logSearchStep(kind, delta) {
    const st = logState[kind];
    if (!st.matches.length) return;
    st.matchPos = (st.matchPos + delta + st.matches.length) % st.matches.length;
    await logGotoMatch(kind, st.matchPos);
  }

  function logClearSearch(kind) {
    const st = logState[kind];
    st.search = ''; st.matches = []; st.matchPos = -1;
    logEl(kind, 'search').value = '';
    logRenderWindow(kind);
    logUpdateCounter(kind);
  }

  function logToggleWrap(kind) {
    const st = logState[kind];
    st.wrap = !st.wrap;
    const body = logEl(kind, 'body');
    body.classList.toggle('is-nowrap', !st.wrap);
    const btn = logEl(kind, 'wrap');
    btn.textContent = st.wrap ? 'Wrap: On' : 'Wrap: Off';
    btn.style.borderColor = st.wrap ? 'var(--cb-ok)' : '';
    btn.style.color = st.wrap ? 'var(--cb-ok)' : '';
    btn.style.background = st.wrap ? 'rgba(18,122,62,.06)' : '';
  }

  async function logDownload(kind) {
    try {
      const res = await call('logs.download', { name: kind });
      const filename = kind === 'activity' ? 'activity.log' : 'debug.log';
      let blob;
      if (state && state.host.transport === 'remote') {
        /* The remote replacement for "System Viewer" (HANDOFF §5): the host
           streams the file from /logs/<name> rather than the browser
           rebuilding it out of the window it happens to have loaded. */
        blob = await cbApi.fetchFile(`/logs/${encodeURIComponent(kind)}`);
      } else {
        const full = await call('logs.tail', { name: kind, offset: 0, limit: 0 });
        const text = full.lines.join('\n') + (full.lines.length ? '\n' : '');
        blob = new Blob([text], { type: 'text/plain' });
      }
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
      toast(`Downloaded ${filename}`);
      return res;
    } catch (_) { /* call() already toasted the reason */ return null; }
  }

  function logHandleAppend(kind, payload) {
    const st = logState[kind];
    if (!st.open || !st.watching || !st.atBottom) return;
    const combined = st.rawLines.concat(payload.lines);
    const dropped = Math.max(0, combined.length - LOG_WINDOW_LIMIT);
    st.rawLines = combined.slice(dropped);
    if (st.currentMatchRawIndex >= 0) {
      st.currentMatchRawIndex -= dropped;
      if (st.currentMatchRawIndex < 0) st.currentMatchRawIndex = -1;
    }
    st.windowEnd = payload.offset;
    st.size = payload.offset;
    st.totalLines += payload.lines.length;
    logRenderWindow(kind);
    logScrollTo(kind, 'bottom');
  }

  function wireLogScreen(kind) {
    logEl(kind, 'filter').addEventListener('change', (e) => {
      logState[kind].filter = e.target.value;
      logRenderWindow(kind);
    });
    logEl(kind, 'wrap').addEventListener('click', () => logToggleWrap(kind));
    logEl(kind, 'search').addEventListener('input', () => logOnSearchInput(kind));
    logEl(kind, 'search').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') logSearchStep(kind, 1);
    });
    logEl(kind, 'prev').addEventListener('click', () => logSearchStep(kind, -1));
    logEl(kind, 'next').addEventListener('click', () => logSearchStep(kind, 1));
    logEl(kind, 'clear').addEventListener('click', () => logClearSearch(kind));
    logEl(kind, 'top').addEventListener('click', () => logJumpTop(kind));
    logEl(kind, 'bottom').addEventListener('click', () => logJumpBottom(kind));
    logEl(kind, 'refresh').addEventListener('click', () => logRefresh(kind));
    logEl(kind, 'download').addEventListener('click', () => logDownload(kind));
    logEl(kind, 'body').addEventListener('scroll', () => {
      const st = logState[kind];
      const body = logEl(kind, 'body');
      if (st.windowStart > 0 && body.scrollTop < 60) { logLoadWindowBefore(kind); return; }
      if (st.atBottom) return;
      const nearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 60;
      if (nearBottom) logLoadWindow(kind, st.windowEnd).then(() => logRenderWindow(kind));
    });
  }

  /* ── database viewer (3g/3h/3i) ────────────────────────────────────────────
     One route, three tabs. Downloads is a lazy-loaded group tree (db.groups
     returns one level of {key,label,count}, drilling in one round trip per
     expand — see task-6-report.md); Watch List and Artwork are flat db.query
     tables. Column widths/order live in localStorage per HANDOFF §2, never
     the config file. Context menus: local transport gets the filesystem
     actions (fs.reveal) plus Copy *; remote keeps only the Copy * actions. */

  const DB_PAGE_SIZE = 200;
  const DB_GROUP_PRESETS = ['Platform › Genre › Channel', 'Genre › Channel',
                             'Channel', 'Platform › Channel'];
  /* Mirrors DownloadsDatabase.GROUP_PRESETS (cratebuilder/db.py) — a
     duplicate literal, not shared code, same reasoning db.py itself uses for
     its own copy of the monolith's GROUP_PRESETS: each layer that needs the
     hierarchy keeps its own copy rather than reaching across a language
     boundary. */
  const GROUP_HIERARCHY = {
    'Platform › Genre › Channel': ['platform', 'genre', 'channel_name'],
    'Genre › Channel': ['genre', 'channel_name'],
    'Channel': ['channel_name'],
    'Platform › Channel': ['platform', 'channel_name'],
  };
  const DB_ARTWORK_FILTERS = ['All tracks', 'Has artwork', 'Missing artwork',
                              'Embedded only', 'Sidecar missing on disk'];

  /* columns.downloads/watchlist/artwork straight from ui-contract.json —
     ids, headings and widths verbatim. */
  const DL_COLUMNS = [
    { id: 'title', head: 'Title / Group', w: 340, align: 'w', pinned: true },
    { id: 'channel', head: 'Channel', w: 160, align: 'w' },
    { id: 'genre', head: 'Genre', w: 110, align: 'w' },
    { id: 'platform', head: 'Platform', w: 80, align: 'w' },
    { id: 'upload', head: 'Upload', w: 110, align: 'w' },
    { id: 'downloaded', head: 'Downloaded', w: 140, align: 'w' },
    { id: 'bitrate', head: 'Bitrate', w: 70, align: 'e' },
  ];
  const WL_COLUMNS = [
    { id: 'sel', head: '', w: 34, align: 'center', pinned: true },
    { id: 'channel', head: 'Channel', w: 180, align: 'w' },
    { id: 'link', head: 'URL Link', w: 220, align: 'w' },
    { id: 'folder', head: 'Folder', w: 260, align: 'w' },
    { id: 'platform', head: 'Platform', w: 80, align: 'w' },
    { id: 'genre', head: 'Genre', w: 110, align: 'w' },
    { id: 'last_scan', head: 'Last scan', w: 120, align: 'w' },
    { id: 'pending', head: 'Pending new', w: 90, align: 'e' },
    { id: 'total', head: "Total dl'd", w: 80, align: 'e' },
    { id: 'status', head: 'Status', w: 90, align: 'w' },
  ];
  const ART_COLUMNS = [
    { id: 'title', head: 'Track', w: 260, align: 'w' },
    { id: 'channel', head: 'Channel', w: 150, align: 'w' },
    { id: 'platform', head: 'Platform', w: 80, align: 'w' },
    { id: 'embedded', head: 'Embedded', w: 80, align: 'center' },
    { id: 'sidecar', head: 'Sidecar', w: 170, align: 'w' },
    /* on_disk is derived from a live filesystem check, not a SQL column, so
       there is nothing for a paged query to ORDER BY — the header must not
       offer a sort it cannot perform. */
    { id: 'on_disk', head: 'On Disk', w: 70, align: 'center', sortable: false },
    { id: 'thumb_url', head: 'Thumbnail URL', w: 240, align: 'w' },
  ];

  const dbState = {
    activeTab: 'downloads',
    downloads: { groupPreset: DB_GROUP_PRESETS[0], platform: 'All platforms',
                genre: 'All genres', search: '', sortCol: 'downloaded',
                sortDesc: true, root: null, cols: null },
    watchlist: { search: '', sortCol: 'channel', sortDesc: false, rows: [],
                total: 0, offset: 0, checked: {}, loaded: false, cols: null },
    artwork: { filter: DB_ARTWORK_FILTERS[0], search: '', sortCol: 'title',
              sortDesc: false, rows: [], total: 0, offset: 0, selected: null,
              loaded: false, cols: null },
  };

  /* ── column widths/order: localStorage, never the config file ────────────── */
  function dbColStorageGet(key) {
    try {
      const raw = localStorage.getItem(key);
      return raw ? JSON.parse(raw) : null;
    } catch (_) { return null; }
  }
  function dbColStorageSet(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* private mode etc */ }
  }
  function makeColumnSet(defs, widthKey, orderKey) {
    const ids = defs.map((d) => d.id);
    const savedOrder = dbColStorageGet(orderKey);
    const order = (Array.isArray(savedOrder) && savedOrder.length === ids.length &&
                   ids.every((id) => savedOrder.includes(id)))
      ? savedOrder.slice() : ids.slice();
    const savedWidths = dbColStorageGet(widthKey) || {};
    const widths = {};
    defs.forEach((d) => { widths[d.id] = Number(savedWidths[d.id]) || d.w; });
    const byId = {};
    defs.forEach((d) => { byId[d.id] = d; });
    return {
      byId, order, widths, widthKey, orderKey,
      persistWidths() { dbColStorageSet(this.widthKey, this.widths); },
      persistOrder() { dbColStorageSet(this.orderKey, this.order); },
    };
  }
  /* Port of DatabaseViewerWindow._reorder_columns (DJ-CrateBuilder_v1.3.py):
     the target index is read from the ORIGINAL order before src is removed —
     reading it after the removal shifts a rightward drag one column short. */
  function dbReorderColumns(order, srcId, tgtId) {
    order = order.slice();
    if (order.indexOf(srcId) === -1) return order;
    const insertAt = (tgtId && order.includes(tgtId)) ? order.indexOf(tgtId) : 0;
    order.splice(order.indexOf(srcId), 1);
    order.splice(insertAt, 0, srcId);
    return order;
  }
  function dbWireHeaders(theadRow, colset, opts) {
    let dragSrc = null;
    theadRow.innerHTML = '';
    colset.order.forEach((id) => {
      const def = colset.byId[id];
      if (!def) return;
      const th = document.createElement('th');
      th.style.position = 'relative';
      th.style.width = colset.widths[id] + 'px';
      th.style.textAlign = def.align === 'e' ? 'right' : def.align === 'center' ? 'center' : 'left';
      const label = document.createElement('span');
      label.style.cursor = def.sortable === false ? 'default' : 'pointer';
      label.textContent = def.head + (opts.sortCol === id ? (opts.sortDesc ? ' ▾' : ' ▴') : '');
      label.setAttribute('data-tt', 'db.column_header');
      th.appendChild(label);
      if (def.sortable !== false) {
        label.addEventListener('click', () => opts.onSort(id));
      }
      if (!def.pinned) {
        th.addEventListener('mousedown', (e) => {
          if (e.target.classList.contains('cb-col-resize')) return;
          dragSrc = id;
        });
        th.addEventListener('mouseup', (e) => {
          const src = dragSrc; dragSrc = null;
          if (!src || src === id || e.target.classList.contains('cb-col-resize')) return;
          colset.order = dbReorderColumns(colset.order, src, id);
          colset.persistOrder();
          opts.onRender();
        });
      }
      const resize = document.createElement('span');
      resize.className = 'cb-col-resize';
      resize.addEventListener('mousedown', (e) => {
        e.stopPropagation();
        e.preventDefault();
        const startX = e.clientX;
        const startW = colset.widths[id];
        function onMove(ev) {
          colset.widths[id] = Math.max(40, startW + (ev.clientX - startX));
          th.style.width = colset.widths[id] + 'px';
        }
        function onUp() {
          document.removeEventListener('mousemove', onMove);
          document.removeEventListener('mouseup', onUp);
          colset.persistWidths();
        }
        document.addEventListener('mousemove', onMove);
        document.addEventListener('mouseup', onUp);
      });
      th.appendChild(resize);
      theadRow.appendChild(th);
    });
    bindTips(theadRow);
  }

  /* ── context menu ─────────────────────────────────────────────────────────
     Local transport gets Open File/Image + Open Containing Folder (fs.reveal
     — local-only per LOCAL_ONLY) alongside Copy *; remote hides those two and
     keeps only Copy * (HANDOFF §6). */
  let dbMenuEl = null;
  function dbMenuEscape(e) { if (e.key === 'Escape') dbHideMenu(); }
  function dbHideMenu() {
    if (dbMenuEl) { dbMenuEl.remove(); dbMenuEl = null; }
    document.removeEventListener('click', dbHideMenu, true);
    document.removeEventListener('keydown', dbMenuEscape, true);
  }
  function dbShowMenu(x, y, items) {
    dbHideMenu();
    const menu = document.createElement('div');
    menu.className = 'cb-menu';
    menu.style.position = 'fixed';
    items.forEach((item) => {
      if (item === '-') {
        const sep = document.createElement('div');
        sep.style.cssText = 'height:1px;background:var(--cb-line-soft);margin:3px 4px;padding:0';
        menu.appendChild(sep);
        return;
      }
      const el = document.createElement('div');
      el.textContent = item.label;
      if (item.disabled) {
        el.style.opacity = '.45';
        el.style.cursor = 'not-allowed';
        if (item.reason) el.setAttribute('data-tt-text', item.reason);
      } else {
        el.addEventListener('click', () => { dbHideMenu(); item.onClick(); });
      }
      menu.appendChild(el);
    });
    document.body.appendChild(menu);
    const bw = menu.offsetWidth, bh = menu.offsetHeight;
    let left = x, top = y;
    if (left + bw > innerWidth - 10) left = innerWidth - bw - 10;
    if (top + bh > innerHeight - 10) top = innerHeight - bh - 10;
    menu.style.left = Math.max(10, left) + 'px';
    menu.style.top = Math.max(10, top) + 'px';
    dbMenuEl = menu;
    bindTips(menu);
    setTimeout(() => {
      document.addEventListener('click', dbHideMenu, true);
      document.addEventListener('keydown', dbMenuEscape, true);
    }, 0);
  }

  async function dbCopyText(text, label) {
    if (!text) { toast('Nothing to copy.', true); return; }
    try {
      await navigator.clipboard.writeText(text);
      toast(`Copied ${label || 'to clipboard'}`);
    } catch (_) { toast('Could not copy to clipboard.', true); }
  }
  async function dbReveal(path, mode) {
    if (!path) { toast('No path is recorded for this row.', true); return; }
    try { await call('fs.reveal', { path, mode }); }
    catch (_) { /* call() already toasted the reason */ }
  }

  /* A watchlist URL is stored text, and this page can reach fs.reveal — so a
     javascript: value in that column must never become an href or a
     window.open target. Anything that isn't http(s) renders as plain text. */
  function dbSafeLink(url) {
    return /^https?:\/\//i.test(url || '') ? url : '';
  }

  async function dbExportCsv(table, filters, sort) {
    try {
      const res = await call('db.export_csv', { table, filters, sort });
      const blob = new Blob([res.csv], { type: 'text/csv' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = res.filename;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
      toast(`Exported ${res.rows} row${res.rows === 1 ? '' : 's'} to ${res.filename}`);
    } catch (_) { /* call() already toasted the reason */ }
  }

  /* ── Downloads tab: lazy group tree ───────────────────────────────────────
     A node is {key,label,count,path,depth,expanded,children,rows,rowsOffset,
     rowsTotal,loading}. path is the ordered list of {key,value} pins an
     ancestor drilled into; children is an array of subgroup nodes once
     fetched (null = not fetched), or stays null forever once a node turns
     out to be leaf-level, at which point rows/rowsOffset/rowsTotal are used
     instead. The synthetic root has depth -1 and is never itself rendered. */

  function dbTreeRootFilters() {
    const st = dbState.downloads;
    const f = {};
    if (st.platform && st.platform !== 'All platforms') f.platform = st.platform;
    if (st.genre && st.genre !== 'All genres') f.genre = st.genre;
    if (st.search) f.search = st.search;
    return f;
  }
  function dbPinnedKeys(path) {
    const pinned = new Set();
    const root = dbTreeRootFilters();
    if (root.platform) pinned.add('platform');
    if (root.genre) pinned.add('genre');
    path.forEach((p) => pinned.add(p.key));
    return pinned;
  }
  function dbNextHierarchyKey(preset, path) {
    const pinned = dbPinnedKeys(path);
    const hierarchy = GROUP_HIERARCHY[preset] || [];
    return hierarchy.find((k) => !pinned.has(k)) || null;
  }
  function dbPathFilters(path) {
    const f = dbTreeRootFilters();
    path.forEach((p) => {
      if (p.key === 'platform' || p.key === 'genre') f[p.key] = p.value;
      else { f.group_key = p.key; f.group_value = p.value; }
    });
    return f;
  }
  async function dbLoadRows(node) {
    const st = dbState.downloads;
    const res = await call('db.query', {
      table: 'downloads', filters: dbPathFilters(node.path),
      sort: { col: st.sortCol, desc: st.sortDesc }, offset: 0, limit: DB_PAGE_SIZE,
    });
    node.children = null;
    node.rows = res.rows; node.rowsTotal = res.total; node.rowsOffset = res.rows.length;
  }
  async function dbLoadMoreRows(node) {
    const st = dbState.downloads;
    /* want_total:false — the first page already established rowsTotal, and
       recounting is a full scan for the filesystem-backed filters. */
    const res = await call('db.query', {
      table: 'downloads', filters: dbPathFilters(node.path),
      sort: { col: st.sortCol, desc: st.sortDesc }, offset: node.rowsOffset,
      limit: DB_PAGE_SIZE, want_total: false,
    });
    node.rows = node.rows.concat(res.rows);
    node.rowsOffset += res.rows.length;
  }
  async function dbLoadGroups(node) {
    const st = dbState.downloads;
    const res = await call('db.groups', { preset: st.groupPreset, filters: dbPathFilters(node.path) });
    const groups = res.groups || [];
    if (!groups.length) { await dbLoadRows(node); return; }
    const nextKey = dbNextHierarchyKey(st.groupPreset, node.path);
    node.children = groups.map((g) => ({
      key: g.key, label: g.label, count: g.count,
      path: node.path.concat([{ key: nextKey, value: g.key }]),
      depth: node.depth + 1, expanded: false, children: null,
      rows: null, rowsOffset: 0, rowsTotal: 0, loading: false,
    }));
  }
  async function dbToggleGroup(node) {
    node.expanded = !node.expanded;
    if (node.expanded && node.children === null && node.rows === null) {
      node.loading = true;
      dbRenderDownloadsTree();
      try {
        const nextKey = dbNextHierarchyKey(dbState.downloads.groupPreset, node.path);
        if (nextKey === null) await dbLoadRows(node); else await dbLoadGroups(node);
      } catch (_) { node.error = true; }
      finally { node.loading = false; }
    }
    dbRenderDownloadsTree();
  }
  async function dbDownloadsReload() {
    const st = dbState.downloads;
    const root = { path: [], depth: -1, expanded: true, children: null,
                   rows: null, rowsOffset: 0, rowsTotal: 0, loading: true };
    st.root = root;
    dbRenderDownloadsTree();
    try {
      const nextKey = dbNextHierarchyKey(st.groupPreset, []);
      if (nextKey === null) await dbLoadRows(root); else await dbLoadGroups(root);
    } catch (_) { root.error = true; }
    finally { root.loading = false; }
    dbRenderDownloadsTree();
  }
  /* ⊞ expands every GROUP, and stops there — the registry calls it "Expand
     all groups" and the tkinter original is a pure display toggle. Descending
     into leaves would mean one db.query per channel and the whole library in
     the DOM, which is exactly what the paging design exists to avoid.
     The leaf test comes FIRST so a leaf-level group is left honestly ▸: marking
     it expanded would draw an open caret over nothing, and the next click would
     collapse it instead of loading its rows. */
  async function dbExpandRecursive(node) {
    if (dbNextHierarchyKey(dbState.downloads.groupPreset, node.path) === null) return;
    if (node.depth >= 0) node.expanded = true;
    if (node.children === null && node.rows === null) {
      node.loading = true;
      dbRenderDownloadsTree();
      try { await dbLoadGroups(node); }
      catch (_) { node.error = true; return; }
      finally { node.loading = false; }
      dbRenderDownloadsTree();
    }
    if (node.children) {
      for (const child of node.children) await dbExpandRecursive(child);
    }
  }
  let dbExpandRunning = false;
  async function dbExpandAllDownloads() {
    if (!dbState.downloads.root || dbExpandRunning) return;
    dbExpandRunning = true;
    const btn = $('#db-dl-expand');
    setDisabled(btn, true, { reason: 'Expanding every group — one moment…' });
    try { await dbExpandRecursive(dbState.downloads.root); }
    finally {
      dbExpandRunning = false;
      setDisabled(btn, false, { ttKey: 'db.expand_all' });
      dbRenderDownloadsTree();
    }
  }
  function dbCollapseAllDownloads() {
    function walk(node) { if (node.children) node.children.forEach((c) => { c.expanded = false; walk(c); }); }
    if (dbState.downloads.root) walk(dbState.downloads.root);
    dbRenderDownloadsTree();
  }

  function dbDownloadsMenuItems(row) {
    const local = state && state.host.transport === 'local';
    const items = [];
    if (local) {
      items.push({ label: 'Open File', disabled: !row.file_path,
        onClick: () => dbReveal(row.file_path, 'open') });
      items.push({ label: 'Open Containing Folder', disabled: !row.file_path,
        onClick: () => dbReveal(row.file_path, 'folder') });
    }
    items.push({ label: 'Copy Path', disabled: !row.file_path,
      onClick: () => dbCopyText(row.file_path, 'file path') });
    items.push('-');
    items.push({ label: 'Copy Source URL', disabled: !row.channel_url,
      onClick: () => dbCopyText(row.channel_url, 'source URL') });
    return items;
  }

  function dbUpdateDownloadsStatbar() {
    const st = dbState.downloads;
    const bar = $('#db-dl-statbar');
    bar.innerHTML = '';
    if (!st.root) return;
    let totalRows = 0;
    if (st.root.children) totalRows = st.root.children.reduce((a, g) => a + g.count, 0);
    else if (st.root.rows) totalRows = st.root.rowsTotal;
    const def = st.cols.byId[st.sortCol];
    const left = document.createElement('span');
    left.textContent = `${num(totalRows)} row${totalRows === 1 ? '' : 's'}`;
    const right = document.createElement('span');
    right.style.marginLeft = 'auto';
    right.textContent = `sorted by ${def ? def.head : st.sortCol} ${st.sortDesc ? '▾' : '▴'}`;
    bar.append(left, right);
  }

  function dbRenderDownloadsTree() {
    const st = dbState.downloads;
    const tbody = $('#db-dl-tbody');
    tbody.innerHTML = '';
    if (!st.root) return;
    const colset = st.cols;

    function renderGroupRow(node) {
      const tr = document.createElement('tr');
      tr.className = 'is-group';
      colset.order.forEach((id, idx) => {
        const td = document.createElement('td');
        if (idx === 0) {
          td.style.paddingLeft = (14 + node.depth * 18) + 'px';
          td.style.cursor = 'pointer';
          const arrow = document.createElement('span');
          arrow.textContent = node.expanded ? '▾ ' : '▸ ';
          const label = document.createElement('span');
          label.textContent = node.label;
          const count = document.createElement('span');
          count.className = 'cb-mut cb-mono';
          count.style.cssText = 'font-weight:400;font-size:11.5px;margin-left:6px';
          count.textContent = String(node.count);
          td.append(arrow, label, count);
          td.addEventListener('click', () => dbToggleGroup(node));
        }
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    }
    function renderLeafRow(row, depth) {
      const tr = document.createElement('tr');
      tr.dataset.id = String(row.id);
      colset.order.forEach((id) => {
        const def = colset.byId[id];
        const td = document.createElement('td');
        td.style.textAlign = def.align === 'e' ? 'right' : def.align === 'center' ? 'center' : 'left';
        if (id === 'title') td.style.paddingLeft = (14 + depth * 18) + 'px';
        else td.className = 'cb-mut';
        if (id === 'upload' || id === 'downloaded') { td.classList.add('cb-mono'); td.style.fontSize = '12px'; }
        if (id === 'bitrate') td.classList.add('cb-mono');
        td.textContent = row[id] != null && row[id] !== '' ? row[id] : (id === 'title' ? row.title : '');
        tr.appendChild(td);
      });
      tr.addEventListener('click', () => {
        $$('#db-dl-tbody tr.is-selected').forEach((r) => r.classList.remove('is-selected'));
        tr.classList.add('is-selected');
      });
      tr.addEventListener('dblclick', () => {
        if (state && state.host.transport === 'local') dbReveal(row.file_path, 'open');
      });
      tr.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        dbShowMenu(e.clientX, e.clientY, dbDownloadsMenuItems(row));
      });
      tbody.appendChild(tr);
    }
    function renderLoadMoreRow(node) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = colset.order.length;
      const remaining = node.rowsTotal - node.rowsOffset;
      td.style.cssText = `padding-left:${14 + (node.depth + 1) * 18}px;cursor:pointer;` +
                         'color:var(--cb-line);font-size:12px';
      td.textContent = `Load ${Math.min(DB_PAGE_SIZE, remaining)} more of ${remaining} remaining…`;
      td.addEventListener('click', async () => {
        try { await dbLoadMoreRows(node); }
        catch (_) { /* call() already toasted the reason */ }
        dbRenderDownloadsTree();
      });
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
    function renderNoteRow(depth, text, color) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = colset.order.length;
      td.style.cssText = `padding-left:${14 + depth * 18}px;color:${color};font-size:12px`;
      td.textContent = text;
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
    function walk(node) {
      if (node.depth >= 0) renderGroupRow(node);
      if (!node.expanded) return;
      if (node.loading) { renderNoteRow(node.depth + 1, 'Loading…', 'var(--cb-muted)'); return; }
      if (node.error && node.children === null && node.rows === null) {
        renderNoteRow(node.depth + 1, 'Could not load this level — try ⟳ Refresh.',
                      'var(--cb-err)');
        return;
      }
      if (node.children) node.children.forEach(walk);
      else if (node.rows) {
        node.rows.forEach((row) => renderLeafRow(row, node.depth + 1));
        if (node.rowsOffset < node.rowsTotal) renderLoadMoreRow(node);
      }
    }
    walk(st.root);
    dbUpdateDownloadsStatbar();
  }

  function dbRenderDownloadsHeaders() {
    dbWireHeaders($('#db-dl-thead'), dbState.downloads.cols, {
      sortCol: dbState.downloads.sortCol, sortDesc: dbState.downloads.sortDesc,
      onSort: (id) => {
        const st = dbState.downloads;
        if (st.sortCol === id) st.sortDesc = !st.sortDesc;
        else { st.sortCol = id; st.sortDesc = (id === 'downloaded' || id === 'upload' || id === 'bitrate'); }
        dbDownloadsReload();
      },
      onRender: () => { dbRenderDownloadsHeaders(); dbRenderDownloadsTree(); },
    });
  }

  function dbWireDownloadsToolbar() {
    const groupSel = $('#db-dl-group');
    DB_GROUP_PRESETS.forEach((p) => {
      const o = document.createElement('option'); o.textContent = p; groupSel.appendChild(o);
    });
    groupSel.value = dbState.downloads.groupPreset;
    groupSel.setAttribute('data-tt', 'db.change_grouping');
    groupSel.addEventListener('change', () => {
      dbState.downloads.groupPreset = groupSel.value; dbDownloadsReload();
    });

    const platSel = $('#db-dl-platform');
    ['All platforms', 'YouTube', 'SoundCloud'].forEach((p) => {
      const o = document.createElement('option'); o.textContent = p; platSel.appendChild(o);
    });
    platSel.addEventListener('change', () => {
      dbState.downloads.platform = platSel.value; dbDownloadsReload();
    });

    const genreSel = $('#db-dl-genre');
    function refreshGenreOptions() {
      const current = genreSel.value;
      genreSel.innerHTML = '';
      ['All genres'].concat(state.genres || []).forEach((g) => {
        const o = document.createElement('option'); o.textContent = g; genreSel.appendChild(o);
      });
      if (current) genreSel.value = current;
    }
    refreshGenreOptions();
    genreSel.addEventListener('change', () => {
      dbState.downloads.genre = genreSel.value; dbDownloadsReload();
    });

    const searchEl = $('#db-dl-search');
    let searchTimer = null;
    searchEl.addEventListener('input', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        dbState.downloads.search = searchEl.value.trim(); dbDownloadsReload();
      }, 250);
    });

    $('#db-dl-expand').addEventListener('click', dbExpandAllDownloads);
    $('#db-dl-collapse').addEventListener('click', dbCollapseAllDownloads);
    $('#db-dl-refresh').addEventListener('click', dbDownloadsReload);
    $('#db-dl-export').addEventListener('click', () => dbExportCsv(
      'downloads', dbTreeRootFilters(),
      { col: dbState.downloads.sortCol, desc: dbState.downloads.sortDesc }));
  }

  /* ── Watch List tab: flat table, small enough to load in one call ───────── */

  async function dbWatchlistReload() {
    const st = dbState.watchlist;
    try {
      const res = await call('db.query', {
        table: 'watchlist', filters: { search: st.search },
        sort: { col: st.sortCol, desc: st.sortDesc }, offset: 0, limit: DB_PAGE_SIZE,
      });
      st.rows = res.rows; st.total = res.total; st.offset = res.rows.length;
      st.loaded = true;
    } catch (_) { st.rows = []; st.total = 0; st.offset = 0; }
    dbRenderWatchlist();
  }
  /* Pages like the other two tabs rather than asking for the lot: a watch
     list is usually dozens of channels, but "usually" is not a bound, and a
     silent truncation would be worse than the round trip. */
  async function dbWatchlistLoadMore() {
    const st = dbState.watchlist;
    try {
      const res = await call('db.query', {
        table: 'watchlist', filters: { search: st.search },
        sort: { col: st.sortCol, desc: st.sortDesc }, offset: st.offset,
        limit: DB_PAGE_SIZE, want_total: false,
      });
      st.rows = st.rows.concat(res.rows); st.offset += res.rows.length;
    } catch (_) { /* call() already toasted the reason */ }
    dbRenderWatchlist();
  }
  function dbRenderWatchlistHeaders() {
    dbWireHeaders($('#db-wl-thead'), dbState.watchlist.cols, {
      sortCol: dbState.watchlist.sortCol, sortDesc: dbState.watchlist.sortDesc,
      onSort: (id) => {
        if (id === 'sel') return;
        const st = dbState.watchlist;
        if (st.sortCol === id) st.sortDesc = !st.sortDesc; else { st.sortCol = id; st.sortDesc = false; }
        dbWatchlistReload();
      },
      onRender: () => { dbRenderWatchlistHeaders(); dbRenderWatchlist(); },
    });
  }
  function dbWatchlistMenuItems(row) {
    const local = state && state.host.transport === 'local';
    const items = [];
    const link = dbSafeLink(row.link);
    if (row.link) {
      items.push({ label: 'Open link in browser', disabled: !link,
        reason: link ? '' : 'Only http and https links can be opened.',
        onClick: () => window.open(link, '_blank', 'noopener') });
      items.push({ label: 'Copy link', onClick: () => dbCopyText(row.link, 'link') });
      items.push('-');
    }
    if (local) {
      items.push({ label: 'Open Folder', disabled: !row.folder,
        onClick: () => dbReveal(row.folder, 'folder') });
    }
    items.push({ label: 'Copy Folder Path', disabled: !row.folder,
      onClick: () => dbCopyText(row.folder, 'folder path') });
    return items;
  }
  function dbRenderWatchlist() {
    const st = dbState.watchlist;
    const tbody = $('#db-wl-tbody');
    tbody.innerHTML = '';
    const colset = st.cols;
    st.rows.forEach((row) => {
      const tr = document.createElement('tr');
      colset.order.forEach((id) => {
        const def = colset.byId[id];
        const td = document.createElement('td');
        td.style.textAlign = def.align === 'e' ? 'right' : def.align === 'center' ? 'center' : 'left';
        if (id === 'sel') {
          const box = document.createElement('input');
          box.type = 'checkbox'; box.className = 'cb-cbx';
          box.checked = !!st.checked[row.id];
          if (!row.eligible) setDisabled(box, true, { reason: row.ineligible_reason });
          else box.addEventListener('change', () => { st.checked[row.id] = box.checked; });
          td.appendChild(box);
        } else if (id === 'channel') {
          td.style.fontWeight = '500'; td.textContent = row.channel;
        } else if (id === 'link') {
          const safe = dbSafeLink(row.link);
          if (safe) {
            const a = document.createElement('a');
            a.href = safe; a.target = '_blank'; a.rel = 'noopener';
            a.className = 'cb-mono'; a.style.fontSize = '11.5px'; a.textContent = safe;
            td.appendChild(a);
          } else if (row.link) {
            const span = document.createElement('span');
            span.className = 'cb-mono cb-mut';
            span.style.fontSize = '11.5px';
            span.textContent = row.link;
            td.appendChild(span);
          } else if (row.link_unresolved) {
            const span = document.createElement('span');
            span.className = 'cb-mono';
            span.style.cssText = 'font-size:11.5px;color:var(--cb-warn)';
            span.textContent = 'unresolved — folder name only';
            td.appendChild(span);
          }
        } else if (id === 'folder') {
          td.className = 'cb-mut cb-mono'; td.style.fontSize = '11.5px';
          td.textContent = row.folder || '—';
        } else if (id === 'platform' || id === 'genre') {
          td.className = 'cb-mut'; td.textContent = row[id] || '';
        } else if (id === 'last_scan') {
          td.className = 'cb-mut cb-mono'; td.style.fontSize = '11.5px';
          td.textContent = fmtWhen(row.last_scan);
        } else if (id === 'pending') {
          td.className = row.pending ? 'cb-mono' : 'cb-mono cb-mut';
          if (row.pending) { td.style.color = 'var(--cb-line)'; td.style.fontWeight = '500'; }
          td.textContent = String(row.pending);
        } else if (id === 'total') {
          td.className = 'cb-mono cb-mut'; td.textContent = String(row.total);
        } else if (id === 'status') {
          if (row.status === 'downloading') {
            const tag = document.createElement('span');
            tag.className = 'cb-tag cb-tag--fill'; tag.textContent = 'downloading';
            td.appendChild(tag);
          } else if (row.link_unresolved) {
            const tag = document.createElement('span');
            tag.className = 'cb-tag cb-tag--attn'; tag.textContent = 'needs link';
            td.appendChild(tag);
          } else {
            td.className = 'cb-mut'; td.textContent = row.status;
          }
        }
        tr.appendChild(td);
      });
      tr.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        dbShowMenu(e.clientX, e.clientY, dbWatchlistMenuItems(row));
      });
      tbody.appendChild(tr);
    });
    if (st.offset < st.total) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = colset.order.length;
      const remaining = st.total - st.offset;
      td.style.cssText = 'padding-left:14px;cursor:pointer;color:var(--cb-line);font-size:12px';
      td.textContent = `Load ${Math.min(DB_PAGE_SIZE, remaining)} more of ${remaining} remaining…`;
      td.addEventListener('click', dbWatchlistLoadMore);
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
    const bar = $('#db-wl-statbar');
    bar.innerHTML = '';
    const pending = st.rows.reduce((a, r) => a + (r.pending || 0), 0);
    const ticked = Object.values(st.checked).filter(Boolean).length;
    /* pending is summed over the LOADED rows only, so it carries the same
       "(N loaded)" qualifier as the channel count — an unqualified number
       next to a full total would read as a figure for the whole list. */
    const partial = st.rows.length < st.total;
    [`${num(st.total)} channel${st.total === 1 ? '' : 's'}` +
       (partial ? ` (${num(st.rows.length)} loaded)` : ''),
     `${num(pending)} pending new` + (partial ? ' (in loaded rows)' : ''),
     `${ticked} ticked for cleanup`].forEach((t) => {
      const span = document.createElement('span'); span.textContent = t; bar.appendChild(span);
    });
    bindTips(tbody);
  }
  function dbWireWatchlistToolbar() {
    // 3h's toolbar has no search box (channel count is small enough that the
    // sortable columns are the whole navigation story) — matching the design
    // exactly rather than adding a control it doesn't have.
    const cleanup = $('#db-wl-cleanup');
    setDisabled(cleanup, true, {
      reason: (TOOLTIPS['db.folders_cleanup'] ? TOOLTIPS['db.folders_cleanup'] + '\n\n' : '') +
        'Not wired up yet — destructive actions need their own sign-off, out of scope for this task.',
    });
    $('#db-wl-refresh').addEventListener('click', dbWatchlistReload);
  }

  /* ── Artwork tab: paged table + preview pane ─────────────────────────────── */

  async function dbArtworkReload() {
    const st = dbState.artwork;
    try {
      const res = await call('db.query', {
        table: 'artwork', filters: { filter_name: st.filter, search: st.search },
        sort: { col: st.sortCol, desc: st.sortDesc }, offset: 0, limit: DB_PAGE_SIZE,
      });
      st.rows = res.rows; st.total = res.total; st.offset = res.rows.length; st.loaded = true;
    } catch (_) { st.rows = []; st.total = 0; st.offset = 0; }
    st.selected = null;
    dbArtworkClearPreview();
    dbRenderArtwork();
  }
  async function dbArtworkLoadMore() {
    const st = dbState.artwork;
    /* want_total:false — "Sidecar missing on disk" counts by statting every
       candidate in the library, so re-asking on every page turn would rescan
       it each time for a number the first page already gave us. */
    try {
      const res = await call('db.query', {
        table: 'artwork', filters: { filter_name: st.filter, search: st.search },
        sort: { col: st.sortCol, desc: st.sortDesc }, offset: st.offset,
        limit: DB_PAGE_SIZE, want_total: false,
      });
      st.rows = st.rows.concat(res.rows); st.offset += res.rows.length;
    } catch (_) { /* call() already toasted the reason */ }
    dbRenderArtwork();
  }
  function dbRenderArtworkHeaders() {
    dbWireHeaders($('#db-art-thead'), dbState.artwork.cols, {
      sortCol: dbState.artwork.sortCol, sortDesc: dbState.artwork.sortDesc,
      onSort: (id) => {
        const st = dbState.artwork;
        if (st.sortCol === id) st.sortDesc = !st.sortDesc; else { st.sortCol = id; st.sortDesc = false; }
        dbArtworkReload();
      },
      onRender: () => { dbRenderArtworkHeaders(); dbRenderArtwork(); },
    });
  }
  function dbArtworkMenuItems(row) {
    const local = state && state.host.transport === 'local';
    const items = [];
    if (local) {
      items.push({ label: 'Open Image', disabled: !row.sidecar_path,
        onClick: () => dbReveal(row.sidecar_path, 'open') });
      items.push({ label: 'Open Containing Folder', disabled: !(row.sidecar_path || row.file_path),
        onClick: () => dbReveal(row.sidecar_path || row.file_path, 'folder') });
    }
    items.push({ label: 'Copy Image Path', disabled: !row.sidecar_path,
      onClick: () => dbCopyText(row.sidecar_path, 'image path') });
    items.push({ label: 'Copy Thumbnail URL', disabled: !row.thumb_url,
      onClick: () => dbCopyText(row.thumb_url, 'thumbnail URL') });
    return items;
  }
  function dbArtworkClearPreview(message) {
    const box = $('#db-art-preview-box');
    box.innerHTML = '';
    box.textContent = message || 'Select a track';
    $('#db-art-preview-meta').innerHTML = '';
  }
  function dbMetaRow(label, value, color) {
    const row = document.createElement('div');
    row.className = 'cb-row'; row.style.gap = '8px';
    const l = document.createElement('span'); l.className = 'cb-mut'; l.textContent = label;
    const v = document.createElement('span'); v.className = 'cb-mono';
    v.style.cssText = 'margin-left:auto;font-size:11px' + (color ? `;color:${color}` : '');
    v.textContent = value;
    row.append(l, v);
    return row;
  }
  async function dbArtworkSelectRow(row) {
    dbState.artwork.selected = row;
    $$('#db-art-tbody tr').forEach((tr) => tr.classList.toggle('is-selected', tr.dataset.id === String(row.id)));
    const box = $('#db-art-preview-box');
    box.textContent = 'Loading…';
    try {
      /* Keyed by row id — the host looks the sidecar/MP3 paths up itself, so
         this call can never name a file the library doesn't own. */
      const res = await call('db.artwork_preview', { id: row.id });
      box.innerHTML = '';
      if (res.data_url) {
        const img = document.createElement('img');
        img.src = res.data_url;
        img.style.cssText = 'max-width:100%;max-height:100%;border-radius:6px;display:block;margin:auto';
        box.appendChild(img);
      } else {
        box.textContent = res.note ||
          (row.sidecar_path ? 'Sidecar file is gone' : 'No artwork');
      }
      const meta = $('#db-art-preview-meta');
      meta.innerHTML = '';
      const dims = res.width && res.height ? `${res.width} × ${res.height}` : '';
      const kb = res.size ? `${Math.max(1, Math.round(res.size / 1024))} KB` : '';
      if (dims || kb) {
        const cap = document.createElement('div');
        cap.className = 'cb-mut cb-mono'; cap.style.fontSize = '10.5px';
        cap.textContent = [dims, kb].filter(Boolean).join(' · ');
        meta.appendChild(cap);
      }
      meta.appendChild(dbMetaRow('Embedded', row.embedded ? 'APIC present' : 'not embedded',
        row.embedded ? 'var(--cb-ok)' : ''));
      meta.appendChild(dbMetaRow('Sidecar', row.sidecar || '—'));
      meta.appendChild(dbMetaRow('Source', row.thumb_url || '—'));
    } catch (_) {
      box.innerHTML = ''; box.textContent = 'Could not load preview.';
    }
  }
  function dbRenderArtwork() {
    const st = dbState.artwork;
    const tbody = $('#db-art-tbody');
    tbody.innerHTML = '';
    const colset = st.cols;
    st.rows.forEach((row) => {
      const tr = document.createElement('tr');
      tr.dataset.id = String(row.id);
      colset.order.forEach((id) => {
        const def = colset.byId[id];
        const td = document.createElement('td');
        td.style.textAlign = def.align === 'e' ? 'right' : def.align === 'center' ? 'center' : 'left';
        if (id === 'title') { td.style.fontWeight = '500'; td.textContent = row.title; }
        else if (id === 'channel' || id === 'platform') { td.className = 'cb-mut'; td.textContent = row[id] || ''; }
        else if (id === 'embedded') {
          td.style.color = row.embedded ? 'var(--cb-ok)' : 'var(--cb-err)';
          td.textContent = row.embedded ? '✓' : '✗';
        } else if (id === 'sidecar') {
          td.className = 'cb-mut cb-mono'; td.style.fontSize = '11px'; td.textContent = row.sidecar || '—';
        } else if (id === 'on_disk') {
          if (row.on_disk === true) { td.style.color = 'var(--cb-ok)'; td.textContent = '✓'; }
          else if (row.on_disk === false) { td.style.color = 'var(--cb-warn)'; td.textContent = 'missing'; }
          else { td.className = 'cb-mut'; td.textContent = '—'; }
        } else if (id === 'thumb_url') {
          td.className = 'cb-mut cb-mono'; td.style.fontSize = '11px'; td.textContent = row.thumb_url || '—';
        }
        tr.appendChild(td);
      });
      tr.addEventListener('click', () => dbArtworkSelectRow(row));
      tr.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        dbShowMenu(e.clientX, e.clientY, dbArtworkMenuItems(row));
      });
      tbody.appendChild(tr);
    });
    if (st.offset < st.total) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = colset.order.length;
      const remaining = st.total - st.offset;
      td.style.cssText = 'padding-left:14px;cursor:pointer;color:var(--cb-line);font-size:12px';
      td.textContent = `Load ${Math.min(DB_PAGE_SIZE, remaining)} more of ${remaining} remaining…`;
      td.addEventListener('click', dbArtworkLoadMore);
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
    const bar = $('#db-art-statbar');
    bar.innerHTML = '';
    const span = document.createElement('span');
    span.textContent = `${num(st.total)} track${st.total === 1 ? '' : 's'} matching filter ` +
                       `(${num(st.rows.length)} loaded)`;
    bar.appendChild(span);
  }
  function dbWireArtworkToolbar() {
    const filterSel = $('#db-art-filter');
    DB_ARTWORK_FILTERS.forEach((f) => {
      const o = document.createElement('option'); o.textContent = f; filterSel.appendChild(o);
    });
    filterSel.value = dbState.artwork.filter;
    filterSel.addEventListener('change', () => {
      dbState.artwork.filter = filterSel.value; dbArtworkReload();
    });
    const searchEl = $('#db-art-search');
    let t = null;
    searchEl.addEventListener('input', () => {
      clearTimeout(t);
      t = setTimeout(() => { dbState.artwork.search = searchEl.value.trim(); dbArtworkReload(); }, 250);
    });
    $('#db-art-refresh').addEventListener('click', dbArtworkReload);
    // 3i's toolbar has no Export CSV button (matching the design) — the
    // service's db.export_csv still accepts table:"artwork" for a future
    // screen that wants it.
    setDisabled($('#db-art-fetch'), true, {
      reason: (TOOLTIPS['settings.fetch_artwork'] ? TOOLTIPS['settings.fetch_artwork'] + '\n\n' : '') +
        "Not wired up yet — maintenance jobs arrive with the web frontend's job runner.",
    });
    setDisabled($('#db-art-reembed'), true, {
      reason: (TOOLTIPS['db.reembed_artwork'] ? TOOLTIPS['db.reembed_artwork'] + '\n\n' : '') +
        "Not wired up yet — maintenance jobs arrive with the web frontend's job runner.",
    });
    $('#db-art-copy-thumb').addEventListener('click', () => {
      const row = dbState.artwork.selected;
      if (!row) { toast('Select a track first.', true); return; }
      dbCopyText(row.thumb_url, 'thumbnail URL');
    });
  }

  /* ── tabs + open/close ─────────────────────────────────────────────────── */
  const DB_TABS = ['downloads', 'watchlist', 'artwork'];
  const DB_HELP_KEY = { downloads: 'db.help_downloads', watchlist: 'db.help_watchlist',
                        artwork: 'db.help_artwork' };
  function dbSwitchTab(tab) {
    dbState.activeTab = tab;
    DB_TABS.forEach((t) => {
      $('#db-tab-' + t).classList.toggle('is-on', t === tab);
      $('#db-tab-btn-' + t).classList.toggle('cb-btn--quiet', t !== tab);
    });
    $('#db-help').setAttribute('data-tt', DB_HELP_KEY[tab]);
    if (tab === 'downloads' && !dbState.downloads.root) dbDownloadsReload();
    else if (tab === 'watchlist' && !dbState.watchlist.loaded) dbWatchlistReload();
    else if (tab === 'artwork' && !dbState.artwork.loaded) dbArtworkReload();
  }
  let dbWired = false;
  function dbInitOnce() {
    if (dbWired) return;
    dbWired = true;
    dbState.downloads.cols = makeColumnSet(DL_COLUMNS, 'db_dl_col_widths', 'db_dl_col_order');
    dbState.watchlist.cols = makeColumnSet(WL_COLUMNS, 'db_wl_col_widths', 'db_wl_col_order');
    dbState.artwork.cols = makeColumnSet(ART_COLUMNS, 'db_art_col_widths', 'db_art_col_order');
    dbRenderDownloadsHeaders();
    dbRenderWatchlistHeaders();
    dbRenderArtworkHeaders();
    dbWireDownloadsToolbar();
    dbWireWatchlistToolbar();
    dbWireArtworkToolbar();
    DB_TABS.forEach((t) => {
      $('#db-tab-btn-' + t).addEventListener('click', () => dbSwitchTab(t));
    });
    bindTips($('#screen-database'));
  }
  function dbOpen() {
    dbInitOnce();
    dbSwitchTab(dbState.activeTab);
  }

  /* ── settings ──────────────────────────────────────────────────────────── */
  const NOT_AVAILABLE_REASON = 'This option is not wired into the web frontend yet — ' +
                               'change it in the desktop app for now.';

  function control(entry, value, available) {
    const wrap = document.createElement('div');

    function mark(el) {
      el.dataset.key = entry.key;
      el.dataset.origTt = entry.tooltip || '';
      if (!available) {
        setDisabled(el, true,
          { reason: tipPlus(entry.tooltip, NOT_AVAILABLE_REASON) });
      } else if (entry.tooltip && TOOLTIPS[entry.tooltip]) {
        el.setAttribute('data-tt', entry.tooltip);
      }
    }

    if (entry.type === 'bool') {
      const label = document.createElement('label');
      label.className = 'cb-row';
      label.style.cssText = 'gap:8px;cursor:pointer';
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.className = 'cb-cbx';
      box.checked = !!value;
      mark(box);
      box.addEventListener('change', () => save(entry.key, box.checked, box));
      const text = document.createElement('span');
      text.className = 'cb-lab';
      text.textContent = entry.label;
      label.append(box, text);
      wrap.appendChild(label);
      return wrap;
    }

    if (entry.key === 'limit_minutes') {
      wrap.appendChild(limiterRow(entry, value, available, mark));
      return wrap;
    }

    const row = document.createElement('div');
    row.className = 'cb-set-row';
    const lab = document.createElement('span');
    lab.className = 'cb-lab';
    lab.textContent = entry.label;
    row.appendChild(lab);

    let input;
    if (entry.type === 'enum') {
      input = document.createElement('select');
      input.className = 'cb-sel';
      const options = (entry.options || []).slice();
      /* The contract's option strings do not always match what the host has
         stored ("192" vs "192 kbps"). Show the real value rather than an empty
         select — silently blanking it would misreport the host's state. */
      const current = value === undefined || value === null ? '' : String(value);
      if (current && !options.includes(current)) options.unshift(current);
      options.forEach((o) => {
        const opt = document.createElement('option');
        opt.value = o;
        opt.textContent = o;
        input.appendChild(opt);
      });
      if (current) input.value = current;
    } else if (entry.type === 'int') {
      input = document.createElement('input');
      input.className = 'cb-in cb-mono';
      input.type = 'number';
      input.style.width = '92px';
      if (entry.min !== undefined) input.min = entry.min;
      if (entry.max !== undefined) input.max = entry.max;
      input.value = value !== undefined && value !== null ? value : (entry.default ?? 0);
    } else {
      input = document.createElement('input');
      input.className = 'cb-in cb-mono';
      input.style.fontSize = '12px';
      input.value = value !== undefined && value !== null ? value : '';
    }
    mark(input);
    input.addEventListener('change', () => {
      const v = entry.type === 'int' ? Number(input.value) : input.value;
      save(entry.key, v, input);
    });
    row.appendChild(input);

    if (entry.unit) {
      const u = document.createElement('span');
      u.className = 'cb-mut cb-mono';
      u.style.fontSize = '11.5px';
      u.textContent = entry.unit;
      row.appendChild(u);
    }
    if (entry.tooltip && TOOLTIPS[entry.tooltip]) {
      const help = document.createElement('span');
      help.className = 'cb-help cb-tt-host';
      help.textContent = '?';
      help.tabIndex = 0;
      help.setAttribute('data-tt', entry.tooltip);
      row.appendChild(help);
    }
    wrap.appendChild(row);
    return wrap;
  }

  /* Design 3j draws the Max Length control as a slider with −/+ steppers and
     a mono readout rather than a bare number field. */
  function limiterRow(entry, value, available, mark) {
    const row = document.createElement('div');
    row.className = 'cb-row';
    row.style.gap = '9px';

    const lab = document.createElement('span');
    lab.className = 'cb-lab';
    lab.style.fontWeight = '500';
    lab.textContent = entry.label + ':';

    const minus = document.createElement('button');
    minus.className = 'cb-btn cb-btn--quiet cb-btn--sm';
    minus.style.padding = '2px 8px';
    minus.textContent = '−';
    minus.dataset.key = 'limit_minutes__minus';

    const slider = document.createElement('input');
    slider.type = 'range';
    slider.className = 'cb-slider';
    slider.style.flex = '1';
    if (entry.min !== undefined) slider.min = entry.min;
    if (entry.max !== undefined) slider.max = entry.max;
    mark(slider);

    const plus = document.createElement('button');
    plus.className = 'cb-btn cb-btn--quiet cb-btn--sm';
    plus.style.padding = '2px 8px';
    plus.textContent = '+';
    plus.dataset.key = 'limit_minutes__plus';

    const readout = document.createElement('span');
    readout.className = 'cb-mono';
    readout.style.cssText = 'font-size:14px;font-weight:500;color:var(--cb-text);width:64px';

    const current = value !== undefined && value !== null ? Number(value) : Number(entry.default ?? 0);
    slider.value = current;
    readout.textContent = `${current} ${entry.unit || 'min'}`;

    function setReadout(v) { readout.textContent = `${v} ${entry.unit || 'min'}`; }
    function commit(v) {
      const min = entry.min !== undefined ? entry.min : v;
      const max = entry.max !== undefined ? entry.max : v;
      v = Math.max(min, Math.min(max, v));
      slider.value = v;
      setReadout(v);
      save(entry.key, v, slider);
    }
    slider.addEventListener('input', () => setReadout(Number(slider.value)));
    slider.addEventListener('change', () => commit(Number(slider.value)));

    if (available) {
      minus.addEventListener('click', () => commit(Number(slider.value) - 1));
      plus.addEventListener('click', () => commit(Number(slider.value) + 1));
    } else {
      setDisabled(minus, true, { reason: NOT_AVAILABLE_REASON });
      setDisabled(plus, true, { reason: NOT_AVAILABLE_REASON });
    }

    row.append(lab, minus, slider, plus, readout);
    return row;
  }

  /* ── settings: cross-field dependencies (tkinter's _on_sleep_toggle /
     _on_cookies_toggle, and limit_enabled greying the limiter row) ──────────
     Re-run after every successful save() so a dependency reacts the moment
     the host confirms the value that drives it — never optimistically. */
  function applySettingsDependencies() {
    const grid = $('#settings-grid');
    if (!grid || !state) return;
    const val = (key) => state.settings[key];
    const el = (key) => grid.querySelector(`[data-key="${key}"]`);
    const set = (key, disabled, reason) => {
      const e = el(key);
      if (!e) return;
      if (disabled) setDisabled(e, true, { reason });
      else setDisabled(e, false, e.dataset.origTt ? { ttKey: e.dataset.origTt } : {});
    };

    // Time / Length Limiter
    const limiterOn = !!val('limit_enabled');
    const limiterReason = 'Enable the limiter first.';
    set('limit_minutes', !limiterOn, limiterReason);
    [el('limit_minutes__minus'), el('limit_minutes__plus')].forEach((b) => {
      if (b) setDisabled(b, !limiterOn, { reason: limiterReason });
    });

    // Download Behavior: Throttle Requests
    const throttleOn = !!val('sleep_enabled');
    const throttleReason = 'Turn on Throttle Requests first.';
    set('sleep_mode', !throttleOn, throttleReason);
    set('sleep_preset', !throttleOn, throttleReason);
    const manual = val('sleep_mode') === 'Manual';
    const manualReason = throttleOn ? 'Enabled with Manual mode.' : throttleReason;
    set('sleep_min', !throttleOn || !manual, manualReason);
    set('sleep_max', !throttleOn || !manual, manualReason);

    // Browser Cookies
    const cookiesOn = !!val('use_cookies');
    const cookiesReason = 'Turn on Use Browser Cookies first.';
    set('cookie_method', !cookiesOn, cookiesReason);
    const isFileMethod = val('cookie_method') === 'Cookie File';
    set('cookies_browser', !cookiesOn || isFileMethod,
      cookiesOn ? 'Switch Method to Browser Profile first.' : cookiesReason);
    set('cookies_profile', !cookiesOn || isFileMethod,
      cookiesOn ? 'Switch Method to Browser Profile first.' : cookiesReason);
    set('cookie_file', !cookiesOn || !isFileMethod,
      cookiesOn ? 'Switch Method to Cookie File first.' : cookiesReason);

    // Run App on Startup writes the host's own registry — local window only.
    const remoteMount = state.host.transport !== 'local';
    set('run_at_startup', remoteMount,
      'Run App on Startup can only be changed from the app window on the host machine.');

    /* The save directory is the boundary a remote session is contained by
       (the host measures fs.reveal against it), so a remote browser must not
       be able to move it. The host refuses the write either way; this is the
       control saying so before the click. */
    set('base_dir', remoteMount,
      'The save directory is the boundary that keeps a remote session inside ' +
      'your crate folder, so it can only be changed from the app window on ' +
      'the host machine.');

    /* Remote Access is read-only on a remote mount (3j): a browser that has
       been let in must not be able to widen the door it came through. */
    REMOTE_SETTING_KEYS.forEach((key) => set(key, remoteMount,
      'Remote access settings can only be changed from the app window on the ' +
      'host machine.'));

    /* A read-only session, or one without the control lock, changes nothing
       at all — the host refuses every settings.set. */
    const blocked = writeBlocked();
    if (blocked) {
      /* The Downloads screen carries two settings controls of its own (3b's
         Skip row), so the sweep is by data-key rather than by container —
         a setting is a write wherever it happens to be drawn. */
      $$('input,select,button', grid)
        .concat($$('#screen-downloads [data-key]'))
        .forEach((el) => {
          if (el.dataset.readOk) return;
          setDisabled(el, true, { reason: blocked });
        });
    }

    bindTips(grid);
    bindTips($('#screen-downloads'));
  }

  async function save(key, value, el) {
    try {
      const res = await cbApi.call('settings.set', { key, value });
      state.settings[key] = res.value;
      toast(`Saved ${key}`);
      applySettingsDependencies();
    } catch (err) {
      toast(err.userFacing ? err.message : `Could not save ${key}`, true);
      if (el && el.type === 'checkbox') el.checked = !el.checked;
    }
  }

  /* Controls the design draws on screen 3j that have no schema key at all —
     they navigate to, or drive, a screen a later task builds. Rendered
     visibly (the layout stays complete) but disabled with the reason, per
     the same rule that governs every other not-yet-wired control. */
  function stubButton(label, cls, ttKey, reason) {
    const b = document.createElement('button');
    b.className = `cb-btn cb-btn--sm ${cls || ''}`.trim();
    b.textContent = label;
    setDisabled(b, true,
      { reason: (ttKey && TOOLTIPS[ttKey] ? TOOLTIPS[ttKey] + '\n\n' : '') + reason });
    return b;
  }

  /* ── database maintenance (3m long-job shell) ─────────────────────────────
     The four Settings ▸ Downloads Database actions. Each is a confirm modal
     quoting the registry tooltip and the count the host just measured, then
     one long-job dialog — the design's 3m shell — driven entirely by the
     host's own events: `progress.overall`/`progress.current` stamped
     job:"maintenance" while it runs, and `job.finished` to settle it. The
     jobs' own summaries arrive as `notification`, never read as a state
     signal. */

  const mt = {
    running: false,   // a maintenance job holds the host's slot
    task: null,       // which one, while we know
    overall: null,    // last progress.overall stamped job:"maintenance"
    current: null,    // last progress.current stamped job:"maintenance"
    note: null,       // the run's closing notification, once it lands
    view: null,       // the open long-job dialog's handles, or null
  };

  /* Everything that differs between the four jobs, in one table: the button,
     the confirm copy (registry tooltip + the monolith's own consequence
     wording, with the host's counts substituted), what the bar counts, and
     whether the run can skip an item. */
  const MAINT_TASKS = {
    'db.rebuild': {
      label: '🔄 Rebuild Database from Files',
      title: '🔄 Rebuild Database',
      tt: 'settings.rebuild_db',
      run: 'Rebuild Database',
      unit: 'channel folder',
      tally: (o) => (o && o.found != null ? `${num(o.found)} found` : ''),
      confirm: (p) => [
        `The downloads table is cleared and rebuilt from the audio files in ` +
        `your library folders. The ${num(p.rows || 0)} row` +
        `${p.rows === 1 ? '' : 's'} it holds now ` +
        `${p.rows === 1 ? 'is' : 'are'} replaced by what is actually on disk.`,
        'Cover art already on disk is reused, never re-downloaded. Your audio ' +
        'files are not touched. This cannot be undone.',
      ],
    },
    'db.dedupe': {
      label: '🧹 Remove Duplicates',
      title: '🧹 Remove Duplicates',
      tt: 'settings.dedupe_db',
      run: 'Remove Duplicates',
      unit: 'step',
      tally: () => '',
      confirm: (p) => [
        `${num(p.extra)} redundant row${p.extra === 1 ? '' : 's'} across ` +
        `${num(p.files)} file${p.files === 1 ? '' : 's'} will be merged down ` +
        'to one row each.',
        'Your audio files, cover art and Watch List are not touched — only ' +
        'the duplicate database rows. Anything the removed rows knew (video ' +
        'id, upload date, cover art) is kept on the row that remains. This ' +
        'cannot be undone.',
      ],
    },
    'db.repair_tags': {
      label: '🏷 Repair Track Tags',
      title: '🏷 Repair Track Tags',
      tt: 'settings.repair_tags',
      run: 'Repair Tags',
      unit: 'track',
      tally: (o) => (o && o.genres != null
        ? `${num(o.genres)} genres • ${num(o.filled || 0)} filled in` : ''),
      confirm: (p) => [
        `Repair the tags on ${num(p.total)} track${p.total === 1 ? '' : 's'}?`,
        `Genre is set to match the folder each track is filed under — a genre ` +
        `tag you set by hand that disagrees with the folder is overwritten, ` +
        `and tracks under '${p.no_genre_dir || '_No Genre'}' have theirs ` +
        `cleared.`,
        'Title, Encoded-by and the source URL are only filled in where a ' +
        'track is missing them, so anything you edited by hand is left alone. ' +
        'Audio is never re-encoded — only the tag is rewritten.',
      ],
    },
    'db.fetch_artwork': {
      label: '🖼 Fetch Missing Artwork',
      title: '🖼 Fetch Missing Artwork',
      tt: 'settings.fetch_artwork',
      run: 'Fetch Artwork',
      unit: 'track',
      skip: true,
      tally: (o) => (o && o.embedded != null ? `${num(o.embedded)} embedded` : ''),
      confirm: (p) => [
        `${num(p.total)} track${p.total === 1 ? '' : 's'} ` +
        `${p.total === 1 ? 'has' : 'have'} no cover art.`,
        'Artwork will be downloaded where available and embedded into each ' +
        'file. This can take a while for a large library, and you can cancel ' +
        'at any point — tracks already done are kept.',
      ],
    },
  };

  const MAINT_BUSY_REASON =
    'A database maintenance job is running. Only one runs at a time — wait ' +
    'for it to finish, or cancel it from its progress window.';
  /* The Cancel copy is the design's own (artboard 3m) and lives in the
     registry as `settings.maintenance_cancel`; the footer reads it by key.
     Skip has no design copy at all — 3m draws no Skip button — so its text is
     inline, per the same rule that governs every other control the contract
     has nothing to say about. */
  const MAINT_SKIP_TT =
    'Give up on the track being fetched right now and move straight on to ' +
    'the next one. Only this track is skipped.';

  /* Step one: ask the host what this job would do, and show it before
     anything runs. A refusal (nothing to de-dup, cover art switched off, no
     audio under the crate root) comes back from the same call and is the
     reason the user sees instead of a dialog. */
  async function maintConfirm(task) {
    const spec = MAINT_TASKS[task];
    let preview;
    try {
      preview = await call('db.maintenance_preview', { task });
    } catch (_) { return; }   // call() already toasted the reason
    openModal({
      title: spec.title,
      width: 520,
      body(body) {
        spec.confirm(preview).forEach((line) => body.appendChild(modalNote(line)));
        const help = modalNote(TOOLTIPS[spec.tt] || '');
        help.style.borderTop = '1px solid var(--cb-line-soft)';
        help.style.paddingTop = '10px';
        help.style.fontSize = '11.5px';
        body.appendChild(help);
      },
      foot(foot, api) {
        const go = modalButton(spec.run, 'cb-btn--warn', async () => {
          api.busy(true);
          try {
            await call(task);
          } catch (_) {
            api.busy(false);
            return;             // call() already toasted the reason
          }
          maintBegin(task);
        }, spec.tt);
        const later = modalButton('Not now', 'cb-btn--quiet', api.close);
        later.style.marginLeft = 'auto';
        foot.append(go, later);
      },
    });
  }

  /* Step two: the run is ours until the host says the slot is free. */
  function maintBegin(task) {
    mt.running = true;
    mt.task = task;
    mt.overall = null;
    mt.current = null;
    mt.note = null;
    maintOpenProgress(task);
    renderSettings();
  }

  function maintOpenProgress(task) {
    const spec = MAINT_TASKS[task];
    const refs = {};
    openModal({
      title: spec.title,
      tag: { text: 'Running', cls: 'cb-tag--fill' },
      width: 520,
      onClose() { mt.view = null; },
      body(body) {
        const line = document.createElement('div');
        line.className = 'cb-row';
        line.style.cssText = 'gap:8px;align-items:baseline';
        refs.kick = document.createElement('span');
        refs.kick.className = 'cb-kick';
        refs.kick.textContent = 'Current';
        const kick = refs.kick;
        refs.item = document.createElement('span');
        refs.item.className = 'cb-maint__item';
        refs.item.textContent = 'Starting…';
        line.append(kick, refs.item);

        const bar = document.createElement('div');
        bar.className = 'cb-bar';
        refs.fill = document.createElement('div');
        refs.fill.className = 'cb-bar__fill';
        refs.fill.style.width = '0%';
        bar.appendChild(refs.fill);

        const counts = document.createElement('div');
        counts.className = 'cb-row';
        counts.style.gap = '8px';
        refs.counts = document.createElement('span');
        refs.counts.className = 'cb-mut cb-mono cb-maint__counts';
        refs.tally = document.createElement('span');
        refs.tally.className = 'cb-mono cb-maint__tally';
        counts.append(refs.counts, refs.tally);

        const wrap = document.createElement('div');
        wrap.append(line, bar);
        body.append(wrap, counts);
      },
      foot(foot, api) {
        refs.note = modalNote('Closing this window leaves the job running.');
        foot.appendChild(refs.note);
        if (spec.skip) {
          refs.skip = modalButton('Skip track', 'cb-btn--quiet',
            () => call('db.maintenance_skip').catch(() => {}));
          refs.skip.setAttribute('data-tt-text', MAINT_SKIP_TT);
          refs.skip.style.marginLeft = 'auto';
          foot.appendChild(refs.skip);
        }
        refs.cancel = modalButton('Cancel', 'cb-btn--warn',
          () => call('db.maintenance_cancel').catch(() => {}),
          'settings.maintenance_cancel');
        if (!spec.skip) refs.cancel.style.marginLeft = 'auto';
        refs.close = modalButton('Close', 'cb-btn--quiet', api.close);
        refs.close.hidden = true;
        foot.append(refs.cancel, refs.close);
      },
    });
    mt.view = { task, refs, modal: $('.cb-modal') };
    maintPaint();
  }

  function maintPaint() {
    if (!mt.view) return;
    const spec = MAINT_TASKS[mt.view.task] || {};
    const { refs } = mt.view;
    const o = mt.overall;
    const percent = o && o.percent != null ? Math.max(0, Math.min(100, o.percent)) : 0;
    refs.fill.style.width = percent + '%';
    if (mt.note) {
      refs.item.textContent = mt.note.body || 'Finished.';
    } else if (mt.current && mt.current.title) {
      refs.item.textContent = mt.current.title +
        (mt.current.note ? ` — ${mt.current.note}` : '');
    }
    const unit = spec.unit || 'item';
    refs.counts.textContent = o
      ? `${num(o.done)} of ${num(o.total)} ${unit}${o.total === 1 ? '' : 's'}`
      : 'Starting…';
    refs.tally.textContent = (spec.tally && spec.tally(o)) || '';
  }

  /* The host released the slot. Settle the dialog in place rather than
     yanking it away mid-read: the outcome takes over the head tag and the
     current-item line, and the only control left is Close.

     Three outcomes, and which one is NEVER guessed from the summary's
     wording. `ok` comes from job.finished — false means the run raised, and
     the run then has no summary of its own, so completing the bar and
     painting it green would be reporting a success that did not happen.
     `cancelled` comes from the run's own notification, where the run computed
     it. Everything else is a finish. */
  function maintSettle(payload) {
    mt.running = false;
    mt.task = null;
    if (!mt.view) return;
    const { refs, modal } = mt.view;
    const failed = !!(payload && payload.ok === false);
    const cancelled = !failed && !!(mt.note && mt.note.cancelled);
    const tag = modal && modal.querySelector('.cb-tag');
    if (tag) {
      tag.textContent = failed ? 'Failed' : (cancelled ? 'Cancelled' : 'Finished');
      tag.className = 'cb-tag ' +
        (failed ? 'cb-tag--err' : (cancelled ? 'cb-tag--grey' : 'cb-tag--ok'));
    }
    // A failed run's bar is left exactly where it stopped — how far it got is
    // the one useful thing left on it, and filling it would read as done.
    if (!failed) refs.fill.style.width = '100%';
    // The line stops being a track name and becomes the run's outcome: it has
    // to wrap instead of truncating, and "Current" no longer describes it.
    refs.kick.textContent = failed ? 'Failed'
      : (cancelled ? 'Stopped' : 'Result');
    refs.item.classList.add('is-summary');
    if (failed) {
      refs.item.classList.add('is-failed');
      refs.item.textContent = (payload && payload.error) ||
        'The job stopped without reporting why.';
    }
    if (refs.skip) refs.skip.hidden = true;
    refs.cancel.hidden = true;
    refs.close.hidden = false;
    refs.close.style.marginLeft = 'auto';
    refs.note.textContent = '';
    if (!failed) maintPaint();
    refs.close.focus();
  }

  /* The three contract keys the host keeps in cratebuilder_remote.json rather
     than the ordinary settings file — they govern who may reach this control
     surface, so they live beside the device tokens they gate. */
  const REMOTE_SETTING_KEYS = ['remote_enabled', 'remote_require_pairing',
                               'remote_read_only'];

  /* The Remote Access card's two live things — the code countdown's interval
     and its subscription — held outside the builder so a re-render can stop
     the previous card's before starting its own. */
  const remoteCard = { tick: null, off: null };

  /* Mark a control that reads rather than writes, so the read-only sweep in
     applySettingsDependencies leaves it alone. */
  function readOnlyOk(el) { el.dataset.readOk = '1'; return el; }

  /* Revoking is the one destructive action on 3j's Remote Access card — every
     paired device loses its token at once — so it goes through the same
     confirm modal shell as every other destructive action. */
  function openRevokeDevices(onDone) {
    openModal({
      title: 'Revoke all paired devices',
      width: 460,
      body(bodyEl) {
        bodyEl.appendChild(modalNote(
          'Every paired device loses its token immediately and has to pair ' +
          'again with a fresh code. Nothing on disk is touched — no download, ' +
          'no Watch List entry, no setting.'));
      },
      foot(footEl, api) {
        footEl.append(
          modalButton('Cancel', 'cb-btn--quiet', api.close),
          modalButton('Revoke all', 'cb-btn--warn', async () => {
            api.busy(true);
            try {
              await call('remote.revoke', { device_id: 'all' });
              api.close();
              toast('Every paired device was revoked.');
              if (onDone) onDone(await call('remote.config'));
            } catch (ex) {
              api.busy(false);
              api.error(ex.userFacing ? ex.message : 'Could not revoke.');
            }
          }, 'remote.revoke_all'));
      },
    });
  }

  const SECTION_EXTRAS = {
    'Default Save Directory': (card) => {
      const row = card.querySelector('[data-key="base_dir"]')?.closest('.cb-set-row');
      if (row) {
        const browse = document.createElement('button');
        browse.className = 'cb-btn cb-btn--quiet cb-btn--sm';
        browse.textContent = 'Browse…';
        if (state.host.transport === 'local') {
          browse.setAttribute('data-tt', 'remote.browse_folder');
          browse.addEventListener('click', async () => {
            try {
              const res = await call('fs.pick_folder', {});
              if (res && res.path) {
                const input = card.querySelector('[data-key="base_dir"]');
                input.value = res.path;
                await save('base_dir', res.path, input);
              }
            } catch (_) { /* call() already toasted the reason */ }
          });
        } else {
          setDisabled(browse, true,
            { reason: 'Folder browsing only works in the app window on the host ' +
                      'machine — type or paste the path instead.' });
        }
        row.appendChild(browse);
      }
      const hint = document.createElement('div');
      hint.className = 'cb-mut cb-mono';
      hint.style.fontSize = '11px';
      hint.textContent = 'YouTube/‹Genre›/‹Channel› -(Complete Catalog)-/Track.mp3';
      card.appendChild(hint);
    },

    'Logs': (card) => {
      const row = document.createElement('div');
      row.className = 'cb-row';
      row.style.cssText = 'gap:8px;flex-wrap:wrap';

      const activityBtn = document.createElement('button');
      activityBtn.className = 'cb-btn cb-btn--quiet cb-btn--sm';
      activityBtn.textContent = '📋 Activity Log';
      activityBtn.setAttribute('data-tt', 'settings.activity_log');
      activityBtn.addEventListener('click', () => show('activity-log'));

      const debugBtn = document.createElement('button');
      debugBtn.className = 'cb-btn cb-btn--quiet cb-btn--sm';
      debugBtn.textContent = '🔍 Debug Log';
      debugBtn.setAttribute('data-tt', 'settings.debug_log');
      debugBtn.addEventListener('click', () => show('debug-log'));

      const bothBtn = document.createElement('button');
      bothBtn.className = 'cb-btn cb-btn--quiet cb-btn--sm';
      bothBtn.textContent = '⤓ Download both';
      bothBtn.setAttribute('data-tt', 'settings.download_both_logs');
      bothBtn.addEventListener('click', async () => {
        await logDownload('activity');
        await logDownload('debug');
      });

      row.append(readOnlyOk(activityBtn), readOnlyOk(debugBtn), readOnlyOk(bothBtn));
      card.appendChild(row);
    },

    'Downloads Database': (card) => {
      const row = document.createElement('div');
      row.className = 'cb-row';
      row.style.cssText = 'gap:8px;flex-wrap:wrap';
      const openDb = document.createElement('button');
      openDb.className = 'cb-btn cb-btn--sm';
      openDb.textContent = '🗂 Open Database';
      openDb.addEventListener('click', () => show('database'));
      row.appendChild(readOnlyOk(openDb));
      Object.keys(MAINT_TASKS).forEach((task) => {
        const spec = MAINT_TASKS[task];
        const b = document.createElement('button');
        b.className = 'cb-btn cb-btn--warn cb-btn--sm';
        b.textContent = spec.label;
        b.addEventListener('click', () => maintConfirm(task));
        setDisabled(b, mt.running,
          { reason: MAINT_BUSY_REASON, ttKey: spec.tt });
        row.appendChild(b);
      });
      // While a job holds the slot the four ways in are shut, so this is the
      // only way back to a dialog the user closed — including after a page
      // reload, since the snapshot is what says a job is running.
      if (mt.running) {
        const back = document.createElement('button');
        back.className = 'cb-btn cb-btn--sm';
        back.textContent = '⏳ Show progress';
        back.addEventListener('click', () => {
          if (!mt.view && MAINT_TASKS[mt.task]) maintOpenProgress(mt.task);
        });
        setDisabled(back, !MAINT_TASKS[mt.task], {
          reason: 'The host has not said which job is running.',
          ttText: 'Reopen the progress window for the job running now.',
        });
        row.appendChild(back);
      }
      card.appendChild(row);
    },

    /* 3j's Remote Access card, below its six toggles: who is paired, the way
       to drop them, and the pairing code itself. Live on the local window,
       read-only-with-reason on a remote mount — a browser that has been let in
       must not be able to pair another one or revoke the host's own devices. */
    'Remote Access': (card) => {
      const local = state.host.transport === 'local';
      /* One card at a time: renderSettings rebuilds the grid, so the previous
         card's countdown and its subscription have to go with it. */
      if (remoteCard.tick) clearInterval(remoteCard.tick);
      if (remoteCard.off) remoteCard.off();
      remoteCard.tick = null;
      remoteCard.off = null;
      const remoteReason = 'Remote access settings can only be changed from ' +
        'the app window on the host machine.';

      const div = document.createElement('div');
      div.className = 'cb-div';
      card.appendChild(div);

      const row = document.createElement('div');
      row.className = 'cb-row';
      row.style.cssText = 'gap:9px;flex-wrap:wrap';
      const lab = document.createElement('span');
      lab.className = 'cb-lab';
      lab.textContent = 'Paired devices';
      const list = document.createElement('span');
      list.className = 'cb-mono cb-mut';
      list.id = 'remote-devices';
      list.style.fontSize = '11.5px';
      list.textContent = 'reading…';

      const revoke = document.createElement('button');
      revoke.className = 'cb-btn cb-btn--quiet cb-btn--sm';
      revoke.style.marginLeft = 'auto';
      revoke.textContent = 'Revoke all & re-pair';

      row.append(lab, list, revoke);
      card.appendChild(row);

      const pairRow = document.createElement('div');
      pairRow.className = 'cb-row';
      pairRow.style.cssText = 'gap:9px;flex-wrap:wrap';
      const pairBtn = document.createElement('button');
      pairBtn.className = 'cb-btn cb-btn--sm';
      pairBtn.textContent = '＋ Pair a device';
      pairBtn.setAttribute('data-tt-text',
        'Shows a 6-digit code here for five minutes. Enter it in the browser ' +
        'on the other device to give it a long-lived token. The code works ' +
        'once, and the host only ever stores a hash of the token it hands out.');
      const codeOut = document.createElement('span');
      codeOut.className = 'cb-mono';
      codeOut.id = 'remote-code';
      codeOut.style.cssText = 'font-size:22px;letter-spacing:.18em;color:var(--cb-accent)';
      const codeNote = document.createElement('span');
      codeNote.className = 'cb-mut cb-mono';
      codeNote.id = 'remote-code-note';
      codeNote.style.fontSize = '11px';
      pairRow.append(pairBtn, codeOut, codeNote);
      card.appendChild(pairRow);

      const hint = document.createElement('div');
      hint.className = 'cb-mut';
      hint.id = 'remote-hint';
      hint.style.cssText = 'font-size:11.5px;line-height:1.6';
      card.appendChild(hint);

      /* The countdown ticks off the expiry the host already sent — a clock,
         not a poll. Nothing is asked of the host between renders; a device
         actually pairing arrives as the `remote.devices` push below. */
      function countdown(pairing) {
        if (remoteCard.tick) clearInterval(remoteCard.tick);
        remoteCard.tick = null;
        if (!pairing || !pairing.expires_at) { codeNote.textContent = ''; return; }
        const paintLeft = () => {
          const left = Math.max(0, Math.round(pairing.expires_at - Date.now() / 1000));
          codeNote.textContent = left
            ? `expires in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`
            : 'expired';
          if (!left) {
            codeOut.textContent = '';
            clearInterval(remoteCard.tick);
            remoteCard.tick = null;
          }
        };
        paintLeft();
        remoteCard.tick = setInterval(paintLeft, 1000);
      }

      function paint(cfg) {
        /* The roster is local-only — who else the user has let in is not a
           fact about a remote caller's own connection, so the host sends a
           count there and the names here. */
        const devices = (cfg && cfg.devices) || [];
        const count = cfg && cfg.device_count != null
          ? cfg.device_count : devices.length;
        list.textContent = !count ? 'none paired yet'
          : devices.length
            ? `${count} — ` + devices.map((d) =>
                `${d.name} (${d.paired_at ? fmtDate(d.paired_at) : 'unknown'})`).join(' · ')
            : `${count} device${count === 1 ? '' : 's'}`;
        setDisabled(revoke, !local || !count,
          { reason: !local ? remoteReason
              : 'No devices are paired, so there is nothing to revoke.',
            ttKey: 'remote.revoke_all' });
        const pairing = cfg && cfg.pairing;
        if (pairing && pairing.code) {
          codeOut.textContent = `${pairing.code.slice(0, 3)} ${pairing.code.slice(3)}`;
          countdown(pairing);
        } else {
          codeOut.textContent = '';
          countdown(null);
        }
        hint.textContent = cfg && cfg.enabled
          ? 'Remote access is on. The host serves this bundle to paired ' +
            'devices on the network; a change here takes effect the next time ' +
            'the host starts. Plain HTTP is LAN-only — put it behind a tunnel ' +
            'for anything wider.'
          : 'Remote access is off. The host answers on this machine only, and ' +
            'no paired device can reach it from elsewhere.';
      }

      setDisabled(pairBtn, !local,
        { reason: remoteReason, ttText: pairBtn.getAttribute('data-tt-text') });
      pairBtn.addEventListener('click', async () => {
        try {
          await call('remote.pair_begin');
          paint(await call('remote.config'));
        } catch (_) { /* call() already toasted the reason */ }
      });
      revoke.addEventListener('click', () => openRevokeDevices(paint));

      /* A device pairing is the one thing that changes this card without
         anything on this screen being touched — the host pushes it, the card
         repaints, and the used-up code disappears with it. */
      remoteCard.off = cbApi.on('remote.devices', () => {
        call('remote.config').then(paint).catch(() => {});
      });

      call('remote.config').then(paint).catch(() => {
        list.textContent = 'unavailable';
      });
    },

    'Browser Cookies': (card) => {
      const row = document.createElement('div');
      row.className = 'cb-row';
      row.style.cssText = 'gap:8px;flex-wrap:wrap';
      row.append(
        stubButton('Test authentication', 'cb-btn--quiet', null,
          'Not wired up yet — cookie testing arrives with the web frontend\'s download service.'),
        stubButton('How-To: dedicated Firefox profile ↗', 'cb-btn--quiet',
          'settings.firefox_profile_howto',
          'Not wired up yet — this walkthrough arrives with the web frontend\'s help screens.'));
      card.appendChild(row);
    },
  };

  /* Section-level help, keyed by the section name the contract's settings keys
     group under. These are the registry's `?` strings — about a whole card,
     not about one control. */
  const SECTION_TOOLTIPS = {
    'Download Behavior': 'settings.download_behavior',
    'Browser Cookies': 'settings.cookies',
    'Downloads Database': 'settings.database',
    'Remote Access': 'remote.access_section',
  };

  function renderSettings() {
    const grid = $('#settings-grid');
    grid.innerHTML = '';
    $('#cfg-path').textContent = state.settings_path || '~/.dj_cratebuilder_config.json';

    const sections = [];
    SETTINGS_KEYS.forEach((entry) => {
      if (entry.section === 'internal' || entry.type === 'dict' || entry.type === 'list') return;
      if (entry.platform && entry.platform !== state.platform && entry.platform === 'win32'
          && state.platform && state.platform !== 'win32') return;
      let sec = sections.find((s) => s.name === entry.section);
      if (!sec) { sec = { name: entry.section, items: [] }; sections.push(sec); }
      sec.items.push(entry);
    });

    let missing = 0;
    sections.forEach((sec) => {
      const card = document.createElement('div');
      card.className = 'cb-card cb-set-card';
      if (sec.items.length > 6 || sec.name === 'Remote Access') card.classList.add('cb-span-2');

      const head = document.createElement('div');
      head.className = 'cb-row';
      head.style.gap = '7px';
      head.innerHTML = '<span class="cb-sect"></span>';
      head.firstChild.textContent = sec.name;
      /* Four sections carry a registry string about the section itself rather
         than about any one control — the desktop app's `?` help icons. The
         contract's third tooltip affordance is exactly this. */
      const secTip = SECTION_TOOLTIPS[sec.name];
      if (secTip && TOOLTIPS[secTip]) {
        const help = document.createElement('span');
        help.className = 'cb-help cb-tt-host';
        help.tabIndex = 0;
        help.textContent = '?';
        help.setAttribute('data-tt', secTip);
        head.appendChild(help);
      }
      card.appendChild(head);

      sec.items.forEach((entry) => {
        const available = Object.prototype.hasOwnProperty.call(state.settings, entry.key);
        if (!available) missing += 1;
        card.appendChild(control(entry, state.settings[entry.key], available));
      });
      if (SECTION_EXTRAS[sec.name]) SECTION_EXTRAS[sec.name](card);
      grid.appendChild(card);
    });

    if (missing) {
      const note = document.createElement('div');
      note.className = 'cb-card cb-pad cb-span-2';
      note.innerHTML =
        `<span class="cb-mut" style="font-size:12px">${missing} option${missing === 1 ? '' : 's'} ` +
        'in the design contract have no matching key in the host config yet, so they render ' +
        'disabled with the reason in their tooltip rather than silently doing nothing.</span>';
      grid.appendChild(note);
    }
    applySettingsDependencies();
    bindTips(grid);
  }

  /* ── About (3n) ───────────────────────────────────────────────────────────
     Read-only, on both mounts: the design shows About in a remote session
     precisely so you can tell whether the host is current. The author fields,
     the two GitHub links and the whole FAQ come from `about.info`, which reads
     them out of the desktop app's own source — one copy, no drift.

     Links go through `fs.open_url`, which is LOCAL_ONLY: the host opens its own
     browser. A remote browser can open its own, but the host cannot open one
     for it, so remotely the address is copied instead — the same local/remote
     split the database viewer's Open File / Copy Path pair uses. */

  const about = { info: null, loading: false, open: {} };

  /* The standing ruling: the updater is a separate effort and is not wired
     here. Rendered visible and disabled with the reason, like every other
     control the frontend has not reached — never removed, because the build
     number beside them is the thing the design put them there for. */
  const ABOUT_UPDATER_DEFERRED =
    'Not wired up in the web frontend — the in-app updater is its own effort ' +
    'and stays with the desktop app for now. Check for updates from the ' +
    'desktop app\'s About tab; the build number above tells you where this ' +
    'host is.';

  const ABOUT_UPDATER_NOTE =
    'The updater runs only in the local window on the host machine — a ' +
    'browser somewhere else should not be able to replace the binary it is ' +
    'talking to. The build number above tells you whether the host is current.';

  async function openUrl(url) {
    if (!url) { toast('No address for that link.', true); return; }
    if (cbApi.transport === 'local') {
      try { await call('fs.open_url', { url }); } catch (_) { /* toasted */ }
      return;
    }
    await dbCopyText(url, 'link — open it in this browser');
  }

  function aboutLinkButton(label, url, ttKey) {
    const b = document.createElement('button');
    b.className = 'cb-btn cb-btn--quiet cb-btn--sm';
    b.textContent = label;
    if (ttKey) b.setAttribute('data-tt', ttKey);
    b.addEventListener('click', () => openUrl(url));
    return b;
  }

  function aboutRow(label, node) {
    const row = document.createElement('div');
    row.className = 'cb-about-row';
    const lab = document.createElement('span');
    lab.className = 'cb-about-lab';
    lab.textContent = label;
    row.append(lab, node);
    return row;
  }

  function aboutFaqNode(rows) {
    const box = document.createElement('div');
    box.className = 'cb-faq';
    if (!rows.length) {
      const note = modalNote('The FAQ could not be read from the host.');
      note.style.padding = '10px 12px';
      box.appendChild(note);
      return box;
    }
    rows.forEach((row, i) => {
      const q = document.createElement('button');
      q.className = 'cb-faq__q';
      q.setAttribute('aria-expanded', about.open[i] ? 'true' : 'false');
      const chev = document.createElement('span');
      chev.className = 'cb-faq__chev';
      chev.textContent = about.open[i] ? '▾' : '▸';
      const text = document.createElement('span');
      text.textContent = row.q;
      q.append(chev, text);
      const a = document.createElement('div');
      a.className = 'cb-faq__a';
      a.textContent = row.a;
      a.hidden = !about.open[i];
      q.addEventListener('click', () => {
        about.open[i] = !about.open[i];
        a.hidden = !about.open[i];
        chev.textContent = about.open[i] ? '▾' : '▸';
        q.setAttribute('aria-expanded', about.open[i] ? 'true' : 'false');
      });
      box.append(q, a);
    });
    return box;
  }

  function renderAbout() {
    const host = $('#about-body');
    if (!host) return;
    host.innerHTML = '';
    const info = about.info;
    if (!info) {
      host.appendChild(ovEmpty(about.loading ? 'Loading…'
        : 'The host could not send the About details.'));
      return;
    }

    // ── identity ──────────────────────────────────────────────────────────
    const head = document.createElement('div');
    head.className = 'cb-row';
    head.style.gap = '14px';
    const logo = document.createElement('img');
    logo.src = 'assets/logo.png';
    logo.alt = '';
    logo.width = 54;
    logo.height = 54;
    logo.style.cssText = 'border-radius:8px;display:block;flex:none';
    const names = document.createElement('div');
    const name = document.createElement('div');
    name.style.cssText =
      'font-weight:600;font-size:19px;color:var(--cb-text);letter-spacing:-.015em';
    name.textContent = info.app_name || 'DJ-CrateBuilder';
    const build = document.createElement('div');
    build.className = 'cb-mono cb-mut';
    build.style.cssText = 'font-size:11.5px;margin-top:3px';
    build.textContent = [info.version ? `version ${info.version}` : '',
                         info.build_status].filter(Boolean).join(' · ');
    names.append(name, build);
    head.append(logo, names);
    const mount = tagNode(
      cbApi.transport === 'local' ? 'Local window'
        : (session && session.read_only ? 'Read-only' : 'Remote session'),
      'cb-tag--grey');
    mount.style.marginLeft = 'auto';
    head.appendChild(mount);
    host.append(head, divNode());

    // ── author ────────────────────────────────────────────────────────────
    const author = document.createElement('div');
    author.style.cssText = 'display:flex;gap:8px;align-items:flex-start';
    const avatar = document.createElement('img');
    avatar.src = info.avatar || 'assets/about_avatar.png';
    avatar.alt = '';
    avatar.width = 44;
    avatar.height = 44;
    avatar.style.cssText = 'border-radius:6px;display:block;flex:none';
    const who = document.createElement('div');
    who.style.cssText = 'display:flex;flex-direction:column;gap:3px;padding-top:3px';
    const person = document.createElement('span');
    person.className = 'cb-about-val';
    person.textContent = info.created_by || '';
    const mail = document.createElement('a');
    mail.href = '#about';
    mail.style.cssText = 'font-size:12.5px;text-decoration:underline';
    mail.textContent = info.contact_email || '';
    mail.setAttribute('data-tt', 'about.mail');
    mail.addEventListener('click', (e) => {
      e.preventDefault();
      openUrl(`mailto:${info.contact_email || ''}`);
    });
    who.append(person, mail);
    author.append(avatar, who);
    host.appendChild(aboutRow('Created by', author));

    const built = document.createElement('span');
    built.className = 'cb-about-val';
    built.textContent = info.description || '';
    host.appendChild(aboutRow('Built with', built));

    // ── links ─────────────────────────────────────────────────────────────
    const links = document.createElement('div');
    links.className = 'cb-row';
    links.style.cssText = 'gap:9px;flex-wrap:wrap';
    links.append(
      aboutLinkButton('View on GitHub ↗', info.github_url, 'about.github'),
      aboutLinkButton('↗ Submit Issues / Suggestions', info.issues_url,
                      'about.issues'));
    if (info.github_url) {
      const licence = aboutLinkButton('Licence',
        `${info.github_url.replace(/\/+$/, '')}/blob/main/LICENSE`);
      licence.setAttribute('data-tt-text',
        'Opens the project licence on GitHub. Remote sessions get the address ' +
        'to open here instead.');
      links.appendChild(licence);
    }
    host.appendChild(links);
    if (info.note) {
      const note = modalNote(info.note);
      note.style.fontSize = '12px';
      host.appendChild(note);
    }

    // ── updates (deferred, per the standing ruling) ────────────────────────
    host.appendChild(divNode());
    const upHead = document.createElement('div');
    upHead.className = 'cb-row';
    upHead.style.gap = '7px';
    const upKick = document.createElement('span');
    upKick.className = 'cb-sect';
    upKick.textContent = 'Updates';
    upHead.append(upKick, tagNode('Local session only', 'cb-tag--grey'));
    const upRow = document.createElement('div');
    upRow.className = 'cb-row';
    upRow.style.cssText = 'gap:9px;flex-wrap:wrap';
    /* Remote adds a second, independent reason — the contract's own copy says
       it — so both are shown there and only the deferral is shown locally,
       where "this is a remote session" would simply be untrue. */
    const remoteHalf = cbApi.transport === 'local' ? null : true;
    [['⟳ Check for updates', 'about.check_updates'],
     ['⤓ Update Now', 'about.update_now']].forEach(([label, key]) => {
      const b = document.createElement('button');
      b.className = 'cb-btn cb-btn--sm';
      b.textContent = label;
      setDisabled(b, true, {
        reason: (remoteHalf && TOOLTIPS[key] ? TOOLTIPS[key] + '\n\n' : '') +
                ABOUT_UPDATER_DEFERRED,
      });
      upRow.appendChild(b);
    });
    const every = document.createElement('select');
    every.className = 'cb-sel';
    every.style.width = '150px';
    every.innerHTML = '<option>Every 24 hours</option>';
    setDisabled(every, true, {
      reason: (TOOLTIPS['about.update_interval_readonly'] ||
               TOOLTIPS['about.update_interval'] || '') +
              '\n\n' + ABOUT_UPDATER_DEFERRED,
    });
    upRow.appendChild(every);
    const warn = document.createElement('div');
    warn.className = 'cb-warnbox';
    warn.textContent = ABOUT_UPDATER_NOTE;
    host.append(upHead, upRow, warn);

    // ── FAQ ───────────────────────────────────────────────────────────────
    host.appendChild(divNode());
    const faqHead = document.createElement('div');
    faqHead.className = 'cb-row';
    const faqKick = document.createElement('span');
    faqKick.className = 'cb-kick';
    faqKick.textContent = 'Frequently Asked Questions';
    const faqBtns = document.createElement('div');
    faqBtns.className = 'cb-row';
    faqBtns.style.cssText = 'margin-left:auto;gap:6px';
    const rows = info.faq || [];
    [['⊞', true, 'about.faq_expand'], ['⊟', false, 'about.faq_collapse']]
      .forEach(([sym, opened, key]) => {
        const b = document.createElement('button');
        b.className = 'cb-btn cb-btn--quiet cb-btn--sm';
        b.style.padding = '3px 8px';
        b.textContent = sym;
        setDisabled(b, !rows.length, {
          reason: tipPlus(key, 'The FAQ could not be read from the host.'),
          ttKey: key,
        });
        if (rows.length) {
          b.addEventListener('click', () => {
            rows.forEach((_, i) => { about.open[i] = opened; });
            renderAbout();
          });
        }
        faqBtns.appendChild(b);
      });
    faqHead.append(faqKick, faqBtns);
    host.append(faqHead, aboutFaqNode(rows));
    bindTips(host);
  }

  async function aboutOpen() {
    if (about.info || about.loading) { renderAbout(); return; }
    about.loading = true;
    renderAbout();
    try {
      about.info = await call('about.info');
    } catch (_) { /* call() already toasted the reason */ }
    about.loading = false;
    renderAbout();
  }

  /* ── wiring ────────────────────────────────────────────────────────────── */
  async function addToBatch(inputEl) {
    const url = inputEl.value.trim();
    if (!url) { toast('Paste a YouTube or SoundCloud link first.', true); return; }
    const platform = $('#dl-platform .is-on')?.dataset.platform || '';
    await call('batch.add', { url, genre: $('#dl-genre').value, platform });
    inputEl.value = '';
    state.batch = await call('batch.list');
    renderBatch();
    toast('Added to batch');
  }

  function wire() {
    wireLogScreen('activity');
    wireLogScreen('debug');

    $('#quick-add').addEventListener('click', () => addToBatch($('#quick-url')));
    $('#quick-url').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') addToBatch($('#quick-url'));
    });
    $('#dl-add').addEventListener('click', () => addToBatch($('#dl-url')));
    $('#dl-url').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') addToBatch($('#dl-url'));
    });

    $('#dl-clear').addEventListener('click', async () => {
      await call('batch.clear');
      state.batch = [];
      renderBatch();
    });

    $$('#dl-platform > span').forEach((seg) => {
      seg.addEventListener('click', () => {
        $$('#dl-platform > span').forEach((s) => s.classList.remove('is-on'));
        seg.classList.add('is-on');
      });
    });

    $('#ov-refresh').addEventListener('click', refresh);
    $('#ov-bell').addEventListener('click', toggleNotifications);
    $('#ov-dl-all').addEventListener('click',
      () => wlRun('watchlist.download_all_new', {},
                  'Downloading every pending track…'));
    /* One pair of controls, whichever job is running — the card says which,
       and these act on that one. Nothing is guessed: overviewJob() is the same
       function that painted the card. */
    $('#ov-pause').addEventListener('click', async () => {
      const job = overviewJob();
      if (!job || !job.pausable) return;
      try {
        if (dl.paused) { await call('download.resume'); dl.paused = false; }
        else { await call('download.pause'); dl.paused = true; }
        renderDownloadsHeader();
        renderOverviewRunning();
      } catch (_) { /* call() already toasted the reason */ }
    });
    $('#ov-cancel').addEventListener('click', async () => {
      const job = overviewJob();
      if (!job) return;
      if (job.key === 'batch') { call('download.cancel').catch(() => {}); return; }
      if (job.key === 'maintenance') {
        call('db.maintenance_cancel').catch(() => {});
        return;
      }
      try {
        await call('watchlist.cancel_all');
        toast(WL_CANCEL_ALL_NOTE);
      } catch (_) { /* call() already toasted the reason */ }
    });

    $('#dl-start').addEventListener('click', async () => {
      try {
        await call('download.start');
        dl.running = true;
        dl.paused = false;
        dl.rows = {};
        dl.current = null;
        dl.overall = null;
        renderDownloads();
      } catch (_) { /* call() already toasted the reason */ }
    });
    $('#dl-cancel').addEventListener('click', () => { call('download.cancel').catch(() => {}); });
    $('#dl-pause').addEventListener('click', async () => {
      try {
        if (dl.paused) { await call('download.resume'); dl.paused = false; }
        else { await call('download.pause'); dl.paused = true; }
        renderDownloadsHeader();
        // Two surfaces, one flag: pausing from either has to move both labels,
        // or the other one reads "Pause" on a batch that is already held.
        renderOverviewRunning();
      } catch (_) { /* call() already toasted the reason */ }
    });

    wireWatchlist();

    /* Open Main Folder is a host-filesystem action, and the bridge exists now
       (fs.reveal). Local opens it; remote copies the path, the same split the
       Watch List card's Open Folder uses — a browser elsewhere cannot be shown
       the host's file manager. */
    $('#dl-openfolder').addEventListener('click', () => {
      const path = (state.settings || {}).base_dir || '';
      if (!path) { toast('No save directory is set.', true); return; }
      if (cbApi.transport === 'local') dbReveal(path, 'folder');
      else dbCopyText(path, 'the save directory path');
    });

    // Actions the service does not implement yet stay visibly disabled, each
    // carrying the reason — never a dead control with no explanation.
    [['#dl-newgenre', 'Creating a genre folder is not wired up in the web ' +
      'frontend — add the genre from the desktop app, or download into it ' +
      'once and it appears here.'],
    ].forEach(([sel, why]) => {
      const el = $(sel);
      if (!el) return;
      setDisabled(el, true, { reason: tipPlus(el.getAttribute('data-tt'), why) });
    });
  }

  function wireWatchlist() {
    const scanAll = () => wlRun('watchlist.scan_all', {}, 'Scanning every channel…');
    $('#quick-scan').addEventListener('click', () => { show('watchlist'); scanAll(); });
    $('#wl-scan').addEventListener('click', scanAll);
    $('#wl-add').addEventListener('click', openAddChannel);
    $('#wl-links').addEventListener('click', runCheckLinks);
    $('#wl-dl-all').addEventListener('click',
      () => wlRun('watchlist.download_all_new', {}, 'Downloading every pending track…'));
    $('#wl-cancel').addEventListener('click', async () => {
      try {
        await call('watchlist.cancel_all');
        toast(WL_CANCEL_ALL_NOTE);
      } catch (_) { /* call() already toasted the reason */ }
    });
    // Clears the pinned view only — activity.log on disk is untouched.
    $('#wl-log-clear').addEventListener('click', wlLogReset);
    $('#wl-log-open').addEventListener('click', () => show('activity-log'));
    wlLogReset();
  }

  /* The snapshot is the only source of "is anything running": it can be taken
     at face value because the host releases a job's slot BEFORE it announces
     the end with job.finished (see _start_job), so no resync triggered by that
     event can be answered with a stale `running`. The runs' own terminal
     events (batch.finished, the closing DONE scan line) are display only and
     never drive a refresh. */
  async function refresh() {
    state = await call('state.snapshot');
    state.platform = state.platform || null;
    // Host truth on every snapshot: a page load (or a manual refresh) while a
    // batch is running must show the running state, not wait for the next
    // progress event to reveal it.
    dl.running = !!(state.running && state.running.batch);
    wl.running = !!(state.running && state.running.watchlist);
    mt.running = !!(state.running && state.running.maintenance);
    mt.task = (state.running && state.running.maintenance_task) || null;
    wl.cards = state.watchlist || [];
    renderShell();
    renderOverview();
    renderGenres();
    renderDownloads();
    renderWatchlist();
    renderSettings();
    bindTips(document);
  }

  // A Main-tab batch and a Watch List download are separate job categories and
  // can run at the same time, both driving the same runner. Every progress
  // frame names its job, so the Main tab takes only its own — without this the
  // watch-list run repaints this bar, its ETA and its current-track line.
  function isBatchProgress(p) {
    return !p || !p.job || p.job === 'batch';
  }

  function subscribeDownloadEvents() {
    cbApi.on('progress.current', (p) => {
      if (p && p.job === 'watchlist') {
        wl.current = p; wlPaintProgress(); renderOverviewRunning(); return;
      }
      if (p && p.job === 'maintenance') {
        mt.current = p; maintPaint(); renderOverviewRunning(); return;
      }
      if (!dl.running || !isBatchProgress(p)) return;
      dl.current = p;
      renderCurrent();
      renderQueueLog();
      renderOverviewRunning();
    });
    cbApi.on('progress.overall', (p) => {
      if (p && p.job === 'watchlist') {
        wl.overall = p; wlPaintProgress(); renderOverviewRunning(); return;
      }
      if (p && p.job === 'maintenance') {
        mt.overall = p; maintPaint(); renderOverviewRunning(); return;
      }
      if (!dl.running || !isBatchProgress(p)) return;
      dl.overall = p;
      renderOverall();
      renderPanelBatchMini();
      renderOverviewRunning();
    });
    cbApi.on('watchlist.card', wlApplyCard);
    // The pinned scan log, and nothing else: a run's closing DONE line is a
    // log line, not a state signal (see job.finished below).
    cbApi.on('scan.line', (entry) => { if (entry) wlLogAppend(entry); });
    cbApi.on('queue.row', (r) => {
      if (!dl.running) return;
      dl.rows[r.id] = { state: r.state, title: r.title, detail: r.detail };
      renderBatch();
    });
    // Also display only — the tally and the local reset, no resync. The host
    // is still holding the batch's job slot when this arrives.
    cbApi.on('batch.finished', (r) => {
      dl.running = false;
      dl.paused = false;
      dl.rows = {};
      dl.current = null;
      dl.overall = null;
      renderDownloads();
      renderOverviewRunning();
      const parts = `${num(r.downloaded)} downloaded, ${num(r.skipped)} skipped, ` +
        `${num(r.errors)} error${r.errors === 1 ? '' : 's'}`;
      toast((r.cancelled ? 'Batch cancelled — ' : 'Batch finished — ') + parts,
        !r.cancelled && r.errors > 0);
    });
    /* The one event that means a job category is free again — emitted after
       the host releases the slot, so the snapshot this asks for cannot come
       back still claiming the run is going. Both job categories resync here;
       neither reads its own terminal event for this. */
    cbApi.on('job.finished', (p) => {
      const job = p && p.job;
      if (job === 'batch') {
        dl.running = false;
        dl.paused = false;
        dl.rows = {};
        dl.current = null;
        dl.overall = null;
      } else if (job === 'watchlist') {
        wl.running = false;
        wl.current = null;
        wl.overall = null;
      } else if (job === 'maintenance') {
        maintSettle(p);
      } else {
        return;
      }
      refresh();
    });
    /* A run's closing summary. Display only, and never a state signal — the
       modal is settled by job.finished above, which is what the host emits
       once the slot is actually free. Toasted only when nobody is watching
       the dialog it would otherwise be shown in twice. */
    cbApi.on('notification', (n) => {
      if (!n) return;
      /* The bell (3n) keeps every one of them, including the maintenance
         summaries the progress dialog also shows: the dialog is a live view of
         one run, the bell is the record of what the host has done. */
      pushNote(n);
      if (n.job === 'maintenance') {
        mt.note = n;
        if (mt.view) { maintPaint(); return; }
      }
      // `warn` is the level a cancelled run, or one with per-track failures,
      // reports at — an outcome that wants looking at, so it takes the
      // attention treatment rather than reading as routine.
      toast(`${n.title} — ${n.body}`,
        n.level === 'error' || n.level === 'warn');
    });
    cbApi.on('state.patch', (p) => {
      if (!state || !p || !p.counts) return;
      state.counts = Object.assign({}, state.counts, p.counts);
      renderShell();
      renderOverview();
    });
    cbApi.on('log.append', (p) => {
      if (p && (p.name === 'activity' || p.name === 'debug')) logHandleAppend(p.name, p);
    });
  }

  let booted = false;

  async function boot() {
    await cbApi.connect();
    if (!cbApi.paired()) { showPairing({ reason: 'unpaired' }); return; }
    await refreshSession();
    const strings = await call('ui_strings');
    TOOLTIPS = strings.tooltips || {};
    SETTINGS_KEYS = strings.settings_keys || [];
    if (!booted) {
      booted = true;
      loadNotes();
      wire();
      subscribeDownloadEvents();
      subscribeSessionEvents();
    }
    renderBell();
    await refresh();
    setHostOffline(false);
    bindTips(document);
    show(location.hash.slice(1) || 'overview');
  }

  /* Wired once, and outside boot()'s try — these are exactly the handlers that
     have to survive a host that went away mid-boot. */
  function subscribeSessionEvents() {
    cbApi.on('host.status', (s) => {
      if (s && s.session) session = s.session;
      if (!state) return;
      state.host.online = !!(s && s.online);
      hostReason = (s && s.reason) || '';
      renderShell();
      setHostOffline(!(s && s.online), s && s.reason);
      if (s && s.online) refresh().catch(() => {});
    });
    /* The single-writer lock changed hands. Every socket hears it, so a client
       that just lost control finds out without asking — and re-asking is one
       call, not a re-render of everything. */
    cbApi.on('control.holder', async () => {
      await refreshSession();
      if (!state) return;
      renderDownloads();
      renderWatchlist();
      renderSettings();
    });
    cbApi.on('auth.required', (info) => {
      setHostOffline(false);
      showPairing(info || {});
    });
  }

  /* A host that is down at first paint must not leave a blank page either —
     the shell stays, the offline bar explains, and Retry re-runs boot. */
  boot().catch((err) => {
    /* Parked on the window as well as the console: a host driving this page
       through evaluate_js can read a property, and cannot read the console —
       so without this, a boot that fails inside the local window leaves
       nothing but "offline" to diagnose from. */
    window.__bootError = err && (err.stack || err.message || String(err));
    console.error(err);
    if (err && err.needsPairing) return;      // the pairing screen is already up
    if (!booted) {
      booted = true;
      subscribeSessionEvents();
    }
    setHostOffline(true);
    toast('Could not reach the host process.', true);
  });
})();
