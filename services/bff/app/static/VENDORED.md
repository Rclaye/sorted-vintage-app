# Vendored files

Served by the BFF itself, so the cluster needs no outside access and pages load no third-party
scripts (contracts/bff-pages.md, Ticket 2.6). `tests/test_pages.py` checks each checksum.

| File | Source | Licence | SHA-256 |
| --- | --- | --- | --- |
| `htmx-2.0.11.min.js` | npm `htmx.org@2.0.11` (`dist/htmx.min.js`), published 2026-09-22; tarball matched npm's sha512 integrity | 0BSD | `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` |

To upgrade: download the new tarball from the npm registry, check it against the registry's
`dist.integrity`, copy `dist/htmx.min.js` under a new versioned name, and update this table, the
test and `templates/base.html` in one pull request.
