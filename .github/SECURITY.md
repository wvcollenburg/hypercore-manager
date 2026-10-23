# Security

HyperCore Manager holds cluster credentials and, outside dry-run mode, live-migrates virtual
machines. Treat a vulnerability report accordingly.

## Reporting

This is community-supported software without a dedicated security team or response SLA.
Report a suspected vulnerability by opening a
[GitHub Issue](https://github.com/wvcollenburg/hypercore-manager/issues) with the **Security**
label.

Please **do not** include a working exploit, credentials, or details that would let someone
reproduce the issue against a live cluster. Describe the affected component, the impact, and
how to contact you; a maintainer will follow up privately for the details.

## Hardening

Before exposing the stack to any network beyond localhost, work through the Security Hardening
Checklist in [the installing chapter of the manual](docs/manual/08-installing-and-updating.md#securing-the-installation): strong secrets, TLS in front of the dashboard, no host
port bindings for InfluxDB and Sentinel, and an extended dry-run observation period before
enabling live migrations.
