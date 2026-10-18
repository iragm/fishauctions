/*
 * The emergency page's storage and sync, shared by /offline/ and every other page on the site.
 *
 * IndexedDB holds two things: a copy of one auction (the "snapshot", as /offline/snapshot/ returns it)
 * and the entries typed on /offline/ that the server hasn't taken yet ("ops", the same ops the app's
 * offline mode sends). An op leaves only when the server answers applied or already_applied, or when
 * the person dismisses it; a conflict stays until they do.
 *
 * On any other page, onPage() registers the service worker, forgets a snapshot that belongs to someone
 * else (signing out, or a second person on the same laptop), and, for the admin of an in-person
 * auction starting about now, refreshes the copy every five minutes and sends anything waiting.
 */
(function () {
  'use strict';

  const DB_NAME = 'auction-fish-offline';
  const HAS_DATA = 'auction-fish-offline';  // localStorage flag: skip opening IndexedDB on pages for people who never used it
  const LAST_PULL = 'auction-fish-offline-pulled';
  const PULL_EVERY = 5 * 60 * 1000;
  const BATCH = 500;  // the server's MAX_OPS_PER_SYNC
  let dbPromise = null;

  function storageGet(key) {
    try { return window.localStorage.getItem(key); } catch (err) { return null; }
  }
  function storageSet(key, value) {
    try { window.localStorage.setItem(key, value); } catch (err) { /* private window: the copy still works for this visit */ }
  }

  function db() {
    if (!dbPromise) {
      dbPromise = new Promise(function (resolve, reject) {
        const request = indexedDB.open(DB_NAME, 1);
        request.onupgradeneeded = function () {
          const database = request.result;
          database.createObjectStore('meta');
          database.createObjectStore('ops', { keyPath: 'op_id' });
        };
        request.onsuccess = function () { resolve(request.result); };
        request.onerror = function () { reject(request.error); };
      });
    }
    return dbPromise;
  }

  function tx(store, mode, work) {
    return db().then(function (database) {
      return new Promise(function (resolve, reject) {
        const transaction = database.transaction(store, mode);
        const result = work(transaction.objectStore(store));
        transaction.oncomplete = function () { resolve(result && 'result' in result ? result.result : undefined); };
        transaction.onerror = function () { reject(transaction.error); };
        transaction.onabort = function () { reject(transaction.error); };
      });
    });
  }

  function getSnapshot() { return tx('meta', 'readonly', function (s) { return s.get('snapshot'); }); }
  function saveSnapshot(snapshot) {
    storageSet(HAS_DATA, '1');
    return tx('meta', 'readwrite', function (s) { s.put(snapshot, 'snapshot'); });
  }
  function clearSnapshot() { return tx('meta', 'readwrite', function (s) { s.delete('snapshot'); }); }
  function listOps() {
    return tx('ops', 'readonly', function (s) { return s.getAll(); }).then(function (ops) {
      return (ops || []).sort(function (a, b) { return a.created_at < b.created_at ? -1 : 1; });
    });
  }
  function putOp(op) {
    storageSet(HAS_DATA, '1');
    return tx('ops', 'readwrite', function (s) { s.put(op); });
  }
  function deleteOp(opId) { return tx('ops', 'readwrite', function (s) { s.delete(opId); }); }

  function uuid() {
    if (window.crypto && crypto.randomUUID) { return crypto.randomUUID(); }
    // crypto.randomUUID needs https; local dev over plain http still gets a v4-shaped id.
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, function (b) { return b.toString(16).padStart(2, '0'); }).join('');
    return hex.slice(0, 8) + '-' + hex.slice(8, 12) + '-' + hex.slice(12, 16) + '-' + hex.slice(16, 20) + '-' + hex.slice(20);
  }

  /** A new pending entry. ``data`` is exactly what the server reads for that type. */
  function newOp(type, data, snapshot) {
    return {
      op_id: uuid(),
      type: type,
      data: data,
      auction: snapshot && snapshot.auction ? snapshot.auction.slug : null,
      owner: snapshot ? snapshot.user_id || null : null,
      created_at: new Date().toISOString(),
      status: 'pending',
    };
  }

  function wire(op) {
    return Object.assign({}, op.data, { op_id: op.op_id, type: op.type, recorded_at: op.created_at });
  }

  function csrfToken() {
    const match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : '';
  }

  /** {online, signedIn, data}. Online means Django answered; a 5xx or no answer is offline. */
  function getJSON(url) {
    return fetch(url, { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } })
      .then(function (response) {
        if (response.status >= 500) { return { online: false }; }
        if (response.status === 401 || response.status === 403) { return { online: true, signedIn: false }; }
        if (!response.ok) { return { online: true, signedIn: true, data: null }; }
        return response.json().then(function (data) { return { online: true, signedIn: true, data: data }; });
      })
      .catch(function () { return { online: false }; });
  }

  function ping() { return getJSON('/offline/snapshot/?ping=1'); }

  /** Download a fresh copy of ``slug`` (or the caller's last admin auction) and keep it. */
  function pull(slug) {
    return getJSON('/offline/snapshot/' + (slug ? '?auction=' + encodeURIComponent(slug) : '')).then(function (answer) {
      if (!answer.online || !answer.signedIn || !answer.data) { return answer; }
      storageSet(LAST_PULL, String(Date.now()));
      if (!answer.data.auction) { return answer; }
      return saveSnapshot(answer.data).then(function () { return answer; });
    });
  }

  function withLock(work) {
    if (navigator.locks && navigator.locks.request) {
      return navigator.locks.request('auction-fish-offline-sync', work);
    }
    return work();
  }

  /**
   * Send every pending entry this person may send. Resolves {online, signedIn, applied, conflicts}.
   * An entry made under someone else's copy waits for them; one made with no copy at all goes to
   * whichever auction the sender is running.
   */
  function sync() {
    return withLock(function () {
      return ping().then(function (status) {
        if (!status.online || !status.signedIn) { return status; }
        const userId = status.data ? status.data.user_id : null;
        return Promise.all([listOps(), getSnapshot()]).then(function (loaded) {
          const ops = loaded[0];
          const snapshot = loaded[1];
          const mine = ops.filter(function (op) {
            return op.status === 'pending' && (!op.owner || op.owner === userId);
          });
          const summary = { online: true, signedIn: true, applied: 0, conflicts: 0, waiting: ops.length - mine.length };
          if (!mine.length) { return summary; }
          const fallback = snapshot && snapshot.auction && snapshot.user_id === userId ? Promise.resolve(snapshot.auction.slug)
            : getJSON('/offline/snapshot/').then(function (answer) { return answer.data && answer.data.auction ? answer.data.auction.slug : null; });
          return fallback.then(function (fallbackSlug) {
            const groups = {};
            mine.forEach(function (op) {
              const slug = op.auction || fallbackSlug;
              if (slug) { (groups[slug] = groups[slug] || []).push(op); }
            });
            let chain = Promise.resolve();
            Object.keys(groups).forEach(function (slug) {
              const list = groups[slug];
              for (let start = 0; start < list.length; start += BATCH) {
                const chunk = list.slice(start, start + BATCH);
                chain = chain.then(function () { return send(slug, chunk, summary, snapshot); });
              }
            });
            return chain.then(function () { return summary; });
          });
        });
      });
    });
  }

  function send(slug, chunk, summary, snapshot) {
    return fetch('/offline/sync/', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-CSRFToken': csrfToken() },
      body: JSON.stringify({ auction: slug, ops: chunk.map(wire) }),
    }).then(function (response) {
      if (!response.ok) {
        if (response.status >= 500) { summary.online = false; }
        // 403 or 404: the auction is gone or no longer theirs. Keep every entry; the page shows them.
        return null;
      }
      return response.json();
    }).then(function (body) {
      if (!body) { return; }
      const byId = {};
      chunk.forEach(function (op) { byId[op.op_id] = op; });
      let writes = Promise.resolve();
      (body.results || []).forEach(function (result) {
        const op = byId[result.op_id];
        if (!op) { return; }
        if (result.status === 'applied' || result.status === 'already_applied') {
          summary.applied += 1;
          writes = writes.then(function () { return deleteOp(op.op_id); });
        } else if (result.status === 'conflict') {
          summary.conflicts += 1;
          op.status = 'conflict';
          op.conflict = result.conflict || 'error';
          op.message = result.message || 'This entry was not saved';
          writes = writes.then(function () { return putOp(op); });
        }
      });
      if (body.snapshot && body.snapshot.auction && (!snapshot || !snapshot.auction || snapshot.auction.slug === slug)) {
        storageSet(LAST_PULL, String(Date.now()));
        writes = writes.then(function () { return saveSnapshot(body.snapshot); });
      }
      return writes;
    }).catch(function () { summary.online = false; });
  }

  /**
   * Take an entry back. Waits for any send in flight (in any tab), so it never deletes an entry the
   * server is taking at that moment. False when it had already gone in.
   */
  function undo(opId) {
    return withLock(function () {
      return tx('ops', 'readonly', function (s) { return s.get(opId); }).then(function (op) {
        if (!op) { return false; }
        return deleteOp(opId).then(function () { return true; });
      });
    });
  }

  function csv(ops) {
    const columns = ['type', 'lot', 'winner', 'winning_price', 'unsold', 'name', 'bidder_number', 'email', 'phone_number', 'seller', 'lot_number', 'lot_name', 'quantity', 'donation', 'status', 'message', 'recorded_at', 'op_id'];
    function cell(value) {
      const text = value === undefined || value === null ? '' : String(value);
      // A leading = + - @ makes a spreadsheet run the cell as a formula.
      const safe = /^[=+\-@\t\r]/.test(text) ? "'" + text : text;
      return /[",\n\r]/.test(safe) ? '"' + safe.replace(/"/g, '""') + '"' : safe;
    }
    const rows = [columns.join(',')];
    ops.forEach(function (op) {
      const values = Object.assign({}, op.data, op.display || {}, { type: op.type, status: op.status, message: op.message, recorded_at: op.created_at, op_id: op.op_id });
      rows.push(columns.map(function (name) { return cell(values[name]); }).join(','));
    });
    return rows.join('\r\n') + '\r\n';
  }

  /** Every page: the worker, the privacy check, and the five-minute copy for an auction in progress. */
  function onPage(options) {
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register(options.worker, { scope: '/' }).catch(function () {});
    }
    if (!window.indexedDB) { return; }
    const userId = options.user || null;
    const used = storageGet(HAS_DATA) === '1';
    if (!used && !options.auction) { return; }
    getSnapshot().then(function (snapshot) {
      if (snapshot && snapshot.user_id !== userId) { return clearSnapshot(); }
      return null;
    }).catch(function () {});
    if (!options.auction) { return; }
    function tick() {
      getSnapshot().then(function (snapshot) {
        const last = Number(storageGet(LAST_PULL) || 0);
        const stale = !snapshot || !snapshot.auction || snapshot.auction.slug !== options.auction || Date.now() - last > PULL_EVERY;
        return listOps().then(function (ops) {
          if (ops.some(function (op) { return op.status === 'pending'; })) { return sync(); }
          return stale ? withLock(function () { return pull(options.auction); }) : null;
        });
      }).catch(function () {});
    }
    if (navigator.storage && navigator.storage.persist) { navigator.storage.persist().catch(function () {}); }
    tick();
    window.setInterval(tick, 60 * 1000);
  }

  window.OfflineStore = {
    getSnapshot: getSnapshot,
    saveSnapshot: saveSnapshot,
    listOps: listOps,
    putOp: putOp,
    deleteOp: deleteOp,
    undo: undo,
    newOp: newOp,
    ping: ping,
    pull: pull,
    sync: sync,
    csv: csv,
    onPage: onPage,
    lastPull: function () { return Number(storageGet(LAST_PULL) || 0); },
  };
})();
