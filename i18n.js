/* ==========================================================================
   TAI Developer Platform — i18n runtime
   --------------------------------------------------------------------------
   Design notes
   * The English copy already lives in the HTML, so English needs no dictionary.
     Only locales/zh.js exists, mapping normalized English source text -> Chinese.
   * Lookup is therefore keyed by the English source string. Untranslated or
     newly added copy silently falls back to English instead of breaking.
   * An explicit key can be forced with data-i18n="some.key"; that wins over
     source-text lookup and is resolved from TAI_I18N.zh["some.key"].
   * Elements marked data-i18n-ignore (and <pre>, <code>, <script>, <style>,
     <textarea>) are never touched.
   * Language resolution: saved choice -> navigator.languages -> 'en'.
   ========================================================================== */
(function () {
  'use strict';

  var STORAGE_KEY = 'tai-lang';
  var SUPPORTED = ['en', 'zh'];
  var ATTRS = ['placeholder', 'title', 'alt', 'aria-label'];

  var DICT = (window.TAI_I18N && window.TAI_I18N.zh) || {};
  var current = 'en';
  var originals = new WeakMap();
  var attrOriginals = new WeakMap();
  var nodeList = [];
  var titleOriginal = null;

  /* ---------------------------------------------------------------- utils */

  function normalize(s) {
    return String(s).replace(/\s+/g, ' ').trim();
  }

  function lookup(source) {
    var key = normalize(source);
    if (!key) return null;
    if (Object.prototype.hasOwnProperty.call(DICT, key)) return DICT[key];
    return null;
  }

  function urlLang() {
    var m = /[?&]lang=(en|zh)(?![a-z])/i.exec(window.location.search);
    return m ? m[1].toLowerCase() : null;
  }

  function detect() {
    // 1. explicit ?lang=xx in the URL wins (shareable / linkable locale)
    var fromUrl = urlLang();
    if (fromUrl) return fromUrl;

    // 2. a previously saved manual choice
    var saved = null;
    try { saved = localStorage.getItem(STORAGE_KEY); } catch (e) { /* private mode */ }
    if (saved && SUPPORTED.indexOf(saved) !== -1) return saved;

    // 3. the browser / system language
    var langs = navigator.languages && navigator.languages.length
      ? navigator.languages
      : [navigator.language || 'en'];
    for (var i = 0; i < langs.length; i++) {
      var l = String(langs[i]).toLowerCase();
      if (l.indexOf('zh') === 0) return 'zh';
      if (l.indexOf('en') === 0) return 'en';
    }
    return 'en';
  }

  /* -------------------------------------------------------------- collect */

  var SKIP_TAGS = {
    SCRIPT: 1, STYLE: 1, PRE: 1, CODE: 1, TEXTAREA: 1, NOSCRIPT: 1
  };

  function collectTextNodes(root) {
    var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (node) {
        var parent = node.parentNode;
        if (!parent) return NodeFilter.FILTER_REJECT;
        if (SKIP_TAGS[parent.nodeName]) return NodeFilter.FILTER_REJECT;
        if (parent.closest && parent.closest('[data-i18n-ignore]')) {
          return NodeFilter.FILTER_REJECT;
        }
        if (!normalize(node.nodeValue)) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var out = [];
    var n;
    while ((n = walker.nextNode())) out.push(n);
    return out;
  }

  function collectAttrTargets(root) {
    var out = [];
    for (var a = 0; a < ATTRS.length; a++) {
      var attr = ATTRS[a];
      var els = root.querySelectorAll('[' + attr + ']');
      for (var i = 0; i < els.length; i++) {
        var el = els[i];
        if (el.closest('[data-i18n-ignore]')) continue;
        var raw = el.getAttribute(attr);
        if (raw && normalize(raw)) out.push({ el: el, attr: attr });
      }
    }
    return out;
  }

  function remember(node, value) {
    if (!originals.has(node)) originals.set(node, value);
    if (nodeList.indexOf(node) === -1) nodeList.push(node);
  }

  /* ---------------------------------------------------------------- apply */

  function applyTo(node) {
    remember(node, originals.has(node) ? originals.get(node) : node.nodeValue);
    var original = originals.get(node);

    if (current === 'en') {
      if (node.nodeValue !== original) node.nodeValue = original;
      return;
    }

    // Explicit key wins over source-text lookup.
    var parent = node.parentNode;
    var explicit = parent && parent.getAttribute && parent.getAttribute('data-i18n');
    var translated = null;

    if (explicit) {
      translated = Object.prototype.hasOwnProperty.call(DICT, explicit)
        ? DICT[explicit]
        : null;
    }
    if (translated === null) translated = lookup(original);
    if (translated === null) return;              // stays English

    var lead = original.match(/^\s*/)[0];
    var trail = original.match(/\s*$/)[0];
    node.nodeValue = lead + translated + trail;
  }

  /* ---------------------------------------------------------------- blocks */

  /* Inline tags split a sentence into several text nodes and the node pass above
     can only match one node at a time, so a sentence containing <strong>, <em> or
     <code> can never be translated however complete the dictionary is. Rather
     than stripping the markup out of the documentation - which loses the emphasis
     and the code styling - a leaf block whose whole text matches a key is
     replaced outright. The dictionary value may contain inline HTML for exactly
     this reason. */
  var BLOCK_SELECTOR = 'p, li, td, th, dt, dd, figcaption, h1, h2, h3, h4, h5, h6';
  var NESTED = 'p, li, div, ul, ol, table, section, article, pre, blockquote';

  var blockOriginals = new WeakMap();
  var blockList = [];

  function collectBlocks(root) {
    var out = [];
    var els = (root || document.body).querySelectorAll(BLOCK_SELECTOR);
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (el.closest && el.closest('[data-i18n-ignore]')) continue;
      if (el.querySelector(NESTED)) continue;      // not a leaf: handled deeper
      if (!normalize(el.textContent)) continue;
      out.push(el);
    }
    return out;
  }

  function applyToBlock(el) {
    if (!blockOriginals.has(el)) {
      blockOriginals.set(el, el.innerHTML);
      blockList.push(el);
    }
    var original = blockOriginals.get(el);

    if (current === 'en') {
      if (el.innerHTML !== original) el.innerHTML = original;
      return true;
    }
    var key = normalize(el.textContent);
    var hit = lookup(key);
    if (hit === null) return false;
    el.innerHTML = hit;
    return true;
  }

  function applyToAttr(entry) {
    var el = entry.el;
    var attr = entry.attr;

    // A WeakMap, not el.dataset: dataset keys cannot contain hyphens, so
    // `aria-label` would throw a DOMStringMap error.
    var store = attrOriginals.get(el);
    if (!store) {
      store = {};
      attrOriginals.set(el, store);
    }
    if (!(attr in store)) store[attr] = el.getAttribute(attr);
    var original = store[attr];

    if (current === 'en') {
      el.setAttribute(attr, original);
      return;
    }
    var translated = lookup(original);
    if (translated !== null) el.setAttribute(attr, translated);
  }

  /* ------------------------------------------------------------- switcher */

  function buildSwitcher() {
    // Appended to the header rather than the nav so it stays reachable when the
    // nav collapses on narrow screens.
    var host = document.querySelector('header.site-header');
    if (!host || document.getElementById('langSwitch')) return;

    var wrap = document.createElement('div');
    wrap.id = 'langSwitch';
    wrap.className = 'lang-switch';
    wrap.setAttribute('role', 'group');
    wrap.setAttribute('aria-label', 'Language');

    var langs = [['en', 'EN'], ['zh', '中文']];
    for (var i = 0; i < langs.length; i++) {
      (function (code, label) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'lang-btn';
        b.dataset.lang = code;
        b.textContent = label;
        b.addEventListener('click', function () { setLang(code); });
        wrap.appendChild(b);
      })(langs[i][0], langs[i][1]);
    }
    host.appendChild(wrap);
  }

  function syncSwitcher() {
    var btns = document.querySelectorAll('.lang-btn');
    for (var i = 0; i < btns.length; i++) {
      var on = btns[i].dataset.lang === current;
      btns[i].classList.toggle('active', on);
      btns[i].setAttribute('aria-pressed', on ? 'true' : 'false');
    }
  }

  /* ------------------------------------------------------------------ api */

  function apply() {
    var i;
    for (i = 0; i < nodeList.length; i++) applyTo(nodeList[i]);
    var attrEntries = collectAttrTargets(document.body);
    for (i = 0; i < attrEntries.length; i++) applyToAttr(attrEntries[i]);

    // Browser tab title lives in <head>, outside the body walk.
    if (titleOriginal === null) titleOriginal = document.title;
    if (current === 'en') {
      document.title = titleOriginal;
    } else {
      var t = lookup(titleOriginal);
      if (t !== null) document.title = t;
    }

    document.documentElement.setAttribute('lang', current === 'zh' ? 'zh-CN' : 'en');
    document.documentElement.classList.remove('i18n-pending');
    syncSwitcher();

    try {
      document.dispatchEvent(new CustomEvent('tai:langchange', { detail: { lang: current } }));
    } catch (e) {
      var ev = document.createEvent('Event');
      ev.initEvent('tai:langchange', true, true);
      document.dispatchEvent(ev);
    }
  }

  function setLang(lang) {
    if (SUPPORTED.indexOf(lang) === -1) return;
    current = lang;
    try { localStorage.setItem(STORAGE_KEY, lang); } catch (e) { /* ignore */ }
    apply();
  }

  function init() {
    current = detect();
    try { localStorage.setItem(STORAGE_KEY, current); } catch (e) { /* ignore */ }
    buildSwitcher();

    // Snapshot every translatable text node once, up front.
    var nodes = collectTextNodes(document.body);
    for (var i = 0; i < nodes.length; i++) remember(nodes[i], nodes[i].nodeValue);

    apply();
  }

  /* Re-scan a subtree. Needed for markup that is injected after load, such as
     the sign-in modal built by auth.js -- those nodes did not exist when init()
     took its snapshot and would otherwise stay English. */
  function translate(root) {
    var scope = root || document.body;
    var nodes = collectTextNodes(scope);
    for (var i = 0; i < nodes.length; i++) remember(nodes[i], nodes[i].nodeValue);
    for (i = 0; i < nodes.length; i++) applyTo(nodes[i]);

    // After the node pass, so a simple element is handled the cheap way and only
    // mixed-content blocks fall through to here.
    var blocks = collectBlocks(scope);
    for (i = 0; i < blocks.length; i++) applyToBlock(blocks[i]);

    var attrEntries = collectAttrTargets(scope);
    for (i = 0; i < attrEntries.length; i++) applyToAttr(attrEntries[i]);
  }

  window.TAIi18n = {
    get lang() { return current; },
    setLang: setLang,
    translate: translate,
    t: function (source, fallback) {
      if (current === 'en') return source;
      var hit = lookup(source);
      return hit !== null ? hit : (fallback !== undefined ? fallback : source);
    }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Failsafe: never leave the page hidden if something above threw.
  setTimeout(function () {
    document.documentElement.classList.remove('i18n-pending');
  }, 2000);
})();
