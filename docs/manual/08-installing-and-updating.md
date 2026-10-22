# Installing and updating

The short version. The [deployment guide](../../DEPLOY.md) has every variable, the security
hardening, dark-site deployment, health checks and troubleshooting.

## What you need

A Linux host or VM with Docker Engine 24 or later and Docker Compose v2 (`docker compose`, no
hyphen; the old python `docker-compose` is not supported). Two vCPU and 4 GB of RAM carry the
core stack; Foresight wants more when it analyses many VMs. A HyperCore account with read and
migration rights, and, for backups, one with snapshot, clone and block-device rights. A laptop
with Docker Desktop is fine for a trial, Apple Silicon included.

## Two ways to install

**With the installer.** `install.py` is an interactive, text-mode installer that walks you
through every setting of `.env`: what it does, why the default is what it is, when to change
it. It generates the secrets for you, warns when the wrong Compose is on the host, and pre-fills
an existing `.env` when you run it again later. Then it shows the commands to start the stack.

```
git clone <repository>
cd hypercore-manager
python3 install.py
```

**By hand.** Copy `.env.template` to `.env`, replace every `CHANGE_ME` (at least
`HCM_SECRET_KEY`, the database secrets and the dashboard password), and start:

```
docker compose up --build -d
```

Either way the dashboard answers on port 5000 within a minute. Log in; the first screen asks for
your cluster, the management address and the HyperCore account. From then on everything else
in this manual applies. The balancer starts in dry-run and needs a few minutes of samples
before it has an opinion; the first charts appear after the collector's first cycle.

## Updating

**From the dashboard.** The header shows **Update** when a newer release is available. Apply it
and the stack pulls the release, rebuilds and restarts itself; database migrations ship with the
release that needs them and apply on the dashboard's next start. Expect a short outage of every
service, including the database containers.

**By hand.** The same thing from a shell on the host:

```
git pull
docker compose up --build -d
```

**Upgrading from 0.9.x** is a bigger step, because clusters moved from `.env` into the
dashboard: add `HCM_SECRET_KEY` and the backup tokens to `.env`, remove the old `SC_*` cluster
variables, rebuild, log in and register the cluster again. Your old metrics are not lost:
Settings → Clusters offers **Adopt history** once it finds them, and Foresight and the balancer
keep their past. The deployment guide has the step list.
