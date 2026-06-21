# isi_kvm_lab

A Python tool for building, listing, and destroying Dell PowerScale (OneFS) prototype storage clusters running on KVM/QEMU.

> **Note:** ACS (Auto-Cluster Setup) via the serial-console first-boot wizard is supported in `--mode auto`; clusters can also be configured manually via the serial console after boot.

## Prerequisites

- Linux host with KVM/QEMU and `libvirt` (`virsh` must be in PATH)
- `qemu-img` for creating virtual disks
- Python 3.6+
- For `--mode auto`: `pexpect` and `requests` Python packages (`pip install pexpect requests`)
- A OneFS boot drive qcow2 image (see [Obtaining the Boot Drive](#obtaining-the-boot-drive))

## Obtaining the Boot Drive

The OneFS boot drive (`.qcow2`) is required to build a cluster. This file is not publicly distributed — contact your **Dell Technologies Solutions Engineer (SE)** to obtain the appropriate image for your OneFS version.

By default the script looks for boot drives in a `bootdrives/` directory next to the script, named `v<version>.qcow2` (e.g. `v9.13.0.0r.qcow2`). Override with `--boot-dir` or supply a direct path with `--boot-image`.

## Usage

```
isi_kvm_lab.py COMMAND [OPTIONS]
```

### Commands

| Flag | Description |
|------|-------------|
| `--build` | Build a new cluster |
| `--destroy` | Tear down an existing cluster |
| `--list` | List all defined clusters |
| `--help` | Show full help |

### Build a cluster (manual mode)

Builds VMs and network resources; you configure the cluster yourself via the serial console after boot.

```bash
./isi_kvm_lab.py --build \
    --cluster mycluster \
    --version 9.13.0.0r \
    --nodes 3
```

Connect to a node's serial console after boot:

```bash
telnet 127.0.0.1 <serial-port>
```

### Build a cluster (auto mode)

Builds VMs, generates `cluster.cfg`, pushes it to node1's serial-console wizard, reboots, then waits for the cluster to come up. Implies `--autostart`.

```bash
./isi_kvm_lab.py --build \
    --cluster mycluster \
    --version 9.13.0.0r \
    --nodes 3 \
    --mode auto
```

Unspecified `--cfg-*` fields are prompted for interactively (or use built-in defaults with `--cfg-*` flags to run fully non-interactively).

### Destroy a cluster

```bash
./isi_kvm_lab.py --destroy --cluster mycluster
```

Add `--force` to skip the confirmation prompt.

### List clusters

```bash
./isi_kvm_lab.py --list
```

## Build Options

| Option | Default | Description |
|--------|---------|-------------|
| `--cluster NAME` | (prompted) | Cluster name |
| `--version VERSION` | (prompted) | OneFS version, e.g. `9.13.0.0r` |
| `--nodes NUM` | `3` | Number of nodes (must be 1 or 3+; 2-node clusters are not supported) |
| `--topdir DIR` | `.` | Parent directory for cluster files |
| `--boot-dir DIR` | `<script-dir>/bootdrives` | Directory containing boot drive images |
| `--boot-image PATH` | — | Direct path to a boot drive qcow2 (overrides `--boot-dir`/`--version`) |
| `--storage-size SIZE` | `20G` | Size of each storage HDD image |
| `--serial-port PORT` | (auto-detected) | Starting TCP port for serial/telnet consoles |
| `--autostart` | off | Start VMs immediately after creation |
| `--mode auto\|manual` | `manual` | `auto` also pushes `cluster.cfg` and waits for the cluster to be ready |

### cluster.cfg overrides (`--mode auto` only)

| Flag | Default |
|------|---------|
| `--cfg-password` | `a` |
| `--cfg-admin-password` | `a` |
| `--cfg-encoding` | `utf-8` |
| `--cfg-internal-as-external` | `False` |
| `--cfg-internal-mtu` | `9000` |
| `--cfg-dns-domains` | `lab.local` |
| `--cfg-dns-servers` | `192.168.122.1` |
| `--cfg-tz-name` | `Greenwich Mean Time` |
| `--cfg-tz-path` | `GMT` |
| `--cfg-internal-netmask` | `255.255.0.0` |
| `--cfg-internal-ips` | `19.1.1.1,19.1.1.2,19.1.1.3` |
| `--cfg-external-gateway` | `192.168.122.1` |
| `--cfg-external-netmask` | `255.255.252.0` |
| `--cfg-external-ips` | `192.168.122.10,192.168.122.11,192.168.122.12` |
| `--cfg-external-mtu` | `1500` |

## VM Layout

Each node gets:
- 8 GiB RAM, 4 vCPUs
- 1 boot disk (copy of the boot drive qcow2)
- 1 NVRAM disk (1 GiB)
- 7 storage HDD disks (default 20 GiB each)
- 2 virtio NICs: one on a private internal cluster network, one on the KVM `default` (NAT) network for external/client access
- A TCP telnet serial console for out-of-band management

## Notes

- 2-node clusters are not supported; use 1 node (single-node cluster) or 3+.
- The `cluster.cfg` `join_mode` is always set to `auto` and `override_serialno_check` to `True`.
- Serial console ports are auto-detected starting at 2445 (increments of 100 if that range is in use).
