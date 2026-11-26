'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { PAGE_SIZES, sortRows, paginate } = require('../app/static/torrent_table.js');

test('pagination: all offered sizes and 57 torrents split without dropping any', () => {
  const rows = Array.from({ length: 57 }, (_, i) => ({ name: `Torrent ${i + 1}` }));
  assert.deepEqual(PAGE_SIZES, [10, 20, 50, 100, 500]);
  for (const size of PAGE_SIZES) {
    const totalPages = Math.ceil(57 / size);
    const pages = Array.from({ length: totalPages }, (_, i) => paginate(rows, size, i + 1));
    assert.equal(pages[0].totalPages, totalPages);
    assert.deepEqual(pages.flatMap(p => p.rows), rows);
  }
  assert.equal(paginate(rows, 20, 3).from, 41);
  assert.equal(paginate(rows, 20, 3).to, 57);
  assert.equal(paginate(rows, 20, 99).page, 3); // inventory shrinks while user is paginating
  assert.deepEqual(paginate([], 10, 15).rows, []);
  assert.equal(paginate([], 10, 15).page, 1);
});

test('leecher sort is numeric, unknown always last, zero is not unknown', () => {
  const rows = [{ name: '10', leechers: 10 }, { name: 'unknown', leechers: -1 },
    { name: '2', leechers: 2 }, { name: 'zero', leechers: 0 },
    { name: 'null', leechers: null }, { name: '100', leechers: 100 }];
  assert.deepEqual(sortRows(rows, 'leechers', 'desc').map(x => x.name),
    ['100', '10', '2', 'zero', 'unknown', 'null']);
  assert.deepEqual(sortRows(rows, 'leechers', 'asc').map(x => x.name),
    ['zero', '2', '10', '100', 'unknown', 'null']);
});

test('ratio and addedOn date sort as numbers, with missing at end', () => {
  const rows = [
    { name: 'A', ratio: 9.1, addedOn: 1700000000 },
    { name: 'B', ratio: 1.2, addedOn: 1800000000 },
    { name: 'C', ratio: null, addedOn: null },
    { name: 'D', ratio: 10.0, addedOn: 1600000000 }
  ];
  assert.deepEqual(sortRows(rows, 'ratio', 'desc').map(r => r.name), ['D', 'A', 'B', 'C']);
  assert.deepEqual(sortRows(rows, 'addedOn', 'asc').map(r => r.name), ['D', 'A', 'B', 'C']);
  assert.deepEqual(sortRows(rows, 'addedOn', 'desc').map(r => r.name), ['B', 'A', 'D', 'C']);
});

test('text sorts accented names and uses natural number ordering', () => {
  const rows = [{ name: 'Film 10', rule: 'Beta', trackers: ['tracker.beta.test'] },
    { name: 'Film 2', rule: 'Alpha', trackers: ['tracker.alpha.test'] },
    { name: 'Été', rule: 'Gamma', trackers: ['tracker.gamma.test'] }];
  assert.deepEqual(sortRows(rows, 'name', 'asc').map(r => r.name), ['Été', 'Film 2', 'Film 10']);
  assert.deepEqual(sortRows(rows, 'rule', 'asc').map(r => r.rule), ['Alpha', 'Beta', 'Gamma']);
  assert.deepEqual(sortRows(rows, 'trackers', 'desc').map(r => r.rule), ['Gamma', 'Beta', 'Alpha']);
  assert.deepEqual(sortRows(rows, null).map(r => r.name), ['Été', 'Film 2', 'Film 10']);
});

test('filter first, then sort entire filtered inventory, then paginate', () => {
  const all = Array.from({ length: 57 }, (_, i) => ({ name: `Torrent ${i}`, leechers: i, rule: i % 2 ? 'alpha' : 'beta' }));
  const filtered = all.filter(r => r.rule === 'alpha');
  const sorted = sortRows(filtered, 'leechers', 'desc');
  const first = paginate(sorted, 10, 1);
  const second = paginate(sorted, 10, 2);
  assert.equal(filtered.length, 28);
  assert.equal(first.totalPages, 3);
  assert.equal(first.rows[0].leechers, 55);
  assert.equal(second.rows[0].leechers, 35);
  assert.equal(paginate(sorted, 10, 3).rows.at(-1).leechers, 1);
});


test('leecher sort reflects the displayed value when only an informational count exists', () => {
  const rows = [{name:'unknown', leechers:-1}, {name:'qbit', leechers:-1, displayLeechers:8},
                {name:'fresh', leechers:5}, {name:'partial', leechers:-1, displayLeechers:3}];
  assert.deepEqual(sortRows(rows,'leechers','desc').map(x => x.name), ['qbit','fresh','partial','unknown']);
});

test('uploaded sort uses real bytes, unknown stays last', () => {
  const rows = [
    {name: 'unknown'},
    {name: 'small', upload: {uploadedBytes: 2048}},
    {name: 'large', upload: {uploadedBytes: 1048576}},
    {name: 'zero', upload: {uploadedBytes: 0}}
  ];
  assert.deepEqual(sortRows(rows, 'uploaded', 'desc').map(x => x.name),
    ['large', 'small', 'zero', 'unknown']);
});

test('active sessions precede previews and stopped torrents before pagination', () => {
  const rows = [{name: 'Alpha', selected:true}, {name: 'Film 10', joalSession:{active:true}},
    {name:'Film 2', active:true}, {name:'Bravo', simulationActive:false, active:true}];
  assert.deepEqual(sortRows(rows, null).map(r => r.name), ['Film 2','Film 10','Alpha','Bravo']);
  assert.equal(sortRows(rows, 'name')[0].name, 'Alpha');
});

test('imported torrents sort by numeric threshold, assigned rule and visible status', () => {
  const a = { name: 'A', threshold: 10, assignedRule: 'Règle 3', soloStatus: 'JOAL actif' };
  const b = { name: 'B', threshold: 2, assignedRule: 'Règle 12', soloStatus: 'En attente' };
  const c = { name: 'C', threshold: null };
  assert.deepEqual(sortRows([c, b, a], 'threshold', 'desc'), [a, b, c]);
  assert.deepEqual(sortRows([c, a, b], 'threshold', 'asc'), [b, a, c]);
  assert.deepEqual(sortRows([c, b, a], 'assignedRule'), [a, b, c]);
  assert.deepEqual(sortRows([c, a, b], 'soloStatus'), [b, a, c]);
});
