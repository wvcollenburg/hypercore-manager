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
- Modules under `shared/` are the single source for the copies inside each service directory.
  Edit them there and run `python3 tools/sync_shared.py`; a test fails when a copy drifts.
- Anything that reads or writes the HyperCore API should match `docs/hypercore-openapi-v1.json`;
  `tests/test_hypercore_spec_contract.py` is where such calls are pinned.
- Run the test suite (`pytest`) before opening the pull request. See below.
- Versioning follows SemVer. Tag releases `vMAJOR.MINOR.PATCH` and keep `VERSION` in sync;
  the in-app updater compares the two.

## Running the stack locally

A laptop with Docker Desktop is enough for a trial run; arm64 (Apple Silicon) works.

```bash
cp .env.template .env
# replace every CHANGE_ME (or run: python3 install.py)
docker compose --profile full up --build -d
```

Then open http://localhost:5000 and register a cluster. Read-only cluster credentials are fine:
every cluster starts in dry-run, and only backups need write rights.

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
reachable. CI (`.github/workflows/ci.yml`) runs the unit tests, validates the compose file and
builds the images on every pull request.
