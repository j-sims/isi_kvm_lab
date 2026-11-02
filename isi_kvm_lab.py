#!/usr/bin/env python3
"""Build/destroy/list OneFS-on-KVM proto-storage clusters.

Python port of build-kvm-pscale-cluster-v2.0.sh.  Supports two modes:
  manual (default): VMs and networks are built; the cluster is configured
    by hand via the serial console after boot.
  auto: partial ACS (Auto-Cluster Setup).  A generated cluster.cfg is
    pushed to node1 through its serial-console first-boot wizard, then the
    remaining nodes are joined via the PAPI add-node call.

All environment-specific values (paths, VM hardware sizing, network
names, cluster.cfg defaults, timeouts) live in a YAML config file next
to this script. On first run, if that file doesn't exist, it is created
with generic defaults and the script exits so the values can be reviewed.
"""

import argparse
import gzip
import http.client
import os
import random
import re
import shutil
import ssl
import subprocess
import sys
import time

import yaml

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(SCRIPT_DIR, "build-kvm-pscale-cluster.yaml")

# ---------------------------------------------------------------------------
# Config: defaults, loading, first-run bootstrap
# ---------------------------------------------------------------------------

CONFIG_DEFAULTS = {
    "paths": {
        "topdir": ".",
        "boot_dir": "/opt/onefs-images",
    },
    "build_defaults": {
        "version": None,
        "storage_size": "20G",
        "num_nodes": 3,
        "autostart": False,
        "mode": "manual",
        "connect": None,
    },
    "vm": {
        "memory_gib": 8,
        "vcpus": 4,
        # DO NOT change unless you know why: QEMU 8.x defaults to SMBIOS 3.0
        # (_SM3_), but the OneFS SMBIOS library only scans for the legacy
        # _SM_ anchor. pc-i440fx-5.2 is the last machine type that provides it.
        "machine_type": "pc-i440fx-5.2",
        "disk_cache_mode": "writethrough",
        "storage_drive_count": 7,
        "scsi_slots_per_controller": 6,
        "video_model": "cirrus",
        "vnc_listen": "127.0.0.1",
    },
    "network": {
        "external_network_name": "default",
        "serial_port_base": 2445,
        "serial_port_search_step": 100,
        "serial_port_search_attempts": 100,
    },
    "cluster_cfg": {
        "password": "changeme",
        "admin_user_password": "changeme",
        "encoding": "utf-8",
        "internal_as_external": "False",
        "internal_mtu": "9000",
        "dns_domains": "example.local",
        "dns_servers": "192.168.122.1",
        "tz_name": "Greenwich Mean Time",
        "tz_path": "GMT",
        "internal_netmask": "255.255.0.0",
        "internal_ips": "19.1.1.1,19.1.1.2,19.1.1.3",
        "external_gateway": "192.168.122.1",
        "external_netmask": "255.255.252.0",
        "external_ips": "192.168.122.10,192.168.122.11,192.168.122.12",
        "external_mtu": "1500",
    },
    "timeouts": {
        "webui_port": 8080,
        "wizard_prompt_seconds": 900,
        "wizard_nudge_interval_seconds": 5,
        "disk_format_seconds": 600,
        "ping_seconds": 600,
        "ping_interval_seconds": 10,
        "webui_seconds": 600,
        "webui_interval_seconds": 10,
        "papi_login_seconds": 600,
    },
}

CONFIG_HEADER = """\
# build-kvm-pscale-cluster configuration
#
# This file was auto-generated with generic defaults because it did not
# exist yet. Review and edit the values below -- in particular
# paths.boot_dir, network.external_network_name, and everything under
# cluster_cfg (passwords, DNS, IP ranges) -- to match your environment,
# then re-run the script.
#
# Command-line flags always override the values here for a single run.
"""

# Per-field comments for the generated config file, in the order they are
# written. Values themselves come from CONFIG_DEFAULTS (single source of
# truth) -- this only supplies the trailing "# ..." description for each line.
CONFIG_FIELD_COMMENTS = {
    "paths": [
        ("topdir", "Directory where cluster subdirectories are created (relative or absolute)"),
        ("boot_dir", "Directory containing v{VERSION}.qcow2 boot drive images"),
    ],
    "build_defaults": [
        ("version", "Default OneFS version to use when --version is not given (null = list available and prompt)"),
        ("storage_size", "Default size for each storage drive (qemu-img size syntax, e.g. 20G)"),
        ("num_nodes", "Default node count for new clusters (must be 1 or 3+)"),
        ("autostart", "Start VMs automatically after --build completes"),
        ("mode", "Default cluster config mode: manual (console only) or auto (also pushes cluster.cfg)"),
        ("connect", "Default --connect value: true, false, or null (null = auto: true for 1-node, false otherwise)"),
    ],
    "vm": [
        ("memory_gib", "RAM per node, in GiB"),
        ("vcpus", "vCPUs per node"),
        ("machine_type", "QEMU machine type -- do not change (SMBIOS 2.x _SM_ anchor requirement, see CLAUDE.md)"),
        ("disk_cache_mode", "QEMU disk cache mode used for every node disk"),
        ("storage_drive_count", "Number of storage drives (storageHDD*.qcow2) created per node"),
        ("scsi_slots_per_controller", "Disks per virtio-scsi controller before a new controller is added"),
        ("video_model", "QEMU video device model"),
        ("vnc_listen", "Address the VNC graphics console listens on"),
    ],
    "network": [
        ("external_network_name", "libvirt network used for each node's external/client interface (vtnet1)"),
        ("serial_port_base", "First serial/telnet port tried when auto-assigning ports"),
        ("serial_port_search_step", "Port range increment tried if a range is already in use"),
        ("serial_port_search_attempts", "Max number of ranges to try before giving up"),
    ],
    "cluster_cfg": [
        ("password", "cluster.cfg [Cluster] password -- root password (--mode auto only)"),
        ("admin_user_password", "cluster.cfg [Cluster] admin_user_password"),
        ("encoding", "cluster.cfg [Cluster] encoding"),
        ("internal_as_external", "cluster.cfg [Cluster] internal_interface_as_external"),
        ("internal_mtu", "cluster.cfg [Cluster] internal_mtu"),
        ("dns_domains", "cluster.cfg [Cluster] dns_domains"),
        ("dns_servers", "cluster.cfg [Cluster] dns_servers"),
        ("tz_name", "cluster.cfg [Timezone] name"),
        ("tz_path", "cluster.cfg [Timezone] path"),
        ("internal_netmask", "cluster.cfg [Internal-A] netmask"),
        ("internal_ips", "cluster.cfg [Internal-A] ip_address_ranges base list, scaled to the node count"),
        ("external_gateway", "cluster.cfg [External-1] gateway"),
        ("external_netmask", "cluster.cfg [External-1] netmask"),
        ("external_ips", "cluster.cfg [External-1] ip_address_ranges base list, scaled to the node count"),
        ("external_mtu", "cluster.cfg [External-1] mtu"),
    ],
    "timeouts": [
        ("webui_port", "OneFS web UI / PAPI port"),
        ("wizard_prompt_seconds", "Max wait for the first-boot wizard yes/no prompt"),
        ("wizard_nudge_interval_seconds", "How often to send a bare CR while waiting for the wizard prompt"),
        ("disk_format_seconds", "Max wait for disk formatting to finish and print the node serial number"),
        ("ping_seconds", "Max wait for a node's external IP to respond to ping"),
        ("ping_interval_seconds", "Delay between ping attempts"),
        ("webui_seconds", "Max wait for the web UI to respond after ping succeeds"),
        ("webui_interval_seconds", "Delay between web UI poll attempts"),
        ("papi_login_seconds", "Max wait for the PAPI session API to become available"),
    ],
}


def deep_merge(base, override):
    """Recursively merge `override` onto a copy of `base`, filling in any
    keys missing from `override` (used so upgrades that add new config
    keys don't require regenerating an existing config file)."""
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _format_scalar(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return f'"{value}"'


def write_default_config(path):
    lines = [CONFIG_HEADER.rstrip("\n")]
    for section, fields in CONFIG_FIELD_COMMENTS.items():
        lines.append(f"{section}:")
        for key, comment in fields:
            value = CONFIG_DEFAULTS[section][key]
            lines.append(f"  {key}: {_format_scalar(value)}  # {comment}")
        lines.append("")
    text = "\n".join(lines).rstrip("\n") + "\n"
    with open(path, "w") as f:
        f.write(text)


def load_config(path):
    if not os.path.isfile(path):
        write_default_config(path)
        print(f"No config file found -- created one with default values at: {path}")
        print("Please review and update it (paths, network, cluster_cfg, etc.), then re-run this script.")
        sys.exit(0)

    with open(path) as f:
        loaded = yaml.safe_load(f) or {}
    return deep_merge(CONFIG_DEFAULTS, loaded)


# (arg attr, section, key, prompt label, config key in cluster_cfg)
# nt4_compatibility, join_mode, and override_serialno_check are intentionally
# not exposed here: nt4_compatibility is always Off, join_mode is always
# auto, and override_serialno_check is always True (required for ACS to
# accept any node serial).
CFG_FIELD_SPECS_TEMPLATE = [
    ("cfg_password", "Cluster", "password", "Cluster root password", "password"),
    ("cfg_admin_password", "Cluster", "admin_user_password", "Admin user password", "admin_user_password"),
    ("cfg_encoding", "Cluster", "encoding", "Encoding", "encoding"),
    ("cfg_internal_as_external", "Cluster", "internal_interface_as_external", "Internal interface as external", "internal_as_external"),
    ("cfg_internal_mtu", "Cluster", "internal_mtu", "Internal MTU", "internal_mtu"),
    ("cfg_dns_domains", "Cluster", "dns_domains", "DNS domains", "dns_domains"),
    ("cfg_dns_servers", "Cluster", "dns_servers", "DNS servers", "dns_servers"),
    ("cfg_tz_name", "Timezone", "name", "Timezone name", "tz_name"),
    ("cfg_tz_path", "Timezone", "path", "Timezone path", "tz_path"),
    ("cfg_internal_netmask", "Internal-A", "netmask", "Internal netmask", "internal_netmask"),
    ("cfg_external_gateway", "External-1", "gateway", "External gateway", "external_gateway"),
    ("cfg_external_netmask", "External-1", "netmask", "External netmask", "external_netmask"),
    ("cfg_external_mtu", "External-1", "mtu", "External MTU", "external_mtu"),
]


def build_cfg_field_specs(config):
    cc = config["cluster_cfg"]
    return [
        (attr, section, key, label, cc[config_key])
        for attr, section, key, label, config_key in CFG_FIELD_SPECS_TEMPLATE
    ]


# ---------------------------------------------------------------------------
# Shell / virsh helpers
# ---------------------------------------------------------------------------

def run(cmd, check=True, capture=False):
    print(f"$ {cmd}")
    kwargs = {"shell": True, "universal_newlines": True}
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    result = subprocess.run(cmd, **kwargs)
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed (rc={result.returncode}): {cmd}")
    return result


def run_quiet(cmd):
    return run(cmd, check=False, capture=True)


def virsh(args, check=True, capture=True):
    return run(f"virsh -c qemu:///system {args}", check=check, capture=capture)


def prompt_input(prompt, default=None):
    if default is not None:
        result = input(f"{prompt} (default: {default}): ").strip()
        return result or default
    result = input(f"{prompt}: ").strip()
    while not result:
        result = input(f"{prompt}: ").strip()
    return result


def list_all_vms():
    """Return list of (name, state) tuples from `virsh list --all`."""
    result = virsh("list --all", check=False)
    lines = (result.stdout or "").splitlines()[2:]
    vms = []
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        name = parts[1]
        state = " ".join(parts[2:])
        vms.append((name, state))
    return vms


def port_in_use(port):
    for name, _ in list_all_vms():
        dump = run_quiet(f"virsh -c qemu:///system dumpxml {name}").stdout or ""
        if f"service='{port}'" in dump:
            return True
    return False


def find_available_serial_port(num_nodes, net_cfg):
    start_port = net_cfg["serial_port_base"]
    step = net_cfg["serial_port_search_step"]
    for _ in range(net_cfg["serial_port_search_attempts"]):
        if all(not port_in_use(p) for p in range(start_port, start_port + num_nodes)):
            return start_port
        start_port += step
    raise RuntimeError("Could not find available serial port range")


def get_serial_port(vm_name):
    dump = run_quiet(f"virsh -c qemu:///system dumpxml {vm_name}").stdout or ""
    m = re.search(r"service='([^']*)'", dump)
    return m.group(1) if m else None


def random_mac():
    return "52:54:00:%02x:%02x:%02x" % (
        random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))


# ---------------------------------------------------------------------------
# Domain / network XML
# ---------------------------------------------------------------------------

def _disk_xml(source_file, target_dev, controller, unit, cache_mode):
    return f"""    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2' cache='{cache_mode}'/>
      <source file='{source_file}'/>
      <target dev='{target_dev}' bus='scsi'/>
      <address type='drive' controller='{controller}' bus='0' target='0' unit='{unit}'/>
    </disk>"""


def _target_dev_names(count):
    """sda, sdb, ... sdz, sdaa, ... (more than enough for any realistic count)."""
    names = []
    letters = "abcdefghijklmnopqrstuvwxyz"
    for i in range(count):
        if i < 26:
            names.append(f"sd{letters[i]}")
        else:
            names.append(f"sd{letters[i // 26 - 1]}{letters[i % 26]}")
    return names


def create_node_xml(node_name, node_dir, boot_drive, serial_port,
                     mac_internal, mac_external, cluster_name, vm_cfg, network_name):
    storage_count = vm_cfg["storage_drive_count"]
    slots_per_controller = vm_cfg["scsi_slots_per_controller"]
    cache_mode = vm_cfg["disk_cache_mode"]

    sources = [boot_drive, os.path.join(node_dir, "isi-nvram.qcow2")]
    sources += [os.path.join(node_dir, f"storageHDD{i}.qcow2") for i in range(storage_count)]

    dev_names = _target_dev_names(len(sources))
    disk_blocks = []
    max_controller = 0
    for i, (source, dev) in enumerate(zip(sources, dev_names)):
        controller = i // slots_per_controller
        unit = i % slots_per_controller
        max_controller = max(max_controller, controller)
        disk_blocks.append(_disk_xml(source, dev, controller, unit, cache_mode))

    controller_blocks = []
    for c in range(max_controller + 1):
        slot = 0x06 + c * 2
        controller_blocks.append(f"""    <controller type='scsi' index='{c}' model='virtio-scsi'>
      <driver queues='4'/>
      <address type='pci' domain='0x0000' bus='0x00' slot='0x{slot:02x}' function='0x0'/>
    </controller>""")

    usb_slot = 0x06 + (max_controller + 1) * 2 + 1
    disks_xml = "\n\n".join(disk_blocks)
    controllers_xml = "\n".join(controller_blocks)

    xml = f"""<domain type='kvm'>
  <name>{node_name}</name>
  <memory unit='GiB'>{vm_cfg["memory_gib"]}</memory>
  <currentMemory unit='GiB'>{vm_cfg["memory_gib"]}</currentMemory>
  <vcpu placement='static'>{vm_cfg["vcpus"]}</vcpu>
  <os>
    <type arch='x86_64' machine='{vm_cfg["machine_type"]}'>hvm</type>
    <boot dev='hd'/>
  </os>
  <features>
    <acpi/>
    <apic/>
    <vmport state='on'/>
  </features>
  <cpu mode='host-passthrough'>
    <topology sockets='1' dies='1' cores='{vm_cfg["vcpus"]}' threads='1'/>
  </cpu>
  <clock offset='utc'>
    <timer name='rtc' tickpolicy='catchup'/>
    <timer name='pit' tickpolicy='delay'/>
    <timer name='hpet' present='no'/>
  </clock>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <on_crash>restart</on_crash>
  <pm>
    <suspend-to-mem enabled='no'/>
    <suspend-to-disk enabled='no'/>
  </pm>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>

{disks_xml}

{controllers_xml}

    <controller type='usb' index='0' model='ich9-ehci1'>
      <address type='pci' domain='0x0000' bus='0x00' slot='0x{usb_slot:02x}' function='0x7'/>
    </controller>

    <controller type='pci' index='0' model='pci-root'/>

    <!-- vtnet0: internal cluster network -->
    <interface type='network'>
      <mac address='{mac_internal}'/>
      <source network='{cluster_name}-internal'/>
      <model type='virtio'/>
      <address type='pci' domain='0x0000' bus='0x00' slot='0x03' function='0x0'/>
    </interface>

    <!-- vtnet1: external/client network -->
    <interface type='network'>
      <mac address='{mac_external}'/>
      <source network='{network_name}'/>
      <model type='virtio'/>
      <address type='pci' domain='0x0000' bus='0x00' slot='0x04' function='0x0'/>
    </interface>

    <serial type='tcp'>
      <source mode='bind' host='127.0.0.1' service='{serial_port}'/>
      <protocol type='telnet'/>
      <target type='isa-serial' port='0'/>
    </serial>
    <console type='tcp'>
      <source mode='bind' host='127.0.0.1' service='{serial_port}'/>
      <protocol type='telnet'/>
      <target type='serial' port='0'/>
    </console>

    <graphics type='vnc' port='-1' autoport='yes' listen='{vm_cfg["vnc_listen"]}'>
      <listen type='address' address='{vm_cfg["vnc_listen"]}'/>
    </graphics>

    <video>
      <model type='{vm_cfg["video_model"]}'/>
    </video>

    <memballoon model='virtio'/>
  </devices>
</domain>
"""
    with open(os.path.join(node_dir, "node.xml"), "w") as f:
        f.write(xml)


def create_network_xml(cluster_name, cluster_dir):
    bridge_name = f"virbr-{cluster_name[:8]}"
    xml = f"""<network>
  <name>{cluster_name}-internal</name>
  <bridge name='{bridge_name}' stp='on' delay='0'/>
</network>
"""
    path = os.path.join(cluster_dir, "internal-net.xml")
    with open(path, "w") as f:
        f.write(xml)
    return path


# ---------------------------------------------------------------------------
# list / destroy
# ---------------------------------------------------------------------------

def list_clusters():
    print("=== OneFS KVM Clusters ===\n")
    names = [name for name, _ in list_all_vms()]
    clusters = sorted({n.split("-OneFS-")[0] for n in names if "-OneFS-" in n})

    if not clusters:
        print("No clusters found")
        return

    for cluster in clusters:
        print(f"Cluster: {cluster}")
        vms = [(n, s) for n, s in list_all_vms() if n.startswith(f"{cluster}-OneFS-")]
        if not vms:
            print("  No nodes")
            print()
            continue
        version = re.sub(r"-Node.*", "", re.sub(r".*-OneFS-", "", vms[0][0]))
        print(f"  Version: {version}")
        print(f"  Nodes: {len(vms)}")
        for name, state in vms:
            status = "running" if state == "running" else "stopped"
            serial_port = get_serial_port(name)
            if serial_port:
                print(f"    {name:<40} {status} (serial: {serial_port})")
            else:
                print(f"    {name:<40} {status}")
        print()


def destroy_cluster(cluster_dir, cluster_name, version):
    print(f"Destroying cluster: {cluster_name} (v{version})")

    for name, _ in list_all_vms():
        if f"{cluster_name}-OneFS-" not in name:
            continue
        run(f"virsh -c qemu:///system destroy {name}", check=False)
        if virsh(f"undefine {name}", check=False).returncode != 0:
            print(f"Error: Failed to undefine VM: {name}")
            sys.exit(1)

    net_list = virsh("net-list --all", check=False).stdout or ""
    if f"{cluster_name}-internal" in net_list:
        run(f"virsh -c qemu:///system net-destroy {cluster_name}-internal", check=False)
        if virsh(f"net-undefine {cluster_name}-internal", check=False).returncode != 0:
            print(f"Error: Failed to undefine network: {cluster_name}-internal")
            sys.exit(1)

    if cluster_dir and os.path.isdir(cluster_dir):
        shutil.rmtree(cluster_dir)

    print("Done.")


# ---------------------------------------------------------------------------
# Auto-mode: cluster.cfg generation (ACS via serial-console first-boot wizard)
# ---------------------------------------------------------------------------

def ip_list_for_count(base_csv, count):
    """Scale a comma-separated IP list to `count` entries by incrementing
    the last octet of the final address."""
    base_ips = [ip.strip() for ip in base_csv.split(",") if ip.strip()]
    if not base_ips:
        return []
    if count <= len(base_ips):
        return base_ips[:count]
    prefix = ".".join(base_ips[-1].split(".")[:-1])
    last_octet = int(base_ips[-1].split(".")[-1])
    result = list(base_ips)
    for i in range(1, count - len(base_ips) + 1):
        result.append(f"{prefix}.{last_octet + i}")
    return result


def cfg_args_given(args, cfg_field_specs):
    """True if the user passed any --cfg-* override on the command line."""
    attrs = [attr for attr, *_ in cfg_field_specs] + ["cfg_internal_ips", "cfg_external_ips"]
    return any(getattr(args, attr) is not None for attr in attrs)


def _resolve_field(cli_val, default, label, non_interactive):
    if cli_val is not None:
        return cli_val
    if non_interactive:
        return default
    return prompt_input(label, default)


def gather_cluster_cfg(args, cluster_name, num_nodes, non_interactive, config, cfg_field_specs):
    values = {}
    for attr, section, key, label, fallback in cfg_field_specs:
        values[(section, key)] = _resolve_field(getattr(args, attr), fallback, label, non_interactive)

    values[("Cluster", "name")] = cluster_name
    values[("Cluster", "nt4_compatibility")] = "Off"
    values[("Cluster", "join_mode")] = "auto"
    values[("Cluster", "override_serialno_check")] = "True"

    cc = config["cluster_cfg"]
    int_default = ",".join(ip_list_for_count(cc["internal_ips"], num_nodes))
    ext_default = ",".join(ip_list_for_count(cc["external_ips"], num_nodes))

    values[("Internal-A", "ip_address_ranges")] = _resolve_field(
        args.cfg_internal_ips, int_default, "Internal IP addresses (comma-separated)", non_interactive)
    values[("External-1", "ip_address_ranges")] = _resolve_field(
        args.cfg_external_ips, ext_default, "External IP addresses (comma-separated)", non_interactive)

    values[("Cluster", "datetime")] = time.strftime("%Y/%m/%d %H:%M:%S")
    values[("Cluster", "nodes")] = "SERIAL"  # substituted with the captured serial at push time

    return values


def render_cluster_cfg(values):
    lines = ["[Cluster]"]
    for key in ("name", "nt4_compatibility", "password", "admin_user_password", "encoding",
                "join_mode", "datetime", "nodes", "internal_interface_as_external",
                "internal_mtu", "dns_domains", "dns_servers", "override_serialno_check"):
        lines.append(f"{key} = {values[('Cluster', key)]}")
    lines.append("[Timezone]")
    lines.append(f"name = {values[('Timezone', 'name')]}")
    lines.append(f"path = {values[('Timezone', 'path')]}")
    lines.append("[Internal-A]")
    lines.append(f"netmask = {values[('Internal-A', 'netmask')]}")
    lines.append(f"ip_address_ranges = {values[('Internal-A', 'ip_address_ranges')]}")
    lines.append("[External-1]")
    lines.append(f"gateway = {values[('External-1', 'gateway')]}")
    lines.append(f"netmask = {values[('External-1', 'netmask')]}")
    lines.append(f"ip_address_ranges = {values[('External-1', 'ip_address_ranges')]}")
    lines.append(f"mtu = {values[('External-1', 'mtu')]}")
    return "\n".join(lines) + "\n"


def write_cluster_cfg(text):
    path = os.path.join(os.getcwd(), "cluster.cfg")
    with open(path, "w") as f:
        f.write(text)
    print(f"Wrote auto-cluster config: {path}")
    return path


class _Tee:
    """Write to multiple file-like streams at once (used as a pexpect
    logfile so the raw console session is both shown live and saved)."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)
            s.flush()

    def flush(self):
        for s in self.streams:
            s.flush()


def expect_with_nudge(child, pattern, overall_timeout=900, nudge_interval=5):
    """expect() that periodically sends a bare carriage return while
    waiting. A fresh telnet connection only sees output written *after*
    it connects -- if the node already printed this prompt and is now
    idle, there's no new data ever coming, so a plain expect() would
    hang forever. A bare CR makes most prompt loops redraw the current
    prompt, which gives expect() something to match against."""
    import pexpect
    deadline = time.time() + overall_timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise pexpect.exceptions.TIMEOUT(f"Timed out waiting for: {pattern}")
        try:
            child.expect(pattern, timeout=min(nudge_interval, remaining))
            return
        except pexpect.exceptions.TIMEOUT:
            child.send("\r")


def run_acs_automation(serial_port, cfg_path, cluster_name, timeouts):
    """Push cluster.cfg to node1 via its serial console wizard and reboot
    it, mirroring buildcluster.exp."""
    try:
        import pexpect
    except ImportError:
        print("Error: --mode auto requires the 'pexpect' module (pip install pexpect)")
        sys.exit(1)

    log_path = os.path.join(os.getcwd(), f"{cluster_name}-acs-console.log")
    print(f"Connecting to node1 console on port {serial_port} to push cluster.cfg...")
    print(f"Raw console session is being logged to {log_path}")

    logfile = open(log_path, "w")
    # Default timeout covers the long wait for OneFS to boot to the wizard;
    # later expects use shorter explicit timeouts since they should follow
    # in quick succession once the wizard has started.
    child = pexpect.spawn(f"telnet 127.0.0.1 {serial_port}", timeout=timeouts["wizard_prompt_seconds"], encoding="utf-8")
    child.logfile = _Tee(logfile, sys.stdout)

    try:
        print(f"Waiting (up to {timeouts['wizard_prompt_seconds'] // 60}m) for the first-boot wizard yes/no prompt...")
        expect_with_nudge(child, r"\(yes or no\)> ", overall_timeout=timeouts["wizard_prompt_seconds"],
                           nudge_interval=timeouts["wizard_nudge_interval_seconds"])
        child.send("yes\r")

        # Require the trailing line terminator so pexpect can't declare a
        # match on a partial read (e.g. "...: SV2" before the rest of the
        # serial has arrived) -- \S+ alone has no required terminator and
        # will match prematurely on whatever has landed in the buffer so far.
        # Disk formatting (shown as a per-bay progress display) can take
        # several minutes before the serial number line is printed.
        print(f"Waiting (up to {timeouts['disk_format_seconds'] // 60}m) for disk formatting to finish...")
        child.expect(r"Node serial number:\s*([^\r\n]+)\r?\n", timeout=timeouts["disk_format_seconds"])
        serial = child.match.group(1).strip()
        print(f"Captured node serial: {serial}")

        child.expect("Wizard >>>", timeout=60)
        child.send("\x03")
        child.send("\x03")
        child.expect("/bin/sh:", timeout=30)
        child.send("/bin/sh\r")

        child.expect("# ", timeout=30)
        child.send("cat > /cluster.cfg\r")

        with open(cfg_path) as f:
            data = f.read().replace("SERIAL", serial)
        for line in data.splitlines():
            child.send(line + "\r")
            time.sleep(0.1)

        child.send("\x04")
        child.expect("# ", timeout=30)
        child.send("reboot\r")
        child.expect("Rebooting...", timeout=60)
        print("cluster.cfg pushed; node1 is rebooting.")
    except pexpect.exceptions.ExceptionPexpect as e:
        print(f"Error: ACS console automation failed: {type(e).__name__}")
        print(f"See the full raw session transcript at {log_path}")
        sys.exit(1)
    finally:
        child.close()
        logfile.close()


def scrape_node_serial(serial_port, cluster_name, node_index, timeouts):
    """Answer the disk-format yes/no prompt on a joining node and capture
    its serial number, then disconnect. join_mode=auto on node1 means a
    joining node needs no cluster.cfg of its own -- it just needs to get
    past the format prompt so it can find and join the existing cluster."""
    try:
        import pexpect
    except ImportError:
        print("Error: --mode auto requires the 'pexpect' module (pip install pexpect)")
        sys.exit(1)

    log_path = os.path.join(os.getcwd(), f"{cluster_name}-node{node_index}-console.log")
    print(f"Connecting to node{node_index} console on port {serial_port} to capture its serial number...")
    print(f"Raw console session is being logged to {log_path}")

    logfile = open(log_path, "w")
    child = pexpect.spawn(f"telnet 127.0.0.1 {serial_port}", timeout=timeouts["wizard_prompt_seconds"], encoding="utf-8")
    child.logfile = _Tee(logfile, sys.stdout)

    try:
        print(f"Waiting (up to {timeouts['wizard_prompt_seconds'] // 60}m) for node{node_index}'s first-boot wizard yes/no prompt...")
        expect_with_nudge(child, r"\(yes or no\)> ", overall_timeout=timeouts["wizard_prompt_seconds"],
                           nudge_interval=timeouts["wizard_nudge_interval_seconds"])
        child.send("yes\r")

        print(f"Waiting (up to {timeouts['disk_format_seconds'] // 60}m) for node{node_index}'s disk formatting to finish...")
        child.expect(r"Node serial number:\s*([^\r\n]+)\r?\n", timeout=timeouts["disk_format_seconds"])
        serial = child.match.group(1).strip()
        print(f"Captured node{node_index} serial: {serial}")
        return serial
    except pexpect.exceptions.ExceptionPexpect as e:
        print(f"Error: failed to capture node{node_index}'s serial number: {type(e).__name__}")
        print(f"See the full raw session transcript at {log_path}")
        sys.exit(1)
    finally:
        child.close()
        logfile.close()


def wait_for_ping(ip, timeout_seconds=600, interval=10):
    print(f"Waiting for {ip} to respond to ping (timeout {timeout_seconds // 60}m)...")
    start = time.time()
    while time.time() - start < timeout_seconds:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", "2", ip],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            return True
        time.sleep(interval)
    return False


def wait_for_webui(ip, port=8080, timeout_seconds=600, interval=10):
    """Poll https://ip:port/ directly (no redirect-following) until it
    returns HTTP 200 with an html body -- not a redirect, not an error."""
    url = f"https://{ip}:{port}/"
    print(f"Waiting for the web UI to respond at {url} (timeout {timeout_seconds // 60}m)...")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    start = time.time()
    while time.time() - start < timeout_seconds:
        try:
            conn = http.client.HTTPSConnection(ip, port, timeout=5, context=ctx)
            try:
                conn.request("GET", "/")
                resp = conn.getresponse()
                raw = resp.read()
                if resp.getheader("Content-Encoding", "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                body = raw.decode("utf-8", errors="replace").lstrip().lower()
                if resp.status == 200 and ("<!doctype html" in body or "<html" in body):
                    return True
            finally:
                conn.close()
        except OSError:
            pass
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# PAPI (OneFS platform API): add-node
# ---------------------------------------------------------------------------

def papi_login(ip, port, user, password, timeout_seconds=600):
    """The web UI can come up before the PAPI session API is ready
    ("Session API is unavailable"), so retry with backoff -- fast at
    first to pick the API up as soon as it's ready, slower later -- and
    only give up once timeout_seconds has elapsed."""
    try:
        import requests
        import urllib3
    except ImportError:
        print("Error: --mode auto requires the 'requests' module (pip install requests)")
        sys.exit(1)
    import json as _json

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    uri = f"https://{ip}:{port}"
    headers = {"Content-Type": "application/json"}
    data = _json.dumps({"username": user, "password": password, "services": ["platform"]})

    print(f"Waiting for the PAPI session API at {uri} (timeout {timeout_seconds // 60}m)...")
    start = time.time()
    delay = 2
    last_error = None
    while time.time() - start < timeout_seconds:
        try:
            session = requests.Session()
            response = session.post(uri + "/session/1/session", data=data, headers=headers,
                                     verify=False, timeout=10)
            if 200 <= response.status_code < 299:
                session.headers["referer"] = uri
                session.headers["X-CSRF-Token"] = session.cookies.get("isicsrf")
                return session, uri + "/platform"
            last_error = f"HTTP {response.status_code}: {response.content}"
        except requests.exceptions.RequestException as e:
            last_error = str(e)
        time.sleep(delay)
        delay = min(delay * 1.5, 30)

    print(f"Error: PAPI authorization failed against {uri} after {timeout_seconds // 60} minutes:")
    print(last_error)
    sys.exit(1)


def papi_add_node(session, papi, serial_number):
    import json as _json

    data = _json.dumps({"serial_number": serial_number})
    response = session.post(papi + "/16/cluster/add-node", data=data,
                             headers={"Content-Type": "application/json"}, verify=False)
    if not (200 <= response.status_code < 299):
        print(f"Error: add-node failed for serial {serial_number} (HTTP {response.status_code}):")
        print(response.content)
        sys.exit(1)
    return response


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def str_to_bool(value):
    if value.lower() in ("true", "1", "yes"):
        return True
    if value.lower() in ("false", "0", "no"):
        return False
    raise argparse.ArgumentTypeError(f"Expected true or false, got: {value}")


def parse_args(config, cfg_field_specs):
    bd = config["build_defaults"]
    paths = config["paths"]

    p = argparse.ArgumentParser(
        add_help=False,
        description="Build/destroy/list OneFS-on-KVM proto-storage clusters.")
    p.add_argument("--build", action="store_true")
    p.add_argument("--destroy", action="store_true")
    p.add_argument("--list", action="store_true")
    p.add_argument("--help", "-h", action="store_true")
    p.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                    help="Path to the YAML config file (default: alongside this script)")
    p.add_argument("--cluster")
    p.add_argument("--version", default=bd["version"])
    p.add_argument("--nodes", type=int, default=None)
    p.add_argument("--topdir", default=paths["topdir"])
    p.add_argument("--boot-dir", default=paths["boot_dir"])
    p.add_argument("--boot-image")
    p.add_argument("--storage-size", default=bd["storage_size"])
    p.add_argument("--serial-port", type=int)
    p.add_argument("--autostart", action="store_true", default=bd["autostart"])
    p.add_argument("--connect", dest="connect", type=str_to_bool, default=bd["connect"],
                    metavar="true|false",
                    help="Connect to node1's serial console after the build completes. "
                         "Defaults to true for single-node clusters, false for multi-node clusters.")
    p.add_argument("--force", action="store_true")
    p.add_argument("--mode", choices=["auto", "manual"], default=bd["mode"])
    p.add_argument("--cfg-internal-ips", dest="cfg_internal_ips", default=None)
    p.add_argument("--cfg-external-ips", dest="cfg_external_ips", default=None)
    for attr, _section, _key, _label, _fallback in cfg_field_specs:
        p.add_argument("--" + attr.replace("_", "-"), dest=attr, default=None)
    return p.parse_args()


def usage():
    print(f"""Usage: build-kvm-pscale-cluster-v3.21.py COMMAND [OPTIONS]

COMMANDS:
  --build              Build a new cluster
  --destroy            Tear down an existing cluster
  --list               List all defined clusters
  --help               Show this help message

GLOBAL OPTIONS:
  --config PATH        Path to the YAML config file (default: {DEFAULT_CONFIG_PATH})
                        All defaults below come from this file; CLI flags override it
                        for a single run. If the file doesn't exist, it is created with
                        generic defaults and the script exits so you can review it.

BUILD OPTIONS:
  --cluster NAME       Cluster name (required)
  --version VERSION    OneFS version (e.g., 9.13.0.0r) (default from config, prompted if unset)
  --nodes NUM          Number of nodes (default from config, must be 1 or 3+)
  --topdir DIR         Cluster directory (default from config)
  --boot-dir DIR       Boot images directory (default from config)
  --boot-image PATH    Direct path to a boot drive qcow2 (overrides --boot-dir
                        and --version; version is inferred from the filename
                        if not given)
  --storage-size SIZE  Storage drive size (default from config)
  --serial-port PORT   Starting serial port (auto-detected if not provided)
  --autostart          Start VMs after creation
  --connect true|false Connect to node1's serial console when the build finishes,
                        instead of exiting. Defaults to true for single-node
                        clusters, false for multi-node clusters.
  --mode auto|manual   manual (default): build VMs only, configure via
                        serial console yourself.
                        auto: also generate cluster.cfg (prompting for any
                        fields not given via --cfg-* flags, defaulting to
                        the config file's cluster_cfg values), push it to
                        node1's console wizard, reboot it, then
                        wait up to 10 minutes for the first external IP to
                        respond to ping. Implies --autostart.
  --cfg-password, --cfg-admin-password, --cfg-encoding,
  --cfg-internal-as-external, --cfg-internal-mtu, --cfg-dns-domains,
  --cfg-dns-servers, --cfg-tz-name, --cfg-tz-path, --cfg-internal-netmask,
  --cfg-internal-ips, --cfg-external-gateway, --cfg-external-netmask,
  --cfg-external-ips, --cfg-external-mtu
                        Override individual cluster.cfg fields (--mode auto
                        only); unset ones are prompted for interactively.
                        cluster.cfg's "name" always matches --cluster;
                        nt4_compatibility is always Off, join_mode is
                        always auto, and override_serialno_check is always
                        True (not configurable).

DESTROY OPTIONS:
  --cluster NAME       Cluster name (required)
  --force              Skip confirmation prompt

NOTE: 2-node clusters are not supported (quorum requires 1 or 3+).""")


def main():
    # --config must be known before we can compute argparse defaults, so
    # scan for it manually ahead of the real parse.
    config_path = DEFAULT_CONFIG_PATH
    if "--config" in sys.argv:
        idx = sys.argv.index("--config")
        if idx + 1 < len(sys.argv):
            config_path = sys.argv[idx + 1]

    config = load_config(config_path)
    cfg_field_specs = build_cfg_field_specs(config)

    args = parse_args(config, cfg_field_specs)

    if args.help:
        usage()
        sys.exit(0)

    if not (args.build or args.destroy or args.list):
        print("Error: No command specified. Use --build, --destroy, or --list.\n")
        usage()
        sys.exit(1)

    if args.list:
        list_clusters()
        sys.exit(0)

    cluster_name = args.cluster or prompt_input("Enter cluster name")

    if args.destroy:
        cluster_dir = os.path.join(args.topdir, cluster_name)
        if not os.path.isdir(cluster_dir):
            print(f"Error: Cluster directory not found: {cluster_dir}")
            sys.exit(1)
        if not os.access(cluster_dir, os.W_OK):
            print(f"Error: No write permission: {cluster_dir}")
            sys.exit(1)
        parent = os.path.dirname(cluster_dir) or "."
        if not os.access(parent, os.W_OK):
            print(f"Error: No write permission: {parent}")
            sys.exit(1)

        version = "unknown"
        for name, _ in list_all_vms():
            m = re.match(rf"{re.escape(cluster_name)}-OneFS-(.*)-Node", name)
            if m:
                version = m.group(1)
                break

        if not args.force:
            confirm = input(f"Destroy cluster '{cluster_name}'? (yes/no): ").strip()
            if confirm != "yes":
                print("Cancelled")
                sys.exit(0)
        destroy_cluster(cluster_dir, cluster_name, version)
        sys.exit(0)

    # --build
    if args.boot_image:
        boot_drive = args.boot_image
        if not os.path.isfile(boot_drive):
            print(f"Error: Boot drive not found: {boot_drive}")
            sys.exit(1)
        version = args.version
        if not version:
            stem = os.path.basename(boot_drive)
            if stem.endswith(".qcow2"):
                stem = stem[:-len(".qcow2")]
            version = stem[1:] if stem.startswith("v") else stem
    else:
        version = args.version
        if not version:
            boot_dir = args.boot_dir
            print("Available boot drives:")
            if os.path.isdir(boot_dir):
                versions = sorted(
                    f[1:-6] for f in os.listdir(boot_dir)
                    if f.startswith("v") and f.endswith(".qcow2"))
                for v in versions:
                    print(v)
            version = prompt_input("Enter OneFS version (e.g., 9.14.0.0r)", config["build_defaults"]["version"])

        boot_drive = os.path.join(args.boot_dir, f"v{version}.qcow2")
        if not os.path.isfile(boot_drive):
            print(f"Error: Boot drive not found: {boot_drive}")
            sys.exit(1)

    num_nodes = args.nodes or int(prompt_input("Enter number of nodes", str(config["build_defaults"]["num_nodes"])))
    if num_nodes < 1:
        print("Error: Number of nodes must be a positive integer")
        sys.exit(1)
    if num_nodes == 2:
        print("Error: 2-node clusters are not supported (quorum requires 1 or 3+)")
        sys.exit(1)

    if args.mode == "auto":
        args.autostart = True

    cluster_dir = os.path.join(args.topdir, cluster_name)
    if not os.path.isabs(cluster_dir):
        cluster_dir = os.path.join(os.path.abspath(args.topdir), cluster_name)

    existing_vms = sum(
        1 for name, _ in list_all_vms()
        if re.search(rf"{re.escape(cluster_name)}-OneFS-.*-Node1$", name))
    existing_network = f"{cluster_name}-internal" in (virsh("net-list --all", check=False).stdout or "")

    if existing_vms > 0 or existing_network or os.path.isdir(cluster_dir):
        print(f"Error: Cluster '{cluster_name}' already exists")
        sys.exit(1)

    print(f"Building cluster '{cluster_name}' (v{version}, {num_nodes} nodes) at {cluster_dir}")

    timeouts = config["timeouts"]
    vm_cfg = config["vm"]
    network_cfg = config["network"]

    cluster_cfg_path = None
    first_ext_ip = None
    if args.mode == "auto":
        cfg_values = gather_cluster_cfg(
            args, cluster_name, num_nodes,
            non_interactive=not cfg_args_given(args, cfg_field_specs),
            config=config, cfg_field_specs=cfg_field_specs)
        first_ext_ip = cfg_values[("External-1", "ip_address_ranges")].split(",")[0].strip()
        cluster_cfg_path = write_cluster_cfg(render_cluster_cfg(cfg_values))

    os.makedirs(cluster_dir)
    create_network_xml(cluster_name, cluster_dir)

    serial_port_start = args.serial_port
    if serial_port_start is None:
        serial_port_start = find_available_serial_port(num_nodes, network_cfg)
    else:
        for port in range(serial_port_start, serial_port_start + num_nodes):
            if port_in_use(port):
                print(f"Error: Serial port {port} is already in use")
                sys.exit(1)

    print(f"Serial ports: {serial_port_start}-{serial_port_start + num_nodes - 1}")

    serial_port = serial_port_start
    for i in range(1, num_nodes + 1):
        node_dir = os.path.join(cluster_dir, f"Node{i}")
        os.makedirs(node_dir)

        node_boot_drive = os.path.join(node_dir, "boot0.qcow2")
        print(f"  Node{i}: copying boot drive...")
        shutil.copy2(boot_drive, node_boot_drive)

        mac_internal = random_mac()
        mac_external = random_mac()

        run(f"qemu-img create -f qcow2 {node_dir}/isi-nvram.qcow2 1G")
        for j in range(vm_cfg["storage_drive_count"]):
            run(f"qemu-img create -f qcow2 {node_dir}/storageHDD{j}.qcow2 {args.storage_size}")

        node_name = f"{cluster_name}-OneFS-{version}-Node{i}"

        create_node_xml(node_name, node_dir, node_boot_drive, serial_port,
                         mac_internal, mac_external, cluster_name, vm_cfg,
                         network_cfg["external_network_name"])
        print(f"  Node{i}: {node_name} telnet-port={serial_port}")

        serial_port += 1

    if existing_network:
        run(f"virsh -c qemu:///system net-destroy {cluster_name}-internal", check=False)
        run(f"virsh -c qemu:///system net-undefine {cluster_name}-internal", check=False)
        time.sleep(1)

    run(f"virsh -c qemu:///system net-define {cluster_dir}/internal-net.xml")
    run(f"virsh -c qemu:///system net-start {cluster_name}-internal")
    run(f"virsh -c qemu:///system net-autostart {cluster_name}-internal")

    for i in range(1, num_nodes + 1):
        run(f"virsh -c qemu:///system define {cluster_dir}/Node{i}/node.xml")

    if args.autostart:
        for i in range(1, num_nodes + 1):
            run(f"virsh -c qemu:///system start {cluster_name}-OneFS-{version}-Node{i}")

    if args.mode == "auto":
        run_acs_automation(serial_port_start, cluster_cfg_path, cluster_name, timeouts)
        if not wait_for_ping(first_ext_ip, timeout_seconds=timeouts["ping_seconds"], interval=timeouts["ping_interval_seconds"]):
            print(f"Cluster build FAILED: {first_ext_ip} did not respond to ping after {timeouts['ping_seconds'] // 60} minutes.")
            print(f"Check the serial console: telnet 127.0.0.1 {serial_port_start}")
            sys.exit(1)

        webui_port = timeouts["webui_port"]
        if not wait_for_webui(first_ext_ip, port=webui_port, timeout_seconds=timeouts["webui_seconds"], interval=timeouts["webui_interval_seconds"]):
            print(f"Cluster build FAILED: {first_ext_ip} is pingable but the web UI "
                  f"never came up at https://{first_ext_ip}:{webui_port}/ after {timeouts['webui_seconds'] // 60} minutes.")
            print(f"Check the serial console: telnet 127.0.0.1 {serial_port_start}")
            sys.exit(1)

        print(f"Node1 is up: https://{first_ext_ip}:{webui_port}/ is responding.")

        joined_serials = {}
        if num_nodes > 1:
            ext_ips = [ip.strip() for ip in cfg_values[("External-1", "ip_address_ranges")].split(",")]
            root_password = cfg_values[("Cluster", "password")]

            # Scrape every joining node's serial first -- this naturally
            # gives node1 time to settle before we touch its PAPI.
            for i in range(2, num_nodes + 1):
                port = serial_port_start + (i - 1)
                joined_serials[i] = scrape_node_serial(port, cluster_name, i, timeouts)

            session, papi = papi_login(first_ext_ip, webui_port, "root", root_password,
                                        timeout_seconds=timeouts["papi_login_seconds"])

            for i in range(2, num_nodes + 1):
                serial = joined_serials[i]
                print(f"Adding node{i} (serial {serial}) to the cluster via PAPI...")
                papi_add_node(session, papi, serial)

                node_ip = ext_ips[i - 1]
                if not wait_for_ping(node_ip, timeout_seconds=timeouts["ping_seconds"], interval=timeouts["ping_interval_seconds"]):
                    print(f"Cluster build FAILED: {node_ip} (node{i}) did not respond to ping after {timeouts['ping_seconds'] // 60} minutes.")
                    sys.exit(1)
                if not wait_for_webui(node_ip, port=webui_port, timeout_seconds=timeouts["webui_seconds"], interval=timeouts["webui_interval_seconds"]):
                    print(f"Cluster build FAILED: {node_ip} (node{i}) web UI never came up after {timeouts['webui_seconds'] // 60} minutes.")
                    sys.exit(1)
                print(f"Node{i} is up: https://{node_ip}:{webui_port}/ is responding.")

        print(f"Cluster build succeeded: https://{first_ext_ip}:{webui_port}/ is up.")
        for i in sorted(joined_serials):
            print(f"  Node{i} serial: {joined_serials[i]}")

    do_connect = args.connect
    if do_connect is None:
        do_connect = num_nodes == 1

    if do_connect:
        print(f"Connecting to node1 serial console: telnet 127.0.0.1 {serial_port_start}")
        os.execvp("telnet", ["telnet", "127.0.0.1", str(serial_port_start)])
    else:
        print(f"Done. Connect: telnet 127.0.0.1 {serial_port_start}")


if __name__ == "__main__":
    main()
