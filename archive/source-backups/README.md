Purpose: keep historical source snapshots and backup trees out of active code directories while preserving them for reference.

## What Lives Here

- `csl/` — archived `.bak` snapshots formerly mixed into the live CSL source tree
- `picasso/` — archived `.bak` Python host/test-runner snapshots formerly mixed into the live package
- `neocortex/` — archived helper-script backups not used by the active CS-3 flow
- `docs/` — backup-named TeX source snapshots moved out of the docs tree
- `csl_backup_sendskip/` — historical backup tree from earlier send-skip work
- `picasso_backup_sendskip/` — historical backup Python tree from earlier send-skip work

## Rules

- Treat everything here as historical reference, not active implementation.
- Do not edit or validate these files unless a task explicitly asks for backup archaeology.
- Do not create new `.bak` files in active source directories; place any intentional preserved snapshot here instead.
- If a document needs to reference an old implementation stage, prefer citing the path under this archive.