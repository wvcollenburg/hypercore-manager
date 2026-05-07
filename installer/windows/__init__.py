"""Windows-only deploy wizard for HyperCore Manager.

`hypercore`   — REST client (login, VM create, virtual-disk upload/attach, power on)
`cloudinit`   — userData/metaData YAML builder (base64-encoded for HyperCore)
`image_cache` — Ubuntu cloud-image download + on-disk cache
`wizard`      — Tk GUI entry point (PyInstaller target)
`build`       — PyInstaller build script that produces deploy.exe + payload zip
"""
