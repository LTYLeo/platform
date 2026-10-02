/* Billing UI: add credit and redeem codes.
 *
 * The backend for this already existed - orders, payment intents, redeem codes,
 * an admin confirmation path - but nothing on the site called it. The pricing
 * page had an "Add Credit" button with no handler and the docs told people to
 * redeem codes with no way to do so. This is the missing half.
 *
 * Loaded after auth.js. Exposes window.TAIBilling.
 */
(function () {
  'use strict';

  var AMOUNTS = [10, 30, 100, 300];
  var MIN_AMOUNT = 1;
  var MAX_AMOUNT = 5000;

  var root = null;      // the overlay element, built once
  var currentOrder = null;
  var pollTimer = null;

  // --------------------------------------------------------------------- //
  // Small helpers
  // --------------------------------------------------------------------- //

  function t(s) {
    return window.TAII18n && window.TAII18n.t ? window.TAII18n.t(s) : s;
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function api(method, path, body) {
    // TAIAuth.request already knows the base URL, the credentials mode and the
    // tunnel header; going through it keeps one place that can be wrong.
    if (window.TAIAuth && window.TAIAuth.request) {
      return window.TAIAuth.request(method, path, body);
    }
    return Promise.reject(new Error('auth.js is not loaded'));
  }

  function requireSignIn() {
    var user = window.TAIAuth && window.TAIAuth.user;
    if (user) return true;
    close();
    if (window.TAIAuth && window.TAIAuth.openModal) {
      window.TAIAuth.openModal('login');
    }
    return false;
  }

  // --------------------------------------------------------------------- //
  // Markup
  // --------------------------------------------------------------------- //

  function build() {
    if (root) return root;

    root = document.createElement('div');
    root.className = 'modal-overlay';
    root.id = 'billingOverlay';
    root.hidden = true;
    root.innerHTML =
      '<div class="modal-content" role="dialog" aria-modal="true" aria-labelledby="billingTitle">' +
        '<button class="modal-close" id="billingClose" aria-label="Close">&times;</button>' +
        '<h2 id="billingTitle" style="margin-top:0;">' + t('Add Credit') + '</h2>' +

        '<div class="billing-tabs">' +
          '<button class="billing-tab active" data-tab="credit">' + t('Add Credit') + '</button>' +
          '<button class="billing-tab" data-tab="redeem">' + t('Redeem a Code') + '</button>' +
        '</div>' +

        // ---- add credit ----
        '<div class="billing-pane" data-pane="credit">' +
          '<p class="muted small">' + t('Credit is added to your account and spent only as you use the API.') + '</p>' +
          '<label>' + t('Amount (CNY)') + '</label>' +
          '<div class="amount-row">' +
            AMOUNTS.map(function (a) {
              return '<button class="amount-chip" data-amount="' + a + '">¥' + a + '</button>';
            }).join('') +
          '</div>' +
          '<input type="number" id="billingAmount" min="' + MIN_AMOUNT + '" max="' + MAX_AMOUNT +
            '" step="1" placeholder="' + t('Or enter an amount') + '" />' +
          '<button class="btn block" id="billingCreate" style="margin-top:16px;">' +
            t('Continue to payment') + '</button>' +
          '<div id="billingIntent" class="billing-intent" hidden></div>' +
        '</div>' +

        // ---- redeem ----
        '<div class="billing-pane" data-pane="redeem" hidden>' +
          '<p class="muted small">' + t('Redeem codes top up your balance instantly. They look like TAI-XXXX-XXXX-XXXX.') + '</p>' +
          '<label for="billingCode">' + t('Redeem code') + '</label>' +
          '<input type="text" id="billingCode" placeholder="TAI-XXXX-XXXX-XXXX" autocomplete="off" spellcheck="false" />' +
          '<button class="btn block" id="billingRedeem" style="margin-top:16px;">' + t('Redeem') + '</button>' +
          '<div id="billingResult" class="billing-intent" hidden></div>' +
        '</div>' +
      '</div>';

    document.body.appendChild(root);

    root.querySelector('#billingClose').addEventListener('click', close);
    root.addEventListener('click', function (e) { if (e.target === root) close(); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !root.hidden) close();
    });

    root.querySelectorAll('.billing-tab').forEach(function (tab) {
      tab.addEventListener('click', function () { showTab(tab.dataset.tab); });
    });

    root.querySelectorAll('.amount-chip').forEach(function (chip) {
      chip.addEventListener('click', function () {
        root.querySelector('#billingAmount').value = chip.dataset.amount;
        root.querySelectorAll('.amount-chip').forEach(function (c) { c.classList.remove('active'); });
        chip.classList.add('active');
      });
    });

    root.querySelector('#billingCreate').addEventListener('click', createOrder);
    root.querySelector('#billingRedeem').addEventListener('click', redeemCode);
    root.querySelector('#billingCode').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); redeemCode(); }
    });

    return root;
  }

  function showTab(name) {
    root.querySelectorAll('.billing-tab').forEach(function (tab) {
      tab.classList.toggle('active', tab.dataset.tab === name);
    });
    root.querySelectorAll('.billing-pane').forEach(function (pane) {
      pane.hidden = pane.dataset.pane !== name;
    });
  }

  function say(node, html, kind) {
    node.hidden = false;
    node.className = 'billing-intent' + (kind ? ' ' + kind : '');
    node.innerHTML = html;
  }

  // --------------------------------------------------------------------- //
  // Adding credit
  // --------------------------------------------------------------------- //

  function createOrder() {
    if (!requireSignIn()) return;

    var amount = parseFloat(root.querySelector('#billingAmount').value);
    var out = root.querySelector('#billingIntent');
    var button = root.querySelector('#billingCreate');

    if (!amount || amount < MIN_AMOUNT) {
      say(out, t('Enter an amount of at least ¥1.'), 'error');
      return;
    }
    if (amount > MAX_AMOUNT) {
      say(out, t('For amounts above ¥5,000 please contact us directly.'), 'error');
      return;
    }

    button.disabled = true;
    button.innerHTML = t('Creating order…');

    // `provider` is left to the server: whichever one is configured is the one
    // that can actually be paid, and the client has no way to know that.
    api('POST', '/billing/orders', { purpose: 'api_credit', amount_cny: amount })
      .then(function (order) {
        currentOrder = order;
        return api('POST', '/billing/orders/' + encodeURIComponent(order.id) + '/intent');
      })
      .then(renderIntent)
      .catch(function (err) {
        say(out, esc(err && err.message ? err.message : t('Could not create the order.')), 'error');
      })
      .finally(function () {
        button.disabled = false;
        button.innerHTML = t('Continue to payment');
      });
  }

  function renderIntent(res) {
    var out = root.querySelector('#billingIntent');
    var intent = res && res.intent;
    var orderId = res && res.order_id;

    if (res && res.status === 'paid') {
      say(out, '<p><strong>' + t('This order is already paid.') + '</strong></p>', 'ok');
      return;
    }
    if (!intent) {
      say(out, esc(t('No payment method is configured. Please contact us.')), 'error');
      return;
    }

    // Big, copyable, and above the button. Afdian has no per-order checkout, so
    // this string is the only thing that ties the payment back to the account -
    // it has to be the most obvious element on the panel, not a footnote.
    var ref =
      '<div class="ref-block">' +
        '<div class="ref-label">' + t('Paste this into the payment message') + '</div>' +
        '<div class="ref-value">' +
          '<code id="billingRef">' + esc(orderId) + '</code>' +
          '<button type="button" class="ref-copy" id="billingRefCopy">' + t('Copy') + '</button>' +
        '</div>' +
        '<div class="ref-hint">' + t('Without it your payment cannot be matched automatically.') + '</div>' +
      '</div>';

    if (intent.kind === 'external_link') {
      var url = esc(intent.url || intent.link || '#');
      say(out,
        '<p>' + t('Complete the payment on the next page, then come back here.') + '</p>' +
        ref +
        '<a class="btn block" id="billingPayLink" href="' + url +
          '" target="_blank" rel="noopener">' + t('Open payment page') + '</a>' +
        '<p class="muted small" style="margin-top:12px;">' +
          t('Forgot to paste it? Your payment is still recorded and we will match it by hand.') +
        '</p>', 'ok');
      startPolling(orderId);

      // Open it for them. Popup blockers may refuse - the button above is the
      // fallback, which is why it stays even when this succeeds.
      try { window.open(intent.url || intent.link, '_blank', 'noopener'); } catch (e) {}

    } else if (intent.kind === 'qrcode') {
      say(out,
        '<p>' + t('Scan to pay:') + '</p>' +
        '<div class="qr-holder" id="billingQR"></div>' + ref, 'ok');
      drawQR(intent.payload);
      startPolling(orderId);

    } else {
      // 'manual' and anything else: show exactly what the server said, since it
      // is written for the person paying.
      var note = intent.note || t('Contact us to arrange payment, quoting the order reference.');
      say(out, '<p>' + esc(note) + '</p>' + ref, 'ok');
    }

    var copy = out.querySelector('#billingRefCopy');
    if (copy) {
      copy.addEventListener('click', function () {
        var code = out.querySelector('#billingRef');
        var text = code ? code.textContent : orderId;
        var done = function () {
          copy.textContent = t('Copied');
          setTimeout(function () { copy.textContent = t('Copy'); }, 1500);
        };
        if (navigator.clipboard) {
          navigator.clipboard.writeText(text).then(done).catch(function () {});
        } else {
          // Older browsers: select it so a manual copy still works.
          var r = document.createRange();
          r.selectNodeContents(code);
          var sel = window.getSelection();
          sel.removeAllRanges(); sel.addRange(r);
        }
      });
    }
  }

  function drawQR(payload) {
    var holder = root.querySelector('#billingQR');
    if (!holder || !payload) return;
    // Loaded on demand: most installs never render a QR, so shipping the library
    // to every visitor would be waste.
    if (window.QRCode) {
      new window.QRCode(holder, { text: payload, width: 190, height: 190 });
      return;
    }
    var s = document.createElement('script');
    s.src = 'https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js';
    s.onload = function () {
      if (window.QRCode) new window.QRCode(holder, { text: payload, width: 190, height: 190 });
      else holder.innerText = payload;
    };
    s.onerror = function () { holder.innerText = payload; };
    document.head.appendChild(s);
  }

  function startPolling(orderId) {
    stopPolling();
    var attempts = 0;
    pollTimer = setInterval(function () {
      attempts += 1;
      if (attempts > 100) { stopPolling(); return; }   // ~5 minutes
      api('GET', '/billing/orders').then(function (list) {
        var rows = (list && list.data) || list || [];
        var found = rows.filter(function (o) { return o.id === orderId; })[0];
        if (found && found.status === 'paid') {
          stopPolling();
          var out = root.querySelector('#billingIntent');
          say(out, '<p><strong>' + t('Payment received. Your balance has been updated.') +
                   '</strong></p>', 'ok');
          if (window.TAIAuth && window.TAIAuth.refresh) window.TAIAuth.refresh();
        }
      }).catch(function () { /* keep polling; a blip is not a failure */ });
    }, 3000);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  // --------------------------------------------------------------------- //
  // Redeeming
  // --------------------------------------------------------------------- //

  function redeemCode() {
    if (!requireSignIn()) return;

    var input = root.querySelector('#billingCode');
    var out = root.querySelector('#billingResult');
    var button = root.querySelector('#billingRedeem');
    var code = (input.value || '').trim();

    if (!code) {
      say(out, t('Enter a code.'), 'error');
      return;
    }

    button.disabled = true;
    button.innerHTML = t('Redeeming…');

    api('POST', '/billing/redeem', { code: code })
      .then(function (res) {
        var parts = [];
        if (res.credit_cny) parts.push('¥' + Number(res.credit_cny).toFixed(2));
        if (res.plan) parts.push(res.plan);
        say(out,
          '<p><strong>' + t('Redeemed.') + '</strong> ' +
          (parts.length ? esc(parts.join(' · ')) : '') + '</p>',
          'ok');
        input.value = '';
        if (window.TAIAuth && window.TAIAuth.refresh) window.TAIAuth.refresh();
      })
      .catch(function (err) {
        say(out, esc(err && err.message ? err.message : t('Could not redeem that code.')), 'error');
      })
      .finally(function () {
        button.disabled = false;
        button.innerHTML = t('Redeem');
      });
  }

  // --------------------------------------------------------------------- //
  // Public
  // --------------------------------------------------------------------- //

  function open(tab) {
    build();
    root.hidden = false;
    showTab(tab || 'credit');
    if (window.TAII18n && window.TAII18n.translate) window.TAII18n.translate(root);
    // Amount presets start on nothing selected so the placeholder is meaningful.
    root.querySelector('#billingAmount').value = '';
    root.querySelectorAll('.amount-chip').forEach(function (c) { c.classList.remove('active'); });
    root.querySelector('#billingIntent').hidden = true;
    root.querySelector('#billingResult').hidden = true;
  }

  function close() {
    stopPolling();
    if (root) root.hidden = true;
  }

  window.TAIBilling = { open: open, close: close };
})();
