/*
 * Draws /offline/: set lot winners, users with what they bought, add user, add lots, and the entries
 * not sent yet. Everything typed becomes an op in OfflineStore (offline_store.js), which the server
 * checks when it is sent; the checks here only catch typos while nobody can ask the server.
 *
 * Names and lot names were typed by members, so they only ever reach the page as textContent.
 */
(function () {
  'use strict';

  const Store = window.OfflineStore;
  const PING_EVERY = 20 * 1000;
  const PULL_EVERY = 5 * 60 * 1000;
  const state = { snapshot: null, ops: [], online: null, signedIn: null, replacing: null, confirm: null };
  const $ = function (id) { return document.getElementById(id); };

  // -- small DOM helpers ------------------------------------------------------

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (key) {
      if (key === 'text') { node.textContent = attrs[key]; } else if (key === 'onclick') { node.addEventListener('click', attrs[key]); } else { node.setAttribute(key, attrs[key]); }
    });
    (children || []).forEach(function (child) { if (child) { node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child); } });
    return node;
  }

  function say(id, text, kind, extra) {
    const box = $(id);
    box.replaceChildren();
    if (!text) { return; }
    box.appendChild(el('div', { class: 'alert py-2 mb-0 alert-' + (kind || 'success') + (kind === 'warning' || kind === 'success' ? ' text-dark' : '') }, [text, extra ? ' ' : null, extra || null]));
  }

  function norm(value) { return String(value === undefined || value === null ? '' : value).trim().toLowerCase(); }

  // -- the merged view: the copy, plus what's been typed since ----------------

  function auction() { return state.snapshot && state.snapshot.auction ? state.snapshot.auction : null; }
  function symbol() { return auction() ? auction().currency_symbol || '$' : '$'; }
  function money(value) { return symbol() + Number(value || 0).toFixed(2); }
  function live() { return state.ops.filter(function (op) { return op.status === 'pending'; }); }

  function findUser(number) {
    const key = norm(number);
    if (!key) { return null; }
    const pending = live().find(function (op) { return op.type === 'add_user' && norm(op.data.bidder_number) === key; });
    if (pending) { return { ref: 'op:' + pending.op_id, key: 'op:' + pending.op_id, number: pending.data.bidder_number, name: pending.data.name, pending: true }; }
    const user = state.snapshot ? (state.snapshot.users || []).find(function (u) { return norm(u.bidder_number) === key; }) : null;
    if (user) { return { ref: user.bidder_number, key: 'pk:' + user.pk, number: user.bidder_number, name: user.name, pk: user.pk }; }
    return null;
  }

  function userByPk(pk) {
    return state.snapshot ? (state.snapshot.users || []).find(function (u) { return u.pk === pk; }) : null;
  }

  function findLot(number) {
    const key = norm(number);
    if (!key) { return null; }
    const pending = live().find(function (op) { return op.type === 'add_lot' && norm(op.data.lot_number) === key; });
    if (pending) { return { ref: 'op:' + pending.op_id, key: 'op:' + pending.op_id, number: pending.data.lot_number, name: pending.data.lot_name, seller: (pending.display || {}).seller, pending: true }; }
    const lot = state.snapshot ? (state.snapshot.lots || []).find(function (l) { return norm(l.lot_number) === key; }) : null;
    if (lot) {
      const seller = userByPk(lot.seller_pk);
      return { ref: lot.lot_number, key: 'pk:' + lot.pk, number: lot.lot_number, name: lot.lot_name, seller: seller ? seller.bidder_number : '', lot: lot };
    }
    return null;
  }

  /** The pending sale typed here for a lot, newest first. */
  function pendingSale(lotKey, lotNumber) {
    const sales = live().filter(function (op) {
      return op.type === 'set_winner' && ((op.display || {}).lot_key === lotKey || norm((op.display || {}).lot) === norm(lotNumber));
    });
    return sales.length ? sales[sales.length - 1] : null;
  }

  function nextNumber(values) {
    let max = 0;
    values.forEach(function (value) { const n = parseInt(value, 10); if (String(n) === String(value).trim() && n > max) { max = n; } });
    return String(max + 1);
  }

  // -- tabs -------------------------------------------------------------------

  function show(pane) {
    document.querySelectorAll('#offline-tabs .nav-link').forEach(function (button) { button.classList.toggle('active', button.dataset.pane === pane); });
    document.querySelectorAll('section[data-pane]').forEach(function (section) { section.hidden = section.dataset.pane !== pane; });
    if (pane === 'add-user' && !$('user-bidder').value) { $('user-bidder').value = suggestBidder(); }
    if (pane === 'add-lots' && !$('lot-number').value) { $('lot-number').value = suggestLot($('lot-seller').value); }
    const first = document.querySelector('section[data-pane="' + pane + '"] input');
    if (first) { first.focus(); }
  }

  function suggestBidder() {
    const numbers = (state.snapshot ? state.snapshot.users || [] : []).map(function (u) { return u.bidder_number; });
    live().forEach(function (op) { if (op.type === 'add_user') { numbers.push(op.data.bidder_number); } });
    return nextNumber(numbers);
  }

  function suggestLot(seller) {
    const lots = (state.snapshot ? state.snapshot.lots || [] : []).map(function (l) { return l.lot_number; });
    live().forEach(function (op) { if (op.type === 'add_lot') { lots.push(op.data.lot_number); } });
    if (auction() && auction().use_seller_dash_lot_numbering) {
      if (!norm(seller)) { return ''; }
      const prefix = norm(seller) + '-';
      const used = lots.filter(function (n) { return norm(n).indexOf(prefix) === 0; }).map(function (n) { return String(n).slice(prefix.length); });
      return String(seller).trim() + '-' + nextNumber(used);
    }
    return nextNumber(lots);
  }

  // -- saving -----------------------------------------------------------------

  /** True when the same warning was already shown for exactly these values: the second Save keeps it. */
  function confirmed(signature, warning, messageId) {
    if (state.confirm === signature) { state.confirm = null; return true; }
    state.confirm = signature;
    say(messageId, warning + ' Save again to keep it anyway.', 'warning');
    return false;
  }

  function save(type, data, display, messageId, text) {
    const op = Store.newOp(type, data, state.snapshot);
    op.display = display;
    // Only an entry of the same kind is being edited; anything else saved meanwhile replaces nothing.
    const replaced = state.replacing && state.replacing.type === type ? state.replacing.op_id : null;
    state.replacing = null;
    state.confirm = null;
    return Store.putOp(op)
      .then(function () { return replaced ? Store.undo(replaced) : null; })
      .then(load)
      .then(function () {
        say(messageId, text, 'success', el('button', { class: 'btn btn-sm btn-link p-0 text-dark', type: 'button', text: 'Undo', onclick: function () { undo(op.op_id, messageId); } }));
        scheduleSync();
      })
      .catch(function () { say(messageId, 'This device could not save that. Write it down.', 'danger'); });
  }

  function undo(opId, messageId) {
    Store.undo(opId).then(function (removed) {
      say(messageId, removed ? 'Undone.' : 'That already went in. Undo it on the site.', removed ? 'success' : 'warning');
      return load();
    });
  }

  function saveWinner(unsold) {
    const lotNumber = $('winner-lot').value.trim();
    const bidder = $('winner-bidder').value.trim();
    const priceText = $('winner-price').value.trim();
    if (!lotNumber) { say('winner-message', 'Type a lot number.', 'warning'); return; }
    const lot = findLot(lotNumber);
    const signature = [unsold, lotNumber, bidder, priceText].join('|');
    const warnings = [];
    if (!lot && state.snapshot) { warnings.push('Lot ' + lotNumber + " isn't in the copy on this device."); }
    if (lot && lot.lot && lot.lot.winner_pk) {
      const winner = userByPk(lot.lot.winner_pk);
      warnings.push('Lot ' + lotNumber + ' was already sold to ' + (winner ? winner.bidder_number : '?') + ' on the site, so the site will keep that sale.');
    }
    const earlier = pendingSale(lot ? lot.key : null, lotNumber);
    if (earlier) { warnings.push('Lot ' + lotNumber + ' was already entered here, and this replaces it.'); }
    const display = { lot: lot ? lot.number : lotNumber, lot_key: lot ? lot.key : null };
    let data;
    let text;
    if (unsold) {
      data = { lot: lot ? lot.ref : lotNumber, unsold: true };
      text = 'Lot ' + display.lot + ' ended unsold.';
    } else {
      if (!bidder) { say('winner-message', 'Type the winning bidder number.', 'warning'); return; }
      const price = Number(priceText);
      if (!priceText || !isFinite(price) || price < 0) { say('winner-message', 'Type the price.', 'warning'); return; }
      if (auction() && auction().only_whole_dollar_bids && price !== Math.floor(price)) { say('winner-message', 'This auction only takes whole dollars.', 'warning'); return; }
      const winner = findUser(bidder);
      if (!winner && state.snapshot) { warnings.push('Bidder ' + bidder + " isn't in the copy on this device."); }
      data = { lot: lot ? lot.ref : lotNumber, winner: winner ? winner.ref : bidder, winning_price: price.toFixed(2) };
      Object.assign(display, { winner: winner ? winner.number : bidder, winner_key: winner ? winner.key : null, name: winner ? winner.name : '' });
      text = 'Lot ' + display.lot + ' sold to ' + display.winner + ' for ' + money(price) + '.';
    }
    if (warnings.length && !confirmed(signature, warnings.join(' '), 'winner-message')) { return; }
    const editing = state.replacing ? state.replacing.op_id : null;
    const replaceEarlier = earlier && earlier.op_id !== editing ? Store.undo(earlier.op_id) : Promise.resolve();
    replaceEarlier.then(function () { return save('set_winner', data, display, 'winner-message', text); }).then(function () {
      $('winner-lot').value = '';
      $('winner-bidder').value = '';
      $('winner-price').value = '';
      $('winner-preview').innerHTML = '&nbsp;';
      $('winner-lot').focus();
    });
  }

  function saveUser() {
    const name = $('user-name').value.trim();
    const number = $('user-bidder').value.trim();
    if (!name || !number) { say('user-message', 'Type a name and a bidder number.', 'warning'); return; }
    const existing = findUser(number);
    if (existing) { say('user-message', 'Bidder ' + number + ' is ' + existing.name + '.', 'warning'); return; }
    const data = { name: name, bidder_number: number, email: $('user-email').value.trim(), phone_number: $('user-phone').value.trim() };
    save('add_user', data, {}, 'user-message', 'Added ' + name + ' as bidder ' + number + '.').then(function () {
      ['user-name', 'user-email', 'user-phone'].forEach(function (id) { $(id).value = ''; });
      $('user-bidder').value = suggestBidder();
      $('user-name').focus();
    });
  }

  function saveLot() {
    const sellerNumber = $('lot-seller').value.trim();
    const number = $('lot-number').value.trim();
    const name = $('lot-name').value.trim();
    if (!sellerNumber || !number || !name) { say('lot-message', 'Type the seller, a lot number and a name.', 'warning'); return; }
    if (findLot(number)) { say('lot-message', 'Lot ' + number + ' already exists.', 'warning'); return; }
    const seller = findUser(sellerNumber);
    if (!seller && state.snapshot && !confirmed(['lot', sellerNumber, number, name].join('|'), 'Seller ' + sellerNumber + " isn't in the copy on this device.", 'lot-message')) { return; }
    const quantity = Math.max(1, parseInt($('lot-quantity').value, 10) || 1);
    const data = { seller: seller ? seller.ref : sellerNumber, lot_number: number, lot_name: name, quantity: quantity, donation: $('lot-donation').checked };
    save('add_lot', data, { seller: seller ? seller.number : sellerNumber }, 'lot-message', 'Added lot ' + number + '.').then(function () {
      $('lot-name').value = '';
      $('lot-quantity').value = '1';
      $('lot-donation').checked = false;
      $('lot-number').value = suggestLot(sellerNumber);
      $('lot-name').focus();
    });
  }

  // -- drawing ----------------------------------------------------------------

  function describe(op) {
    const d = op.display || {};
    if (op.type === 'set_winner') {
      return op.data.unsold ? 'Lot ' + d.lot + ' unsold' : 'Lot ' + d.lot + ' to ' + d.winner + (d.name ? ' (' + d.name + ')' : '') + ' for ' + money(op.data.winning_price);
    }
    if (op.type === 'add_user') { return 'Add ' + op.data.name + ' as bidder ' + op.data.bidder_number; }
    if (op.type === 'add_lot') { return 'Add lot ' + op.data.lot_number + ': ' + op.data.lot_name + ' (seller ' + (d.seller || op.data.seller) + ')'; }
    return op.type;
  }

  function opActions(op, messageId) {
    if (op.status === 'conflict') {
      return el('td', { class: 'text-end text-nowrap' }, [
        el('button', { class: 'btn btn-sm btn-secondary me-1', type: 'button', text: 'Edit', onclick: function () { edit(op); } }),
        el('button', { class: 'btn btn-sm btn-secondary', type: 'button', text: 'Dismiss', onclick: function () {
          if (window.confirm('Dismiss this entry? It was never saved on the site.')) { undo(op.op_id, messageId); }
        } }),
      ]);
    }
    return el('td', { class: 'text-end' }, [el('button', { class: 'btn btn-sm btn-secondary', type: 'button', text: 'Undo', onclick: function () { undo(op.op_id, messageId); } })]);
  }

  function edit(op) {
    state.replacing = { op_id: op.op_id, type: op.type };
    const d = op.display || {};
    if (op.type === 'set_winner') {
      show('winners');
      $('winner-lot').value = d.lot || op.data.lot;
      $('winner-bidder').value = d.winner || '';
      $('winner-price').value = op.data.winning_price || '';
    } else if (op.type === 'add_user') {
      show('add-user');
      $('user-name').value = op.data.name || '';
      $('user-bidder').value = op.data.bidder_number || '';
      $('user-email').value = op.data.email || '';
      $('user-phone').value = op.data.phone_number || '';
    } else if (op.type === 'add_lot') {
      show('add-lots');
      $('lot-seller').value = d.seller || op.data.seller;
      $('lot-number').value = op.data.lot_number || '';
      $('lot-name').value = op.data.lot_name || '';
      $('lot-quantity').value = op.data.quantity || 1;
      $('lot-donation').checked = !!op.data.donation;
    }
  }

  function drawHeader() {
    const a = auction();
    $('offline-auction').textContent = a ? a.title : 'No auction saved on this device';
    const generated = state.snapshot && state.snapshot.generated_at ? new Date(state.snapshot.generated_at) : null;
    $('offline-age').textContent = generated && !isNaN(generated) ? 'Copy from ' + generated.toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' }) : '';
    const conflicts = state.ops.filter(function (op) { return op.status === 'conflict'; }).length;
    const badge = $('offline-pending-count');
    badge.textContent = String(state.ops.length);
    badge.className = 'badge ' + (conflicts ? 'bg-danger' : state.ops.length ? 'bg-warning text-dark' : 'bg-secondary');
    drawStatus();
  }

  function drawStatus() {
    const box = $('offline-status');
    box.replaceChildren();
    const back = window.location.pathname === '/offline/' ? '/' : window.location.href;
    if (state.online === null) {
      box.className = 'alert alert-secondary py-2';
      box.textContent = 'Checking the site…';
    } else if (!state.online) {
      box.className = 'alert alert-warning text-dark py-2';
      box.textContent = "The site isn't answering, so everything you enter is kept on this device and sent when it's back.";
    } else if (!state.signedIn && state.ops.length) {
      box.className = 'alert alert-warning text-dark py-2';
      box.append('The site is back. ', el('a', { href: '/login/?next=/offline/', text: 'Sign in' }), ' to send what you entered.');
    } else {
      box.className = 'alert alert-success text-dark py-2';
      box.append('The site is back. ', el('a', { href: back, text: 'Open it' }));
    }
  }

  function drawRecent() {
    const rows = $('winner-recent');
    rows.replaceChildren();
    state.ops.filter(function (op) { return op.type === 'set_winner'; }).slice(-15).reverse().forEach(function (op) {
      rows.appendChild(el('tr', {}, [
        el('td', { text: describe(op) }),
        el('td', { class: op.status === 'conflict' ? 'text-danger' : 'text-muted', text: op.status === 'conflict' ? op.message : 'Not sent' }),
        opActions(op, 'winner-message'),
      ]));
    });
  }

  /** Bought: the plain sum of winning prices. No fees, tax or club cut -- rules can change while we can't see them. */
  function totals() {
    const sums = {};
    function add(key, price) { if (key) { sums[key] = (sums[key] || 0) + Number(price || 0); } }
    const decided = {};
    live().forEach(function (op) {
      if (op.type !== 'set_winner') { return; }
      const d = op.display || {};
      decided[d.lot_key || 'n:' + norm(d.lot)] = op;
    });
    (state.snapshot ? state.snapshot.lots || [] : []).forEach(function (lot) {
      const key = 'pk:' + lot.pk;
      if (decided[key] || decided['n:' + norm(lot.lot_number)]) { return; }
      if (lot.winner_pk && lot.winning_price !== null) { add('pk:' + lot.winner_pk, lot.winning_price); }
    });
    Object.keys(decided).forEach(function (key) {
      const op = decided[key];
      if (op.data.unsold) { return; }
      const winner = findUser((op.display || {}).winner);
      add(winner ? winner.key : (op.display || {}).winner_key, op.data.winning_price);
    });
    return sums;
  }

  function drawUsers() {
    const rows = $('users-rows');
    rows.replaceChildren();
    const sums = totals();
    const q = norm($('users-search').value);
    const people = (state.snapshot ? state.snapshot.users || [] : []).map(function (u) {
      return { key: 'pk:' + u.pk, number: u.bidder_number, name: u.name, email: u.email, pending: false };
    });
    live().forEach(function (op) {
      if (op.type === 'add_user') { people.push({ key: 'op:' + op.op_id, number: op.data.bidder_number, name: op.data.name, email: op.data.email, pending: true }); }
    });
    people.filter(function (p) {
      return !q || norm(p.number).indexOf(q) !== -1 || norm(p.name).indexOf(q) !== -1 || norm(p.email).indexOf(q) !== -1;
    }).forEach(function (p) {
      rows.appendChild(el('tr', {}, [
        el('td', { text: p.number || '' }),
        el('td', {}, [p.name || '', p.pending ? el('span', { class: 'badge bg-warning text-dark ms-1', text: 'not sent' }) : null]),
        el('td', { class: 'text-end', text: money(sums[p.key] || 0) }),
      ]));
    });
  }

  function drawPending() {
    const rows = $('pending-rows');
    rows.replaceChildren();
    state.ops.slice().reverse().forEach(function (op) {
      rows.appendChild(el('tr', {}, [
        el('td', { text: describe(op) }),
        el('td', { class: op.status === 'conflict' ? 'text-danger' : 'text-muted', text: op.status === 'conflict' ? op.message : new Date(op.created_at).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) }),
        opActions(op, 'pending-message'),
      ]));
    });
  }

  function preview() {
    const lotNumber = $('winner-lot').value.trim();
    const lot = findLot(lotNumber);
    const winner = findUser($('winner-bidder').value);
    const parts = [];
    if (lotNumber) { parts.push(lot ? 'Lot ' + lot.number + ': ' + lot.name + (lot.seller ? ' (seller ' + lot.seller + ')' : '') : 'Lot ' + lotNumber + ' not in this copy'); }
    if (winner) { parts.push('Winner: ' + winner.name); }
    $('winner-preview').textContent = parts.join(' · ') || ' ';
    const seller = findUser($('lot-seller').value);
    $('lot-preview').textContent = seller ? 'Seller: ' + seller.name : ' ';
  }

  function draw() {
    drawHeader();
    drawRecent();
    drawUsers();
    drawPending();
    preview();
  }

  function load() {
    return Promise.all([Store.getSnapshot(), Store.listOps()]).then(function (loaded) {
      state.snapshot = loaded[0] || null;
      state.ops = loaded[1] || [];
      draw();
    });
  }

  // -- talking to the site ----------------------------------------------------

  let syncTimer = null;
  function scheduleSync() {
    window.clearTimeout(syncTimer);
    syncTimer = window.setTimeout(check, 2000);
  }

  function check() {
    return Store.ping().then(function (status) {
      state.online = status.online;
      state.signedIn = status.signedIn;
      drawStatus();
      if (!status.online) { return null; }
      // Only with an answer from the site: offline, the copy is the point of this page.
      if (status.signedIn === false) { return Store.forgetUnlessOwner(null); }
      if (!status.data) { return null; }
      return Store.forgetUnlessOwner(status.data.user_id).then(Store.listOps).then(function (ops) {
        return (ops.some(function (op) { return op.status === 'pending'; }) ? Store.sync() : Promise.resolve(null)).then(function (result) {
          return Store.getSnapshot().then(function (snapshot) {
            if (!snapshot || Date.now() - Store.lastPull() > PULL_EVERY) {
              return Store.pull(snapshot && snapshot.auction ? snapshot.auction.slug : null).then(function () { return result; });
            }
            return result;
          });
        });
      });
    }).then(function (result) {
      if (result) { explain(result); }
      return load();
    }).catch(function () {});
  }

  function explain(result) {
    if (result.unassigned) {
      say('pending-message', result.unassigned + ' were entered with no auction saved here. Send now puts them in yours.', 'warning');
    } else if (result.waiting) {
      say('pending-message', 'Some were entered by someone else signed in on this device, and wait for them.', 'secondary');
    }
  }

  // -- wiring -----------------------------------------------------------------

  document.querySelectorAll('#offline-tabs .nav-link').forEach(function (button) {
    // Leaving a pane abandons an Edit in it: the entry being edited stays as it was.
    button.addEventListener('click', function () { state.replacing = null; show(button.dataset.pane); });
  });
  $('winner-form').addEventListener('submit', function (event) { event.preventDefault(); saveWinner(false); });
  $('winner-unsold').addEventListener('click', function () { saveWinner(true); });
  $('user-form').addEventListener('submit', function (event) { event.preventDefault(); saveUser(); });
  $('lot-form').addEventListener('submit', function (event) { event.preventDefault(); saveLot(); });
  ['winner-lot', 'winner-bidder', 'lot-seller'].forEach(function (id) { $(id).addEventListener('input', preview); });
  $('lot-seller').addEventListener('change', function () {
    if (auction() && auction().use_seller_dash_lot_numbering) { $('lot-number').value = suggestLot($('lot-seller').value); }
  });
  $('users-search').addEventListener('input', drawUsers);
  $('pending-sync').addEventListener('click', function () {
    const unassigned = live().filter(function (op) { return !op.auction; }).length;
    let target = Promise.resolve(null);
    if (unassigned) {
      // Entries typed with no copy name no auction: the person sending them says which one.
      target = Store.pull(null).then(function (answer) {
        const a = answer && answer.data ? answer.data.auction : null;
        if (!a) { return null; }
        return window.confirm('Put the ' + unassigned + ' entries made with no auction saved into ' + a.title + '?') ? a.slug : null;
      });
    }
    say('pending-message', 'Sending…', 'secondary');
    target.then(function (slug) { return Store.sync(slug); }).then(function (result) {
      if (!result.online) { say('pending-message', "The site still isn't answering.", 'warning'); } else if (result.signedIn === false) { say('pending-message', 'Sign in to send these.', 'warning'); } else if (!result.signedIn) { say('pending-message', 'The site is busy. Try again in a minute.', 'warning'); } else if (result.unassigned || result.waiting) { explain(result); } else { say('pending-message', result.applied + ' sent' + (result.conflicts ? ', ' + result.conflicts + ' need a look.' : '.'), result.conflicts ? 'warning' : 'success'); }
      return load();
    });
  });
  $('pending-csv').addEventListener('click', function () {
    const blob = new Blob([Store.csv(state.ops)], { type: 'text/csv' });
    const link = el('a', { href: URL.createObjectURL(blob), download: 'auction-entries-' + new Date().toISOString().slice(0, 16).replace(/[T:]/g, '-') + '.csv' });
    document.body.appendChild(link);
    link.click();
    link.remove();
  });

  if (navigator.storage && navigator.storage.persist) { navigator.storage.persist().catch(function () {}); }
  window.addEventListener('online', check);
  load().then(check);
  window.setInterval(check, PING_EVERY);
})();
