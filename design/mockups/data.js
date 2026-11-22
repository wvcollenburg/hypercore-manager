// Shared sample data for the three mockups, so they compare like for like.
const KPIS = [
  { label: 'Nodes', value: '3 / 3', note: 'all online · HyperCore 9.4.30' },
  { label: 'VMs running', value: '24 / 27', note: '3 stopped' },
  { label: 'CPU', value: '38%', pct: 38 },
  { label: 'Memory', value: '412 / 672 GiB', pct: 61 },
  { label: 'Storage', value: '18.4 / 34.0 TiB', pct: 54 },
];
const VMS = [
  { name: 'dc01', os: 'Windows Server 2022', on: true, node: 'Node 1', cpu: 12, vcpu: 4, ram: 8, disk: 120, tags: ['infra'] },
  { name: 'dc02', os: 'Windows Server 2022', on: true, node: 'Node 3', cpu: 9, vcpu: 4, ram: 8, disk: 120, tags: ['infra'] },
  { name: 'sql01', os: 'Windows Server 2019', on: true, node: 'Node 2', cpu: 64, vcpu: 8, ram: 64, disk: 900, tags: ['erp', 'db'] },
  { name: 'erp-app01', os: 'Windows Server 2022', on: true, node: 'Node 2', cpu: 41, vcpu: 8, ram: 32, disk: 250, tags: ['erp'] },
  { name: 'web01', os: 'Ubuntu 24.04', on: true, node: 'Node 1', cpu: 18, vcpu: 2, ram: 4, disk: 40, tags: ['web'] },
  { name: 'web02', os: 'Ubuntu 24.04', on: true, node: 'Node 3', cpu: 16, vcpu: 2, ram: 4, disk: 40, tags: ['web'] },
  { name: 'file01', os: 'Windows Server 2022', on: true, node: 'Node 1', cpu: 6, vcpu: 4, ram: 16, disk: 4096, tags: ['infra'] },
  { name: 'rds01', os: 'Windows Server 2022', on: true, node: 'Node 3', cpu: 52, vcpu: 16, ram: 96, disk: 300, tags: ['rds'] },
  { name: 'backup-old', os: 'Ubuntu 20.04', on: false, node: 'Node 2', cpu: 0, vcpu: 2, ram: 4, disk: 500, tags: [] },
];
