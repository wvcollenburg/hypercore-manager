# Connectivity

This chapter lists every network connection HyperCore Manager makes: who starts it, where it
goes, on which port, and what it is for. Use it to plan firewall rules when the installation,
the clusters, the backup proxies and the backup share are not all on one network.

## The parts

| Part | What it is |
|---|---|
| **HCM host** | The machine (usually a VM) running the Docker stack: dashboard, collector, balancer, Foresight, backup, console, PostgreSQL and InfluxDB. If the host is a VM on a cluster, it can also run the optional in-stack proxy. |
| **Cluster nodes** | The HyperCore nodes of every registered cluster. Each node serves the REST API on its LAN IP. |
| **Backup proxy** | A small VM on a cluster that reads snapshot clones and writes backups to the share. There is usually one per cluster; it is deployed from the dashboard or built by hand. |
| **Backup share** | The SMB or NFS share that a backup target points to (a NAS, a file server, or a share on another site). |
| **Browsers** | Users of the dashboard and, optionally, guests with a one-time console link. |

## The rule: the HCM host calls out, nothing calls in

Every connection is started by the HCM host, a proxy or a cluster node towards something else.
Clusters and proxies never connect to the HCM host. The only connections into the HCM host
come from browsers. On a stateful firewall, open each connection below in the direction it is
listed; replies are allowed by the firewall's connection tracking.

```
  Browsers ── TCP 443 (reverse proxy) or 5000 ──▶ HCM host
                                                    │
                   ┌────────── TCP 443 ─────────────┼────────── TCP 8083 ──────────┐
                   ▼                                │                              ▼
             Cluster nodes                          │ SMB 445                Backup proxy
                   │                                ▼                              │
                   └──────── SMB 445 ───────▶  Backup share  ◀ SMB 445 / NFS 2049 ─┘
```

## Connections from the HCM host

| To | Port | Protocol | What for |
|---|---|---|---|
| Every cluster node's LAN IP | TCP 443 (or the port in the cluster's address) | HTTPS | The REST API: metrics, the balancer's migrations, Foresight, VM management, snapshots and clones for backups, restores, imports, and proxy deployment. Open it to every node, not just the one you registered: when a node is down, the services switch to another node of the cluster. |
| Every cluster node's LAN IP | TCP 443 | WSS (websocket over TLS) | The VM console and the open console session check. The console connects to the node the session runs on, which is why every node must be reachable. |
| Every backup proxy | TCP 8083 | HTTPS (self-signed certificate, pinned at registration) | Backup and restore jobs, verification, file-level restore, throughput tests, logs, and pushing proxy images during deploys and updates. |
| Backup share | TCP 445 | SMB | **Test connection** on a target, creating the folder's directory, the copy list in **Import**, and pruning. |
| Backup share | NFS | NFS | Only for NFS targets with a prune path: the host mounts the share for pruning. Uses the same ports as the host's own NFS mount. |
| Mail server | TCP 587, 465 or 25 (Settings → Mail) | SMTP with STARTTLS, TLS or none | Invitations, password resets, console requests, verification alerts and cluster alerts. |
| Webhook receiver | As configured | HTTPS | Only if `SENTINEL_WEBHOOK_URL` is set: Foresight alerts. |
| `cloud-images.ubuntu.com` or your mirror | TCP 443 | HTTPS | Only when deploying a proxy from the dashboard, once per cluster: the Ubuntu cloud image is downloaded and uploaded to the cluster. The image URL in the deploy dialog can point at a local mirror. |
| `github.com` | TCP 443 (HTTPS remote) or 22 (SSH remote) | git | The update check and updates (Settings → update). |
| Docker Hub, PyPI, Debian mirrors, `github.com`, `cdnjs.cloudflare.com`, `cdn.jsdelivr.net`, `fonts.googleapis.com`, `fonts.gstatic.com` | TCP 443 (Debian mirrors may use 80) | HTTPS | Only while installing or updating, when the images are built. Once the images are built, the dashboard pages load nothing from the internet. |
| DNS servers | UDP/TCP 53 | DNS | Names of clusters, proxies, shares, the mail server and the internet sources above. |
| NTP servers | UDP 123 | NTP | Recommended. Schedules use the cluster's time zone, but run times and logs follow the host clock. |

## Connections into the HCM host

| From | Port | What for |
|---|---|---|
| Users' browsers | TCP 443 on your reverse proxy (or 5000 directly) | The dashboard, including the VM console websocket on `/console/ws` and the manual on `/manual`. |
| Guests' browsers | TCP 443 on your reverse proxy | Only if you use the guest console. Publish only `/guest/` to the outside; see [Installing and updating → Publishing only the guest console](08-installing-and-updating.md#publishing-only-the-guest-console). |

Nothing else needs to reach the host. The stack also publishes ports 8081 (Foresight API), 8082
(backup API) and 8086 (InfluxDB), on 127.0.0.1 only: the dashboard reaches them over Docker's
internal network. `HCM_INTERNAL_BIND` in `.env` publishes them on another address (for a Grafana
elsewhere); restrict them in a firewall in front of the host then, since Docker's published ports
bypass host firewalls such as ufw. Internal traffic between the containers
(PostgreSQL 5432, the console service 8084, the in-stack proxy 8083) never leaves the host.

## Connections from a backup proxy

| To | Port | Protocol | What for |
|---|---|---|---|
| Backup share | TCP 445 | SMB (CIFS) | SMB targets: writing backups, verification, restores, file-level restore. |
| Backup share | TCP 2049 | NFS v4 | NFS targets, for the same jobs. NFS v4 needs no portmapper (111) or mountd. |
| Ubuntu package archive or your apt mirror | TCP 80/443 | HTTP(S) | Only for a proxy deployed from the dashboard, once, on first boot: cloud-init installs Docker, the guest agent and lvm2. The proxy image itself is pushed by the HCM host, so the VM needs no container registry. |
| DNS servers | UDP/TCP 53 | DNS | The share's name and the package archive. |
| NTP servers | UDP 123 | NTP | Recommended; Ubuntu's default is `ntp.ubuntu.com`. |

A proxy never connects to the HCM host or to the cluster's API. Disks reach it as clones that
HyperCore attaches to the proxy VM, not over the network.

**Into the proxy:** TCP 8083 from the HCM host only. SSH (TCP 22) only if you gave the VM a key
when deploying it and want to log in.

**The network test of a test restore** runs on the proxy's test NIC, on the isolated test VLAN.
There the proxy serves DHCP (UDP 67/68) with one fixed lease per restored VM, pings the VM and
tries the VM's TCP ports listed in the test plan. None of this traffic leaves the test VLAN, so
the VLAN needs no route or firewall rule. Keep it isolated: the restored VM carries the same
addresses as the original.

## Connections from the cluster nodes

| To | Port | Protocol | What for |
|---|---|---|---|
| Backup share | TCP 445 | SMB | Restores and test restores (HyperCore imports the restored VM's definition, NVRAM and vTPM from the share), export-mode backups (HyperCore writes the copy itself) and **Import**. |

HyperCore imports and exports over SMB only, never NFS. On an **NFS target**, backups,
verification and file-level restore work (they run on the proxy), but a VM restore, a test
restore, export mode and **Import** do not; HyperCore Manager refuses them on an NFS target. Use an SMB target for anything you may need to
restore as a whole VM.

Incremental backups do not need this connection; restores do. Open it when you set up the
share, not when you first need a restore. A cluster that you only restore *onto* (a DR cluster
in a cross-cluster restore) needs it as well, and so does that cluster's proxy.

## Typical layouts

**Everything on one network.** No firewall rules are needed beyond the usual ones for browsers,
mail and updates.

**HCM in a management network, clusters on customer sites (MSP).** Per site, allow:
- HCM host → every node LAN IP, TCP 443;
- HCM host → the site's proxy, TCP 8083;
- HCM host → the site's share, TCP 445 (for Test connection and Import).

On the site itself, the proxy and the nodes need to reach the share. No site needs to reach the
management network. A VPN or a routed link only has to carry connections that start at the HCM
host.

**Share on another site than the cluster.** The proxy and the nodes connect to the share across
the link: SMB 445 (or NFS 2049) from the proxy, SMB 445 from the nodes. Backup and restore speed
then depends on that link. **Measure throughput** on the proxy shows what it delivers.

**DR cluster.** The DR cluster's proxy verifies the share around the clock and restores from
it. The DR cluster's proxy and nodes need the share; the HCM host needs the DR nodes (443) and
the DR proxy (8083).

**No internet on the HCM host.** Clusters, proxies, backups and the dashboard work without it.
What you lose:
- the update check and updates (build the new images elsewhere or open access during the update);
- a proxy deployed from the dashboard needs a local mirror for the cloud image (deploy dialog) and an apt mirror for the VM, or you build the proxy VM by hand (see [Installing and updating → The backup proxy](08-installing-and-updating.md#the-backup-proxy));
- mail needs a reachable mail server.
