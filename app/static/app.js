'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const app = { csrf: null, state: null, config: null, saved: null, clients: [], view: 'dashboard', pageSize: 100, page: 1, sortKey: null, sortDirection: 'asc', jsonDirty: false };
  const soloSort = {key: null, direction: 'asc'};
  const homeSort = {key: null, direction: 'asc'};
  const views = {
    dashboard: ['Accueil', 'Activité des simulations et torrents suivis.'],
    torrents: ['Torrents suivis', 'Activité, pairs et règles par torrent.'],
    swarm: ['Relevés autonomes', 'Scrapes directs des trackers, sans requêtes à qBittorrent.'],
    import: ['Bibliothèque JOAL uniquement', 'Fichiers .torrent conservés dans /data/torrents sans passer par qBittorrent.'],
    rules: ['Règles & trackers', 'Configuration visuelle des critères par tracker.'],
    cleanup: ['Nettoyage & historique', 'Compteur basé sur des relevés frais des trackers.'],
    settings: ['Paramètres', 'Comportement et fréquence des contrôles.'],
    diagnostics: ['Diagnostics & historique', 'État du système, événements et sessions JOAL enregistrées.'],
    json: ['Configuration avancée', 'Modification directe de policy.json.'],
    health: ['Santé', 'Threads de surveillance, journaux, stockage et dossier surveillé.'],
    backup: ['Sauvegardes persistantes', 'Archivage et restauration contrôlée des données de /data.']
  };
  const deepcopy = obj => JSON.parse(JSON.stringify(obj));
  const number = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  const str = x => x == null ? '' : String(x);
  const newEl = (name, className, txt) => {
    const e = document.createElement(name);
    if (className) e.className = className;
    if (txt != null) e.textContent = String(txt);
    return e;
  };
  function clear(el) { el.replaceChildren(); }
  // Readable label for internal Forced rule ids (':forced', ':forced-idle', 'solo-:forced'…).
  function ruleLabel(id) {
    const base = String(id || '').replace(/^solo-/, '');
    if (base === ':forced') return 'Mode Forced';
    if (base === ':forced-idle') return 'Mode Forced (sans upload)';
    return id;
  }
  function badge(label, type) { return newEl('span', 'badge badge-' + (type || 'neutral'), label); }
  function showToast(message, isError = false) {
    const e = $('toast');
    e.textContent = message;
    e.className = 'toast show' + (isError ? ' error' : '');
    clearTimeout(showToast.timer);
    showToast.timer = setTimeout(() => { e.className = 'toast'; }, 4500);
  }
  async function api(path, options = {}) {
    const request = { cache: 'no-store', credentials: 'same-origin', ...options };
    if (request.method && request.method.toUpperCase() !== 'GET') {
      request.headers = {...(request.headers || {}), 'X-CSRF-Token': app.csrf || ''};
    }
    const res = await fetch(path, request);
    if (res.status === 401) { location.replace('/login'); throw new Error('Session expirée'); }
    const contentType = res.headers.get('content-type') || '';
    const data = contentType.includes('application/json') ? await res.json() : null;
    if (!res.ok) throw new Error((data && data.error) || ('Réponse HTTP ' + res.status));
    return data;
  }
  function dirty() { return app.config && app.saved && JSON.stringify(app.config) !== JSON.stringify(app.saved); }
  function updateDirty() {
    const msg = dirty() ? 'Modifications non enregistrées' : 'Toutes les modifications sont enregistrées';
    $('rule-save-status').textContent = msg;
    $('settings-save-status').textContent = msg;
  }
  function changeView(target) {
    if (target === app.view) return;
    if (app.view === 'json' && target !== 'json' && app.jsonDirty && !confirm('Abandonner les modifications JSON non enregistrées ?')) return;
    app.view = target;
    if (target === 'dashboard') { renderOverview(); refreshChart(); }
    $('view-picker').value = target;
    const activeGroup = document.querySelector('.nav-link[data-view="' + target + '"]')?.closest('details');
    if (activeGroup) activeGroup.open = true;
    window.scrollTo(0, 0);
    for (const name of Object.keys(views)) $('view-' + name).classList.toggle('hidden', target !== name);
    for (const nav of document.querySelectorAll('[data-view]')) nav.classList.toggle('active', nav.classList.contains('nav-link') && nav.dataset.view === target);
    $('page-title').textContent = views[target][0];
    $('page-subtitle').textContent = views[target][1];
    if (target === 'rules') renderRuleEditor();
    if (target === 'settings') { renderSettings(); if (!qbitReady) loadQbit(); }
    if (target === 'torrents') { renderTorrents(); requestAnimationFrame(updateTorrentScrollbar); }
    if (target === 'cleanup') renderCleanup();
    if (target === 'swarm') renderSwarm();
    if (target === 'import') renderSolo();
    if (target === 'diagnostics') renderDiagnostics();
    if (target === 'health') renderHealth();
    if (target === 'json') { $('json-editor').value = JSON.stringify(app.config || {}, null, 2); app.jsonDirty = false; $('json-save-status').textContent = 'En attente de modifications'; }
  }
  const formatTime = iso => {
    if (!iso) return 'Pas encore de synchronisation';
    const time = new Date(iso);
    return Number.isNaN(time.getTime()) ? 'Date inconnue' : time.toLocaleString('fr-BE', { dateStyle: 'short', timeStyle: 'medium' });
  };
  function ruleStatusType(s) {
    if (/erreur|impossible/i.test(s)) return 'bad';
    if (/désactiv|aucun|attente|simulation/i.test(s)) return 'warn';
    return 'good';
  }
  function formatJoalSpeed(rate) {
    if (rate == null) return 'Indisponible';
    if (rate === 0) return '0 o/s';
    const unit = rate >= 1e6 ? 'Mo/s' : rate >= 1000 ? 'ko/s' : 'o/s';
    const scale = unit === 'Mo/s' ? 1e6 : unit === 'ko/s' ? 1000 : 1;
    return (rate / scale).toLocaleString('fr-BE', {maximumFractionDigits: 2}) + ' ' + unit;
  }
  const baseTitle = document.title;
  const chart = {end: null, serverNow: null, earliest: null, points: [], revision: 0, busy: false};
  const chartClock = new Intl.DateTimeFormat('fr-BE', {hour: '2-digit', minute: '2-digit'});
  function chartDate(time) {
    const date = new Date(time * 1000);
    return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  }
  function selectChart(end) {
    chart.end = end;
    chart.points = [];
    chart.revision++;
    chart.busy = false;
    $('upload-line').setAttribute('d', '');
    $('chart-max').textContent = '—';
    $('chart-period').textContent = 'Chargement…';
    $('chart-live').setAttribute('aria-pressed', String(end == null));
    refreshChart();
  }
  async function refreshChart() {
    if (app.view !== 'dashboard' || chart.busy || document.hidden) return;
    chart.busy = true;
    const revision = chart.revision;
    try {
      const last = chart.points.at(-1);
      const query = chart.end != null ? '?end=' + chart.end : last ? '?since=' + last[0] : '';
      const result = await api('/api/upload-history' + query);
      if (revision !== chart.revision) return;
      chart.earliest = result.earliest;
      if (chart.end == null) chart.serverNow = result.end;
      else chart.end = result.end;
      const keep = chart.end == null && last ? chart.points.filter(p => p[0] >= result.end - 3600 && p[0] < last[0]) : [];
      chart.points = keep.concat(result.points);
      renderUploadHistory(chart.points, result.end);
      const date = $('chart-date');
      if (document.activeElement !== date) date.value = chartDate(result.end);
      date.min = chartDate(result.earliest + 3600);
      date.max = chartDate(result.earliest + 7 * 86400);
      $('chart-prev').disabled = result.end <= result.earliest + 3600;
      $('chart-next').disabled = chart.end == null;
      $('chart-period').textContent = (chart.end == null ? 'En direct' : 'Historique') +
        (chart.points.some(p => p[1] != null) ? '' : ' · aucune mesure disponible');
    } catch (_) {
      if (revision === chart.revision) $('chart-period').textContent = 'Relevé indisponible';
    } finally {
      if (revision === chart.revision) chart.busy = false;
    }
  }
  function renderUploadHistory(points, end) {
    const start = end - 3600;
    for (const [id, t] of [['chart-start', start], ['chart-middle', end - 1800], ['chart-end', end]]) {
      $(id).textContent = chartClock.format(t * 1000);
    }
    const visible = points.filter(([time]) => time >= start);
    const rates = visible.map(point => point[1]).filter(Number.isFinite);
    const ceiling = Math.max(1, ...rates);
    $('chart-max').textContent = rates.length ? formatJoalSpeed(ceiling) : '—';
    let previous = null;
    const path = [];
    for (const [time, rate] of visible) {
      if (!Number.isFinite(rate)) { previous = null; continue; }
      const x = (time - start) / 5;
      const y = 78 - 76 * rate / ceiling;
      const newSegment = previous == null || time - previous > 3;
      path.push((newSegment ? 'M' : 'L') + x.toFixed(2) + ' ' + y.toFixed(2));
      if (newSegment) path.push('l0 0');
      previous = time;
    }
    $('upload-line').setAttribute('d', path.join(' '));
  }
  function renderStatus() {
    const data = app.state || {};
    const conf = app.config || { rules: [] };
    const totals = data.totals || {};
    const failed = !!data.error || !data.qbitConnected;
    $('alert').classList.toggle('hidden', !data.error);
    $('alert').textContent = data.error ? 'Surveillance interrompue : ' + data.error : '';
    $('side-dot').className = 'pulse' + (failed ? ' bad' : ' good');
    $('side-connection').textContent = failed ? ((data.solo || {}).count ? 'Bibliothèque JOAL autonome · qBit indisponible' : 'qBittorrent indisponible') : 'qBittorrent connecté';
    $('side-last').textContent = 'Inventaire : ' + formatTime(data.inventoryTime || data.time);
    const modeLabel = data.dryRun ? 'SIMULATION LOCALE' : 'MODE JOAL';
    $('mode-badge').textContent = modeLabel;
    $('mode-badge').className = 'badge badge-' + (data.dryRun ? 'warn' : 'good');
    $('mode-title').textContent = data.dryRun ? 'Mode simulation locale' : 'Mode JOAL configuré';
    $('mode-desc').textContent = data.dryRun
      ? 'Aucune annonce JOAL : la sélection affichée est une prévisualisation. Les vitesses sont des consignes, pas des transferts.'
      : 'Les états de processus JOAL sont affichés ci-dessous. Cette interface ne mesure pas les transferts réels ni les statistiques acceptées par les trackers.';
    const paused = data.simulationsPaused;
    $('pause-all').disabled = typeof paused !== 'boolean' || !!app.pauseBusy;
    $('pause-all').textContent = paused ? 'Reprendre les simulations' : 'Mettre tout JOAL en pause';
    $('pause-state').textContent = paused ? 'Simulations en pause' : data.dryRun ? 'Prévisualisation locale' : 'Simulations autorisées';
    if (paused) $('mode-badge').textContent = 'JOAL EN PAUSE';
    const joalTotal = formatJoalSpeed(data.joalUpload?.bytesPerSecond);
    document.title = '↑ ' + joalTotal + ' · ' + baseTitle;

    $('kpi-joal-upload').textContent = joalTotal;
    $('torrent-joal-upload').textContent = joalTotal;
    $('kpi-tagged').textContent = str(totals.tagged ?? '—');
    $('kpi-solo').textContent = str((data.solo || {}).count ?? 0);
    $('kpi-selected').textContent = str(totals.selected ?? '—');
    $('kpi-eligible').textContent = str(totals.eligible ?? '—');
    $('kpi-blocked').textContent = str(totals.blocked ?? '—');
    $('inventory-time').textContent = formatTime(data.inventoryTime);
    $('evaluation-time').textContent = formatTime(data.evaluationTime);
    const count = conf.rules.filter(r => r.enabled).length;
    $('kpi-rules').textContent = count + '/' + conf.rules.length;
    if (app.view === 'dashboard') renderOverview();
    if (app.view === 'torrents') renderTorrents();
    if (app.view === 'cleanup') renderCleanup();
    if (app.view === 'swarm') renderSwarm();
    if (app.view === 'import') renderSolo();
  }
  function withActivity(row) {
    if (row.importBlocked) return {...row, simulationActive: false};
    const live = app.state?.joalUpload?.byInfoHash;
    return {...row, simulationActive: live ? Object.hasOwn(live, row.infoHash) :
      Boolean(row.active || row.joalSession?.active)};
  }
  function allTrackedRows() {
    const state = app.state || {};
    const qbit = (state.torrents || []).map(t => ({...t, mode: 'qbit'}));
    const solo = ((state.solo || {}).items || []).map(t => ({
      ...t, mode: 'solo', hash: t.hash || str(t.infoHash).slice(0, 12),
      trackers: t.trackers || [], stopped: true, progress: null,
      eligible: !!t.selected, displayLeechers: t.leechers,
      displaySeeders: t.seeders, seederSource: t.seederSource || 'scrape autonome',
      displaySource: t.source || 'scrape autonome',
      leecherSampledAt: t.sampledAt || null, ratio: null,
      targetRatio: null, upload: {uploadedBytes:null, targetBytes:null, percent:null},
      addedOn: t.addedOn, qbitState: null,
      checks: [
        {label:'Torrent dans la bibliothèque JOAL uniquement',result:true},
        {label:'Règle correspondante',result:!!t.rule},
        {label:'Statistique du tracker disponible',result:t.threshold === 0 ? true : t.leechers == null ? null : true,
          detail:t.threshold === 0 ? 'Facultative : contrôle désactivé' : (t.leechers == null ? 'Relevé absent ou invalide' : String(t.leechers))},
        {label:'Minimum de leechers atteint',result:t.threshold === 0 ? true : t.leechers == null || t.threshold == null ? null : t.leechers >= t.threshold,
          detail: t.threshold === 0 ? 'Désactivé par la règle (minimum 0)' : (t.leechers == null ? 'Relevé absent ou invalide' : t.leechers + ' / minimum ' + t.threshold)},
        {label:'Session JOAL active',result:!!t.active}
      ]
    }));
    return qbit.concat(solo).map(item => {
      const row = withActivity(item);
      const peers = displayedPeers(row);
      return peers?.leechers == null ? row : {...row, joalLeechers: peers.leechers};
    });
  }
  function trackerDiagnosticFor(t) {
    const record = ((app.state?.swarm || {}).items || []).find(x => x.infoHash === t.infoHash);
    const rule = t.rule || '';
    const fromMonitor = record?.rules?.[rule];
    if (t.trackerDiagnostics?.length && (t.mode === 'solo' || t.mode === 'JOAL uniquement')) {
      return {...fromMonitor, attemptedAt:t.peerAttempt, sampledAt:t.sampledAt ||
        Math.max(0, ...Object.values(t.lastPeers || {}).map(p => p.time * 1000)),
        trackerDiagnostics:t.trackerDiagnostics};
    }
    return fromMonitor || {status:'en attente', attemptedAt:null,
      trackerDiagnostics:t.trackerDiagnostics || [],trackerCount:(t.trackers || []).length,successCount:0};
  }
  function openTrackerDiagnostic(t) {
    const obs = trackerDiagnosticFor(t);
    const dialog = newEl('dialog', 'assign-trackers-dialog tracker-diagnostic-dialog');
    dialog.append(newEl('h3', '', 'Diagnostic des trackers'));
    dialog.append(newEl('p', 'muted', t.name));
    dialog.append(newEl('p', 'muted', 'Règle : ' + (ruleLabel(t.rule) || 'aucune') +
      ' · Dernière tentative : ' + formatTime(obs.attemptedAt) +
      ' · Dernier relevé valide : ' + formatTime(obs.sampledAt)));
    const policy = app.config?.rules?.find(r => r.id === (app.config?.torrentRuleOverrides?.[t.infoHash] || t.rule));
    const announcements = app.state?.joalUpload?.announcements?.[t.infoHash];
    if (announcements || policy?.maxUploadRate === 0) {
      dialog.append(newEl('h4', '', 'Annonce de présence · session en cours'));
      if (announcements && !t.importBlocked) {
        for (const [host, attempt] of Object.entries(announcements)) {
          dialog.append(newEl('p', '', host + ' · ' + attempt.result + ' · ' + formatTime(attempt.time * 1000)));
        }
      } else {
        dialog.append(newEl('p', 'muted', 'Aucune réponse d’annonce enregistrée pour cette session. ' + (t.reason || '')));
      }
      dialog.append(newEl('h4', '', 'Relevé des compteurs (scrape)'));
    }
    const list = newEl('div', 'tracker-diagnostic-list');
    const items = Array.isArray(obs.trackerDiagnostics) ? obs.trackerDiagnostics : [];
    if (items.length) {
      for (const item of items) {
        const line = newEl('div', 'tracker-diagnostic-line');
        line.append(newEl('strong', '', item.domain || 'Domaine inconnu'));
        const success = item.state === 'ok';
        line.append(badge(success ? ('S ' + (item.seeders == null ? '?' : item.seeders) + ' · L ' + (item.leechers == null ? '?' : item.leechers)) : 'Indisponible',
          success ? 'good' : 'warn'));
        line.append(newEl('small', 'muted', item.reason || 'Réponse non disponible'));
        list.append(line);
      }
    } else {
      const hosts = t.trackers || [];
      list.append(newEl('div', 'empty', hosts.length
        ? 'Aucun diagnostic détaillé enregistré. Prochaine tentative prévue selon l’intervalle configuré.'
        : 'Aucun tracker détecté.'));
    }
    dialog.append(list, newEl('p', 'muted',
      'Les informations sont des relevés de lecture : un tracker qui refuse le scrape peut malgré tout répondre aux annonces BitTorrent. Aucun compteur inconnu n’est remplacé par zéro.'));
    const close = newEl('button', 'btn btn-primary', 'Fermer'); close.type = 'button';
    close.addEventListener('click', () => dialog.close());
    dialog.append(close);
    dialog.addEventListener('close', () => dialog.remove());
    document.body.append(dialog); dialog.showModal();
  }
  function diagnosticButton(t) {
    const button = newEl('button', 'btn btn-compact btn-outline tracker-diagnostic-button', 'Diagnostic trackers');
    button.type = 'button';
    const outcomes = Object.values(app.state?.joalUpload?.announcements?.[t.infoHash] || {});
    if (outcomes.length) {
      const ok = outcomes.filter(item => item.result === 'Acceptée').length;
      const failed = outcomes.length - ok;
      button.textContent = [ok && (ok + ' acceptée' + (ok > 1 ? 's' : '')),
        failed && (failed + ' en échec')].filter(Boolean).join(' · ');
      button.title = 'Voir les annonces par tracker';
    }
    button.addEventListener('click', () => openTrackerDiagnostic(t));
    return button;
  }
  function displayedPeers(t) {
    const live = app.state?.joalUpload?.peers?.[t.infoHash] || {};
    const saved = t.lastPeers || {};
    const notes = [];
    const times = {};
    function choose(key, fallback) {
      const candidates = [
        {count: fallback, time: Date.parse(t.sampledAt || t.leecherSampledAt) || 0, source:t.source || 'Relevé'},
        saved[key] && {...saved[key], time:saved[key].time * 1000},
        {count: live[key], time:(app.state?.joalUpload?.peerTimes?.[t.infoHash] || 0) * 1000, source:'JOAL'}
      ].filter(x => x && Number.isInteger(x.count) && x.count >= 0);
      if (!candidates.length && Number.isInteger(t.qbitCachedPeers?.[key])) {
        notes.push(key + ' · cache qBittorrent (date du relevé inconnue)');
        return t.qbitCachedPeers[key];
      }
      if (!candidates.length) return null;
      const best = candidates.reduce((a,b) => b.time >= a.time ? b : a);
      if (best.time) {
        times[key] = best.time;
        notes.push(key + ' · ' + best.source + ' · ' + formatTime(best.time));
      }
      return best.count;
    }
    const seeders = choose('seeders', t.displaySeeders ?? t.seeders);
    const leechers = choose('leechers', t.displayLeechers ?? t.leechers);
    return {seeders, leechers, notes, times};
  }
  function swarmSummary(t) {
    const peers = app.state?.joalUpload?.peers?.[t.infoHash] || {};
    const {seeders: s, leechers: l, notes, times} = displayedPeers(t);
    const source = t.seederSource || t.displaySource || 'inconnu';
    const span = newEl('span', 'swarm-counters',
      'S ' + (s == null ? '?' : String(s)) + ' · L ' + (l == null || l < 0 ? '?' : String(l)));
    span.title = 'S = seeders · L = leechers. Source seeders : ' + source +
      '. Les nombres peuvent provenir de trackers différents ou de données qBittorrent non datées.';
    if (peers.seeders != null || peers.leechers != null) {
      span.title = 'Dernière réponse du tracker à JOAL pour les compteurs disponibles. Les autres proviennent des relevés existants.';
    }
    const imported = t.mode === 'solo' || t.mode === 'JOAL uniquement';
    if (notes.length) {
      const dates = [...new Set(Object.values(times))];
      const label = !dates.length ? 'Cache qBittorrent · non daté' : imported ? (dates.length === 1 ? 'Relevé · ' + formatTime(dates[0]) :
        ['seeders','leechers'].filter(k => times[k]).map(k => (k === 'seeders' ? 'S' : 'L') + ' · ' + formatTime(times[k])).join(' / ')) : 'Dernier relevé';
      span.append(newEl('small', 'peer-history-label', label));
      span.title = notes.join(' · ');
    }
    if (imported) {
      const check = app.config?.swarm?.enabled === false ? 'Relevés autonomes désactivés' :
        t.peerCheckStatus === 'indisponible' && t.peerAttempt ? 'Relevé indisponible · ' + formatTime(t.peerAttempt) :
        !notes.length && !t.peerAttempt ? 'En attente du premier relevé' : '';
      if (check) span.append(newEl('small', 'peer-history-label', check));
      if (t.peerCheckStatus === 'indisponible' && (s == null || l == null)) {
        const failed = (t.trackerDiagnostics || []).find(item => item.state === 'error' && item.reason);
        if (failed) span.append(newEl('small', 'peer-history-label', failed.domain + ' · ' + failed.reason));
      }
      if (t.peerAttempt) span.title += ' · Dernière tentative de scrape : ' + formatTime(t.peerAttempt);
    }
    if (s == null || l == null) span.title += ' ? indique un compteur indisponible.';
    return span;
  }
  function renderOverview() {
    const data = app.state || {};
    const conf = app.config || { rules: [] };
    const statuses = new Map((data.rules || []).map(r => [r.id, r]));
    const grid = $('overview-rules'); clear(grid);
    if (!conf.rules.length) grid.append(newEl('div', 'empty', 'Aucune règle configurée. Ajoute une règle depuis le menu Règles & trackers.'));
    for (const rule of conf.rules) {
      const current = statuses.get(rule.id) || { state: rule.enabled ? 'En attente de synchronisation' : 'désactivée', eligible: 0 };
      const card = newEl('article', 'rule-card');
      const head = newEl('div', 'rule-head');
      head.append(newEl('h3', '', rule.id), badge(current.state, rule.enabled ? ruleStatusType(current.state) : 'neutral'));
      card.append(head, newEl('p', '', (rule.trackerDomains || []).join(' · ') || 'Aucun domaine'));
      const meta = newEl('div', 'rule-meta');
      for (const text of [
        `${current.eligible || 0} retenu(s)`,
        `Ratio local cible : ${rule.targetRatio || 'désactivé'}`,
        `≥ ${rule.minLeechers} leechers`,
        `${rule.minUploadRate}–${rule.maxUploadRate} kB/s · réglage`,
        `Limite règle ${rule.simultaneousSeed} · globale ${conf.maxCandidates}`
      ]) meta.append(newEl('span', '', text));
      card.append(meta); grid.append(card);
    }
    $('overview-reset-sort').hidden = !homeSort.key;
    const body = $('overview-items'); clear(body);
    const all = allTrackedRows();
    const search = $('overview-search').value.trim().toLocaleLowerCase();
    const visible = all.filter(t => !search || [t.name, t.rule, ...(t.trackers || [])].some(v => str(v).toLocaleLowerCase().includes(search)));
    setSelectionScope('dashboard', visible);
    $('overview-count').textContent = visible.length + ' torrent(s) · ' + (data.torrents || []).length +
      ' qBittorrent · ' + ((data.solo || {}).count || 0) + ' JOAL uniquement';
    if (!visible.length) return tableEmpty(body, 6, 'Aucun torrent trouvé dans les bibliothèques.');
    const upload = data.joalUpload;
    const rows = visible.map(t => {
      const peers = displayedPeers(t);
      const rate = upload && Object.hasOwn(upload.byInfoHash, t.infoHash) ? upload.byInfoHash[t.infoHash] : upload ? 0 : null;
      return {...t, rate, volume: upload?.totalUploadedBytes?.[t.infoHash] ?? upload?.uploadedBytes?.[t.infoHash],
        seeders: peers?.seeders ?? t.displaySeeders ?? t.seeders,
        homeLeechers: peers?.leechers ?? t.displayLeechers ?? t.leechers};
    });
    for (const t of TorrentTable.sortRows(rows, homeSort.key, homeSort.direction)) {
      const tr = newEl('tr');
      tr.append(torrentName(t), cell(t.mode === 'solo' ? 'JOAL uniquement' : 'qBittorrent'),
        ruleCell(t), (()=>{const td=newEl('td');td.append(swarmSummary(t));return td;})(),
        statusCell(t));
      const diag = newEl('td'); diag.append(diagnosticButton(t), peerChartButton(t)); tr.append(diag);
      body.append(tr);
    }
  }
  function cell(value) { const td = newEl('td', '', value); td.title = str(value); return td; }
  const selectedModes = new Set();
  const selectionScopes = new Map();
  function updateToggle(button) {
    const hashes = selectionScopes.get(button.dataset.toggleAll) || [];
    const all = hashes.length > 0 && hashes.every(h => selectedModes.has(h));
    button.textContent = all ? 'Désélectionner tout' : 'Sélectionner tout';
    button.disabled = modeBusy || !hashes.length;
    button.title = 'Tous les torrents correspondant aux filtres de cette vue, toutes pages comprises';
  }
  function setSelectionScope(view, rows) {
    selectionScopes.set(view, [...new Set(rows.map(row => row.infoHash).filter(Boolean))]);
    updateToggle(document.querySelector('[data-toggle-all="' + view + '"]'));
  }
  let modeBusy = false;
  // Only torrents of the JOAL-only library can be removed; qBittorrent ones are never touched.
  function selectedLibraryHashes() {
    const library = new Set((((app.state || {}).solo || {}).items || []).map(row => row.infoHash));
    return [...selectedModes].filter(h => library.has(h));
  }
  async function removeSelectedLibrary() {
    const hashes = selectedLibraryHashes();
    if (modeBusy || !hashes.length) return;
    const others = selectedModes.size - hashes.length;
    const note = others ? `\n\n${others} torrent(s) qBittorrent de la sélection ne seront pas touchés.` : '';
    if (!confirm(`Retirer ${hashes.length} torrent(s) importé(s) de JOAL ? Les fichiers .torrent seront supprimés de la bibliothèque, jamais les données qBittorrent.${note}`)) return;
    modeBusy = true;
    for (const h of hashes) removing.add(h);
    syncModeControls(); renderSolo();
    try {
      const result = await api('/api/solo/delete', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({infoHashes:hashes})});
      for (const h of result.removed || []) { applyLibraryEdit([], h); selectedModes.delete(h); }
      const missing = (result.missing || []).length;
      showToast(`${(result.removed || []).length} fichier(s) .torrent retiré(s).` + (missing ? ` ${missing} déjà absent(s).` : ''));
    } catch (error) { showToast(error.message, true); }
    finally {
      for (const h of hashes) removing.delete(h);
      modeBusy = false; syncModeControls(); renderSolo(); void fetchStatus();
    }
  }
  function syncModeControls() {
    for (const button of document.querySelectorAll('[data-toggle-all]')) updateToggle(button);
    for (const box of document.querySelectorAll('input[data-mode-hash]')) {
      box.checked = selectedModes.has(box.dataset.modeHash);
      box.disabled = modeBusy;
    }
    for (const label of document.querySelectorAll('.mode-count')) label.textContent = selectedModes.size + ' sélectionné(s)';
    for (const button of document.querySelectorAll('[data-batch-mode],[data-batch-rule],[data-clear-mode]')) {
      button.disabled = modeBusy || !selectedModes.size;
    }
    const removable = selectedLibraryHashes();
    for (const button of document.querySelectorAll('[data-batch-remove]')) {
      button.textContent = removable.length ? `Retirer de JOAL (${removable.length})` : 'Retirer de JOAL';
      button.disabled = modeBusy || !removable.length;
    }
    for (const select of document.querySelectorAll('.torrent-mode-choice')) select.disabled = modeBusy;
  }
  async function applySimulationMode(hashes, mode, bulk) {
    if (modeBusy || !hashes.length) return;
    modeBusy = true;
    syncModeControls();
    try {
      await api('/api/torrent-modes', {method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({hashes, mode})});
      if (bulk) selectedModes.clear();
      await fetchStatus();
      showToast('Mode ' + mode + ' enregistré pour ' + hashes.length + ' torrent(s).');
    } catch (error) { showToast(error.message, true); await fetchStatus(); }
    finally { modeBusy = false; syncModeControls(); }
  }
  function modeControls(t) {
    const wrap = newEl('div','torrent-mode-controls');
    const box = document.createElement('input');
    box.type = 'checkbox'; box.dataset.modeHash = t.infoHash;
    box.checked = selectedModes.has(t.infoHash); box.disabled = modeBusy;
    box.setAttribute('aria-label', 'Sélectionner ' + t.name);
    box.addEventListener('change', () => {
      if (box.checked) selectedModes.add(t.infoHash); else selectedModes.delete(t.infoHash);
      syncModeControls();
    });
    const select = newEl('select','torrent-mode-choice');
    select.setAttribute('aria-label','Mode de simulation de ' + t.name);
    for (const mode of ['auto','forced','stop']) select.add(new Option(mode[0].toUpperCase()+mode.slice(1),mode));
    select.value = app.state?.simulationModes?.[t.infoHash] || 'auto';
    select.disabled = modeBusy;
    select.addEventListener('change', () => applySimulationMode([t.infoHash], select.value, false));
    wrap.append(box, select);
    return wrap;
  }
  function torrentName(t, showMeasurement = true) {
    const td = newEl('td');
    const title = newEl('span', 'torrent-title', t.name);
    title.title = str(t.name);
    if (t.simulationActive) td.classList.add('active-torrent');
    td.append(modeControls(t), title, newEl('span', 'torrent-sub', t.mode === 'solo'
      ? 'JOAL uniquement · .torrent conservé dans /data/torrents · ' + str(t.hash || '')
      : str(t.qbitState || 'inconnu') + ' · ' + str(t.progress) + '% · ' + str(t.hash || '')));
    if (showMeasurement) td.append(joalMeasurement(t));
    return td;
  }
  function joalMeasurement(t) {
    if (t.importBlocked) return newEl('strong', 'upload-joal-status', 'Simulation importée bloquée · Fichiers : ' + formatBytes(t.sizeBytes));
    const upload = app.state?.joalUpload;
    const rate = upload ? (Object.hasOwn(upload.byInfoHash, t.infoHash) ? upload.byInfoHash[t.infoHash] : 0) : null;
    const volume = upload?.uploadedBytes?.[t.infoHash];
    const text = 'JOAL · ' + formatJoalSpeed(rate) + (volume == null ? (upload && !Object.hasOwn(upload.byInfoHash, t.infoHash) ? ' · Session arrêtée' : ' · Volume session : —') : ' · ' + formatBytes(volume) + ' simulés cette session');
    const size = t.sizeBytes ?? t.upload?.contentSizeBytes ?? t.upload?.sizeBytes;
    const cumulative = upload?.totalUploadedBytes;
    const total = cumulative == null ? null : cumulative[t.infoHash] ?? 0;
    const totalLabel = ' · Total simulé : ' + (total == null ? '—' : formatBytes(total));
    const totalRatio = total != null && size > 0
      ? ' · Ratio total ' + (total / size).toLocaleString('fr-FR', {maximumFractionDigits: 3}) + ' ×' : '';
    const ratio = volume != null && size > 0
      ? ' · Ratio session ' + (volume / size).toLocaleString('fr-FR', {maximumFractionDigits: 3}) + ' ×'
      : '';
    const status = newEl('strong', 'upload-joal-status', text + ' · Fichiers : ' + formatBytes(size) + ratio + totalLabel + totalRatio);
    if (!t.uploadHint) return status;
    const box = newEl('span', 'upload-joal-wrap');
    box.append(status, newEl('span', 'upload-hint', t.uploadHint));
    return box;
  }
  let statusTooltip = null;
  let tooltipHideTimer = null;
  function closeStatusTooltip() {
    clearTimeout(tooltipHideTimer);
    if (statusTooltip) statusTooltip.hidden = true;
  }
  function scheduleTooltipClose() {
    clearTimeout(tooltipHideTimer);
    tooltipHideTimer = setTimeout(closeStatusTooltip, 160);
  }
  function showStatusTooltip(t, trigger) {
    clearTimeout(tooltipHideTimer);
    if (!statusTooltip) {
      statusTooltip = newEl('aside', 'status-inspector');
      statusTooltip.id = 'status-inspector';
      statusTooltip.setAttribute('role', 'tooltip');
      statusTooltip.hidden = true;
      statusTooltip.addEventListener('mouseenter', () => clearTimeout(tooltipHideTimer));
      statusTooltip.addEventListener('mouseleave', scheduleTooltipClose);
      document.body.append(statusTooltip);
    }
    clear(statusTooltip);
    statusTooltip.append(newEl('strong', 'inspector-title', t.rule ? 'Vérification · ' + ruleLabel(t.rule) : 'Vérification · aucune règle'));
    statusTooltip.append(newEl('p', 'inspector-reason', t.reason || 'Évaluation en attente'));
    const checks = Array.isArray(t.checks) && t.checks.length ? t.checks : [
      {label:'Torrent complet', result:t.progress === 100, detail:'100 % requis'},
      {label:'qBittorrent arrêté', result:!!t.stopped},
      {label:'Évaluation des règles', result:null, detail:'En attente de résultats détaillés'}
    ];
    const list = newEl('ul', 'inspector-checks');
    for (const check of checks) {
      const li = newEl('li', check.result === true ? 'check-pass' : check.result === false ? 'check-fail' : 'check-unknown');
      const icon = newEl('span', 'inspector-check-icon', check.result === true ? '✓' : check.result === false ? '✕' : '?');
      const content = newEl('span');
      content.append(newEl('strong', '', check.label || 'Condition'));
      if (check.detail) content.append(newEl('small', '', check.detail));
      li.append(icon, content); list.append(li);
    }
    statusTooltip.append(list);
    if (Array.isArray(t.trackerDetails) && t.trackerDetails.length) {
      statusTooltip.append(newEl('div', 'inspector-footer', 'Trackers détectés : ' + t.trackerDetails.map(x =>
        `${x.domain} (${x.leechers == null ? 'leechers inconnus' : x.leechers + ' leechers'}${x.status == null ? '' : ', statut ' + x.status})`).join(' · ')));
    }
    if (t.displaySource) statusTooltip.append(newEl('div', 'inspector-footer', 'Source des leechers affichés : ' + t.displaySource + (t.leecherSampledAt ? ' · relevé ' + formatTime(t.leecherSampledAt) : '')));
    statusTooltip.append(newEl('div', 'inspector-footer', 'Dernière évaluation : ' + formatTime(t.mode === 'solo' ? app.state?.solo?.updatedAt : app.state?.evaluationTime)));
    statusTooltip.hidden = false;
    const box = trigger.getBoundingClientRect();
    const width = Math.min(420, Math.max(270, window.innerWidth - 20));
    statusTooltip.style.width = width + 'px';
    statusTooltip.style.left = Math.max(8, Math.min(box.left, window.innerWidth - width - 8)) + 'px';
    statusTooltip.style.top = Math.max(8, Math.min(box.bottom + 7, window.innerHeight - statusTooltip.offsetHeight - 8)) + 'px';
  }
  function hostCovered(host, rule) {
    const normalized = String(host || '').toLowerCase().replace(/\.$/, '');
    return (rule.trackerDomains || []).some(pattern => {
      const value = String(pattern || '').toLowerCase().replace(/\.$/, '');
      return !!value && (normalized === value || normalized.endsWith('.' + value));
    });
  }
  function ruleCell(t) {
    const td = newEl('td', 'rule-choice-cell');
    const chosen = app.config?.torrentRuleOverrides?.[t.infoHash];
    const assigned = chosen || ruleLabel(t.rule) || 'Aucune règle';
    const button = newEl('button', 'rule-choice' + (chosen ? ' rule-choice-manual' : ''), assigned);
    button.type = 'button';
    button.title = chosen ? 'Règle manuelle — cliquer pour modifier ou revenir au choix automatique' :
      'Règle automatique — cliquer pour affecter une règle à ce torrent';
    button.setAttribute('aria-label', 'Choisir la règle de ' + t.name);
    button.append(newEl('span', 'rule-choice-edit', '✎'));
    button.addEventListener('click', () => openTorrentRuleDialog(t));
    td.append(button);
    if (chosen) td.append(newEl('small', 'rule-choice-caption', 'Manuelle'));
    return td;
  }
  function openTorrentRuleDialog(t) {
    const hashes = Array.isArray(t) ? t : [t.infoHash];
    if (!hashes.length || hashes.some(h => !/^[0-9a-f]{40}$/i.test(h))) return;
    const enabled = app.config.rules.filter(r => r.enabled);
    const dialog = newEl('dialog', 'assign-trackers-dialog torrent-rule-dialog');
    dialog.append(newEl('h3', '', 'Affecter une règle'));
    dialog.append(newEl('p', 'muted', t.name || (hashes.length + ' torrent(s) sélectionné(s)')));
    const label = newEl('label', 'assign-selector');
    label.append(newEl('span', '', 'Règle appliquée'));
    const select = newEl('select');
    select.setAttribute('aria-label', 'Règle appliquée au torrent');
    select.append(new Option('Automatique — selon les trackers', ''));
    for (const rule of enabled) {
      select.append(new Option(rule.id + ' · min. ' + (rule.minLeechersByHash?.[t.infoHash] ?? rule.minLeechers) + ' leechers · ' + rule.minUploadRate + '–' + rule.maxUploadRate + ' kB/s', rule.id));
    }
    if (!enabled.some(r => !r.trackerDomains.length && !r.maxUploadRate && !r.minLeechers)) {
      select.append(new Option('Créer « Seed sans upload » · 0 kB/s', ':presence'));
    }
    const assigned = new Set(hashes.map(h => app.config.torrentRuleOverrides?.[h] || ''));
    if (assigned.size === 1) select.value = [...assigned][0];
    else select.selectedIndex = -1;
    label.append(select); dialog.append(label);
    dialog.append(newEl('p', 'muted', 'Une affectation manuelle utilise les trackers de chaque torrent. Les torrents en Forced repassent en Auto pour suivre la règle. Les torrents en Stop restent arrêtés.'));
    const controls = newEl('div', 'assign-controls');
    const cancel = newEl('button', 'btn btn-outline', 'Annuler'); cancel.type = 'button';
    cancel.addEventListener('click', () => dialog.close());
    const submit = newEl('button', 'btn btn-primary', 'Enregistrer'); submit.type = 'button';
    submit.disabled = select.selectedIndex < 0;
    select.addEventListener('change', () => { submit.disabled = false; });
    submit.addEventListener('click', async () => {
      if (dirty() && !confirm('Enregistrer également les autres modifications de configuration en attente ?')) return;
      const config = deepcopy(app.config);
      config.torrentRuleOverrides ||= {};
      let id = select.value;
      if (id === ':presence') {
        if (config.rules.length >= 20) return showToast('Maximum de 20 règles atteint.', true);
        id = config.rules.some(r => r.id === 'seed-sans-upload') ? uniqueId('seed-sans-upload') : 'seed-sans-upload';
        config.rules.push({id, enabled:true, trackerDomains:[], client:null, minLeechers:0,
          minLeechersByHash:{}, minUploadRate:0, maxUploadRate:0, simultaneousSeed:Math.min(250, config.maxCandidates), targetRatio:0});
      }
      for (const h of hashes) {
        if (id) config.torrentRuleOverrides[h] = id;
        else delete config.torrentRuleOverrides[h];
      }
      submit.disabled = true;
      try {
        if (await saveConfig(config)) {
          const forced = hashes.filter(h => app.state?.simulationModes?.[h] === 'forced');
          if (forced.length) await api('/api/torrent-modes', {method:'POST', headers:{'Content-Type':'application/json'},
            body:JSON.stringify({hashes:forced, mode:'auto'})});
          dialog.close();
          showToast(select.value ? 'Règle manuelle enregistrée. Réévaluation en cours.' : 'Choix automatique rétabli.');
          await fetchStatus();
        }
      } catch (error) { showToast(error.message, true); } finally { submit.disabled = false; }
    });
    controls.append(cancel, submit); dialog.append(controls);
    dialog.addEventListener('close', () => dialog.remove());
    document.body.append(dialog); dialog.showModal(); select.focus();
  }
  function trackerCell(t) {
    const td = newEl('td', 'tracker-list-cell');
    const details = Array.isArray(t.trackerDetails) && t.trackerDetails.length ? t.trackerDetails :
      (t.trackers || []).map(domain => ({domain, protocol:'', status:null, leechers:null}));
    if (!details.length) return cell('—');
    const group = newEl('div', 'tracker-chips');
    const added = new Set();
    for (const tr of details) {
      const host = str(tr.domain).toLowerCase().replace(/\.$/, '');
      if (!host || added.has(host)) continue;
      added.add(host);
      const protocols = [...new Set(details.filter(x => str(x.domain).toLowerCase().replace(/\.$/, '') === host)
        .map(x => str(x.protocol).toUpperCase()).filter(Boolean))];
      const covering = (app.config?.rules || []).filter(rule => hostCovered(host, rule));
      const matched = covering.some(rule => rule.enabled);
      const count = details.filter(x => str(x.domain).toLowerCase().replace(/\.$/, '') === host)
        .map(x => x.leechers).filter(value => Number.isInteger(value) && value >= 0);
      const info = `${protocols.join(', ') || 'Protocole inconnu'} · Leechers ${count.length ? Math.max(...count) : 'inconnus'}`;
      // All tracker chips are clickable. An already covered tracker can be
      // added to another rule; its existing associations remain unchanged.
      const button = newEl('button', 'tracker-chip tracker-assign ' +
        (covering.length ? (matched ? 'tracker-covered' : 'tracker-disabled') : 'tracker-add'), host);
      button.type = 'button';
      button.title = info + (covering.length
        ? ' · Déjà couvert par : ' + covering.map(r => r.id).join(', ') + ' · Cliquer pour associer à une autre règle'
        : ' · Cliquer pour choisir une règle');
      button.setAttribute('aria-label', 'Associer le domaine ' + host + ' à une règle');
      button.addEventListener('click', () => openTrackerRuleDialog(t, host));
      group.append(button);
    }
    td.append(group);
    return td;
  }
  function openTrackerRuleDialog(t, initialHost) {
    if (!app.config?.rules?.length) {
      showToast('Crée au moins une règle dans « Règles & trackers ».', true);
      return;
    }
    const domains = new Map();
    for (const tr of t.trackerDetails || []) {
      const host = str(tr.domain).toLowerCase().replace(/\.$/, '');
      if (!host) continue;
      if (!domains.has(host)) domains.set(host, new Set());
      if (tr.protocol) domains.get(host).add(str(tr.protocol).toUpperCase());
    }
    // Support rows populated by older monitoring responses.
    for (const hostRaw of t.trackers || []) {
      const host = str(hostRaw).toLowerCase().replace(/\.$/, '');
      if (host && !domains.has(host)) domains.set(host, new Set());
    }
    if (!domains.has(initialHost)) {
      showToast('Ce domaine n’est plus disponible.', true);
      return;
    }
    const dialog = newEl('dialog', 'assign-trackers-dialog');
    dialog.append(newEl('h3', '', 'Associer un tracker à une règle'));
    dialog.append(newEl('p', 'muted', 'Choisis une règle, puis les domaines à lui ajouter. Seul le domaine est enregistré ; aucune passkey ni URL privée n’est copiée.'));
    const chooser = newEl('label', 'assign-selector');
    chooser.append(newEl('span', '', 'Règle de destination'));
    const select = newEl('select');
    select.setAttribute('aria-label', 'Règle de destination');
    for (const rule of app.config.rules) {
      const option = newEl('option', '', rule.id + (rule.enabled ? '' : ' (désactivée)'));
      option.value = rule.id;
      select.append(option);
    }
    const preferred = app.config.rules.find(r => r.enabled && !hostCovered(initialHost, r)) ||
      app.config.rules.find(r => !hostCovered(initialHost, r)) ||
      app.config.rules.find(r => r.id === t.rule) || app.config.rules[0];
    if (preferred) select.value = preferred.id;
    chooser.append(select); dialog.append(chooser);
    const optionsEl = newEl('div', 'assign-domain-list');
    for (const [host, protocols] of domains) {
      const line = newEl('label', 'assign-domain');
      const check = newEl('input'); check.type = 'checkbox'; check.checked = host === initialHost; check.value = host;
      const label = newEl('span', '', host);
      if (protocols.size) label.append(newEl('small', 'assign-protocol', ` · ${[...protocols].join(', ')}`));
      line.append(check, label); optionsEl.append(line);
    }
    dialog.append(optionsEl);
    const coveredHint = newEl('p', 'muted', 'Un domaine peut appartenir à plusieurs règles. Les domaines déjà présents dans la règle choisie ne seront pas ajoutés une seconde fois.');
    dialog.append(coveredHint);
    if ([...domains.values()].some(protocols => protocols.has('UDP'))) {
      dialog.append(newEl('p', 'assign-warning', 'Tracker UDP : le scrape autonome UDP est désormais disponible si le tracker le permet, sans annonce BitTorrent.'));
    }
    const controls = newEl('div', 'assign-controls');
    const cancel = newEl('button', 'btn btn-outline', 'Annuler'); cancel.type = 'button';
    cancel.addEventListener('click', () => dialog.close());
    const submit = newEl('button', 'btn btn-primary', 'Ajouter à la règle'); submit.type = 'button';
    submit.addEventListener('click', async () => {
      const picked = [...optionsEl.querySelectorAll('input:checked')].map(el => el.value);
      if (!picked.length) return showToast('Sélectionne au moins un domaine.', true);
      const config = deepcopy(app.config);
      const rule = config.rules.find(r => r.id === select.value);
      if (!rule) return showToast('Règle introuvable.', true);
      const add = picked.filter(host => !hostCovered(host, rule));
      if (!add.length) return showToast('Ces domaines sont déjà couverts par cette règle.', true);
      if (rule.trackerDomains.length + add.length > 20) return showToast('Maximum de 20 domaines par règle.', true);
      if (dirty() && !confirm('Des modifications de règles non enregistrées existent. Les enregistrer également ?')) return;
      rule.trackerDomains.push(...add);
      submit.disabled = true;
      try {
        const saved = await saveConfig(config);
        if (saved) {
          dialog.close();
          showToast(`${add.length} domaine(s) ajouté(s) à « ${rule.id} ».${rule.enabled ? '' : ' Règle encore désactivée.'}`);
          await fetchStatus();
        }
      } finally { submit.disabled = false; }
    });
    controls.append(cancel, submit); dialog.append(controls);
    dialog.addEventListener('close', () => dialog.remove());
    document.body.append(dialog); dialog.showModal();
    select.focus();
  }
  function statusCell(t) {
    const td = newEl('td');
    const isRunning = t.simulationActive;
    const reason = str(t.reason);
    if (t.mode === 'solo') {
      const label = isRunning ? 'JOAL actif' : t.selected && app.state?.dryRun ? 'Prévisualisé' :
        t.threshold !== 0 && t.leechers == null ? 'À vérifier' :
        t.threshold !== 0 && t.leechers < (t.threshold ?? 0) ? 'Seuil insuffisant' : 'En attente';
      const trigger = newEl('button', 'status-check-trigger'); trigger.type = 'button';
      trigger.title = t.reason || 'État du torrent autonome';
      trigger.setAttribute('aria-label', label + '. Voir les conditions de suivi');
      trigger.append(badge(label, isRunning ? 'good' : 'warn'), newEl('span', 'status-help-icon', 'ⓘ'));
      trigger.addEventListener('mouseenter', () => showStatusTooltip(t, trigger));
      trigger.addEventListener('mouseleave', scheduleTooltipClose);
      trigger.addEventListener('focus', () => showStatusTooltip(t, trigger));
      trigger.addEventListener('blur', scheduleTooltipClose);
      trigger.addEventListener('click', () => showStatusTooltip(t, trigger));
      td.append(trigger); return td;
    }
    const label = isRunning ? 'JOAL démarré' : t.selected ? (app.state?.dryRun ? 'Prévisualisé' : 'Retenu')
      : !app.state?.qbitConnected ? 'Indisponible'
      : t.eligible ? 'Éligible, non retenu'
      : t.progress < 100 ? 'Incomplet'
      : !t.stopped ? 'qBittorrent actif'
      : /en attente/i.test(reason) ? 'À vérifier' : 'Exclu';
    const tone = isRunning ? 'good' : t.selected ? 'warn'
      : t.eligible || /en attente/i.test(reason) ? 'warn' : 'neutral';
    const trigger = newEl('button', 'status-check-trigger');
    trigger.type = 'button';
    trigger.setAttribute('aria-label', `${label}. Voir les vérifications des règles`);
    trigger.setAttribute('aria-describedby', 'status-inspector');
    trigger.title = 'Survoler ou cliquer pour afficher les conditions OK, non satisfaites ou inconnues';
    trigger.append(badge(label, tone), newEl('span', 'status-help-icon', 'ⓘ'));
    trigger.addEventListener('mouseenter', () => showStatusTooltip(t, trigger));
    trigger.addEventListener('mouseleave', scheduleTooltipClose);
    trigger.addEventListener('focus', () => showStatusTooltip(t, trigger));
    trigger.addEventListener('blur', scheduleTooltipClose);
    trigger.addEventListener('click', () => showStatusTooltip(t, trigger));
    td.append(trigger);
    return td;
  }
  function tableEmpty(tbody, columns, msg) {
    const row = newEl('tr');
    const td = newEl('td', 'empty', msg); td.colSpan = columns; row.append(td); tbody.append(row);
  }
  function updateSortHeaders() {
    for (const heading of document.querySelectorAll('#view-torrents th[data-sort]')) {
      const active = heading.dataset.sort === app.sortKey;
      heading.setAttribute('aria-sort', active ? (app.sortDirection === 'desc' ? 'descending' : 'ascending') : 'none');
      const indicator = heading.querySelector('.sort-indicator');
      if (indicator) indicator.textContent = active ? (app.sortDirection === 'desc' ? '↓' : '↑') : '↕';
    }
  }
  function displayAddedOn(value) {
    const stamp = Number(value);
    if (!Number.isFinite(stamp) || stamp <= 0) return '—';
    const date = new Date(stamp * 1000);
    return Number.isNaN(date.getTime()) ? '—' : date.toLocaleDateString('fr-BE', { year: 'numeric', month: '2-digit', day: '2-digit' });
  }
  function handoffActions(t) {
    const td = newEl('td', 'handoff-actions');
    if (t.mode === 'solo') {
      td.append(newEl('div', 'muted', 'Sans passage par qBittorrent'), diagnosticButton(t), peerChartButton(t));
      const history = newEl('button', 'btn btn-compact btn-outline', 'Historique');
      history.type = 'button'; history.addEventListener('click', () => openTorrentHistory(t));
      td.append(history);
      return td;
    }
    const status = t.handoff?.manualRequest;
    const isPending = status?.status === 'pending';
    const current = t.handoff?.managed;
    const locked = !app.state?.qbitConnected || app.state?.dryRun || app.busyHandoff;
    const configEnabled = !!app.config?.handoff?.enabled;
    const actions = newEl('div', 'handoff-buttons');
    const toJoal = newEl('button', 'btn btn-compact btn-outline', 'Vers JOAL');
    toJoal.type = 'button';
    toJoal.title = 'Demande prioritaire soumise aux relevés récents, seuils, confirmations, quota et délai de 60 min';
    toJoal.disabled = locked || !configEnabled || isPending || t.progress < 100 || current;
    toJoal.addEventListener('click', () => submitHandoff(t, 'joal'));
    const toQbit = newEl('button', 'btn btn-compact btn-outline', 'Vers qBittorrent');
    toQbit.type = 'button';
    toQbit.title = 'Retour prioritaire après arrêt JOAL confirmé ; démarre le torrent si nécessaire';
    toQbit.disabled = locked || (isPending && status.target === 'qbit') || t.progress < 100;
    toQbit.addEventListener('click', () => submitHandoff(t, 'qbit'));
    actions.append(toJoal, toQbit);
    td.append(actions);
    td.append(peerChartButton(t));
    const historyButton = newEl('button', 'btn btn-compact btn-outline history-link', 'Historique');
    historyButton.type = 'button';
    historyButton.title = 'Consulter les événements et sessions enregistrés pour ce torrent';
    historyButton.disabled = !t.infoHash;
    historyButton.addEventListener('click', () => openTorrentHistory(t));
    td.append(historyButton);
    if (status) {
      const stateLabels = {pending:'En attente', completed:'Terminé', rejected:'Refusé'};
      const label = newEl('div', 'handoff-request-state', `${stateLabels[status.status] || status.status} : ${status.detail || '—'}`);
      label.title = String(status.detail || '');
      td.append(label);
    }
    return td;
  }
  async function submitHandoff(t, target) {
    if (!t.infoHash) return showToast('Infohash manquant.', true);
    const direction = target === 'joal' ? 'JOAL' : 'qBittorrent';
    const details = target === 'joal'
      ? 'Le basculement vers JOAL sera seulement demandé. Il restera soumis aux seuils de leechers, relevés frais, confirmations, capacités et temporisation. Il peut être refusé ou différé.'
      : 'Le contrôleur arrêtera JOAL avant de (re)démarrer qBittorrent. Un délai minimum de 60 minutes avant JOAL sera appliqué.';
    if (!confirm(`Demander le passage de « ${t.name} » vers ${direction} ?\n\n${details}`)) return;
    app.busyHandoff = true;
    renderTorrents();
    try {
      const response = await api('/api/torrents/handoff', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({infoHash:t.infoHash, target})
      });
      showToast(response.message || 'Demande enregistrée.');
      await fetchStatus();
    } catch (error) { showToast(error.message, true); }
    finally { app.busyHandoff = false; renderTorrents(); }
  }
  function formatBytes(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value)) || Number(value) < 0) return 'Inconnu';
    const units = ['o', 'Kio', 'Mio', 'Gio', 'Tio', 'Pio'];
    let amount = Number(value), unit = 0;
    while (amount >= 1024 && unit < units.length - 1) { amount /= 1024; unit++; }
    return amount.toLocaleString('fr-FR', {maximumFractionDigits: unit ? 2 : 0}) + ' ' + units[unit];
  }
  function uploadProgressCell(t) {
    const td = newEl('td', 'upload-cell');
    if (t.simulationActive || t.active || t.joalSession?.active) {
      const size = t.sizeBytes ?? t.upload?.contentSizeBytes ?? t.upload?.sizeBytes;
      const total = app.state?.joalUpload?.totalUploadedBytes?.[t.infoHash];
      if (size > 0 && Number.isFinite(total)) {
        const pct = 100 * total / size;
        const bar = newEl('progress', 'simulated-progress');
        bar.max = 100;
        bar.value = Math.min(100, pct);
        bar.setAttribute('aria-label', 'Volume simulé cumulé rapporté à la taille des fichiers');
        td.append(bar, newEl('span', 'upload-percent', pct.toLocaleString('fr-FR', {maximumFractionDigits:1}) + ' % de la taille simulés'));
      } else {
        td.append(newEl('span', 'upload-meta', 'Progression simulée indisponible'));
      }
    }
    if (t.mode === 'solo') {
      td.append(newEl('span', 'upload-heading', 'Session JOAL uniquement'));
      if (t.active) {
        const age = t.joalSession?.uptimeSeconds;
        td.append(newEl('span', 'upload-joal-status',
          'Processus actif · ' + (age == null ? 'durée inconnue' : displayDuration(age))));

      } else {
        td.append(newEl('span', 'upload-meta', 'Session arrêtée ou en attente'));
      }
      td.append(joalMeasurement(t));
      return td;
    }
    const info = t.upload || {};
    const uploaded = info.uploadedBytes;
    const target = info.targetBytes;
    const percent = info.percent;
    td.append(newEl('span', 'upload-heading', 'Partage réel qBittorrent'));
    if (uploaded === null || uploaded === undefined) {
      td.append(newEl('span', 'upload-meta', 'Volume réel : indisponible (API qBittorrent).'));
    } else {
      const line = target != null
        ? formatBytes(uploaded) + ' / ' + formatBytes(target) + ' (objectif local)'
        : formatBytes(uploaded) + ' uploadés · pas de ratio cible';
      td.append(newEl('span', 'upload-meta', line));
    }
    const track = newEl('div', 'upload-progress-track');
    if (percent != null && Number.isFinite(Number(percent))) {
      const pct = Math.min(100, Math.max(0, Number(percent)));
      track.setAttribute('role', 'progressbar');
      track.setAttribute('aria-label', 'Partage réel qBittorrent par rapport à la cible');
      track.setAttribute('aria-valuemin', '0');
      track.setAttribute('aria-valuemax', '100');
      track.setAttribute('aria-valuenow', String(pct));
      const fill = newEl('span', 'upload-progress-fill');
      fill.style.width = pct + '%';
      track.append(fill);
      td.append(track, newEl('span', 'upload-percent', pct.toLocaleString('fr-FR', {maximumFractionDigits:1}) + '% de l’objectif réel'));
    } else {
      track.classList.add('upload-progress-unavailable');
      td.append(track, newEl('span', 'upload-meta', 'Progression non calculable sans cible et volume mesuré.'));
    }
    const session = t.joalSession;
    if (session && session.active) {
      const age = session.uptimeSeconds == null ? 'durée inconnue' : displayDuration(session.uptimeSeconds);
      td.append(newEl('span', 'upload-joal-status', 'JOAL actif depuis ' + age));
    }
    td.append(joalMeasurement(t));
    return td;
  }

  function renderTorrents() {
    $('torrent-reset-sort').hidden = !app.sortKey;
    const body = $('torrent-items'); clear(body);
    const data = app.state || {};
    const rows = allTrackedRows();
    const filter = $('torrent-search').value.trim().toLowerCase();
    const byMode = $('torrent-mode').value;
    const byRule = $('torrent-rule').value;
    const byStatus = $('torrent-status').value;
    const filtered = rows.filter(t => {
      if (byMode !== 'all' && t.mode !== byMode) return false;
      if (byRule !== 'all' && t.rule !== byRule) return false;
      if (byStatus === 'selected' && !t.selected) return false;
      if (byStatus === 'eligible' && (!t.eligible || t.selected)) return false;
      if (byStatus === 'excluded' && t.eligible) return false;
      return !filter || [t.name, t.rule, t.hash, ...(t.trackers || [])].some(v => str(v).toLowerCase().includes(filter));
    });
    $('table-count').textContent = filtered.length + ' ligne(s)';
    setSelectionScope('torrents', filtered);
    const ordered = TorrentTable.sortRows(filtered, app.sortKey, app.sortDirection);
    const pagination = TorrentTable.paginate(ordered, app.pageSize, app.page);
    app.page = pagination.page; // Reconcile when the inventory shrinks after a refresh.
    updateSortHeaders();
    for (const t of pagination.rows) {
      const row = newEl('tr');
      row.append(torrentName(t, false), cell(t.mode === 'solo' ? 'JOAL uniquement' : 'qBittorrent'), trackerCell(t),
        ruleCell(t),
        (()=>{const td=newEl('td'); td.append(swarmSummary(t));
          if(t.threshold != null) td.append(newEl('small','swarm-sub','Seuil L : '+t.threshold));
          return td;})(),
        cell((t.ratio == null ? '—' : Number(t.ratio).toFixed(2)) + ' / ' + (t.targetRatio || '—')),
        uploadProgressCell(t), cell(displayAddedOn(t.addedOn)), statusCell(t), cell([
          t.reason || '—',
          t.handoff?.managed ? 'Pilotage automatique : arrêt sous qBittorrent confirmé' : '',
          t.handoff?.cooldownRemainingSeconds > 0 ?
            'Priorité qBittorrent : ' + Math.ceil(Math.max(0, (new Date(t.handoff.cooldownUntil).getTime() - Date.now()) / 60000)) + ' min restantes' : '',
          t.handoff?.lastScrapeAt ? 'Dernier relevé : ' + formatTime(t.handoff.lastScrapeAt) : ''
        ].filter(Boolean).join(' · ')), handoffActions(t));
      body.append(row);
    }
    if (!pagination.rows.length) tableEmpty(body, 11, 'Aucun torrent ne correspond aux filtres.');
    const all = data.totals?.evaluated ?? rows.length;
    $('table-footnote').textContent = `${pagination.from}–${pagination.to} sur ${filtered.length} ligne(s)${all > rows.length ? ' · Inventaire serveur limité à ' + rows.length + ' sur ' + all : ''}`;
    $('torrent-page-label').textContent = `Page ${pagination.page} / ${pagination.totalPages}`;
    $('torrent-prev').disabled = pagination.page <= 1;
    $('torrent-next').disabled = pagination.page >= pagination.totalPages;
    updateTorrentScrollbar();
  }
  /* v1.11.1 — all width settings are local to the browser, not policy.json. */
  const torrentDefaultWidths = [250, 125, 190, 160, 165, 150, 300, 130, 180, 340, 260];
  const torrentMinWidths = [130, 110, 125, 100, 105, 115, 200, 105, 120, 170, 195];
  const torrentWidthStorageKey = 'joal-smart-torrent-widths-v1';
  let torrentWidths = torrentDefaultWidths.slice();
  function readTorrentWidths() {
    try {
      const values = JSON.parse(localStorage.getItem(torrentWidthStorageKey));
      if (Array.isArray(values) && values.length === 9) values.splice(5, 0, 300);
      if (Array.isArray(values) && values.length === 10) values.splice(1, 0, 125);
      if (Array.isArray(values) && values.length === torrentDefaultWidths.length &&
        values.every((n, i) => Number.isFinite(n) && n >= torrentMinWidths[i] && n <= 1200)) torrentWidths = values.slice();
    } catch (_) { /* Unavailable storage: keep defaults. */ }
  }
  function saveTorrentWidths() {
    try { localStorage.setItem(torrentWidthStorageKey, JSON.stringify(torrentWidths)); } catch (_) { /* Optional. */ }
  }
  let mirroredBottom = null;
  function updateTorrentScrollbar() {
    const viewport = $('torrent-table-scroll');
    if (app.view !== 'torrents' || !viewport.clientWidth) return;
    const bottom = $('torrent-bottom-scroll');
    const maxScroll = Math.max(0, viewport.scrollWidth - viewport.clientWidth);
    const overflow = maxScroll > 1;
    $('torrent-horizontal-nav').classList.toggle('no-overflow', !overflow);
    const inner = $('torrent-scroll-inner');
    const width = (bottom.clientWidth + maxScroll) + 'px';
    if (inner.style.width !== width) {
      inner.style.width = width;
      bottom.scrollLeft = viewport.scrollLeft;
      mirroredBottom = bottom.scrollLeft;
    }
    $('torrent-scroll-left').disabled = !overflow || viewport.scrollLeft < 1;
    $('torrent-scroll-right').disabled = !overflow || viewport.scrollLeft >= maxScroll - 1;
  }
  function setTorrentWidths() {
    const table = $('torrent-table');
    const cols = table.querySelectorAll('colgroup col');
    torrentWidths.forEach((width, i) => { cols[i].style.width = width + 'px'; });
    table.style.width = torrentWidths.reduce((sum, n) => sum + n, 0) + 'px';
    table.querySelectorAll('.col-resizer').forEach((handle, i) => handle.setAttribute('aria-valuenow', String(torrentWidths[i])));
    updateTorrentScrollbar();
  }
  function changeTorrentWidth(i, width, persist = true) {
    torrentWidths[i] = Math.max(torrentMinWidths[i], Math.min(1200, Math.round(width)));
    setTorrentWidths();
    if (persist) saveTorrentWidths();
  }
  function initTorrentColumnControls() {
    readTorrentWidths();
    const table = $('torrent-table');
    const viewport = $('torrent-table-scroll');
    const bottom = $('torrent-bottom-scroll');
    Array.from(table.querySelectorAll('thead th')).forEach((th, i) => {
      const handle = newEl('span', 'col-resizer');
      handle.setAttribute('role', 'separator');
      handle.tabIndex = 0;
      handle.setAttribute('aria-label', 'Redimensionner la colonne ' + th.textContent.trim());
      handle.setAttribute('aria-orientation', 'vertical');
      handle.setAttribute('aria-valuemin', String(torrentMinWidths[i]));
      handle.setAttribute('aria-valuemax', '1200');
      handle.title = 'Glisser pour redimensionner · double-clic pour réinitialiser · flèches clavier';
      handle.addEventListener('click', event => event.stopPropagation());
      handle.addEventListener('dblclick', event => {
        event.preventDefault(); event.stopPropagation();
        changeTorrentWidth(i, torrentDefaultWidths[i]);
      });
      handle.addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
        event.preventDefault(); event.stopPropagation();
        changeTorrentWidth(i, torrentWidths[i] + (event.key === 'ArrowRight' ? 20 : -20));
      });
      handle.addEventListener('pointerdown', event => {
        if (event.button !== 0) return;
        event.preventDefault(); event.stopPropagation();
        const startX = event.clientX, startWidth = torrentWidths[i];
        document.body.classList.add('torrent-column-resizing');
        handle.classList.add('dragging');
        const move = next => changeTorrentWidth(i, startWidth + next.clientX - startX, false);
        const finish = () => {
          window.removeEventListener('pointermove', move);
          window.removeEventListener('pointerup', finish);
          window.removeEventListener('pointercancel', finish);
          document.body.classList.remove('torrent-column-resizing');
          handle.classList.remove('dragging');
          saveTorrentWidths();
        };
        window.addEventListener('pointermove', move);
        window.addEventListener('pointerup', finish);
        window.addEventListener('pointercancel', finish);
      });
      th.append(handle);
    });
    viewport.addEventListener('scroll', () => {
      if (Math.abs(bottom.scrollLeft - viewport.scrollLeft) > 1) {
        bottom.scrollLeft = viewport.scrollLeft;
        mirroredBottom = bottom.scrollLeft;
      }
      updateTorrentScrollbar();
    }, {passive:true});
    bottom.addEventListener('scroll', () => {
      if (bottom.scrollLeft !== mirroredBottom && Math.abs(bottom.scrollLeft - viewport.scrollLeft) > 1) {
        viewport.scrollLeft = bottom.scrollLeft;
      }
      mirroredBottom = null;
    }, {passive:true});
    $('torrent-scroll-left').addEventListener('click', () => viewport.scrollBy({left:-Math.max(200, viewport.clientWidth * .65),behavior:'instant'}));
    $('torrent-scroll-right').addEventListener('click', () => viewport.scrollBy({left:Math.max(200, viewport.clientWidth * .65),behavior:'instant'}));
    $('torrent-reset-widths').addEventListener('click', () => {
      torrentWidths = torrentDefaultWidths.slice(); setTorrentWidths(); saveTorrentWidths();
    });
    window.addEventListener('resize', updateTorrentScrollbar);
    if (typeof ResizeObserver !== 'undefined') new ResizeObserver(updateTorrentScrollbar).observe(viewport);
    setTorrentWidths();
  }

  function displayDuration(seconds) {
    if (seconds == null || !Number.isFinite(Number(seconds))) return '—';
    const s = Math.max(0, Math.ceil(Number(seconds)));
    const days = Math.floor(s / 86400), hours = Math.floor(s % 86400 / 3600);
    return s === 0 ? 'Seuil atteint' : `${days} j ${hours} h`;
  }
  function renderSwarm() {
    const monitor = app.state?.swarm || {};
    const items = monitor.items || [];
    $('swarm-count').textContent = `${monitor.count ?? items.length} torrent(s)`;
    $('swarm-time').textContent = formatTime(monitor.updatedAt);
    const body = $('swarm-items'); clear(body);
    if (!items.length) return tableEmpty(body, 8, 'Aucun torrent mémorisé. Importer un .torrent ou appliquer le tag ou la catégorie configurés dans qBittorrent.');
    for (const item of items) {
      const rules = Object.entries(item.rules || {});
      for (const [rule, obs] of (rules.length ? rules : [['—', { leechers: null, sampledAt: null, status: 'en attente' }]])) {
        const tr = newEl('tr');
        tr.append(cell(item.name), cell((item.trackers || []).join(', ') || '—'),
          cell(rule), cell((obs.seeders == null ? '?' : String(obs.seeders)) + ' / ' + (obs.leechers == null ? '?' : String(obs.leechers))),
          cell(obs.sampledAt ? formatTime(obs.sampledAt) : '—'),
          cell((obs.status || 'inconnu') + (obs.status === 'partiel' ? ` (${obs.successCount || 0}/${obs.trackerCount || 0} trackers)` : '') + (obs.source ? ' · ' + obs.source : '')));
        const historyCell = newEl('td');
        if (rule !== '—') {
          const historyButton = newEl('button', 'btn btn-outline btn-compact', 'Graphique');
          historyButton.type = 'button';
          historyButton.addEventListener('click', () => openSwarmHistory(item, rule));
          historyCell.append(historyButton);
        } else historyCell.textContent = '—';
        const diagnostics = newEl('td');
        const entry = {name:item.name,infoHash:item.infoHash,trackers:item.trackers,rule};
        diagnostics.append(diagnosticButton(entry));
        tr.append(historyCell, diagnostics);
        body.append(tr);
      }
    }
  }

  function addSvg(parent, tag, attributes = {}, text = null) {
    const child = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const [key, value] of Object.entries(attributes)) child.setAttribute(key, String(value));
    if (text != null) child.textContent = String(text);
    parent.appendChild(child);
    return child;
  }
  function drawSwarmChart(points) {
    const frame = $('swarm-history-chart'); clear(frame);
    const valid = points.filter(p => Number.isInteger(p.leechers) && p.leechers >= 0 && Number.isFinite(Date.parse(p.time)));
    if (!valid.length) {
      frame.append(newEl('p', 'muted', 'Aucun relevé exploitable enregistré pour cette règle.'));
      return;
    }
    const w = 740, h = 280, left = 46, right = 16, top = 18, bottom = 45;
    const start = Math.min(...points.map(p=>Date.parse(p.time)).filter(Number.isFinite));
    const end = Math.max(...points.map(p=>Date.parse(p.time)).filter(Number.isFinite));
    const domainEnd = Math.max(end, start + 1000);
    const highest = Math.max(1, ...valid.map(p=>p.leechers));
    const svg = addSvg(frame, 'svg', {viewBox:`0 0 ${w} ${h}`, role:'img', 'aria-label':'Leechers mesurés selon le temps, sans interpolation des relevés inconnus'});
    const x = time => left + (Date.parse(time) - start) / (domainEnd - start) * (w-left-right);
    const y = value => top + (1 - value/highest) * (h-top-bottom);
    for (let i=0; i<=4; i++) {
      const count = highest*i/4;
      const yy = y(count);
      addSvg(svg, 'line', {x1:left,y1:yy,x2:w-right,y2:yy,stroke:'#30495b','stroke-width':1});
      addSvg(svg, 'text', {x:left-8,y:yy+4,'text-anchor':'end',fill:'#a8c0d0','font-size':12}, String(Math.round(count)));
    }
    const fmtDate = t => new Date(t).toLocaleDateString('fr-BE',{day:'2-digit',month:'2-digit'});
    addSvg(svg, 'text', {x:left,y:h-12,fill:'#a8c0d0','font-size':12}, fmtDate(start));
    addSvg(svg, 'text', {x:w-right,y:h-12,'text-anchor':'end',fill:'#a8c0d0','font-size':12}, fmtDate(end));
    let segment=[];
    const flush = () => {
      if (segment.length>=2) addSvg(svg, 'polyline', {points:segment.map(p=>`${x(p.time)},${y(p.leechers)}`).join(' '),stroke:'#71d5d0',fill:'none','stroke-width':2.5,'stroke-linejoin':'round'});
      segment=[];
    };
    for (const point of points) {
      if (!Number.isInteger(point.leechers) || point.leechers<0 || !Number.isFinite(Date.parse(point.time))) {flush();continue;}
      segment.push(point);
    }
    flush();
    // Individual measurements remain visible even when a gap breaks the line.
    for (const point of valid) {
      const dot = addSvg(svg, 'circle', {cx:x(point.time),cy:y(point.leechers),r:3.7,
        fill:point.status==='ok'?'#70d6cc':'#e7be79'});
      addSvg(dot, 'title', {}, `${formatTime(point.time)} : ${point.leechers} leechers · ${point.status}`);
    }
  }
  async function openSwarmHistory(item, rule) {
    const dialog = $('swarm-history-dialog');
    $('swarm-history-title').textContent = 'Historique des leechers · ' + rule;
    $('swarm-history-subtitle').textContent = item.name;
    $('swarm-history-description').textContent = 'Chargement…';
    clear($('swarm-history-chart'));
    if (dialog.showModal) dialog.showModal(); else dialog.setAttribute('open', '');
    try {
      const result = await api('/api/swarm/history?infoHash='+encodeURIComponent(item.infoHash)+'&rule='+encodeURIComponent(rule));
      const picker = $('swarm-history-series'); clear(picker);
      picker.append(new Option('Maximum observé (règle)', 'all'));
      for (const host of Object.keys(result.trackerSeries || {}).sort()) picker.append(new Option(host,host));
      const redraw = () => {
        const active = picker.value === 'all' ? result.points : (result.trackerSeries || {})[picker.value] || [];
        drawSwarmChart(active || []);
      };
      picker.onchange = redraw;
      redraw();
      const valid = (result.points || []).filter(p => Number.isInteger(p.leechers));
      $('swarm-history-description').textContent = `${valid.length} relevé(s) disponibles · ${result.points.length} tentatives · conservation maximale ${result.retentionDays} jours. Les points manquants ne sont pas traités comme zéro.`;
    } catch (error) {$('swarm-history-description').textContent = 'Historique indisponible : ' + error.message;}
  }
  function peerChartButton(t) {
    const button = newEl('button', 'btn btn-compact btn-outline', 'Graphique S/L');
    button.type = 'button';
    button.title = 'Historique des seeders et leechers (relevés et annonces)';
    button.addEventListener('click', () => openPeerChart(t));
    return button;
  }
  function drawPeerChart(points) {
    const frame = $('peer-chart'); clear(frame);
    const known = v => Number.isInteger(v) && v >= 0;
    const valid = points.filter(p => Number.isFinite(Date.parse(p.time)) && (known(p.seeders) || known(p.leechers)));
    if (!valid.length) {
      frame.append(newEl('p', 'muted', 'Aucun relevé ni réponse d’annonce enregistré pour ce torrent.'));
      return;
    }
    const w = 740, h = 280, left = 46, right = 16, top = 18, bottom = 45;
    const times = valid.map(p => Date.parse(p.time));
    const start = Math.min(...times), end = Math.max(...times);
    const domainEnd = Math.max(end, start + 1000);
    // Round the scale up to a multiple of 4 so the 5 grid labels are distinct integers.
    const highest = Math.max(4, Math.ceil(Math.max(0, ...valid.flatMap(p => [p.seeders, p.leechers].filter(known))) / 4) * 4);
    const svg = addSvg(frame, 'svg', {viewBox:`0 0 ${w} ${h}`, role:'img', 'aria-label':'Seeders et leechers selon le temps'});
    const x = time => left + (Date.parse(time) - start) / (domainEnd - start) * (w-left-right);
    const y = value => top + (1 - value/highest) * (h-top-bottom);
    for (let i=0; i<=4; i++) {
      const count = highest*i/4, yy = y(count);
      addSvg(svg, 'line', {x1:left,y1:yy,x2:w-right,y2:yy,stroke:'#30495b','stroke-width':1});
      addSvg(svg, 'text', {x:left-8,y:yy+4,'text-anchor':'end',fill:'#a8c0d0','font-size':12}, String(Math.round(count)));
    }
    const fmt = t => new Date(t).toLocaleString('fr-BE',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
    addSvg(svg, 'text', {x:left,y:h-12,fill:'#a8c0d0','font-size':12}, fmt(start));
    addSvg(svg, 'text', {x:w-right,y:h-12,'text-anchor':'end',fill:'#a8c0d0','font-size':12}, fmt(end));
    for (const [key, color] of [['seeders', '#7fb6ff'], ['leechers', '#71d5d0']]) {
      // Unknown values break the line: a gap, never an invented zero.
      let segment = [];
      const flush = () => {
        if (segment.length >= 2) addSvg(svg, 'polyline', {points:segment.map(p=>`${x(p.time)},${y(p[key])}`).join(' '),
          stroke:color, fill:'none', 'stroke-width':2.2, 'stroke-linejoin':'round'});
        segment = [];
      };
      for (const p of valid) { if (known(p[key])) segment.push(p); else flush(); }
      flush();
      for (const p of valid.filter(item => known(item[key]))) {
        const dot = addSvg(svg, 'circle', {cx:x(p.time), cy:y(p[key]), r:3.2, fill:color});
        addSvg(dot, 'title', {}, `${formatTime(p.time)} · ${p[key]} ${key} · ${p.source}`);
      }
    }
  }
  async function openPeerChart(t) {
    const dialog = $('peer-chart-dialog');
    $('peer-chart-subtitle').textContent = t.name || t.infoHash;
    $('peer-chart-description').textContent = 'Chargement…';
    clear($('peer-chart'));
    if (dialog.showModal) dialog.showModal(); else dialog.setAttribute('open', '');
    try {
      const result = await api('/api/torrent/peers?infoHash=' + encodeURIComponent(t.infoHash));
      drawPeerChart(result.points || []);
      const sources = [...new Set((result.points || []).map(p => p.source))].join(', ') || 'aucune';
      $('peer-chart-description').textContent = `${(result.points || []).length} point(s) · sources : ${sources} · conservation ${result.retentionDays} jours. Les valeurs inconnues interrompent la courbe.`;
    } catch (error) { $('peer-chart-description').textContent = 'Historique indisponible : ' + error.message; }
  }
  function scheduleEditor(rule) {
    const box = newEl('div', 'schedule-box');
    const toggle = newEl('label', 'schedule-toggle');
    const enabled = newEl('input'); enabled.type = 'checkbox'; enabled.checked = !!rule.schedule;
    toggle.append(enabled, document.createTextNode('Plage horaire'));
    const days = newEl('div', 'schedule-days');
    const names = ['lun', 'mar', 'mer', 'jeu', 'ven', 'sam', 'dim'];
    const current = () => rule.schedule || {days:[1,2,3,4,5,6,7], start:'22:00', end:'07:00'};
    const boxes = names.map((name, i) => {
      const label = newEl('label'); const input = newEl('input'); input.type = 'checkbox';
      input.checked = current().days.includes(i + 1);
      label.append(input, document.createTextNode(name)); days.append(label); return input;
    });
    const start = newEl('input'); start.type = 'time'; start.value = current().start; start.setAttribute('aria-label', 'Début');
    const end = newEl('input'); end.type = 'time'; end.value = current().end; end.setAttribute('aria-label', 'Fin');
    const hint = newEl('small', 'muted', 'Heure du conteneur (variable TZ). Une plage 22:00 → 07:00 traverse minuit ; le jour coché est celui du début.');
    const sync = () => {
      for (const input of [...boxes, start, end]) input.disabled = !enabled.checked;
      rule.schedule = enabled.checked ? {days: boxes.flatMap((b, i) => b.checked ? [i + 1] : []), start: start.value, end: end.value} : null;
      if (!enabled.checked) delete rule.schedule;
      updateDirty();
    };
    for (const input of [enabled, ...boxes, start, end]) input.addEventListener('change', sync);
    for (const input of [...boxes, start, end]) input.disabled = !enabled.checked;
    box.append(toggle, days, newEl('span', '', 'de'), start, newEl('span', '', 'à'), end, hint);
    return box;
  }
  function renderWatchFolder() {
    const info = app.state?.importWatch;
    if (info?.folder) $('watch-folder').textContent = info.folder;
    const list = $('watch-recent'); clear(list);
    const items = info?.recent || [];
    if (!items.length) { list.append(newEl('li', 'muted', 'Aucun fichier traité depuis le démarrage.')); return; }
    const labels = {added: 'Importé', duplicate: 'Doublon', error: 'Refusé'};
    for (const item of items.slice(0, 10)) {
      list.append(newEl('li', item.status === 'error' ? 'is-error' : item.status === 'duplicate' ? 'is-duplicate' : '',
        `${formatTime(item.at)} · ${labels[item.status] || item.status} · ${item.filename} · ${item.message}`));
    }
  }
  function shortDuration(seconds) {
    const s = Math.max(0, Math.round(Number(seconds) || 0));
    if (s < 60) return s + ' s';
    if (s < 3600) return Math.floor(s / 60) + ' min';
    if (s < 86400) return Math.floor(s / 3600) + ' h ' + String(Math.floor(s % 3600 / 60)).padStart(2, '0');
    return Math.floor(s / 86400) + ' j ' + Math.floor(s % 86400 / 3600) + ' h';
  }
  async function renderHealth() {
    if (app.view !== 'health') return;
    try {
      const health = await api('/api/health');
      $('health-version').textContent = 'v' + health.version;
      $('health-uptime').textContent = shortDuration(health.uptimeSeconds);
      const disk = health.storage?.disk;
      $('health-disk').textContent = disk ? formatBytes(disk.freeBytes) : 'Inconnu';
      $('health-disk-label').textContent = disk ? `Espace libre /data (sur ${formatBytes(disk.totalBytes)})` : 'Espace libre /data';
      $('health-qbit').textContent = !health.qbitConfigured ? 'Non configuré' : (health.qbitConnected ? 'Connecté' : 'Indisponible');
      const threads = $('health-threads'); clear(threads);
      for (const item of health.threads || []) {
        const tr = newEl('tr');
        const state = newEl('td', item.status === 'ok' ? 'health-ok' : (item.status === 'en retard' ? 'health-warn' : 'muted'), item.status);
        tr.append(cell(item.label), state,
          cell(item.ageSeconds == null ? '—' : 'il y a ' + shortDuration(item.ageSeconds)),
          cell(shortDuration(item.periodSeconds)),
          cell(String(item.restarts || 0)), cell(item.lastError || '—'));
        threads.append(tr);
      }
      const logs = $('health-logs'); clear(logs);
      for (const item of health.storage?.logs || []) {
        const tr = newEl('tr'); tr.append(cell(item.rule), cell(item.file), cell(formatBytes(item.sizeBytes))); logs.append(tr);
      }
      if (!health.storage?.logs?.length) tableEmpty(logs, 3, 'Aucun journal de session.');
      const limit = health.storage?.logLimit;
      if (limit) $('health-log-limit').textContent = `Rotation automatique : ${formatBytes(limit.maxBytes)} par fichier, ${limit.backups} archive(s) conservée(s).`;
      const waiting = health.storage?.importWaiting;
      $('health-import').textContent = `${health.storage?.importFolder || '—'} · ${waiting == null ? 'inaccessible' : waiting + ' fichier(s) en attente'}`;
    } catch (error) { showToast('Santé indisponible : ' + error.message, true); }
  }
  async function refreshNotifications() {
    try {
      const data = await api('/api/notifications');
      const count = $('notifications-count');
      count.textContent = String(data.unread || 0);
      count.hidden = !(data.unread>0);
      const list = $('notification-items'); clear(list);
      for (const notice of data.items || []) {
        const entry = newEl('div', 'notification-entry'+(notice.read?' read':''));
        entry.append(newEl('span','notification-level '+(notice.level==='error'?'is-error':'is-success'),notice.level==='error'?'Incident':'Rétabli'),
          newEl('span','',notice.message||'Notification'),newEl('small','muted',formatTime(notice.at)));
        list.append(entry);
      }
      if (!data.items?.length) list.append(newEl('p','muted','Aucune notification enregistrée.'));
    } catch (_) { /* A notification issue must not block the dashboard. */ }
  }
  async function downloadFullBackup() {
    try {
      const res = await fetch('/api/backup/export',{credentials:'same-origin',cache:'no-store'});
      if (res.status===401) {location.replace('/login');return;}
      if (!res.ok) {const data=await res.json();throw new Error(data.error||'Export impossible');}
      const blob=await res.blob();
      const uri=URL.createObjectURL(blob); const anchor=newEl('a');
      anchor.href=uri;anchor.download='joal-smart-data-'+new Date().toISOString().slice(0,10)+'.zip';
      document.body.append(anchor);anchor.click();anchor.remove();setTimeout(()=>URL.revokeObjectURL(uri),1000);
      showToast('Sauvegarde téléchargée — fichier confidentiel.');
    } catch (error) {showToast(error.message,true);}
  }
  async function uploadFullBackup() {
    const file = $('backup-file').files[0];
    if (!file || !$('backup-confirm').checked) return showToast('Choisis un ZIP et coche la confirmation.', true);
    if (file.size > 64*1024*1024) return showToast('ZIP trop volumineux.',true);
    if (!confirm('Préparer la restauration complète ? Elle sera appliquée au prochain redémarrage. Le partage doit être arrêté et dryRun activé.')) return;
    try {
      const result = await api('/api/backup/restore',{method:'POST',headers:{'Content-Type':'application/zip'},body:await file.arrayBuffer()});
      $('backup-result').textContent = result.message+' ('+result.files+' fichiers).';
      showToast('Restauration préparée. Redémarrer ensuite le conteneur.');
      $('backup-confirm').checked = false;
    } catch (error) { $('backup-result').textContent = error.message; showToast(error.message,true); }
  }

  function renderCleanup() {
    const data = app.state?.cleanup || {};
    $('cleanup-state').textContent = !data.enabled ? 'Désactivé' : data.deleteData && !app.state?.dryRun ? 'Suppression automatique activée' : 'Suivi uniquement';
    $('cleanup-time').textContent = formatTime(data.lastCheck);
    $('cleanup-count').textContent = String((data.items || []).length);
    $('cleanup-deleted').textContent = String(data.deleted || 0);
    const body = $('cleanup-items'); clear(body);
    const rows = data.items || [];
    if (!rows.length) tableEmpty(body, 5, data.error || 'Aucun relevé. Activer le suivi dans Paramètres.');
    for (const item of rows) {
      const row = newEl('tr');
      row.append(cell(item.name), cell(item.leechers == null ? '—' : String(item.leechers)),
        cell(item.zeroSince == null ? '—' : new Date(item.zeroSince * 1000).toLocaleString('fr-BE')),
        cell(displayDuration(item.secondsRemaining)), cell(item.status || 'À vérifier'));
      body.append(row);
    }
    $('cleanup-footnote').textContent = data.error ||
      'Scrapes HTTP(S) frais · au maximum 60 torrents vérifiés par cycle · aucune suppression sur valeurs mises en cache' +
      (data.skipped ? ` · ${data.skipped} torrent(s) non vérifiés ce cycle` : '');
  }
  function renderRuleOptions() {
    const filter = $('torrent-rule');
    const current = filter.value;
    clear(filter);
    filter.append(new Option('Toutes les règles', 'all'));
    for (const rule of app.config?.rules || []) filter.append(new Option(rule.id, rule.id));
    filter.value = [...filter.options].some(o => o.value === current) ? current : 'all';
  }
  function field(label, key, value, options = {}) {
    const outer = newEl('label', options.wide ? 'wide' : '');
    outer.append(newEl('span', '', label));
    const input = options.type === 'textarea' ? newEl('textarea') : newEl('input');
    input.value = str(value);
    if (options.type !== 'textarea') input.type = options.type || 'text';
    if (options.type === 'number') { input.min = options.min ?? '1'; input.max = options.max ?? '100000'; input.step = options.step ?? '1'; }
    if (options.placeholder) input.placeholder = options.placeholder;
    input.dataset.key = key;
    outer.append(input);
    return [outer, input];
  }
  function renderRuleEditor() {
    const root = $('rule-editor'); clear(root);
    if (!app.config) return;
    if (!app.config.rules.length) root.append(newEl('div', 'empty', 'Aucune règle. Utilise « Nouvelle règle ».'));
    app.config.rules.forEach((rule, index) => {
      const card = newEl('article', 'edit-card' + (rule.enabled ? '' : ' is-disabled'));
      const top = newEl('div', 'edit-top');
      top.append(newEl('h3', '', 'Règle ' + (index + 1) + ' — ' + rule.id));
      const actions = newEl('div', 'edit-actions');
      const enabled = newEl('label', 'toggle-label');
      const enabledCheckbox = newEl('input'); enabledCheckbox.type = 'checkbox'; enabledCheckbox.checked = rule.enabled;
      enabledCheckbox.setAttribute('role', 'switch');
      enabledCheckbox.addEventListener('change', () => { rule.enabled = enabledCheckbox.checked; card.classList.toggle('is-disabled', !rule.enabled); updateDirty(); });
      enabled.append(enabledCheckbox, document.createTextNode('Activée'));
      const dup = newEl('button', 'btn btn-compact btn-outline', 'Dupliquer'); dup.type = 'button';
      dup.addEventListener('click', () => { if (app.config.rules.length >= 20) return showToast('Maximum de 20 règles.', true); const copy = deepcopy(rule); copy.id = uniqueId('tracker'); copy.enabled = false; app.config.rules.splice(index + 1, 0, copy); renderRuleEditor(); updateDirty(); });
      const del = newEl('button', 'btn btn-compact btn-danger', 'Supprimer'); del.type = 'button';
      del.addEventListener('click', () => { if (!confirm('Supprimer la règle « ' + rule.id + ' » ?')) return; app.config.rules.splice(index, 1); renderRuleEditor(); updateDirty(); });
      actions.append(enabled, dup, del); top.append(actions); card.append(top);
      const grid = newEl('div', 'edit-grid');
      const definitions = [
        ['Identifiant', 'id', rule.id, { wide: true, placeholder: 'tracker-prive' }],
        ['Profil client JOAL (.client)', 'client', rule.client || '', { wide: true, placeholder: 'profil.client' }],
        ['Domaines de trackers (facultatifs pour une affectation manuelle)', 'trackerDomains', (rule.trackerDomains || []).join('\n'), { wide: true, type: 'textarea', placeholder: 'tracker.exemple.org\nautre.exemple.net' }],
        ['Leechers minimum (défaut de la règle)', 'minLeechers', rule.minLeechers, { type: 'number', min: '0', max: '100000' }],
        ['Seuils par torrent (infohash : minimum, un par ligne)', 'minLeechersByHash', Object.entries(rule.minLeechersByHash || {}).map(([hash, n]) => `${hash}: ${n}`).join('\n'), { wide: true, type: 'textarea', placeholder: '40 caractères infohash: 6' }],
        ['Torrents simultanés max.', 'simultaneousSeed', rule.simultaneousSeed, { type: 'number', min: '1', max: '250' }],
        ['Vitesse minimale (kB/s)', 'minUploadRate', rule.minUploadRate, { type: 'number', min: '0', max: '100000' }],
        ['Vitesse maximale (kB/s)', 'maxUploadRate', rule.maxUploadRate, { type: 'number', min: '0', max: '100000' }],
        ['Ratio cible qBittorrent (0 = désactivé)', 'targetRatio', rule.targetRatio ?? 0, { type: 'number', min: '0', max: '100', step: '0.05' }]
      ];
      for (const [label, key, val, options] of definitions) {
        const [wrapper, input] = field(label, key, val, options);
        if (key === 'client') {
          const list = newEl('datalist'); list.id = 'clients-' + index;
          app.clients.forEach(c => list.append(new Option(c)));
          input.setAttribute('list', list.id);
          wrapper.append(list);
        }
        input.addEventListener('input', () => {
          if (key === 'trackerDomains') rule[key] = input.value.split(/[\s,;]+/).filter(Boolean);
          else if (key === 'minLeechersByHash') {
            const entries = input.value.split(/[\r\n]+/).map(x => x.trim()).filter(Boolean);
            rule[key] = Object.fromEntries(entries.map(line => { const pair = line.match(/^([a-fA-F0-9]{40})\s*[:=]\s*(\d+)$/); return pair ? [pair[1].toLowerCase(), Number(pair[2])] : [line, null]; }));
          }
          else if (options.type === 'number') rule[key] = input.value === '' ? null : Number(input.value);
          else rule[key] = input.value;
          updateDirty();
        });
        grid.append(wrapper);
      }
      grid.append(scheduleEditor(rule));
      const note = newEl('p', 'muted', 'Le seuil 0 désactive le contrôle des leechers : aucun relevé n’est requis et les valeurs inconnues restent affichées comme inconnues. Un tracker peut figurer dans plusieurs règles. Clique sur une règle dans le tableau pour associer manuellement un torrent à la règle souhaitée. La règle applicable est choisie selon le seuil de leechers effectif le plus bas (exceptions par infohash comprises), puis selon la cible de ratio et la capacité. Un torrent ne peut occuper qu’une seule session.');
      note.classList.add('rule-help');
      card.append(grid); root.append(card);
      if (index === 0) root.prepend(note);  // shown once, above the rules
    });
    updateDirty();
  }
  function uniqueId(prefix) {
    const ids = new Set(app.config.rules.map(r => r.id));
    for (let i = 1; i <= 100; i++) if (!ids.has(prefix + '-' + i)) return prefix + '-' + i;
    return 'tracker-' + Date.now();
  }
  let qbitReady = false;
  function showQbit(config) {
    $('qbit-url').value = config.url;
    $('qbit-user').value = config.username;
    $('qbit-password').value = '';
    $('qbit-password').placeholder = config.hasPassword ? 'Déjà configuré' : 'Facultatif';
    $('qbit-clear-password').checked = false;
    qbitReady = true;
    $('qbit-save').disabled = $('qbit-docker').disabled = false;
  }
  async function loadQbit() {
    try { showQbit(await api('/api/qbit-connection')); }
    catch (error) { $('qbit-message').textContent = error.message; }
  }
  async function saveQbit(useDocker) {
    const body = useDocker ? {useDocker:true} : {url:$('qbit-url').value, username:$('qbit-user').value};
    if (!useDocker && ($('qbit-password').value || $('qbit-clear-password').checked)) {
      body.password = $('qbit-clear-password').checked ? '' : $('qbit-password').value;
    }
    $('qbit-save').disabled = $('qbit-docker').disabled = true;
    try {
      showQbit(await api('/api/qbit-connection', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}));
      $('qbit-message').textContent = 'Connexion enregistrée et appliquée immédiatement.';
    } catch (error) { $('qbit-message').textContent = error.message; }
    finally { $('qbit-save').disabled = $('qbit-docker').disabled = false; }
  }
  function renderSettings() {
    if (!app.config) return;
    $('setting-dryrun').checked = !!app.config.dryRun;
    $('setting-filter-mode').value = app.config.qbitFilterMode || 'tag';
    $('tag-field').classList.toggle('hidden', $('setting-filter-mode').value === 'category');
    $('category-field').classList.toggle('hidden', $('setting-filter-mode').value === 'tag');
    $('setting-category').value = app.config.qbitCategory || '';
    $('setting-tag').value = app.config.qbitTag || '';
    $('setting-refresh').value = str(app.config.refreshSeconds);
    $('setting-max').value = str(app.config.maxCandidates);
    $('setting-completed').value = app.config.completedTag || 'JoalCompleted';
    $('setting-guard').value = str(app.config.guardSeconds || 5);
    const c = app.config.cleanup || { enabled: false, deleteData: false, afterDays: 3, scrapeEveryMinutes: 60 };
    $('setting-cleanup-enabled').checked = !!c.enabled;
    $('setting-cleanup-delete').checked = !!c.deleteData;
    $('setting-cleanup-days').value = str(c.afterDays);
    $('setting-cleanup-frequency').value = str(c.scrapeEveryMinutes);
    const sw = app.config.swarm || { enabled: true, intervalMinutes: 15, maxAgeMinutes: 45 };
    $('setting-swarm-enabled').checked = !!sw.enabled;
    $('setting-swarm-interval').value = str(sw.intervalMinutes);
    $('setting-swarm-age').value = str(sw.maxAgeMinutes);
    const h = app.config.handoff || {enabled: false, cooldownMinutes: 60, confirmations: 2, fastTrackMargin: 5};
    $('setting-handoff-enabled').checked = !!h.enabled;
    $('setting-handoff-cooldown').value = str(h.cooldownMinutes || 60);
    $('setting-handoff-confirmations').value = str(h.confirmations || 2);
    $('setting-handoff-fast-margin').value = str(h.fastTrackMargin || 5);
    updateDirty();
  }
  async function saveConfig(config) {
    try {
      await api('/api/config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(config) });
      app.config = deepcopy(config);
      app.saved = deepcopy(config);
      app.jsonDirty = false;
      updateDirty();
      renderRuleOptions();
      renderStatus();
      showToast('Configuration validée et enregistrée. Une nouvelle surveillance a été demandée.');
      return true;
    } catch (error) { showToast('Configuration rejetée : ' + error.message, true); return false; }
  }
  let stateRequest = 0;
  let appliedRequest = 0;
  const removing = new Set();
  function applyLibraryEdit(added, removed) {
    appliedRequest = ++stateRequest;
    if (!app.state) return;
    const solo = app.state.solo ||= {items: [], count: 0, active: 0};
    const items = new Map((solo.items || []).map(row => [row.infoHash, row]));
    if (removed && items.has(removed)) {
      if (items.get(removed).active) solo.active = Math.max(0, solo.active - 1);
      items.delete(removed);
      solo.count = Math.max(0, solo.count - 1);
      selectedModes.delete(removed);
    }
    for (const row of added) {
      if (!items.has(row.infoHash)) {
        items.set(row.infoHash, row);
        solo.count++;
      }
    }
    solo.items = [...items.values()];
    renderStatus();
  }
  async function fetchStatus() {
    const request = ++stateRequest;
    try {
      const snapshot = await api('/api/state');
      if (request < appliedRequest) return;
      appliedRequest = request;
      app.state = snapshot;
      renderStatus();
    }
    catch (error) {
      if (request < appliedRequest) return;
      if (app.state?.joalUpload) {
        app.state.joalUpload = null;
        renderStatus();
      }
      $('alert').textContent = 'Impossible de lire l’état : ' + error.message;
      $('alert').classList.remove('hidden');
    }
  }
  async function fetchConfig() {
    const config = await api('/api/config');
    app.config = deepcopy(config); app.saved = deepcopy(config);
    renderRuleOptions(); renderRuleEditor(); renderSettings(); renderStatus();
  }
  let inspectedFiles = [];
  let importRevision = 0;
  const selectedImportFiles = () => [...$('import-file').files];
  const sameFiles = (a, b) => a.length === b.length && a.every((file, i) => file === b[i]);
  function resetImport() {
    importRevision++;
    inspectedFiles = [];
    $('import-submit').disabled = true;
    $('import-preview').classList.add('hidden');
    $('import-preview').replaceChildren();
    const count = selectedImportFiles().length;
    $('import-message').textContent = count
      ? `Analyse automatique de ${count} fichier(s) en cours…`
      : 'Choisir un ou plusieurs fichiers .torrent pour lancer l’analyse automatique.';
  }
  function importPayload(files = selectedImportFiles()) {
    if (!files.length || files.length > 30) throw new Error('Sélectionner entre 1 et 30 fichiers .torrent.');
    const total = files.reduce((n, f) => n + f.size, 0);
    if (files.length === 1 && files[0].size > 50 * 1024 * 1024) throw new Error('Fichier supérieur à 50 Mio.');
    if (total > 64 * 1024 * 1024) throw new Error('Lot de fichiers supérieur à 64 Mio.');
    const data = new FormData();
    for (const file of files) data.append('torrent', file);
    return data;
  }
  function importRows(response, files) {
    if (Array.isArray(response.results)) return response.results;
    if (response.torrent) return [{filename: files[0].name, status:'ok', ...response.torrent}];
    return [{filename: files[0].name, status:'added', name:response.name || files[0].name,
      hash:response.hash, item:response.item, message:response.message, storedFileName:response.storedFileName}];
  }
  function showImportRows(rows, isResult=false) {
    const root = $('import-preview'); root.replaceChildren();
    for (const row of rows) {
      const card = newEl('div', 'import-item');
      const status = row.status === 'ok' ? 'Validé' : row.status === 'added' ? 'Importé'
        : row.status === 'duplicate' ? 'Doublon' : 'Erreur';
      card.append(newEl('strong', '', row.name || row.filename || 'Torrent'),
        badge(status, row.status === 'ok' || row.status === 'added' ? 'good' : 'warn'));
      if (row.hash) card.append(newEl('p', 'muted', 'Infohash : ' + row.hash));
      if (row.domains?.length) card.append(newEl('p', 'muted', 'Trackers : ' + row.domains.join(', ')));
      if (isResult && row.status === 'added' && row.storedFileName)
        card.append(newEl('p', 'muted', 'Fichier enregistré : ' + row.storedFileName));
      if (row.message) card.append(newEl('p', 'muted', row.message));
      if (!isResult && row.status === 'ok' && !row.matchedDomains?.length)
        card.append(newEl('p','muted','Aucune règle active ne couvre ce tracker : le torrent restera en attente.'));
      root.append(card);
    }
    root.classList.remove('hidden');
  }
  async function inspectImport() {
    const revision = importRevision;
    const files = selectedImportFiles();
    if (!files.length) return;
    try {
      const response = await api('/api/import/inspect', {method:'POST', body:importPayload(files)});
      if (revision !== importRevision || !sameFiles(selectedImportFiles(), files)) return;
      const rows = importRows(response, files);
      const valid = rows.filter(row => row.status === 'ok').length;
      inspectedFiles = valid ? files : [];
      showImportRows(rows);
      $('import-submit').disabled = !valid;
      $('import-message').textContent = `${valid} fichier(s) analysé(s) sur ${files.length}. ${valid ? 'Clique sur « Ajouter à JOAL uniquement » pour les enregistrer.' : 'Aucun fichier valide.'}`;
    } catch (e) {
      if (revision !== importRevision || !sameFiles(selectedImportFiles(), files)) return;
      inspectedFiles = [];
      $('import-submit').disabled = true;
      $('import-message').textContent = 'Analyse impossible : ' + e.message;
      showToast(e.message, true);
    }
  }
  async function onImportFileChanged() {
    resetImport();
    if (selectedImportFiles().length) await inspectImport();
  }
  async function submitImport(event) {
    event.preventDefault();
    const files = selectedImportFiles();
    if (!files.length || !sameFiles(files, inspectedFiles)) {
      return showToast('Attendre la fin de l’analyse automatique avant l’importation.', true);
    }
    const button = $('import-submit'); button.disabled = true;
    $('import-message').textContent = `Enregistrement de ${files.length} fichier(s)…`;
    try {
      const response = await api('/api/import/add', {method:'POST', body:importPayload(files)});
      const rows = importRows(response, files);
      const added = rows.filter(x => x.status === 'added').length;
      const duplicate = rows.filter(x => x.status === 'duplicate').length;
      const errors = rows.filter(x => x.status === 'error').length;
      // Invalidates in-flight analysis requests; results remain visible after selection reset.
      importRevision++;
      inspectedFiles = [];
      $('import-file').value = '';
      applyLibraryEdit(rows.filter(row => row.status === 'added' && row.item).map(row => row.item));
      showImportRows(rows, true);
      $('import-message').textContent = `${added} importé(s), ${duplicate} doublon(s), ${errors} erreur(s).`;
      showToast(`${added} fichier(s) ajouté(s) à JOAL uniquement.`);
      await fetchStatus();
    } catch (e) {
      showToast(e.message, true);
      $('import-message').textContent = e.message;
      button.disabled = false;
    }
  }

  function bindSortControls(selector, dataKey, resetId, sort, render) {
    const buttons = document.querySelectorAll(selector);
    function apply(button) {
      const key = button?.dataset[dataKey] ?? null;
      sort.direction = key && key === sort.key && sort.direction === 'asc' ? 'desc' : 'asc';
      sort.key = key;
      for (const item of buttons) {
        const active = item === button;
        item.setAttribute('aria-pressed', String(active));
        item.querySelector('span').textContent = active ? (sort.direction === 'asc' ? '↑' : '↓') : '↕';
        item.closest('th').removeAttribute('aria-sort');
      }
      if (button) button.closest('th').setAttribute('aria-sort', sort.direction === 'asc' ? 'ascending' : 'descending');
      $(resetId).hidden = !key;
      render();
    }
    for (const button of buttons) button.addEventListener('click', () => apply(button));
    $(resetId).addEventListener('click', () => apply(null));
  }

  function renderSolo() {
    renderWatchFolder();
    const solo = (app.state || {}).solo || {count:0, items:[]};
    setSelectionScope('import', solo.items || []);
    $('solo-count').textContent = `${solo.count || 0} torrent(s) · ${solo.active || 0} actif(s)`;
    const body = $('solo-items'); clear(body);
    if (!(solo.items || []).length) {
      const tr = newEl('tr'); const td = newEl('td', 'empty', 'Aucun fichier .torrent importé dans JOAL uniquement.');
      td.colSpan = 7; tr.append(td); body.append(tr); return;
    }
    const upload = app.state?.joalUpload;
    const rows = solo.items.map(item => {
      const t = withActivity(item);
      const peers = displayedPeers(t);
      return {...t, seeders: peers.seeders, homeLeechers: peers.leechers,
        assignedRule: app.config?.torrentRuleOverrides?.[t.infoHash] || ruleLabel(t.rule),
        soloStatus: t.importBlocked ? 'Bloqué' : t.simulationActive ? 'JOAL actif' : (t.selected && app.state?.dryRun ? 'Prévisualisé' : 'En attente'),
        rate: upload && Object.hasOwn(upload.byInfoHash, t.infoHash) ? upload.byInfoHash[t.infoHash] : upload ? 0 : null,
        volume: upload?.totalUploadedBytes == null ? null : upload.totalUploadedBytes[t.infoHash] ?? 0};
    });
    for (const t of TorrentTable.sortRows(rows, soloSort.key, soloSort.direction)) {
      const tr = newEl('tr');
      const title = cell(t.name); if (t.simulationActive) title.classList.add('active-torrent'); title.title = t.name; title.prepend(modeControls(t)); title.append(joalMeasurement(t));
      tr.append(title, trackerCell(t), ruleCell(t),
        (()=>{const td = newEl('td'); td.append(swarmSummary(t)); return td;})(), cell(t.threshold == null ? '—' : t.threshold));
      const status = newEl('td'); status.append(badge(t.soloStatus, t.simulationActive ? 'good' : 'warn'), newEl('div','muted',t.reason));
      tr.append(status);
      const actions = newEl('td');
      const remove = newEl('button', 'btn btn-compact btn-outline', 'Retirer');
      remove.type = 'button';
      remove.disabled = removing.has(t.infoHash);
      if (remove.disabled) remove.textContent = 'Retrait en cours…';
      remove.addEventListener('click', async () => {
        if (!confirm(`Retirer « ${t.name} » de JOAL uniquement ? Le fichier .torrent sera supprimé de la bibliothèque, jamais les données qBittorrent.`)) return;
        removing.add(t.infoHash);
        remove.disabled = true;
        remove.textContent = 'Retrait en cours…';
        try {
          await api('/api/solo/delete', {method:'POST', headers:{'Content-Type':'application/json'},
            body:JSON.stringify({infoHash:t.infoHash})});
          applyLibraryEdit([], t.infoHash);
          showToast('Fichier .torrent retiré.');
        } catch(e) {showToast(e.message,true);}
        finally { removing.delete(t.infoHash); renderSolo(); void fetchStatus(); }
      });
      actions.append(diagnosticButton(t), peerChartButton(t), remove); tr.append(actions); body.append(tr);
    }
  }

  const diagValue = value => value == null ? 'Non disponible' : String(value);
  const shortHash = value => value ? String(value).slice(0, 12) + '…' : '—';
  const isoOrDash = value => value ? formatTime(value) : '—';
  let lastDiagnostic = null;
  async function renderDiagnostics() {
    if (app.view !== 'diagnostics') return;
    try {
      const [health, diag] = await Promise.all([api('/api/health'), api('/api/diagnostics')]);
      lastDiagnostic = {health, ...diag};
      $('diag-qbit').textContent = health.qbitConnected ? 'Connecté' : 'Indisponible';
      $('diag-processes').textContent = String(health.activeProcesses ?? '—');
      $('diag-inventory').textContent = health.inventoryAgeSeconds == null ? 'Inconnu' : (health.inventoryAgeSeconds < 60 ? health.inventoryAgeSeconds + ' secondes' : displayDuration(health.inventoryAgeSeconds));
      $('diag-scrape').textContent = isoOrDash(health.trackerMonitorLastAt);
      const events = $('diag-events'); clear(events);
      for (const item of diag.events || []) {
        const tr = newEl('tr');
        tr.append(cell(isoOrDash(item.time)), cell((item.level === 'error' ? '⚠ ' : '') + (item.message || item.code || '—')),
          cell(shortHash(item.infoHash)), cell(ruleLabel(item.rule) || '—'));
        events.append(tr);
      }
      if (!diag.events?.length) tableEmpty(events, 4, 'Aucun événement enregistré.');
      const sessions = $('diag-sessions'); clear(sessions);
      for (const entry of diag.sessions || []) {
        const tr = newEl('tr');
        tr.append(cell(isoOrDash(entry.startedAt)), cell(isoOrDash(entry.endedAt)), cell(shortHash(entry.infoHash)),
          cell(ruleLabel(entry.rule) || '—'), cell(entry.status || '—'),
          cell(entry.durationSeconds == null ? 'En cours / inconnu' : displayDuration(entry.durationSeconds)),
          cell('Non mesuré'));
        sessions.append(tr);
      }
      if (!diag.sessions?.length) tableEmpty(sessions, 7, 'Aucune session enregistrée.');
      $('diag-notice').textContent = diag.loadWarning ? 'Attention : ancien historique invalide, nouveau journal initialisé.' : 'Historique local borné à 1 500 événements et 400 sessions ; aucun accès tracker sensible sauvegardé.';
    } catch (error) { showToast('Diagnostics : ' + error.message, true); }
  }
  async function openTorrentHistory(t) {
    const dlg = $('torrent-history-dialog');
    $('history-title').textContent = 'Historique du torrent';
    $('history-subtitle').textContent = t.name + ' · ' + t.infoHash;
    $('history-content').textContent = 'Chargement de l’historique…';
    if (typeof dlg.showModal === 'function') dlg.showModal(); else dlg.setAttribute('open', '');
    try {
      const data = await api('/api/diagnostics?infoHash=' + encodeURIComponent(t.infoHash));
      const wrap = $('history-content'); clear(wrap);
      const title1 = newEl('h3', null, 'Événements'); wrap.append(title1);
      if (!data.events?.length) wrap.append(newEl('p', 'muted', 'Aucun changement enregistré.'));
      for (const event of data.events || []) wrap.append(newEl('div', 'history-entry',
        isoOrDash(event.time) + ' · ' + (event.message || event.code || '—')));
      wrap.append(newEl('h3', null, 'Sessions JOAL'));
      if (!data.sessions?.length) wrap.append(newEl('p', 'muted', 'Aucune session enregistrée.'));
      for (const session of data.sessions || []) wrap.append(newEl('div', 'history-entry',
        isoOrDash(session.startedAt) + ' → ' + isoOrDash(session.endedAt) +
        ' · ' + (session.status || '—') + ' · Compteur JOAL : non mesuré'));
    } catch (error) { $('history-content').textContent = error.message; }
  }
  async function exportDiagnostics() {
    if (!lastDiagnostic) await renderDiagnostics();
    if (!lastDiagnostic) return;
    const blob = new Blob([JSON.stringify(lastDiagnostic, null, 2)], {type:'application/json'});
    const url = URL.createObjectURL(blob);
    const link = newEl('a'); link.href = url; link.download = 'joal-smart-diagnostics.json';
    document.body.append(link); link.click(); link.remove(); URL.revokeObjectURL(url);
  }

  // Remember which sidebar groups and page sections are open, per browser.
  // Storage can be unavailable (private mode, blocked): then sections keep their default.
  function rememberFolds() {
    for (const fold of document.querySelectorAll('details')) {
      const label = fold.querySelector(':scope > summary')?.textContent.trim();
      if (!label) continue;
      const key = 'joal.fold.' + label;
      try {
        const saved = localStorage.getItem(key);
        if (saved !== null) fold.open = saved === '1';
      } catch (_) { return; }
      fold.addEventListener('toggle', () => {
        try { localStorage.setItem(key, fold.open ? '1' : '0'); } catch (_) { /* best effort */ }
      });
    }
  }
  // Interface design: saved server-side, applied at once by swapping the stylesheet.
  function applyDesign(design, stylesheet) {
    const link = document.querySelector('link[rel="stylesheet"]');
    if (link && stylesheet && link.getAttribute('href') !== stylesheet) link.setAttribute('href', stylesheet);
    document.documentElement.dataset.design = design;
    document.querySelector('meta[name="color-scheme"]')?.setAttribute('content', design === 'baie' ? 'light' : 'dark');
  }
  function initDesignPicker() {
    const inputs = [...document.querySelectorAll('input[name="interface-design"]')];
    const sync = () => {
      const current = document.documentElement.dataset.design || 'classique';
      for (const input of inputs) input.checked = input.value === current;
    };
    sync();
    for (const input of inputs) {
      input.addEventListener('change', async () => {
        if (!input.checked) return;
        const name = input.closest('label')?.querySelector('strong')?.textContent || input.value;
        try {
          const result = await api('/api/interface-design', {method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({design: input.value})});
          applyDesign(result.design, result.stylesheet);
          $('design-message').textContent = 'Design « ' + name + ' » appliqué.';
        } catch (error) {
          sync();
          $('design-message').textContent = 'Design non modifié : ' + error.message;
        }
      });
    }
  }
  async function init() {
    const auth = await fetch('/api/auth/session', {credentials:'same-origin', cache:'no-store'})
      .then(response => response.ok ? response.json() : null).catch(() => null);
    if (!auth || !auth.authenticated || !auth.csrfToken) {
      location.replace('/login');
      return;
    }
    app.csrf = auth.csrfToken;
    rememberFolds();
    initDesignPicker();
    $('logout-btn').addEventListener('click', async () => {
      try { await api('/api/auth/logout', {method:'POST'}); }
      catch (_) { /* Even if expired, do not keep an apparently authenticated UI. */ }
      location.replace('/login');
    });
    $('diag-refresh').addEventListener('click', renderDiagnostics);
    $('diag-export').addEventListener('click', exportDiagnostics);
    $('history-close').addEventListener('click', () => $('torrent-history-dialog').close());
    $('swarm-history-close').addEventListener('click', () => $('swarm-history-dialog').close());
    $('peer-chart-close').addEventListener('click', () => $('peer-chart-dialog').close());
    $('health-refresh').addEventListener('click', renderHealth);
    $('notifications-toggle').addEventListener('click', async () => { $('notification-panel').classList.toggle('hidden'); if (!$('notification-panel').classList.contains('hidden')) await refreshNotifications(); });
    $('notification-close').addEventListener('click', () => $('notification-panel').classList.add('hidden'));
    $('notification-read').addEventListener('click', async () => { try { await api('/api/notifications/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({all:true})}); await refreshNotifications(); } catch(e){showToast(e.message,true);} });
    $('backup-export').addEventListener('click', downloadFullBackup);
    $('backup-restore').addEventListener('click', uploadFullBackup);
    initTorrentColumnControls();
    for (const el of document.querySelectorAll('[data-view]')) el.addEventListener('click', () => changeView(el.dataset.view));
    for (const button of document.querySelectorAll('[data-batch-mode]')) {
      button.addEventListener('click', () => applySimulationMode([...selectedModes],button.dataset.batchMode,true));
    }
    for (const button of document.querySelectorAll('[data-batch-rule]')) {
      button.addEventListener('click', () => openTorrentRuleDialog([...selectedModes]));
    }
    for (const button of document.querySelectorAll('[data-batch-remove]')) {
      button.addEventListener('click', removeSelectedLibrary);
    }
    for (const button of document.querySelectorAll('[data-toggle-all]')) {
      button.addEventListener('click', () => {
        const hashes = selectionScopes.get(button.dataset.toggleAll) || [];
        const remove = hashes.every(h => selectedModes.has(h));
        for (const h of hashes) {
          if (remove) selectedModes.delete(h);
          else selectedModes.add(h);
        }
        syncModeControls();
      });
    }
    for (const button of document.querySelectorAll('[data-clear-mode]')) {
      button.addEventListener('click', () => { selectedModes.clear(); syncModeControls(); });
    }
    $('chart-prev').addEventListener('click', () => selectChart((chart.end ?? chart.serverNow ?? Math.floor(Date.now() / 1000)) - 3600));
    $('chart-next').addEventListener('click', () => {
      const next = chart.end + 3600;
      selectChart(next >= Math.floor(Date.now() / 1000) ? null : next);
    });
    $('chart-live').addEventListener('click', () => selectChart(null));
    $('chart-date').addEventListener('change', e => {
      const time = new Date(e.target.value).getTime() / 1000;
      if (Number.isFinite(time)) selectChart(Math.min(Math.floor(Date.now() / 1000), Math.max(time, (chart.earliest ?? 0) + 3600)));
    });
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshChart(); });
    refreshChart();
    setInterval(() => { if (chart.end == null) refreshChart(); }, 1000);
    $('view-picker').addEventListener('change', e => {
      changeView(e.target.value);
      e.target.value = app.view;
    });
    $('pause-all').addEventListener('click', async () => {
      if (app.pauseBusy) return;
      const action = app.state.simulationsPaused ? 'resume' : 'pause';
      app.pauseBusy = true;
      $('pause-all').disabled = true;
      try {
        await api('/api/simulations/' + action, {method: 'POST'});
        await fetchStatus();
      } catch (error) { showToast(error.message, true); }
      finally { app.pauseBusy = false; renderStatus(); }
    });
    $('refresh-btn').addEventListener('click', async () => { await fetchStatus(); showToast('Inventaire actualisé depuis le dernier relevé serveur.'); });
    $('import-file').addEventListener('change', onImportFileChanged);
    $('import-form').addEventListener('submit', submitImport);
    const resetTorrentPage = () => { app.page = 1; renderTorrents(); };
    $('torrent-search').addEventListener('input', resetTorrentPage);
    $('torrent-status').addEventListener('change', resetTorrentPage);
    $('torrent-mode').addEventListener('change', resetTorrentPage);
    $('torrent-reset-sort').addEventListener('click', () => {
      app.sortKey = null;
      resetTorrentPage();
    });
    bindSortControls('[data-home-sort]', 'homeSort', 'overview-reset-sort', homeSort, renderOverview);
    bindSortControls('[data-solo-sort]', 'soloSort', 'solo-reset-sort', soloSort, renderSolo);
    $('overview-search').addEventListener('input', renderOverview);
    $('torrent-rule').addEventListener('change', resetTorrentPage);
    $('torrent-page-size').addEventListener('change', () => {
      app.pageSize = Number($('torrent-page-size').value);
      resetTorrentPage();
    });
    for (const button of document.querySelectorAll('[data-sort-key]')) {
      button.addEventListener('click', () => {
        const key = button.dataset.sortKey;
        if (app.sortKey === key) app.sortDirection = app.sortDirection === 'asc' ? 'desc' : 'asc';
        else {
          app.sortKey = key;
          app.sortDirection = ['leechers', 'ratio', 'uploaded', 'addedOn', 'status'].includes(key) ? 'desc' : 'asc';
        }
        resetTorrentPage();
      });
    }
    $('torrent-prev').addEventListener('click', () => { app.page--; renderTorrents(); });
    $('torrent-next').addEventListener('click', () => { app.page++; renderTorrents(); });
    $('add-rule').addEventListener('click', () => { if (app.config.rules.length >= 20) return showToast('Maximum de 20 règles.', true); app.config.rules.push({ id: uniqueId('tracker'), enabled: false, trackerDomains: ['tracker.exemple.org'], minLeechers: 3, minUploadRate: 30, maxUploadRate: 160, simultaneousSeed: 10, targetRatio: 0, client: '' }); renderRuleEditor(); updateDirty(); });
    $('save-rules').addEventListener('click', () => saveConfig(app.config));
    $('cancel-rules').addEventListener('click', () => { if (dirty() && !confirm('Annuler les modifications non enregistrées ?')) return; app.config = deepcopy(app.saved); renderRuleEditor(); renderSettings(); });
    $('setting-filter-mode').addEventListener('change', () => {
      app.config.qbitFilterMode = $('setting-filter-mode').value;
      renderSettings();
      updateDirty();
    });
    for (const [id, key, isNumber] of [['setting-category','qbitCategory',false],['setting-tag','qbitTag', false],['setting-refresh','refreshSeconds', true],['setting-max','maxCandidates',true],['setting-completed','completedTag',false],['setting-guard','guardSeconds',true]]) {
      $(id).addEventListener('input', () => { app.config[key] = isNumber ? ($(id).value === '' ? null : Number($(id).value)) : $(id).value; updateDirty(); });
    }
    $('setting-dryrun').addEventListener('change', () => { app.config.dryRun = $('setting-dryrun').checked; updateDirty(); });
    const cfgSwarm = () => (app.config.swarm ||= { enabled: true, intervalMinutes: 15, maxAgeMinutes: 45 });
    $('setting-swarm-enabled').addEventListener('change', () => { cfgSwarm().enabled = $('setting-swarm-enabled').checked; updateDirty(); });
    for (const [id, key] of [['setting-swarm-interval','intervalMinutes'],['setting-swarm-age','maxAgeMinutes']]) {
      $(id).addEventListener('input', () => { cfgSwarm()[key] = $(id).value === '' ? null : Number($(id).value); updateDirty(); });
    }

    const cfgHandoff = () => (app.config.handoff ||= {enabled: false, cooldownMinutes: 60, confirmations: 2, fastTrackMargin: 5});
    $('setting-handoff-enabled').addEventListener('change', () => {
      const h = cfgHandoff();
      if ($('setting-handoff-enabled').checked && !confirm('Activer le pilotage automatique des arrêts/reprises qBittorrent ? Les mesures scrape récentes et les protections anti-basculement sont obligatoires.')) {
        $('setting-handoff-enabled').checked = false;
      }
      h.enabled = $('setting-handoff-enabled').checked;
      updateDirty();
    });
    for (const [id, key] of [['setting-handoff-cooldown', 'cooldownMinutes'], ['setting-handoff-confirmations', 'confirmations'], ['setting-handoff-fast-margin', 'fastTrackMargin']]) {
      $(id).addEventListener('input', () => {cfgHandoff()[key] = $(id).value === '' ? null : Number($(id).value); updateDirty();});
    }
    const cfgCleanup = () => (app.config.cleanup ||= { enabled: false, deleteData: false, afterDays: 3, scrapeEveryMinutes: 60 });
    $('setting-cleanup-enabled').addEventListener('change', () => {
      const c = cfgCleanup(); c.enabled = $('setting-cleanup-enabled').checked;
      if (!c.enabled) { c.deleteData = false; $('setting-cleanup-delete').checked = false; }
      updateDirty();
    });
    $('setting-cleanup-delete').addEventListener('change', () => {
      const input = $('setting-cleanup-delete'); const c = cfgCleanup();
      if (input.checked && (!c.enabled || !confirm('ATTENTION : vous autorisez la suppression permanente des torrents ET de leurs fichiers une fois le seuil atteint et vérifié. Une sauvegarde est-elle en place ?'))) {
        input.checked = false;
      }
      c.deleteData = input.checked; updateDirty();
    });
    for (const [id, key] of [['setting-cleanup-days','afterDays'],['setting-cleanup-frequency','scrapeEveryMinutes']]) {
      $(id).addEventListener('input', () => { cfgCleanup()[key] = $(id).value === '' ? null : Number($(id).value); updateDirty(); });
    }
    $('qbit-connection').addEventListener('submit', event => { event.preventDefault(); saveQbit(false); });
    $('qbit-docker').addEventListener('click', () => saveQbit(true));
    $('save-settings').addEventListener('click', () => saveConfig(app.config));
    $('cancel-settings').addEventListener('click', () => { if (dirty() && !confirm('Annuler les modifications non enregistrées ?')) return; app.config = deepcopy(app.saved); renderSettings(); renderRuleEditor(); });
    $('json-editor').addEventListener('input', () => { app.jsonDirty = true; $('json-save-status').textContent = 'JSON modifié, non enregistré'; });
    $('cancel-json').addEventListener('click', () => { if (app.jsonDirty && !confirm('Recharger le JSON et perdre les changements ?')) return; $('json-editor').value = JSON.stringify(app.config, null, 2); app.jsonDirty = false; });
    $('save-json').addEventListener('click', async () => { let c; try { c = JSON.parse($('json-editor').value); } catch (error) { return showToast('JSON incorrect : ' + error.message, true); } if (await saveConfig(c)) { $('json-save-status').textContent = 'Enregistré'; renderRuleEditor(); renderSettings(); } });
    $('export-policy').addEventListener('click', () => {
      if (!app.saved) return showToast('Configuration indisponible.', true);
      const blob = new Blob([JSON.stringify(app.saved, null, 2) + '\n'], {type:'application/json'});
      const url = URL.createObjectURL(blob);
      const link = newEl('a'); link.href = url; link.download = 'joal-smart-policy-backup.json';
      document.body.append(link); link.click(); link.remove(); URL.revokeObjectURL(url);
    });
    $('restore-policy').addEventListener('click', async () => {
      const file = $('restore-policy-file').files[0];
      if (!file) return showToast('Sélectionne un fichier JSON de sauvegarde.', true);
      if (file.size > 60000) return showToast('Fichier trop volumineux (60 Kio maximum).', true);
      let restored;
      try { restored = JSON.parse(await file.text()); }
      catch (_) { return showToast('Le fichier n’est pas un JSON valide.', true); }
      if (!restored || typeof restored !== 'object' || Array.isArray(restored)) return showToast('Format de sauvegarde incorrect.', true);
      if (!confirm('Remplacer la configuration actuelle par ce fichier ? Sauvegarde les règles actuelles avant de continuer. Les changements seront appliqués immédiatement.')) return;
      if (await saveConfig(restored)) {
        $('json-editor').value = JSON.stringify(app.config, null, 2);
        renderRuleEditor(); renderSettings();
        $('restore-policy-file').value = '';
      }
    });
    try { await fetchConfig(); } catch (error) { $('alert').textContent = 'Erreur de configuration : ' + error.message; $('alert').classList.remove('hidden'); }
    try { app.clients = (await api('/api/clients')).clients || []; } catch (_e) { /* clients optional for dry-run */ }
    await fetchStatus();
    await refreshNotifications();
    setInterval(fetchStatus, 5000);
    setInterval(() => { if (app.view === 'health') renderHealth(); }, 10000);
    setInterval(refreshNotifications, 30000);
  }
  init();
})();
