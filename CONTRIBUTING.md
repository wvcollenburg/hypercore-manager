# Contributing

HyperCore Manager is community-supported software. There is no official Scale Computing
support behind it, and no service-level commitment. This page explains how to get help, how
to report problems well, and how code changes are handled.

## Getting help and reporting problems

Everything goes through [GitHub Issues](https://github.com/wvcollenburg/hypercore-manager/issues).
Pick the matching template (bug report or feature request). A bug report that can be acted on
includes:

- The running version (bottom-right of every dashboard page, or `cat VERSION`).
- Host OS, CPU architecture (`uname -m`), Docker and Compose versions (`docker compose version`).
- `docker compose ps` output and the relevant service logs
  (`docker compose logs --tail=200 <service>`). Redact credentials and cluster addresses.
- What you expected, what happened, and how to reproduce it.
- For balancing decisions: the balancer log around the event and the relevant node and VM
  tags. Dry-run output is perfectly fine.

Translation problems are bugs too. Open an issue, name the language and the text, and suggest
the better wording.

## Code contributions

Code contributions are currently limited to Scale Computing employees. Pull requests from
outside will be closed with a pointer to the issue tracker; the idea itself is welcome as an
issue, and that is where it is most likely to get picked up.

This is a capacity decision, not a statement about outside contributors. If it changes, this
page will say so.

### For maintainers

- Work on a branch, open a pull request, keep the commit history meaningful.
- Every user-facing string lives in all four languages (`dashboard/static/lang/{en,nl,fr,de}.js`).
  A string added, changed or removed in one file is added, changed or removed in all four in
  the same change. Imperfect translations are fixed later via issues; partial ones are not
  merged.
- Postgres schema changes are a new numbered file under `postgres/init/`, idempotent
  (`IF NOT EXISTS` and friends). The migration runner applies it on the next dashboard start.
- Run the test suite (`pytest`) before opening the pull request. See below.
- Versioning follows SemVer. Tag releases `vMAJOR.MINOR.PATCH` and keep `VERSION` in sync;
  the in-app updater compares the two.

## Running the stack locally

A laptop with Docker Desktop is enough for a trial run; arm64 (Apple Silicon) works.

```bash
cp .env.template .env
# set SC_HOST / SC_USERNAME / SC_PASSWORD (read-only cluster credentials are fine with SC_DRY_RUN=true)
# replace every CHANGE_ME, or run: python3 install.py
docker compose --profile sentinel up --build -d
```

On macOS, point `SC_REPO_PATH` at your checkout and `SC_SSH_KEY_PATH` at a directory that
exists, because the dashboard container bind-mounts both for the in-app updater.

## Running the tests

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r tests/requirements.txt
pytest                      # unit tests only
pytest -m integration       # also hits a running stack on http://localhost:5000
```

Integration tests read the dashboard password from `.env` and skip themselves when no stack is
reachable.
