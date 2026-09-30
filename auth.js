/* ==========================================================================
   TAI Developer Platform — authentication client
   --------------------------------------------------------------------------
   Owns three things:
     1. the session state (GET /api/auth/me on boot)
     2. the sign-in / register modal, injected on demand so no page duplicates it
     3. the API-key table on the dashboard, when the page asks for it

   Every user-facing string goes through TAIi18n.t(), so the English literal is
   also the dictionary key in locales/zh.js.
   ========================================================================== */
(function () {
  'use strict';

  // GitHub Pages cannot host the API, so the base URL is configurable.
  // '' means same origin (local dev); see config.js.
  var CONFIG = window.TAI_CONFIG || {};
  var API = String(CONFIG.apiBase || '').replace(/\/+$/, '') + '/api';

  var state = { user: null, loaded: false };
  var listeners = [];

  function t(s) {
    return window.TAIi18n ? window.TAIi18n.t(s) : s;
  }

  /* ------------------------------------------------------------- requests */

  function request(method, path, body) {
    var opts = {
      method: method,
      credentials: 'include',   // required when the API is on another origin
      headers: { 'Accept': 'application/json' }
    };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }

    return fetch(API + path, opts).then(function (res) {
      if (res.status === 204) return null;
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) {
          var err = new Error(t(data.message || 'Something went wrong. Please try again.'));
          err.code = data.code || 'error';
          err.status = res.status;
          throw err;
        }
        return data;
      });
    }).catch(function (err) {
      if (err instanceof TypeError) {                 // network / server down
        var netErr = new Error(t('Could not reach the server. Please try again.'));
        netErr.code = 'network';
        throw netErr;
      }
      throw err;
    });
  }

  /* --------------------------------------------------------------- state */

  function emit() {
    for (var i = 0; i < listeners.length; i++) {
      try { listeners[i](state.user); } catch (e) { /* keep other listeners alive */ }
    }
  }

  function setUser(user) {
    state.user = user || null;
    state.loaded = true;
    renderHeader();
    emit();
  }

  function refresh() {
    return request('GET', '/auth/me')
      .then(function (user) { setUser(user); return user; })
      .catch(function () { setUser(null); return null; });
  }

  /* -------------------------------------------------------------- header */

  function renderHeader() {
    var btn = document.getElementById('loginBtn');
    if (!btn) return;

    var existing = document.getElementById('userBox');
    if (existing) existing.parentNode.removeChild(existing);

    if (!state.user) {
      btn.style.display = '';
      btn.textContent = t('Login');
      return;
    }

    btn.style.display = 'none';

    var box = document.createElement('span');
    box.id = 'userBox';
    box.className = 'user-box';

    var name = document.createElement('span');
    name.className = 'user-name';
    name.textContent = state.user.name;          // textContent: no HTML injection
    name.title = state.user.email;

    var out = document.createElement('button');
    out.type = 'button';
    out.className = 'btn sm';
    out.textContent = t('Logout');
    out.addEventListener('click', function () {
      out.disabled = true;
      out.innerHTML = '<span class="loader"></span>' + t('Logging out...');
      logout().catch(function () { /* fall through to the finally below */ })
        .then(function () { out.disabled = false; });
    });

    box.appendChild(name);
    box.appendChild(out);
    btn.parentNode.insertBefore(box, btn.nextSibling);
  }

  /* --------------------------------------------------------------- modal */

  function modalMarkup() {
    return '' +
      '<div class="modal-content">' +
      '  <button type="button" class="modal-close" data-auth="close" aria-label="Close">&times;</button>' +
      '  <h2 class="modal-title">Welcome to TAI</h2>' +
      '  <div class="tabs">' +
      '    <button type="button" class="tab active" data-auth="tab-login">Login</button>' +
      '    <button type="button" class="tab" data-auth="tab-register">Register</button>' +
      '  </div>' +
      '  <form data-auth="login-form" novalidate>' +
      '    <div class="field"><label for="loginEmail">Email</label>' +
      '      <input id="loginEmail" type="email" autocomplete="email" placeholder="your@email.com" required /></div>' +
      '    <div class="field"><label for="loginPassword">Password</label>' +
      '      <input id="loginPassword" type="password" autocomplete="current-password" placeholder="••••••••" required /></div>' +
      '    <p class="form-error" data-auth="login-error" hidden></p>' +
      '    <button type="submit" class="btn block">Login</button>' +
      '  </form>' +
      '  <form data-auth="register-form" class="hidden" novalidate>' +
      '    <div class="field"><label for="registerName">Full Name</label>' +
      '      <input id="registerName" type="text" autocomplete="name" placeholder="John Doe" required /></div>' +
      '    <div class="field"><label for="registerEmail">Email</label>' +
      '      <input id="registerEmail" type="email" autocomplete="email" placeholder="your@email.com" required /></div>' +
      '    <div class="field"><label for="registerPassword">Password</label>' +
      '      <input id="registerPassword" type="password" autocomplete="new-password" placeholder="••••••••" required /></div>' +
      '    <div class="field"><label for="registerConfirm">Confirm Password</label>' +
      '      <input id="registerConfirm" type="password" autocomplete="new-password" placeholder="••••••••" required /></div>' +
      '    <p class="form-error" data-auth="register-error" hidden></p>' +
      '    <button type="submit" class="btn block">Create Account</button>' +
      '  </form>' +
      '</div>';
  }

  var modal = null;

  function buildModal() {
    if (modal) return modal;
    modal = document.createElement('div');
    modal.id = 'loginModal';
    modal.className = 'modal-overlay hidden';
    modal.innerHTML = modalMarkup();
    document.body.appendChild(modal);

    // The modal did not exist when i18n.js took its snapshot, so translate it now.
    if (window.TAIi18n && window.TAIi18n.translate) window.TAIi18n.translate(modal);

    var loginForm = modal.querySelector('[data-auth="login-form"]');
    var registerForm = modal.querySelector('[data-auth="register-form"]');
    var loginError = modal.querySelector('[data-auth="login-error"]');
    var registerError = modal.querySelector('[data-auth="register-error"]');
    var tabLogin = modal.querySelector('[data-auth="tab-login"]');
    var tabRegister = modal.querySelector('[data-auth="tab-register"]');

    function showTab(which) {
      var isLogin = which === 'login';
      tabLogin.classList.toggle('active', isLogin);
      tabRegister.classList.toggle('active', !isLogin);
      loginForm.classList.toggle('hidden', !isLogin);
      registerForm.classList.toggle('hidden', isLogin);
      hide(loginError); hide(registerError);
    }

    tabLogin.addEventListener('click', function () { showTab('login'); });
    tabRegister.addEventListener('click', function () { showTab('register'); });
    modal.querySelector('[data-auth="close"]').addEventListener('click', closeModal);
    modal.addEventListener('click', function (e) {
      if (e.target === modal) closeModal();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !modal.classList.contains('hidden')) closeModal();
    });

    loginForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var email = modal.querySelector('#loginEmail').value;
      var password = modal.querySelector('#loginPassword').value;
      busy(loginForm, true, 'Logging in...');
      hide(loginError);
      login(email, password)
        .then(function () { closeModal(); loginForm.reset(); })
        .catch(function (err) { show(loginError, err.message); })
        .then(function () { busy(loginForm, false); });
    });

    registerForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var name = modal.querySelector('#registerName').value;
      var email = modal.querySelector('#registerEmail').value;
      var password = modal.querySelector('#registerPassword').value;
      var confirm = modal.querySelector('#registerConfirm').value;
      hide(registerError);

      if (password !== confirm) {
        show(registerError, t('Passwords do not match'));
        return;
      }
      busy(registerForm, true, 'Creating account...');
      register(name, email, password)
        .then(function () { closeModal(); registerForm.reset(); })
        .catch(function (err) { show(registerError, err.message); })
        .then(function () { busy(registerForm, false); });
    });

    return modal;
  }

  function busy(form, isBusy, label) {
    var btn = form.querySelector('button[type="submit"]');
    if (!btn) return;
    if (isBusy) {
      if (!btn.dataset.label) btn.dataset.label = btn.textContent;
      btn.innerHTML = '<span class="loader"></span>' + t(label);
      btn.disabled = true;
    } else {
      btn.textContent = btn.dataset.label || btn.textContent;
      btn.disabled = false;
    }
  }

  function show(el, message) { el.textContent = message; el.hidden = false; }
  function hide(el) { el.hidden = true; el.textContent = ''; }

  function openModal(tab) {
    buildModal();
    var which = tab || 'login';
    modal.querySelector('[data-auth="tab-' + which + '"]').click();
    modal.classList.remove('hidden');
    var focus = modal.querySelector(which === 'login' ? '#loginEmail' : '#registerName');
    if (focus) focus.focus();
  }

  function closeModal() {
    if (modal) modal.classList.add('hidden');
  }

  /* ----------------------------------------------------------------- api */

  function login(email, password) {
    return request('POST', '/auth/login', { email: email, password: password })
      .then(function (user) { setUser(user); return user; });
  }

  function register(name, email, password) {
    return request('POST', '/auth/register', { name: name, email: email, password: password })
      .then(function (user) { setUser(user); return user; });
  }

  function logout() {
    return request('POST', '/auth/logout')
      .then(function () { setUser(null); })
      .catch(function () { setUser(null); });
  }

  function listKeys() { return request('GET', '/keys'); }
  function createKey(name) { return request('POST', '/keys', { name: name }); }
  function revokeKey(id) { return request('DELETE', '/keys/' + id); }

  /* --------------------------------------------------------------- boot */

  function wireLoginButton() {
    document.addEventListener('click', function (e) {
      var btn = e.target.closest && e.target.closest('#loginBtn');
      if (!btn) return;
      e.preventDefault();
      if (state.user) return;
      openModal('login');
    });
  }

  function init() {
    wireLoginButton();
    renderHeader();
    refresh();

    // ?login=1 opens the modal straight away (used by "sign in" deep links)
    if (/[?&]login=1/.test(window.location.search)) openModal('login');
  }

  window.TAIAuth = {
    get user() { return state.user; },
    get ready() { return state.loaded; },
    onChange: function (fn) { listeners.push(fn); if (state.loaded) fn(state.user); },
    refresh: refresh,
    openModal: openModal,
    closeModal: closeModal,
    login: login,
    register: register,
    logout: logout,
    listKeys: listKeys,
    createKey: createKey,
    revokeKey: revokeKey,
    request: request
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
