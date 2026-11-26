/* Pure torrent-table helpers; shared by the browser and the Node test suite. */
(function (root, factory) {
  const helpers = factory();
  if (typeof module === 'object' && module.exports) module.exports = helpers;
  if (root) root.TorrentTable = helpers;
})(typeof window !== 'undefined' ? window : globalThis, function () {
  'use strict';
  const PAGE_SIZES = Object.freeze([10, 20, 50, 100, 500]);
  const NUMERIC_KEYS = new Set(['threshold', 'rate', 'volume', 'seeders', 'homeLeechers', 'leechers', 'ratio', 'uploaded', 'addedOn', 'status']);
  const collator = new Intl.Collator('fr', { numeric: true, sensitivity: 'base' });

  function sortValue(row, key) {
    if (key === 'trackers') return Array.isArray(row.trackers) ? row.trackers.join(', ') : '';
    if (key === 'status') {
      // Group by status in the same progression as the table badges.
      return row.selected ? 5 : row.eligible ? 4 : Number(row.progress) < 100 ? 3 :
        !row.stopped ? 2 : /en attente|à vérifier/i.test(row.reason || '') ? 1 : 0;
    }
    if (NUMERIC_KEYS.has(key)) {
      // Sort on the value shown in the table, including clearly marked
      // informational qBittorrent / partial-scrape counts.
      if (key === 'leechers' && Number.isFinite(row.joalLeechers)) return row.joalLeechers;
      if (key === 'uploaded') {
        const bytes = row.upload?.uploadedBytes;
        return Number.isFinite(bytes) && bytes >= 0 ? bytes : null;
      }
      const value = key === 'leechers' && (row.leechers == null || row.leechers < 0)
        ? (row.displayLeechers ?? row.leechers) : row[key];
      if (value == null || value === '' || typeof value === 'boolean') return null;
      const n = Number(value);
      if (!Number.isFinite(n) || (['homeLeechers','seeders','leechers'].includes(key) && n < 0) ||
          (key === 'addedOn' && n <= 0) || (key === 'ratio' && n < 0)) return null;
      return n;
    }
    const value = key === 'name' || key === 'rule' || key === 'reason' || key === 'soloStatus' || key === 'assignedRule' || key === 'mode' ? row[key] : null;
    return value == null || !String(value).trim() ? null : String(value);
  }

  function sortRows(rows, key, direction = 'asc') {
    if (!key) {
      const active = row => Boolean(row.simulationActive ?? (row.active || row.joalSession?.active));
      return rows.slice().sort((a, b) => Number(active(b)) - Number(active(a)) ||
        collator.compare(a.name || '', b.name || '') ||
        collator.compare(a.infoHash || '', b.infoHash || ''));
    }
    const multiplier = direction === 'desc' ? -1 : 1;
    const numeric = NUMERIC_KEYS.has(key);
    return rows.map((row, index) => ({ row, index, value: sortValue(row, key) }))
      .sort((a, b) => {
        // Unknown values stay at the bottom in both directions, never equal to 0.
        if (a.value == null) return b.value == null ? a.index - b.index : 1;
        if (b.value == null) return -1;
        const compared = numeric ? a.value - b.value : collator.compare(a.value, b.value);
        return compared === 0 ? a.index - b.index : Math.sign(compared) * multiplier;
      })
      .map(entry => entry.row);
  }

  function paginate(rows, pageSize = 100, page = 1) {
    const size = PAGE_SIZES.includes(Number(pageSize)) ? Number(pageSize) : 100;
    const totalPages = Math.max(1, Math.ceil(rows.length / size));
    const requested = Number.isFinite(Number(page)) ? Math.trunc(Number(page)) : 1;
    const current = Math.max(1, Math.min(totalPages, requested));
    const start = (current - 1) * size;
    const end = Math.min(start + size, rows.length);
    return {
      rows: rows.slice(start, end), page: current, pageSize: size,
      totalPages, total: rows.length, from: rows.length ? start + 1 : 0, to: end
    };
  }
  return { PAGE_SIZES, sortRows, paginate, sortValue };
});
