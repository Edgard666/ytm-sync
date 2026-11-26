/* YTM Sync — tri et filtre par colonne pour tous les tableaux */
(function () {
  const norm = s => String(s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().trim();

  function cellText(td) {
    if (!td) return '';
    if (td.dataset.sort !== undefined) return td.dataset.sort;
    const inp = td.querySelector('input[type=text],select');
    if (inp) return inp.value || inp.placeholder || '';
    const cb = td.querySelector('input[type=checkbox]');
    if (cb && !td.textContent.trim()) return cb.checked ? '1' : '0';
    return td.textContent.replace(/\s+/g, ' ').trim();
  }

  // Valeur triable : date FR, duree m:ss, nombre, sinon texte
  function sortKey(t) {
    if (!t || t === '—') return null;
    let m = t.match(/^(\d{2})\/(\d{2})\/(\d{4})(?:\s+(\d{2}):(\d{2}))?/);
    if (m) return { n: Date.UTC(+m[3], m[2] - 1, +m[1], +(m[4] || 0), +(m[5] || 0)) };
    m = t.match(/^(\d+):(\d{2})$/);
    if (m) return { n: +m[1] * 60 + +m[2] };
    if (/^-?\d+([.,]\d+)?\s*%?$/.test(t)) return { n: parseFloat(t.replace(',', '.')) };
    return { s: norm(t) };
  }

  function compare(a, b) {
    if (a === null && b === null) return 0;
    if (a === null) return 1;            // vides toujours en bas
    if (b === null) return -1;
    if (a.n !== undefined && b.n !== undefined) return a.n - b.n;
    const sa = a.s !== undefined ? a.s : String(a.n), sb = b.s !== undefined ? b.s : String(b.n);
    return sa.localeCompare(sb, 'fr', { numeric: true });
  }

  function enhance(table) {
    if (table.dataset.enhanced || table.dataset.noenhance !== undefined) return;
    const head = [...table.rows].find(r => r.querySelector('th'));
    if (!head) return;
    table.dataset.enhanced = '1';
    const ths = [...head.cells];
    const filters = {};
    let sortCol = -1, sortDir = 1;
    const dataRows = () => [...table.rows].filter(r => r !== head && r.cells.length === ths.length && !r.querySelector('th'));

    function applyFilters() {
      const active = Object.entries(filters).filter(([, v]) => v);
      dataRows().forEach(r => {
        const ok = active.every(([i, v]) => norm(cellText(r.cells[i])).includes(v));
        r.classList.toggle('cf-hide', !ok);
      });
      table.dispatchEvent(new CustomEvent('tablefiltered', { bubbles: true }));
    }

    function sortBy(i) {
      sortDir = sortCol === i ? -sortDir : 1;
      sortCol = i;
      const rows = dataRows();
      const parent = rows.length ? rows[0].parentNode : null;
      if (!parent) return;
      const tail = [...parent.children].filter(r => !rows.includes(r) && r !== head);
      rows.map(r => ({ r, k: sortKey(cellText(r.cells[i])) }))
        .sort((a, b) => { const c = compare(a.k, b.k); return (a.k === null || b.k === null) ? c : c * sortDir; })
        .forEach(x => parent.appendChild(x.r));
      tail.forEach(r => parent.appendChild(r));   // lignes "aucun resultat" en bas
      ths.forEach((t, j) => t.classList.toggle('sort-asc', j === i && sortDir === 1));
      ths.forEach((t, j) => t.classList.toggle('sort-desc', j === i && sortDir === -1));
    }

    ths.forEach((th, i) => {
      const label = th.textContent.trim();
      if (!label) return;                  // colonnes d'actions / cases a cocher
      th.classList.add('sortable');
      th.innerHTML = '';
      const top = document.createElement('div');
      top.className = 'th-top';
      const lab = document.createElement('span');
      lab.textContent = label;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'th-filter';
      btn.title = 'Filtrer cette colonne';
      btn.textContent = '⌕';
      top.append(lab, btn);
      const box = document.createElement('div');
      box.className = 'th-box';
      box.style.display = 'none';
      const inp = document.createElement('input');
      inp.type = 'text';
      inp.placeholder = 'Filtrer…';
      const clr = document.createElement('button');
      clr.type = 'button';
      clr.className = 'th-clear';
      clr.textContent = '×';
      clr.title = 'Effacer le filtre';
      box.append(inp, clr);
      th.append(top, box);

      th.addEventListener('click', ev => { if (!box.contains(ev.target) && ev.target !== btn) sortBy(i); });
      btn.addEventListener('click', ev => {
        ev.stopPropagation();
        const open = box.style.display === 'none';
        box.style.display = open ? 'flex' : 'none';
        if (open) inp.focus();
      });
      inp.addEventListener('input', () => {
        filters[i] = norm(inp.value);
        btn.classList.toggle('on', !!inp.value);
        applyFilters();
      });
      inp.addEventListener('keydown', ev => { if (ev.key === 'Enter') ev.preventDefault(); });
      clr.addEventListener('click', ev => {
        ev.stopPropagation();
        inp.value = '';
        filters[i] = '';
        btn.classList.remove('on');
        box.style.display = 'none';
        applyFilters();
      });
    });
  }

  function scan(root) { (root.querySelectorAll ? root : document).querySelectorAll('table').forEach(enhance); }
  document.addEventListener('DOMContentLoaded', () => {
    scan(document);
    new MutationObserver(muts => muts.forEach(m => m.addedNodes.forEach(n => {
      if (n.nodeType !== 1) return;
      if (n.tagName === 'TABLE') enhance(n); else scan(n);
    }))).observe(document.body, { childList: true, subtree: true });
  });
})();
