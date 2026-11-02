# isi_kvm_lab

A Python tool for building, listing, and destroying Dell PowerScale (OneFS) prototype storage clusters on KVM/QEMU via libvirt.

Two build modes are supported:

- **manual** (default): builds the VMs and networks; you configure the cluster yourself over the serial console.
- **auto**: also drives the first-boot wizard on node1 over its serial console to push a generated `cluster.cfg`, then joins the remaining nodes through the OneFS Platform API (PAPI) and waits for every node's web UI.

## Prerequisites

- Linux host with KVM/QEMU and `libvirt` (`virsh` in PATH, access to `qemu:///system`)
- `qemu-img`
- `telnet` client (serial console access, and used by auto mode)
- `ping` (auto mode)
- Python 3.6+ with `PyYAML` (always required)
- `pexpect` and `requests` Python packages (`--mode auto` only)
- A OneFS boot drive qcow2 image (see below)

## Obtaining the Boot Drive

The OneFS boot drive (`.qcow2`) is not publicly distributed — contact your **Dell Technologies Solutions Engineer (SE)** for the image matching your OneFS version.

Boot drives are expected in the configured `paths.boot_dir`, named `v<version>.qcow2` (e.g. `v9.13.0.0r.qcow2`). Override the directory with `--boot-dir`, or give a file directly with `--boot-image` (the version is then inferred from the filename unless `--version` is given).

## Configuration File

All environment-specific values live in a YAML file, `build-kvm-pscale-cluster.yaml`, next to the script (override with `--config PATH`).

**First run:** if the file does not exist, it is created with generic defaults and the script exits so you can review it. At minimum review `paths.boot_dir`, `network.external_network_name`, and everything under `cluster_cfg` (passwords, DNS, IP ranges). Keys missing from an existing file fall back to built-in defaults, so upgrades don't require regenerating it.

Command-line flags always override the file for a single run.

| Section | Keys (defaults) |
|---------|-----------------|
| `paths` | `topdir` (`.`), `boot_dir` (`/opt/onefs-images`) |
| `build_defaults` | `version` (null), `storage_size` (`20G`), `num_nodes` (3), `autostart` (false), `mode` (`manual`), `connect` (null) |
| `vm` | `memory_gib` (8), `vcpus` (4), `machine_type` (`pc-i440fx-5.2`), `disk_cache_mode` (`writethrough`), `storage_drive_count` (7), `scsi_slots_per_controller` (6), `video_model` (`cirrus`), `vnc_listen` (`127.0.0.1`) |
| `network` | `external_network_name` (`default`), `serial_port_base` (2445), `serial_port_search_step` (100), `serial_port_search_attempts` (100) |
| `cluster_cfg` | see [cluster.cfg values](#clustercfg-values-mode-auto) |
| `timeouts` | `webui_port` (8080), `wizard_prompt_seconds` (900), `wizard_nudge_interval_seconds` (5), `disk_format_seconds` (600), `ping_seconds` (600), `ping_interval_seconds` (10), `webui_seconds` (600), `webui_interval_seconds` (10), `papi_login_seconds` (600) |

> **Do not change `vm.machine_type`.** QEMU 8.x defaults to SMBIOS 3.0, but OneFS only scans for the legacy `_SM_` anchor; `pc-i440fx-5.2` is the last machine type that provides it.

## Usage

```
isi_kvm_lab.py COMMAND [OPTIONS]
```

| Command | Description |
|---------|-------------|
| `--build` | Build a new cluster |
| `--destroy` | Tear down an existing cluster |
| `--list` | List all defined clusters |
| `--help`, `-h` | Show help |

### Build (manual mode)

```bash
./isi_kvm_lab.py --build --cluster mycluster --version 9.13.0.0r --nodes 3
```

Connect to a node's serial console:

```bash
telnet 127.0.0.1 <serial-port>
```

Missing `--cluster`, `--version` (available boot drives are listed) and `--nodes` are prompted for.

### Build (auto mode)

```bash
./isi_kvm_lab.py --build --cluster mycluster --version 9.13.0.0r --nodes 3 --mode auto
```

Implies `--autostart`. The sequence is:

1. Generate `cluster.cfg` (written to the current directory). If any `--cfg-*` flag is given, the remaining fields are prompted for interactively; if none are given, the config-file values are used without prompting.
2. Build and start all VMs.
3. Connect to node1's console, answer the first-boot wizard, wait for disk formatting and capture the node serial, drop to a shell, write `/cluster.cfg`, and reboot. The raw session is logged to `<cluster>-acs-console.log`.
4. Wait for node1's first external IP to answer ping, then for the web UI (`https://<ip>:<webui_port>/`) to return HTTP 200.
5. For a multi-node cluster: capture each joining node's serial from its console (logged to `<cluster>-node<N>-console.log`), log in to PAPI as `root`, call `add-node` for each, and wait for each node's ping and web UI.

Any failed wait exits non-zero with a pointer to the serial console.

### Destroy

```bash
./isi_kvm_lab.py --destroy --cluster mycluster [--force]
```

Destroys and undefines the cluster's VMs, removes its internal libvirt network, and deletes the cluster directory. Prompts for `yes` unless `--force` is given.

### List

```bash
./isi_kvm_lab.py --list
```

Shows each cluster with its OneFS version, nodes, running/stopped state, and serial port.

## Build Options

| Option | Default | Description |
|--------|---------|-------------|
| `--config PATH` | `<script-dir>/build-kvm-pscale-cluster.yaml` | YAML config file |
| `--cluster NAME` | (prompted) | Cluster name |
| `--version VERSION` | config / prompted | OneFS version, e.g. `9.13.0.0r` |
| `--nodes NUM` | config (3) / prompted | Node count; must be 1 or 3+ |
| `--topdir DIR` | config (`.`) | Parent directory for cluster files |
| `--boot-dir DIR` | config | Directory containing `v<version>.qcow2` images |
| `--boot-image PATH` | — | Direct path to a boot drive (overrides `--boot-dir`) |
| `--storage-size SIZE` | config (`20G`) | Size of each storage drive |
| `--serial-port PORT` | auto-detected | First serial/telnet port (one per node, consecutive) |
| `--autostart` | config (off) | Start VMs after creation |
| `--connect true\|false` | config; auto | Attach to node1's serial console when the build finishes. Default: true for 1-node clusters, false otherwise |
| `--mode auto\|manual` | config (`manual`) | See above |

`--destroy` also accepts `--cluster` and `--force`.

### cluster.cfg values (`--mode auto`)

Defaults come from the `cluster_cfg` section of the config file (generated values shown); each can be overridden with the flag.

| Flag | Default | cluster.cfg field |
|------|---------|-------------------|
| `--cfg-password` | `changeme` | `[Cluster] password` (root) |
| `--cfg-admin-password` | `changeme` | `[Cluster] admin_user_password` |
| `--cfg-encoding` | `utf-8` | `[Cluster] encoding` |
| `--cfg-internal-as-external` | `False` | `[Cluster] internal_interface_as_external` |
| `--cfg-internal-mtu` | `9000` | `[Cluster] internal_mtu` |
| `--cfg-dns-domains` | `example.local` | `[Cluster] dns_domains` |
| `--cfg-dns-servers` | `192.168.122.1` | `[Cluster] dns_servers` |
| `--cfg-tz-name` | `Greenwich Mean Time` | `[Timezone] name` |
| `--cfg-tz-path` | `GMT` | `[Timezone] path` |
| `--cfg-internal-netmask` | `255.255.0.0` | `[Internal-A] netmask` |
| `--cfg-internal-ips` | `19.1.1.1,19.1.1.2,19.1.1.3` | `[Internal-A] ip_address_ranges` |
| `--cfg-external-gateway` | `192.168.122.1` | `[External-1] gateway` |
| `--cfg-external-netmask` | `255.255.252.0` | `[External-1] netmask` |
| `--cfg-external-ips` | `192.168.122.10,192.168.122.11,192.168.122.12` | `[External-1] ip_address_ranges` |
| `--cfg-external-mtu` | `1500` | `[External-1] mtu` |

The IP lists are scaled to the node count by incrementing the last octet of the final address (or truncated if there are more addresses than nodes). Fixed, non-configurable values: `name` = `--cluster`, `nt4_compatibility` = `Off`, `join_mode` = `auto`, `override_serialno_check` = `True`.

## VM Layout

Each node (named `<cluster>-OneFS-<version>-Node<N>`) gets:

- Memory and vCPUs per `vm.memory_gib` / `vm.vcpus` (default 8 GiB, 4 vCPUs), host-passthrough CPU
- 1 boot disk (`boot0.qcow2`, a copy of the boot drive)
- 1 NVRAM disk (`isi-nvram.qcow2`, 1 GiB)
- `vm.storage_drive_count` storage disks (`storageHDD<N>.qcow2`, default 7 × 20 GiB), on virtio-SCSI controllers with up to `vm.scsi_slots_per_controller` disks each
- 2 virtio NICs with random MACs: `vtnet0` on a per-cluster internal libvirt network (`<cluster>-internal`, bridge `virbr-<first 8 chars of name>`), `vtnet1` on the libvirt network named by `network.external_network_name` (default `default`, NAT)
- A localhost-only telnet serial console (one TCP port per node) and a VNC console on `vnc_listen`

Files are stored under `<topdir>/<cluster>/` (`Node<N>/` per node, plus `internal-net.xml`).

## Serial Ports

Ports are auto-assigned starting at `network.serial_port_base` (2445), one per node. If any port in the range is used by an existing VM, the start is advanced by `serial_port_search_step` (100) up to `serial_port_search_attempts` times. An explicit `--serial-port` fails if any needed port is in use.

## Notes

- 2-node clusters are not supported (quorum requires 1 or 3+).
- Building fails if a VM, internal network, or cluster directory with the same cluster name already exists.
- `cluster.cfg` and the console logs are written to the current working directory.
- Change the default `changeme` passwords in the config file before using auto mode.
