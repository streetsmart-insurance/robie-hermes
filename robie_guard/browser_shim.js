/* robie_guard browser shim -- CR-1 enforcement INSIDE the page.
 *
 * Why this exists: the 2026-09 whole-policy deletion did not go through any
 * Python code. A free-form agent driving Chrome over CDP clicked a Delete
 * control in the EZLynx UI. Python-side guards cannot see that. This shim can.
 *
 * Install as a Playwright init script (add_init_script) or paste into the CDP
 * session before handing the page to any agent. It is idempotent.
 *
 * Behaviour: capture-phase interception of clicks on destructive controls, plus
 * blocking of DELETE-verb fetch/XHR. Blocked attempts are recorded on
 * window.__ROBIE_GUARD.blocked so a job receipt can prove nothing slipped.
 */
(function () {
  if (window.__ROBIE_GUARD && window.__ROBIE_GUARD.installed) return;

  var DESTRUCTIVE = ['delete', 'remove', 'void', 'detach', 'discard', 'trash',
                     'purge', 'unlink', 'erase', 'destroy', 'wipe', 'revoke'];
  var NEVER = ['delete policy', 'delete account', 'delete applicant',
               'delete household', 'delete client', 'remove policy',
               'remove applicant', 'delete document', 'delete contact',
               'delete all', 'remove all', 'delete permanently'];

  var G = window.__ROBIE_GUARD = {
    installed: true,
    version: 1,
    blocked: [],
    /* Unlock is deliberately awkward: it needs an explicit token AND expires.
     * Set from the driver only inside guarded_transaction_delete(). */
    unlock: null,
    unlockUntil: 0
  };

  function textOf(el) {
    if (!el || el.nodeType !== 1) return '';
    var bits = [el.textContent || '', el.getAttribute('title') || '',
                el.getAttribute('aria-label') || '', el.getAttribute('id') || '',
                el.className && el.className.toString ? el.className.toString() : '',
                el.getAttribute('data-action') || ''];
    return bits.join(' ').toLowerCase().replace(/\s+/g, ' ').trim();
  }

  function hits(hay, list) {
    for (var i = 0; i < list.length; i++) if (hay.indexOf(list[i]) !== -1) return list[i];
    return null;
  }

  function unlocked() {
    return !!(G.unlock && Date.now() < G.unlockUntil);
  }

  /* Is this element inside a policy-history transaction row?
   * Used only to allow the ONE exception; absence of proof means deny. */
  function inTransactionRow(el) {
    var node = el, depth = 0;
    while (node && depth++ < 12) {
      if (node.nodeType === 1) {
        var t = textOf(node);
        var isRow = node.tagName === 'TR' || (node.getAttribute &&
                    (node.getAttribute('role') === 'row' ||
                     /transaction|history/.test(node.className || '')));
        if (isRow && /\brwl\b|renewal/.test(t) && /pending/.test(t)) return true;
      }
      node = node.parentNode;
    }
    return false;
  }

  function record(reason, el, extra) {
    var entry = {
      at: new Date().toISOString(),
      reason: reason,
      text: (textOf(el) || '').slice(0, 200),
      url: location.href
    };
    if (extra) for (var k in extra) entry[k] = extra[k];
    G.blocked.push(entry);
    try { console.error('[ROBIE-GUARD] BLOCKED: ' + reason + ' :: ' + entry.text); } catch (e) {}
  }

  function evaluate(el) {
    var node = el, depth = 0;
    while (node && depth++ < 6) {
      if (node.nodeType === 1) {
        var hay = textOf(node);
        if (hay) {
          var never = hits(hay, NEVER);
          if (never) return { block: true, reason: 'never-allowed phrase "' + never + '"' };
          var d = hits(hay, DESTRUCTIVE);
          if (d) {
            if (unlocked() && inTransactionRow(node)) return { block: false };
            return { block: true, reason: 'destructive control "' + d + '" (unlocked=' +
                     unlocked() + ', inTxRow=' + inTransactionRow(node) + ')' };
          }
        }
      }
      node = node.parentNode;
    }
    return { block: false };
  }

  document.addEventListener('click', function (ev) {
    var verdict = evaluate(ev.target);
    if (verdict.block) {
      ev.preventDefault();
      ev.stopImmediatePropagation();
      record(verdict.reason, ev.target);
    }
  }, true);

  /* Programmatic .click() bypasses nothing if we patch it too. */
  var nativeClick = HTMLElement.prototype.click;
  HTMLElement.prototype.click = function () {
    var verdict = evaluate(this);
    if (verdict.block) { record(verdict.reason + ' (programmatic)', this); return; }
    return nativeClick.apply(this, arguments);
  };

  /* Network backstop: no DELETE verb leaves this page. */
  var nativeFetch = window.fetch;
  if (nativeFetch) {
    window.fetch = function (input, init) {
      var method = ((init && init.method) ||
                    (input && input.method) || 'GET').toUpperCase();
      var url = (typeof input === 'string' ? input : (input && input.url) || '');
      if (method === 'DELETE' && !unlocked()) {
        record('DELETE fetch blocked', null, { requestUrl: url });
        return Promise.reject(new Error('[ROBIE-GUARD] DELETE blocked by CR-1'));
      }
      return nativeFetch.apply(this, arguments);
    };
  }

  var nativeOpen = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (method, url) {
    if ((method || '').toUpperCase() === 'DELETE' && !unlocked()) {
      record('DELETE xhr blocked', null, { requestUrl: url });
      throw new Error('[ROBIE-GUARD] DELETE blocked by CR-1');
    }
    return nativeOpen.apply(this, arguments);
  };

  try { console.log('[ROBIE-GUARD] installed v' + G.version + ' -- CR-1 active'); } catch (e) {}
})();
