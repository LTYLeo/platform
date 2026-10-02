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
    root.classList.add('hidden');
    // Payment is handled by hand: the buyer contacts us, we confirm the money
    // arrived, and we send back a code. There is no automatic checkout, and
    // nothing in this dialog can take a payment.
    root.innerHTML =
      '<div class="modal-content" role="dialog" aria-modal="true" aria-labelledby="billingTitle">' +
        '<button class="modal-close" id="billingClose" aria-label="Close">&times;</button>' +
        '<h2 id="billingTitle" style="margin-top:0;">' + t('Top up your account') + '</h2>' +

        '<p class="muted small">' + t('Contact us, tell us the amount or the plan you want, and pay directly. We confirm it by hand and send you a redeem code.') + '</p>' +
        '<div class="contact-card">' +
          '<div class="contact-row">' +
            '<span class="contact-label">' + t('WeChat') + '</span>' +
            '<span class="contact-value" data-copy="Lambda_Prime">Lambda_Prime</span>' +
          '</div>' +
          '<div class="contact-row">' +
            '<span class="contact-label">' + t('QQ') + '</span>' +
            '<span class="contact-value" data-copy="1637321445">1637321445</span>' +
          '</div>' +
          '<div class="contact-row">' +
            '<span class="contact-label">' + t('Email') + '</span>' +
            '<span class="contact-value" data-copy="1637321445@qq.com">1637321445@qq.com</span>' +
          '</div>' +
        '</div>' +
        '<p class="muted small">' + t('Tell us your account email too, so the code is issued to the right account.') + '</p>' +

        '<hr class="billing-sep" />' +
        '<label for="billingCode">' + t('Already have a code?') + '</label>' +
        '<input type="text" id="billingCode" placeholder="TAI-XXXX-XXXX-XXXX" autocomplete="off" spellcheck="false" />' +
        '<button class="btn block" id="billingRedeem" style="margin-top:16px;">' + t('Redeem') + '</button>' +
        '<div id="billingResult" class="billing-intent" hidden></div>' +
      '</div>';

    document.body.appendChild(root);

    root.querySelector('#billingClose').addEventListener('click', close);
    root.addEventListener('click', function (e) { if (e.target === root) close(); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !root.classList.contains('hidden')) close();
    });

    // Click a contact detail to copy it. Long IDs typed by hand are the usual
    // reason a payment ends up unmatchable.
    root.querySelectorAll('[data-copy]').forEach(function (el) {
      el.addEventListener('click', function () {
        const value = el.dataset.copy;
        const done = function () {
          const was = el.innerHTML;
          el.innerHTML = t('Copied');
          setTimeout(function () { el.innerHTML = was; }, 1200);
        };
        if (navigator.clipboard) navigator.clipboard.writeText(value).then(done, done);
        else done();
      });
    });

    root.querySelector('#billingRedeem').addEventListener('click', redeemCode);
    root.querySelector('#billingCode').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); redeemCode(); }
    });

    return root;
  }


  function say(node, html, kind) {
    node.hidden = false;
    node.className = 'billing-intent' + (kind ? ' ' + kind : '');
    node.innerHTML = html;
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

  function open() {
    build();
    // Both: the attribute is what assistive technology reads, the class is what
    // actually removes it from the layer.
    root.hidden = false;
    root.classList.remove('hidden');
    if (window.TAII18n && window.TAII18n.translate) window.TAII18n.translate(root);
    root.querySelector('#billingResult').hidden = true;
    const code = root.querySelector('#billingCode');
    if (code) code.value = '';
  }

  function close() {
    if (!root) return;
    // `.modal-overlay` sets `display: flex`, which outranks the UA rule for
    // `[hidden]` - so setting the attribute alone left the dialog on screen with
    // a close button that appeared to do nothing.
    root.classList.add('hidden');
    root.hidden = true;
  }

  window.TAIBilling = { open: open, close: close };
})();
