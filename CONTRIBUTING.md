# Working on this repo

Two branches.

| Branch | For |
|---|---|
| `main` | known-good. What you demo from and what the office laptop clones. Protected against force-push and deletion. |
| `test` | everything else. Unrestricted — commit and push freely. |

## Daily loop

```bash
git switch test
# ...work...
git commit -am "what changed"
git push
```

When `test` is good:

```bash
git switch main
git merge test
git push
```

No pull request needed. `main` only blocks force-push and deletion, so a
normal merge goes straight through.

If you ever *do* want the diff in front of you before it lands, open a PR from
`test` → `main` on GitHub and merge it there. Optional, not enforced.

## Before merging test → main

CI runs `compileall` and `tsc --noEmit` on every push, but the check that
actually matters needs a database, so run it locally:

```bash
cd backend
python test_violation_query.py        # must print "N match, 0 wrong"
```

That regenerates every rule's violation query, runs it against `source_db`, and
compares the row count to what the engine recorded. If those disagree the app
is showing someone the wrong rows to go and fix.

## Office laptop

```bash
git clone https://github.com/HON-AEROIT/smtc-data-validation.git
cd smtc-data-validation
# backend/.env comes with the clone -- just set DB_PASSWORD for this machine
```

`backend/.env` is gitignored and never leaves the machine. Full setup steps are
in `SETUP.md`.

## Never commit

- `.env` or any credential — put them in `backend/.env` on each machine by hand
- real customer data — `data_dump/` and `*.csv` are gitignored
- font binaries — PP Telegraf is a paid licence, drop the files into
  `frontend/public/fonts/` on each machine

CI fails the build if any of these are tracked.
