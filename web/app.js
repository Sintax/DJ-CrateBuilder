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
  const SCREENS = ['overview', 'downloads', 'watchlist', 'settings'];

  function show(name) {
    if (!SCREENS.includes(name)) name = 'overview';
    $$('.cb-screen').forEach((s) => s.classList.toggle('is-on', s.id === 'screen-' + name));
    $$('.cb-nav').forEach((a) => a.classList.toggle('is-on', a.dataset.screen === name));
    $('.cb-main').scrollTop = 0;
    if (location.hash.slice(1) !== name) location.hash = name;
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

  /* ── settings ──────────────────────────────────────────────────────────── */
  function control(entry, value, available) {
    const wrap = document.createElement('div');
    const reason = 'This option is not wired into the web frontend yet — ' +
                   'change it in the desktop app for now.';

    function mark(el) {
      if (!available) {
        el.disabled = true;
        el.setAttribute('data-tt-text',
          (entry.tooltip && TOOLTIPS[entry.tooltip] ? TOOLTIPS[entry.tooltip] + '\n\n' : '') + reason);
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

  async function save(key, value, el) {
    try {
      const res = await cbApi.call('settings.set', { key, value });
      state.settings[key] = res.value;
      toast(`Saved ${key}`);
    } catch (err) {
      toast(err.userFacing ? err.message : `Could not save ${key}`, true);
      if (el && el.type === 'checkbox') el.checked = !el.checked;
    }
  }

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
