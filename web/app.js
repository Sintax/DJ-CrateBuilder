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
  let quickScanIdleReason = '';

  const DL_MARK = { done: '✓', active: '▶', skipped: '⊘', error: '✗', queued: '·' };
  const DL_LOG_CLASS = { done: 'downloaded', skipped: 'skipped', error: 'error', queued: 'default' };
  const DL_MARK_COLOR = { done: 'var(--cb-ok)', skipped: 'var(--cb-warn)', error: 'var(--cb-err)', active: 'var(--cb-accent)' };

  /* ── tooltips ───────────────────────────────────────────────────────────
     theme.css styles a hover-only mockup; the contract requires focus,
     Escape and aria-describedby, so the live behaviour is driven here. */
  const tip = { el: null, timer: null, host: null };

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

    host.setAttribute('aria-describedby', el.id);
    tip.el = el;
    tip.host = host;
  }

  function hideTip() {
    if (tip.el) tip.el.remove();
    if (tip.host) tip.host.removeAttribute('aria-describedby');
    tip.el = null;
    tip.host = null;
  }

  function tipText(host) {
    const key = host.getAttribute('data-tt');
    return host.getAttribute('data-tt-text') || (key ? TOOLTIPS[key] : '') || '';
  }

  function bindTips(root) {
    $$('[data-tt],[data-tt-text]', root).forEach((host) => {
      if (host.__tipBound) return;
      host.__tipBound = true;
      host.addEventListener('mouseenter', () => {
        clearTimeout(tip.timer);
        tip.timer = setTimeout(() => showTip(host, tipText(host)), 350);
      });
      host.addEventListener('mouseleave', () => { clearTimeout(tip.timer); hideTip(); });
      host.addEventListener('focus', () => showTip(host, tipText(host)));
      host.addEventListener('blur', hideTip);
    });
  }

  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hideTip(); });
  addEventListener('scroll', hideTip, true);

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

  /* ── navigation ────────────────────────────────────────────────────────── */
  const SCREENS = ['overview', 'downloads', 'watchlist', 'settings',
                    'activity-log', 'debug-log'];
  /* The log screens aren't nav items (they open from Settings, per the
     contract's shell.not_in_nav) — while either is open, Settings stays the
     highlighted nav entry, per shell.active_item_rule. */
  const NAV_ALIAS = { 'activity-log': 'settings', 'debug-log': 'settings' };
  const LOG_KIND_BY_SCREEN = { 'activity-log': 'activity', 'debug-log': 'debug' };
  let currentScreen = null;

  function show(name) {
    if (!SCREENS.includes(name)) name = 'overview';
    const previous = currentScreen;
    currentScreen = name;
    $$('.cb-screen').forEach((s) => s.classList.toggle('is-on', s.id === 'screen-' + name));
    const navName = NAV_ALIAS[name] || name;
    $$('.cb-nav').forEach((a) => a.classList.toggle('is-on', a.dataset.screen === navName));
    $('.cb-main').scrollTop = 0;
    if (location.hash.slice(1) !== name) location.hash = name;

    const leavingKind = LOG_KIND_BY_SCREEN[previous];
    if (leavingKind && leavingKind !== LOG_KIND_BY_SCREEN[name]) logClose(leavingKind);
    const enteringKind = LOG_KIND_BY_SCREEN[name];
    if (enteringKind) logOpen(enteringKind);
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

  function renderShell() {
    const app = state.app;
    const host = state.host;
    $('#mount-tag').textContent = host.transport === 'local' ? 'Local' : 'Remote';
    $('#host-dot').classList.toggle('is-on', !!host.online);
    $('#host-label').textContent = host.online
      ? (host.transport === 'local' ? 'host · this machine' : 'host · paired')
      : 'host offline';
    $('#host-version').textContent = app.version ? `v${app.version} · build ${app.build}` : '';

    const pending = state.counts.pending_new || 0;
    const badge = $('#nav-count');
    badge.textContent = pending;
    badge.hidden = pending === 0;
  }

  function renderOverview() {
    const c = state.counts;
    $('#ov-library').innerHTML =
      `Library <span class="cb-mono">${num(c.downloads)}</span> tracks · ` +
      `<span class="cb-mono">${num(c.genres)}</span> genres · ` +
      `<span class="cb-mono">${num(c.watchlist)}</span> channels`;
    $('#ov-new').textContent = num(c.pending_new);
    $('#ov-new-sub').textContent =
      `new tracks across ${num(c.watchlist)} channel${c.watchlist === 1 ? '' : 's'}`;
    $('#ov-tracks').textContent = num(c.downloads);
    $('#ov-dbpath').textContent = state.library.path || '';
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
    setDisabled($('#dl-cancel'), !dl.running,
      { reason: 'No download is running.', ttKey: 'main.cancel_batch' });
    setDisabled($('#dl-pause'), !dl.running,
      { reason: 'No download is running.', ttKey: 'main.pause_batch' });
    $('#dl-progress').style.opacity = dl.running ? '1' : '.6';

    const qs = $('#quick-scan');
    if (qs) {
      qs.setAttribute('data-tt-text', dl.running
        ? 'Unavailable while a batch is running — a scan and a download ' +
          "can't share the host's yt-dlp session."
        : quickScanIdleReason);
    }
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

  function renderOverviewRunning() {
    const el = $('#ov-running');
    if (!el) return;
    el.hidden = !dl.running;
    if (!dl.running) return;
    const p = dl.overall;
    $('#ov-run-meta').textContent = p
      ? `${num(p.done)} / ${num(p.total)} · ${p.percent || 0}%` : 'starting…';
  }

  function skipBtn(row, warn) {
    const b = document.createElement('button');
    b.className = 'cb-btn cb-btn--sm cb-icon ' + (warn ? 'cb-btn--warn' : 'cb-btn--quiet');
    b.textContent = warn ? '⏭ Skip' : '⏭';
    b.setAttribute('data-tt', row.state === 'skipped' ? 'main.row_skip_marked' : 'main.row_skip');
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
      setDisabled($('#dl-clear'), running,
        { reason: 'The queue is locked while a download is running. Cancel it first, or skip the row instead.',
          ttKey: 'main.batch_clear' });
      renderQueueLog();
      return;
    }
    setStartDisabled(running, running ? 'A batch is already running.' : '');
    setDisabled($('#dl-clear'), running,
      { reason: 'The queue is locked while a download is running. Cancel it first, or skip the row instead.',
        ttKey: 'main.batch_clear' });

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

  function setStartDisabled(disabled, reason) {
    setDisabled($('#dl-start'), disabled, { reason, ttKey: 'main.start_downloads' });
  }

  function renderDownloads() {
    renderDownloadsHeader();
    renderCurrent();
    renderOverall();
    renderPanelBatchMini();
    renderBatch();
  }

  function renderWatchlist() {
    const rows = state.watchlist || [];
    const pending = state.counts.pending_new || 0;
    $('#wl-summary').innerHTML =
      `<span class="cb-mono">${num(rows.length)}</span> channels · ` +
      `<span class="cb-mono">${num(pending)}</span> new`;
    $('#wl-dl-all').textContent = `⬇ Download All New (${num(pending)})`;

    const host = $('#wl-cards');
    host.innerHTML = '';
    if (!rows.length) {
      host.innerHTML =
        '<div class="cb-card cb-pad"><span class="cb-mut">No channels tracked yet — ' +
        'add one to have new uploads found automatically.</span></div>';
      return;
    }

    rows.forEach((row) => {
      const name = row.name;
      const newCount = row.new_count;
      const unresolved = row.unresolved;

      const card = document.createElement('div');
      card.className = 'cb-card';
      card.style.cssText = 'padding:14px 16px;display:flex;flex-direction:column;gap:8px';

      const head = document.createElement('div');
      head.className = 'cb-row';
      head.style.gap = '9px';
      head.innerHTML =
        `<span style="font-weight:600;font-size:14.5px;color:var(--cb-text)"></span>` +
        `<span class="cb-tag cb-tag--grey"></span>` +
        `<span class="cb-tag"></span>` +
        (unresolved ? '<span class="cb-tag cb-tag--attn">Link unresolved</span>' : '') +
        `<span class="cb-mono" style="margin-left:auto;color:var(--cb-line);font-size:12.5px;font-weight:500">${newCount} new</span>`;
      head.children[0].textContent = name;
      head.children[1].textContent = row.platform || '—';
      head.children[2].textContent = row.genre || '(none)';
      card.appendChild(head);

      const meta = document.createElement('div');
      meta.className = 'cb-mut cb-mono';
      meta.style.fontSize = '11px';
      const bits = [];
      if (row.last_scan) bits.push(`last scan ${fmtWhen(row.last_scan)}`);
      bits.push(`${num(row.downloaded)} downloaded`);
      if (unresolved) bits.push('folder has no canonical channel id');
      bits.push(row.status || 'idle');
      meta.textContent = bits.join(' · ');
      card.appendChild(meta);

      const actions = document.createElement('div');
      actions.className = 'cb-row';
      actions.style.cssText = 'gap:5px;flex-wrap:wrap';
      [['🔍 Scan', 'wl.card_scan', 'cb-btn--quiet'],
       ['⚡ Force Download', 'wl.card_force', 'cb-btn--quiet'],
       [`⬇ Download New (${newCount})`, 'wl.card_download_new', ''],
       ['✏ Edit', 'wl.card_edit', 'cb-btn--quiet'],
       ['✕ Remove', 'wl.card_remove', 'cb-btn--quiet'],
      ].forEach(([label, ttKey, extra]) => {
        const b = document.createElement('button');
        b.className = `cb-btn cb-btn--sm ${extra}`.trim();
        b.textContent = label;
        b.setAttribute('data-tt', ttKey);
        b.disabled = true;
        b.setAttribute('data-tt-text',
          (TOOLTIPS[ttKey] ? TOOLTIPS[ttKey] + '\n\n' : '') +
          'Not wired up yet — the Watch List actions arrive with the service layer.');
        actions.appendChild(b);
      });
      if (unresolved) {
        const fix = document.createElement('button');
        fix.className = 'cb-btn cb-btn--sm';
        fix.style.cssText = 'background:#FF8C00;border-color:#FF8C00;color:#1a1a1a;font-weight:600';
        fix.textContent = '🛠 Fix Link';
        fix.setAttribute('data-tt', 'wl.card_fix_link');
        fix.disabled = true;
        actions.appendChild(fix);
      }
      card.appendChild(actions);
      host.appendChild(card);
    });
    bindTips(host);
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

  function logRenderLine(kind, line, isCurrentMatch) {
    const cfg = LOG_KINDS[kind];
    const div = document.createElement('div');
    const cls = cfg.lineClass(line);
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
      if (!state || state.host.transport !== 'local') {
        toast(`The ${kind} log lives at ${res.path} on the host — browser ` +
              'download arrives with a later update.');
        return res;
      }
      const full = await call('logs.tail', { name: kind, offset: 0, limit: 0 });
      const text = full.lines.join('\n') + (full.lines.length ? '\n' : '');
      const blob = new Blob([text], { type: 'text/plain' });
      const url = URL.createObjectURL(blob);
      const filename = kind === 'activity' ? 'activity.log' : 'debug.log';
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

  /* ── settings ──────────────────────────────────────────────────────────── */
  const NOT_AVAILABLE_REASON = 'This option is not wired into the web frontend yet — ' +
                               'change it in the desktop app for now.';

  function control(entry, value, available) {
    const wrap = document.createElement('div');

    function mark(el) {
      el.dataset.key = entry.key;
      el.dataset.origTt = entry.tooltip || '';
      if (!available) {
        el.disabled = true;
        el.setAttribute('data-tt-text',
          (entry.tooltip && TOOLTIPS[entry.tooltip] ? TOOLTIPS[entry.tooltip] + '\n\n' : '') +
          NOT_AVAILABLE_REASON);
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
    set('run_at_startup', state.host.transport !== 'local',
      'Run App on Startup can only be changed from the app window on the host machine.');

    bindTips(grid);
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

      row.append(activityBtn, debugBtn, bothBtn);
      card.appendChild(row);
    },

    'Downloads Database': (card) => {
      const row = document.createElement('div');
      row.className = 'cb-row';
      row.style.cssText = 'gap:8px;flex-wrap:wrap';
      row.appendChild(stubButton('🗂 Open Database', '', null,
        "Not wired up yet — the database viewer arrives with the web " +
        "frontend's database screen."));
      const maintReason = "Not wired up yet — maintenance jobs arrive with the " +
                          "web frontend's job runner.";
      row.append(
        stubButton('🔄 Rebuild Database from Files', 'cb-btn--warn', 'settings.rebuild_db', maintReason),
        stubButton('🧹 Remove Duplicates', 'cb-btn--warn', 'settings.dedupe_db', maintReason),
        stubButton('🖼 Fetch Missing Artwork', 'cb-btn--warn', 'settings.fetch_artwork', maintReason),
        stubButton('🏷 Repair Track Tags', 'cb-btn--warn', 'settings.repair_tags', maintReason));
      card.appendChild(row);
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
    $('#ov-goto-wl').addEventListener('click', () => show('watchlist'));

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
      } catch (_) { /* call() already toasted the reason */ }
    });

    // Actions the service does not implement yet stay visibly disabled, each
    // carrying the reason — never a dead control with no explanation.
    [['#quick-scan', 'Scanning from the web frontend arrives with the download service.'],
     ['#wl-scan', 'Scanning from the web frontend arrives with the download service.'],
     ['#wl-add', 'Adding a channel arrives with the Watch List service.'],
     ['#wl-links', 'Link checking arrives with the Watch List service.'],
     ['#wl-dl-all', 'Downloading arrives with the download service.'],
     ['#dl-openfolder', 'Folder actions arrive with the host filesystem bridge.'],
     ['#dl-newgenre', 'Creating a genre folder arrives with the host filesystem bridge.'],
    ].forEach(([sel, why]) => {
      const el = $(sel);
      if (!el) return;
      el.disabled = true;
      const existing = el.getAttribute('data-tt');
      const base = existing && TOOLTIPS[existing] ? TOOLTIPS[existing] + '\n\n' : '';
      el.setAttribute('data-tt-text', base + why);
      if (sel === '#quick-scan') quickScanIdleReason = base + why;
    });
  }

  async function refresh() {
    state = await call('state.snapshot');
    state.platform = state.platform || null;
    // Host truth on every snapshot: a page load (or a manual refresh) while a
    // batch is running must show the running state, not wait for the next
    // progress event to reveal it.
    dl.running = !!(state.running && state.running.batch);
    renderShell();
    renderOverview();
    renderGenres();
    renderDownloads();
    renderWatchlist();
    renderSettings();
    bindTips(document);
  }

  function subscribeDownloadEvents() {
    cbApi.on('progress.current', (p) => {
      if (!dl.running) return;
      dl.current = p;
      renderCurrent();
      renderQueueLog();
    });
    cbApi.on('progress.overall', (p) => {
      if (!dl.running) return;
      dl.overall = p;
      renderOverall();
      renderPanelBatchMini();
      renderOverviewRunning();
    });
    cbApi.on('queue.row', (r) => {
      if (!dl.running) return;
      dl.rows[r.id] = { state: r.state, title: r.title, detail: r.detail };
      renderBatch();
    });
    cbApi.on('batch.finished', async (r) => {
      dl.running = false;
      dl.paused = false;
      dl.rows = {};
      dl.current = null;
      dl.overall = null;
      const parts = `${num(r.downloaded)} downloaded, ${num(r.skipped)} skipped, ` +
        `${num(r.errors)} error${r.errors === 1 ? '' : 's'}`;
      toast((r.cancelled ? 'Batch cancelled — ' : 'Batch finished — ') + parts,
        !r.cancelled && r.errors > 0);
      await refresh();
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

  async function boot() {
    await cbApi.connect();
    const strings = await call('ui_strings');
    TOOLTIPS = strings.tooltips || {};
    SETTINGS_KEYS = strings.settings_keys || [];
    wire();
    subscribeDownloadEvents();
    await refresh();
    bindTips(document);
    show(location.hash.slice(1) || 'overview');

    cbApi.on('host.status', (s) => {
      if (!state) return;
      state.host.online = !!s.online;
      renderShell();
      document.body.classList.toggle('cb-offline', !s.online);
    });
  }

  boot().catch((err) => {
    console.error(err);
    toast('Could not reach the host process.', true);
  });
})();
