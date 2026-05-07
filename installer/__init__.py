"""HyperCore Manager installer package.

`installer.core`        — shared schema, env-file IO, validation, secret autogen.
                          Used by both the curses TUI (install.py at repo root)
                          and the Windows deploy wizard (installer/windows).
`installer.windows`     — Windows-only deploy wizard that provisions a VM on
                          HyperCore via cloud-init, then boots it with the full
                          stack ready to run.
"""
