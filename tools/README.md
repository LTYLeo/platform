# Site tooling

## stamp-assets.py

Rewrites `theme.css`, `i18n.js`, `auth.js`, `config.js` and `locales/zh.js`
references with a `?v=<content hash>` query on every page.

**Run it after editing any of those five files.**

GitHub Pages caches static assets hard, and the pages reference them without a
version, so an updated stylesheet or dictionary keeps serving the old copy from
the browser cache. The symptom is confusing rather than obvious: the deploy is
correct, the server returns the new file, and the browser shows the old one — a
CSS change looks like it was never committed and a new translation looks
missing. Appending a hash makes the URL change whenever the file does.

It is idempotent, so running it twice is harmless.
