# VM definition templates

One libvirt domain XML template per HyperCore machine type and OS profile, captured from
HyperCore 9.8.4 exports on 2026-10-22 and neutralised. `vm_definition.render()` fills the
placeholders from a `VirDomain` record so a restore needs no native export. A machine type
without a template fails a restore with "no definition template for <type>"; add one here by
exporting any VM of that type natively, diffing the XML against the nearest template (so far the
differences are the qemu machine model, the type name and the TPM block) and shipping it with a
test. Machine types follow the hardware's CPU generation, so new ones arrive with new platforms.

Placeholders: `uuid`, `description`, `mem_kib`, `vcpu`, `nvram_uuid` (UEFI types), `vtpm_uuid`
(tpm types), `os`, `cpu_type`, `tags`, `devices` (generated disk/cdrom/interface elements).

| machine type | firmware | qemu machine | file |
|---|---|---|---|
| scale-7.2 | BIOS | pc-i440fx-rhel7.0.0 | scale-7.2__os_{other,windows}.xml |
| scale-bios-9.3 | BIOS | pc-i440fx-rhel7.6.0 | scale-bios-9.3__os_{other,windows}.xml |
| scale-8.10 | UEFI | pc-q35-rhel7.6.0 | scale-8.10__os_{other,windows}.xml |
| scale-uefi-9.3 | UEFI | pc-q35-rhel8.6.0 | scale-uefi-9.3__os_{other,windows}.xml |
| scale-uefi-tpm-9.3 | UEFI + vTPM | pc-q35-rhel8.6.0 | scale-uefi-tpm-9.3__os_{other,windows}.xml |
| scale-uefi-tpm-9.5 | UEFI + vTPM | pc-q35-rhel9.2.0 | scale-uefi-tpm-9.5__os_{other,windows}.xml |
| scale-uefi-9.5 | UEFI | pc-q35-rhel9.2.0 | scale-uefi-9.5__os_{other,windows}.xml (derived from the tpm-9.5 capture minus the vTPM, checked against a HyperCore 9.7.8 export on 2026-10-22) |

The Windows profile adds the `<hyperv>` enlightenments and the `hypervclock` timer; everything
else in a template is per machine type. `os_windows` covers every `os_windows_*` value.
