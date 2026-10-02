#!/usr/bin/env python3
"""Stamp asset URLs with a content hash.

The pages are static and served by GitHub Pages, which caches aggressively. A
plain ``href="theme.css"`` therefore keeps serving the old stylesheet after an
update, so a CSS change appears not to have been deployed and a new translation
simply does not show up. That failure is invisible: everything looks correct on
the server and stale in the browser.

Rewriting each reference as ``theme.css?v=<content hash>`` makes the URL change
whenever the file does, which is what forces a refetch. The hash is derived from
the file's bytes, so it needs no manual bumping and never goes stale.

Run it after editing any shared asset::

    python3 tools/stamp-assets.py

It is idempotent: running it twice changes nothing the second time.
"""

from __future__ import annotations

import hashlib
import pathlib
import re

# Assets that every page pulls in and that change independently of the markup.
STAMPED = (
    "theme.css",
    "i18n.js",
    "auth.js",
    "config.js",
    "locales/zh.js",
)


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:10]


def main() -> None:
    root = pathlib.Path(__file__).resolve().parent.parent
    versions = {}
    for name in STAMPED:
        target = root / name
        if target.exists():
            versions[name] = digest(target)
    if not versions:
        raise SystemExit("no assets found; run this from the site root")

    pages = sorted(root.glob("*.html"))
    changed = 0
    for page in pages:
        text = original = page.read_text(encoding="utf-8")
        for name, version in versions.items():
            # Matches both href="theme.css" and href="theme.css?v=..." plus the
            # src= form, without touching a comment that merely mentions the file.
            pattern = re.compile(
                r'(?P<attr>\b(?:href|src)=")(?P<path>' + re.escape(name) + r')(?:\?v=[0-9a-f]+)?(?P<close>")'
            )
            text = pattern.sub(lambda m: f"{m.group('attr')}{m.group('path')}?v={version}{m.group('close')}", text)
        if text != original:
            page.write_text(text, encoding="utf-8")
            changed += 1

    print("stamped %d asset(s) across %d page(s)" % (len(versions), changed))
    for name, version in sorted(versions.items()):
        print("  %-16s v=%s" % (name, version))


if __name__ == "__main__":
    main()
