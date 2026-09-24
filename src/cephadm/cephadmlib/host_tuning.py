# host_tuning.py - advisory host tuning and readiness checks
#
# Copyright (C) 2026 Clyso Technologies Inc.
#
# This is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License version 2.1, as published by the Free Software
# Foundation.  See file COPYING.

"""Advisory checks of host tuning that matters for Ceph.

Unlike check-host, none of these checks is required for cephadm to manage a
host. Each check produces one or more results with a status of ok, warn, fail
or info so the caller can decide how strict to be.
"""

import ipaddress
import logging
import os
import re

from glob import glob
from typing import Any, Dict, List, Optional, Set

from .call_wrappers import call, CallVerbosity
from .context import CephadmContext
from .exe_utils import find_executable
from .file_utils import read_file
from . import host_hw

logger = logging.getLogger()

STATUS_OK = 'ok'
STATUS_INFO = 'info'
STATUS_WARN = 'warn'
STATUS_FAIL = 'fail'
STATUS_ORDER = [STATUS_OK, STATUS_INFO, STATUS_WARN, STATUS_FAIL]

GiB = 1024**3

# How a sysctl outside its recommended range is reported:
# SYSCTL_WARN     the default is known to hurt Ceph
# SYSCTL_ADVICE   worth raising on busy or fast hosts, but the distribution
#                 default is fine for many clusters
# SYSCTL_MANAGED  cephadm applies it itself when deploying an OSD
SYSCTL_WARN = 'warn'
SYSCTL_ADVICE = 'advice'
SYSCTL_MANAGED = 'managed'

# (sysctl, operator, value, field, level, reason)
# field selects a whitespace separated field of a multi-valued sysctl such
# as net.ipv4.tcp_rmem.
SYSCTL_RECOMMENDATIONS = [
    (
        'fs.aio-max-nr',
        '>=',
        1048576,
        None,
        SYSCTL_MANAGED,
        'BlueStore uses many concurrent aio contexts',
    ),
    (
        'kernel.pid_max',
        '>=',
        4194304,
        None,
        SYSCTL_MANAGED,
        'OSDs run many threads',
    ),
    (
        'fs.file-max',
        '>=',
        6553600,
        None,
        SYSCTL_WARN,
        'daemons hold many sockets and files open',
    ),
    (
        'vm.swappiness',
        '<=',
        10,
        None,
        SYSCTL_WARN,
        'swapping daemon memory causes latency spikes and heartbeat failures',
    ),
    (
        'net.core.somaxconn',
        '>=',
        1024,
        None,
        SYSCTL_WARN,
        'large clusters open many connections at once',
    ),
    (
        'net.core.netdev_max_backlog',
        '>=',
        10000,
        None,
        SYSCTL_ADVICE,
        'NICs of 25 Gb/s and above can drop packets with a small backlog',
    ),
    (
        'net.ipv4.tcp_rmem',
        '>=',
        16 * 1024 * 1024,
        2,
        SYSCTL_ADVICE,
        'TCP receive autotuning is capped by the max value',
    ),
    (
        'net.ipv4.tcp_wmem',
        '>=',
        16 * 1024 * 1024,
        2,
        SYSCTL_ADVICE,
        'TCP send autotuning is capped by the max value',
    ),
]

# tuned profiles that trade throughput or latency for power savings
TUNED_BAD_PROFILES = {
    'balanced',
    'powersave',
    'desktop',
    'laptop-battery-powersave',
}
TUNED_GOOD_PROFILES = {
    'throughput-performance',
    'latency-performance',
    'network-throughput',
    'network-latency',
    'hpc-compute',
}

# block devices that are never OSD candidates
EXCLUDED_BLOCK_DEVICES = (
    'sr',
    'zram',
    'dm-',
    'loop',
    'md',
    'nbd',
    'rbd',
    'ram',
    'fd',
)

# interfaces created by container engines, libvirt etc.
VIRTUAL_IFACE_PREFIXES = (
    'lo',
    'veth',
    'docker',
    'podman',
    'cni',
    'virbr',
    'vnet',
    'tap',
    'tun',
    'flannel',
    'cali',
    'kube',
)

# per-daemon resource heuristics used for the sizing checks
MEM_PER_OSD = 4 * GiB  # osd_memory_target default
MEM_BASE = 8 * GiB  # OS, mon/mgr/other colocated daemons
# MemTotal is below the installed memory by what the kernel and firmware
# reserve, so a host with exactly the recommended DIMMs must still pass
MEM_TOLERANCE = 0.95
THREADS_PER_HDD_OSD = 1
THREADS_PER_FLASH_OSD = 2
# ignore tiny devices when counting OSD candidates
MIN_OSD_DEVICE_BYTES = 5 * GiB

SYS_BLOCK = '/sys/block'
SYS_NET = '/sys/class/net'
SYS_CPU = '/sys/devices/system/cpu'
SYS_IOMMU = '/sys/class/iommu'
SYS_NODE = '/sys/devices/system/node'
# idle states that take longer than this to exit add latency to every I/O
CSTATE_MAX_LATENCY_US = 10
THP_PATH = '/sys/kernel/mm/transparent_hugepage/enabled'


def _result(
    check: str,
    status: str,
    message: str,
    target: str = '',
    value: Any = None,
    expected: Any = None,
) -> Dict[str, Any]:
    r: Dict[str, Any] = {
        'check': check,
        'status': status,
        'target': target,
        'message': message,
    }
    if value is not None:
        r['value'] = value
    if expected is not None:
        r['expected'] = expected
    return r


def _read(path: str) -> Optional[str]:
    v = read_file([path])
    if v == 'Unknown' and not os.path.exists(path):
        return None
    return v


def _read_int(path: str) -> Optional[int]:
    v = _read(path)
    if v is None:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _bracketed(value: str) -> str:
    """Return the selected item of a sysfs list like 'a [b] c'."""
    m = re.search(r'\[([^\]]+)\]', value)
    return m.group(1) if m else value.strip()


##################################
# block device helpers


def _kname(path: str) -> str:
    """Resolve a /dev path (or symlink) to its kernel device name."""
    return os.path.basename(os.path.realpath(path))


def list_block_devices() -> List[str]:
    if not os.path.isdir(SYS_BLOCK):
        return []
    return sorted(
        dev
        for dev in os.listdir(SYS_BLOCK)
        if not dev.startswith(EXCLUDED_BLOCK_DEVICES)
        and os.path.exists(os.path.join(SYS_BLOCK, dev, 'dev'))
    )


def _partitions(dev: str) -> List[str]:
    return sorted(
        os.path.basename(os.path.dirname(p))
        for p in glob(os.path.join(SYS_BLOCK, dev, dev + '*', 'partition'))
    )


def _holders(kname: str) -> List[str]:
    """Return every device stacked on top of kname (LVM, dm-crypt, md...)."""
    # partitions live below the disk, other devices directly in /sys/block
    paths = glob(os.path.join(SYS_BLOCK, kname, 'holders', '*'))
    paths += glob(os.path.join(SYS_BLOCK, '*', kname, 'holders', '*'))
    out: List[str] = []
    for p in paths:
        h = os.path.basename(p)
        if h not in out:
            out.append(h)
            out.extend(x for x in _holders(h) if x not in out)
    return out


def _device_tree(dev: str) -> Set[str]:
    """dev, its partitions and everything stacked on any of them."""
    names = {dev}
    for part in _partitions(dev):
        names.add(part)
    for n in list(names):
        names.update(_holders(n))
    return names


def _mounted_knames() -> Dict[str, str]:
    """Map kernel device name -> mountpoint (or '[swap]')."""
    out: Dict[str, str] = {}
    mounts = read_file(['/proc/mounts'])
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) < 2 or not fields[0].startswith('/dev/'):
            continue
        out.setdefault(_kname(fields[0]), fields[1])
    swaps = read_file(['/proc/swaps'])
    for line in swaps.splitlines()[1:]:
        fields = line.split()
        if fields and fields[0].startswith('/dev/'):
            out.setdefault(_kname(fields[0]), '[swap]')
    return out


def device_usage(
    dev: str, mounted: Optional[Dict[str, str]] = None
) -> List[str]:
    """Return the reasons a block device is in use (empty if unused)."""
    if mounted is None:
        mounted = _mounted_knames()
    reasons = []
    tree = _device_tree(dev)
    for n in sorted(tree):
        if n in mounted:
            reasons.append('%s mounted at %s' % (n, mounted[n]))
    parts = _partitions(dev)
    if parts:
        reasons.append('has partitions: %s' % ','.join(parts))
    holders = sorted(tree - {dev} - set(parts))
    if holders:
        reasons.append('has holders: %s' % ','.join(holders))
    if _read_int(os.path.join(SYS_BLOCK, dev, 'ro')) == 1:
        reasons.append('is read-only')
    return reasons


def os_devices(mounted: Optional[Dict[str, str]] = None) -> Set[str]:
    """Disks backing /, /boot, /var etc. or swap."""
    if mounted is None:
        mounted = _mounted_knames()
    system_mounts = ('/', '/boot', '/boot/efi', '/usr', '/var', '[swap]')
    out = set()
    for dev in list_block_devices():
        for n in _device_tree(dev):
            if mounted.get(n) in system_mounts or (
                mounted.get(n, '').startswith('/var/lib')
            ):
                out.add(dev)
    return out


def device_info(dev: str) -> Dict[str, Any]:
    base = os.path.join(SYS_BLOCK, dev)
    sectors = _read_int(os.path.join(base, 'size')) or 0
    rotational = _read_int(os.path.join(base, 'queue', 'rotational'))
    return {
        'dev': dev,
        'path': '/dev/' + dev,
        'size_bytes': sectors * 512,
        'rotational': rotational == 1,
        'removable': _read_int(os.path.join(base, 'removable')) == 1,
        'model': (_read(os.path.join(base, 'device', 'model')) or '').strip(),
    }


def data_devices() -> List[Dict[str, Any]]:
    """Non-OS block devices that could host an OSD, used or not."""
    mounted = _mounted_knames()
    os_devs = os_devices(mounted)
    out = []
    for dev in list_block_devices():
        if dev in os_devs:
            continue
        info = device_info(dev)
        if info['removable'] or info['size_bytes'] < MIN_OSD_DEVICE_BYTES:
            continue
        info['in_use'] = device_usage(dev, mounted)
        info['mounted'] = any(n in mounted for n in _device_tree(dev))
        out.append(info)
    return out


##################################
# checks


def check_sysctls(ctx: CephadmContext) -> List[Dict[str, Any]]:
    results = []
    for name, op, want, field, level, why in SYSCTL_RECOMMENDATIONS:
        path = '/proc/sys/' + name.replace('.', '/')
        raw = _read(path)
        if raw is None:
            results.append(
                _result(
                    'sysctl', STATUS_INFO, 'not present', name, expected=want
                )
            )
            continue
        try:
            fields = raw.split()
            value = int(fields[field] if field is not None else fields[0])
        except (ValueError, IndexError):
            results.append(
                _result('sysctl', STATUS_INFO, 'unparsable', name, raw)
            )
            continue
        good = value >= want if op == '>=' else value <= want
        expected = '%s %d' % (op, want)
        if good:
            results.append(
                _result('sysctl', STATUS_OK, why, name, value, expected)
            )
        elif level == SYSCTL_MANAGED:
            results.append(
                _result(
                    'sysctl',
                    STATUS_INFO,
                    'cephadm sets this when deploying an OSD; ' + why,
                    name,
                    value,
                    expected,
                )
            )
        else:
            status = STATUS_WARN if level == SYSCTL_WARN else STATUS_INFO
            results.append(
                _result('sysctl', status, why, name, value, expected)
            )
    return results


def check_thp(ctx: CephadmContext) -> List[Dict[str, Any]]:
    raw = _read(THP_PATH)
    if raw is None:
        return [
            _result('thp', STATUS_INFO, 'transparent hugepages not available')
        ]
    mode = _bracketed(raw)
    if mode == 'always':
        return [
            _result(
                'thp',
                STATUS_WARN,
                'THP "always" inflates daemon RSS and causes allocation stalls',
                value=mode,
                expected='madvise or never',
            )
        ]
    return [_result('thp', STATUS_OK, 'transparent hugepages', value=mode)]


def check_tuned(ctx: CephadmContext) -> List[Dict[str, Any]]:
    if not find_executable('tuned-adm'):
        return [_result('tuned', STATUS_INFO, 'tuned is not installed')]
    out, err, code = call(
        ctx, ['tuned-adm', 'active'], verbosity=CallVerbosity.QUIET
    )
    m = re.search(r'Current active profile:\s*(\S+)', out)
    if code or not m:
        return [
            _result(
                'tuned',
                STATUS_WARN,
                'tuned is installed but no profile is active',
                expected=' or '.join(sorted(TUNED_GOOD_PROFILES)),
            )
        ]
    profile = m.group(1)
    if profile in TUNED_BAD_PROFILES:
        status = STATUS_WARN
        msg = 'profile favours power saving over performance'
    elif profile in TUNED_GOOD_PROFILES:
        status = STATUS_OK
        msg = 'tuned profile'
    else:
        status = STATUS_INFO
        msg = 'custom or unrecognised tuned profile'
    return [
        _result(
            'tuned',
            status,
            msg,
            value=profile,
            expected=' or '.join(sorted(TUNED_GOOD_PROFILES)),
        )
    ]


def check_cpu_governor(ctx: CephadmContext) -> List[Dict[str, Any]]:
    governors: Dict[str, List[str]] = {}
    for p in glob(
        os.path.join(SYS_CPU, 'cpu[0-9]*', 'cpufreq', 'scaling_governor')
    ):
        gov = _read(p)
        if gov:
            cpu = p.split(os.sep)[-3]
            governors.setdefault(gov, []).append(cpu)
    if not governors:
        return [
            _result(
                'cpu_governor',
                STATUS_INFO,
                'no cpufreq scaling (virtual machine or firmware controlled)',
            )
        ]
    names = sorted(governors)
    if names == ['performance']:
        return [
            _result(
                'cpu_governor',
                STATUS_OK,
                'CPU frequency governor',
                value='performance',
            )
        ]
    return [
        _result(
            'cpu_governor',
            STATUS_WARN,
            'power saving governors add latency to every I/O',
            value=','.join(
                '%s(%d cpus)' % (g, len(governors[g])) for g in names
            ),
            expected='performance',
        )
    ]


def check_swap(ctx: CephadmContext) -> List[Dict[str, Any]]:
    lines = read_file(['/proc/swaps']).splitlines()[1:]
    swaps = [line.split()[0] for line in lines if line.strip()]
    # zram swaps to compressed memory, not to disk
    disk_swaps = [
        s for s in swaps if not os.path.basename(s).startswith('zram')
    ]
    if disk_swaps:
        return [
            _result(
                'swap',
                STATUS_INFO,
                'swap is enabled; keep vm.swappiness low',
                value=','.join(disk_swaps),
            )
        ]
    if swaps:
        return [
            _result(
                'swap',
                STATUS_OK,
                'only compressed memory (zram) swap',
                value=','.join(swaps),
            )
        ]
    return [_result('swap', STATUS_OK, 'no swap configured')]


def check_block_devices(ctx: CephadmContext) -> List[Dict[str, Any]]:
    results = []
    devs = data_devices()
    if not devs:
        results.append(
            _result('disks', STATUS_INFO, 'no non-OS data devices found')
        )
    for d in devs:
        dev = d['dev']
        kind = 'hdd' if d['rotational'] else 'flash'
        sched_raw = _read(os.path.join(SYS_BLOCK, dev, 'queue', 'scheduler'))
        if sched_raw is None:
            continue
        sched = _bracketed(sched_raw)
        if d['rotational']:
            good = {'mq-deadline', 'deadline'}
            bad = {'bfq', 'cfq'}
            expected = 'mq-deadline'
        else:
            good = {'none', 'noop'}
            bad = {'bfq', 'cfq'}
            expected = 'none'
        if sched in good:
            status = STATUS_OK
        elif sched in bad:
            status = STATUS_WARN
        else:
            status = STATUS_INFO
        results.append(
            _result(
                'io_scheduler',
                status,
                '%s %s' % (kind, d['model']),
                dev,
                sched,
                expected,
            )
        )
        if d['rotational']:
            wc = _read(os.path.join(SYS_BLOCK, dev, 'queue', 'write_cache'))
            if wc == 'write back':
                results.append(
                    _result(
                        'write_cache',
                        STATUS_INFO,
                        'volatile write cache is enabled; disabling it '
                        'lowers commit latency on many HDD models',
                        dev,
                        wc,
                    )
                )
        if d['in_use']:
            results.append(
                _result(
                    'disk_available',
                    STATUS_INFO,
                    '; '.join(d['in_use']),
                    dev,
                )
            )
    return results


def _is_virtual_iface(iface: str) -> bool:
    return iface.startswith(VIRTUAL_IFACE_PREFIXES)


def _iface_info(iface: str) -> Dict[str, Any]:
    base = os.path.join(SYS_NET, iface)
    return {
        'mtu': _read_int(os.path.join(base, 'mtu')),
        'operstate': _read(os.path.join(base, 'operstate')) or 'unknown',
        # speed/duplex reads fail with EINVAL when the link is down
        'speed': _read_int(os.path.join(base, 'speed')),
        'duplex': _read(os.path.join(base, 'duplex')),
        'physical': os.path.exists(os.path.join(base, 'device')),
        'bond': os.path.isdir(os.path.join(base, 'bonding')),
        'lower': sorted(
            os.path.basename(p)[len('lower_') :]
            for p in glob(os.path.join(base, 'lower_*'))
        ),
    }


def check_network(
    ctx: CephadmContext, networks: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    results = []
    if not os.path.isdir(SYS_NET):
        return [_result('network', STATUS_INFO, 'no %s' % SYS_NET)]
    ifaces = {
        i: _iface_info(i)
        for i in sorted(os.listdir(SYS_NET))
        if not _is_virtual_iface(i)
    }

    for iface, info in ifaces.items():
        if info['physical'] and info['operstate'] == 'up':
            speed = info['speed']
            if speed is not None and 0 < speed < 10000:
                results.append(
                    _result(
                        'nic_speed',
                        STATUS_WARN,
                        'link speed is below 10 Gb/s',
                        iface,
                        '%d Mb/s' % speed,
                        '>= 10000 Mb/s',
                    )
                )
            elif speed is not None and speed > 0:
                results.append(
                    _result(
                        'nic_speed',
                        STATUS_OK,
                        'link speed',
                        iface,
                        '%d Mb/s' % speed,
                    )
                )
            if info['duplex'] and info['duplex'] not in ('full', 'unknown'):
                results.append(
                    _result(
                        'nic_duplex',
                        STATUS_WARN,
                        'link is not full duplex',
                        iface,
                        info['duplex'],
                        'full',
                    )
                )

        if info['bond']:
            mode = _read(os.path.join(SYS_NET, iface, 'bonding', 'mode'))
            down = [
                s
                for s in info['lower']
                if ifaces.get(s, {}).get('operstate') != 'up'
            ]
            if down:
                results.append(
                    _result(
                        'bond',
                        STATUS_WARN,
                        'bond is degraded; members down: %s' % ','.join(down),
                        iface,
                        mode,
                    )
                )
            else:
                results.append(
                    _result(
                        'bond',
                        STATUS_OK,
                        'members: %s' % ','.join(info['lower']),
                        iface,
                        mode,
                    )
                )

        # a lower device with a smaller MTU silently caps the upper one
        for lower in info['lower']:
            lmtu = ifaces.get(lower, {}).get('mtu')
            if lmtu is not None and info['mtu'] and lmtu < info['mtu']:
                results.append(
                    _result(
                        'mtu',
                        STATUS_WARN,
                        'lower device %s has a smaller MTU than %s'
                        % (lower, iface),
                        lower,
                        lmtu,
                        '>= %d' % info['mtu'],
                    )
                )

    if networks:
        results.extend(_check_ceph_networks(ctx, networks, ifaces))
    return results


def _check_ceph_networks(
    ctx: CephadmContext,
    networks: List[str],
    ifaces: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    # imported here to keep this module light for callers that only need
    # the device helpers
    from .host_facts import list_networks

    results = []
    host_nets = list_networks(ctx)
    for cidr in networks:
        try:
            want = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            results.append(
                _result('ceph_network', STATUS_FAIL, 'invalid network', cidr)
            )
            continue
        found = []
        for iface_map in host_nets.values():
            for iface, addrs in iface_map.items():
                for addr in addrs:
                    try:
                        if ipaddress.ip_address(addr) in want:
                            found.append((iface, addr))
                    except ValueError:
                        continue
        if not found:
            results.append(
                _result(
                    'ceph_network',
                    STATUS_FAIL,
                    'host has no address in this network',
                    cidr,
                )
            )
            continue
        for iface, addr in sorted(set(found)):
            info = ifaces.get(iface) or _iface_info(iface)
            results.append(
                _result(
                    'ceph_network',
                    STATUS_OK,
                    '%s on %s' % (addr, iface),
                    cidr,
                    'mtu %s' % info.get('mtu'),
                )
            )
    return results


def check_sizing(ctx: CephadmContext) -> List[Dict[str, Any]]:
    """Compare memory and CPU with what OSDs on every data disk would need."""
    results: List[Dict[str, Any]] = []
    # a mounted disk holds something other than an OSD; an LVM holder may
    # well be an existing OSD, so those still count
    devs = [d for d in data_devices() if not d['mounted']]
    n_hdd = len([d for d in devs if d['rotational']])
    n_flash = len(devs) - n_hdd
    if not devs:
        return results

    mem_kb = 0
    for line in read_file(['/proc/meminfo']).splitlines():
        if line.startswith('MemTotal:'):
            mem_kb = int(line.split()[1])
    mem = mem_kb * 1024
    need = MEM_BASE + MEM_PER_OSD * len(devs)
    results.append(
        _result(
            'memory',
            STATUS_OK if mem >= need * MEM_TOLERANCE else STATUS_WARN,
            '%d data devices at %d GiB osd_memory_target + %d GiB base'
            % (len(devs), MEM_PER_OSD // GiB, MEM_BASE // GiB),
            value='%.1f GiB' % (mem / GiB),
            expected='>= %d GiB' % (need // GiB),
        )
    )

    threads = os.cpu_count() or 0
    need_threads = (
        2 + n_hdd * THREADS_PER_HDD_OSD + n_flash * THREADS_PER_FLASH_OSD
    )
    results.append(
        _result(
            'cpu',
            STATUS_OK if threads >= need_threads else STATUS_WARN,
            '%d hdd and %d flash data devices' % (n_hdd, n_flash),
            value='%d threads' % threads,
            expected='>= %d threads' % need_threads,
        )
    )
    return results


def _cmdline() -> List[str]:
    return read_file(['/proc/cmdline']).split()


def _cpu_model() -> str:
    for line in read_file(['/proc/cpuinfo']).splitlines():
        if line.startswith('model name'):
            return line.split(':', 1)[1].strip()
    return ''


def check_kernel(ctx: CephadmContext) -> List[Dict[str, Any]]:
    results = []
    cmdline = _cmdline()
    cpu = _cpu_model()

    # the IOMMU translating every DMA costs AMD EPYC hosts a lot of network
    # and NVMe throughput
    if 'EPYC' in cpu:
        active = bool(glob(SYS_IOMMU + '/*'))
        if 'amd_iommu=off' in cmdline or 'iommu=off' in cmdline:
            results.append(
                _result('iommu', STATUS_OK, 'IOMMU disabled', value=cpu)
            )
        elif 'iommu=pt' in cmdline:
            results.append(
                _result(
                    'iommu',
                    STATUS_INFO,
                    'IOMMU in passthrough mode; amd_iommu=off is recommended '
                    'on AMD EPYC',
                    value='iommu=pt',
                    expected='amd_iommu=off',
                )
            )
        elif active:
            results.append(
                _result(
                    'iommu',
                    STATUS_WARN,
                    'the IOMMU costs AMD EPYC hosts network and NVMe '
                    'throughput; boot with amd_iommu=off',
                    value='enabled',
                    expected='amd_iommu=off',
                )
            )
        else:
            results.append(
                _result('iommu', STATUS_OK, 'IOMMU not active', value=cpu)
            )

    # mitigations cost a lot of syscall- and interrupt-heavy OSD work
    if 'mitigations=off' in cmdline:
        results.append(
            _result(
                'cpu_mitigations', STATUS_OK, 'mitigations=off', value='off'
            )
        )
    else:
        vulns = glob(os.path.join(SYS_CPU, 'vulnerabilities', '*'))
        mitigated = sorted(
            os.path.basename(v)
            for v in vulns
            if (_read(v) or '').startswith('Mitigation')
        )
        if mitigated:
            results.append(
                _result(
                    'cpu_mitigations',
                    STATUS_INFO,
                    'CPU vulnerability mitigations cost OSD throughput; '
                    'mitigations=off is recommended for dedicated OSD hosts, '
                    'but not for hosts that also run hypervisors or other '
                    'clients',
                    value='%d mitigated: %s'
                    % (len(mitigated), ','.join(mitigated)),
                    expected='mitigations=off',
                )
            )
    return results


def check_smart(ctx: CephadmContext) -> List[Dict[str, Any]]:
    results = []
    devs = list_block_devices()
    if not host_hw.find_executable('smartctl'):
        return [
            _result(
                'smart',
                STATUS_INFO,
                'smartctl is not installed (smartmontools); disk health '
                'was not checked',
            )
        ]
    for dev in devs:
        data = host_hw.smartctl(ctx, dev)
        if data is None:
            continue
        if not data.get('smart_status') and '_error' not in data:
            # virtual disks and some RAID volumes have no SMART
            results.append(
                _result('smart', STATUS_INFO, 'no SMART data', dev)
            )
            continue
        problems = host_hw.smart_problems(data)
        if not problems:
            temp = data.get('temperature', {}).get('current')
            results.append(
                _result(
                    'smart',
                    STATUS_OK,
                    '%s firmware %s'
                    % (
                        data.get('model_name', ''),
                        data.get('firmware_version', ''),
                    ),
                    dev,
                    '%s C' % temp if temp is not None else None,
                )
            )
        for sev, msg in problems:
            results.append(
                _result(
                    'smart',
                    {'fail': STATUS_FAIL, 'warn': STATUS_WARN}.get(
                        sev, STATUS_INFO
                    ),
                    msg,
                    dev,
                )
            )
    return results


def check_pcie(ctx: CephadmContext) -> List[Dict[str, Any]]:
    results = []
    for link in host_hw.pci_links():
        narrow = link['cur_width'] < link['max_width']
        slow = link['cur_speed'] < link['max_speed']
        value = 'x%d %.1f GT/s' % (link['cur_width'], link['cur_speed'])
        expected = 'x%d %.1f GT/s' % (link['max_width'], link['max_speed'])
        if not (narrow or slow):
            results.append(
                _result('pcie', STATUS_OK, 'full link', link['label'], value)
            )
            continue
        limited = (
            narrow and 0 < link['upstream_width'] <= link['cur_width']
        ) or (slow and 0 < link['upstream_speed'] <= link['cur_speed'])
        msg = (
            'link is limited by the slot or upstream port'
            if limited
            else 'link trained below its capability: reseat the card, '
            'check the slot and riser, or check BIOS lane settings'
        )
        results.append(
            _result('pcie', STATUS_WARN, msg, link['label'], value, expected)
        )
    return results


def firmware_inventory(
    ctx: CephadmContext,
) -> Dict[str, List[Dict[str, str]]]:
    disks = [host_hw.disk_firmware(d) for d in list_block_devices()]
    nics = []
    for iface in host_hw.physical_nics():
        info = host_hw.ethtool_info(ctx, iface) or {}
        nics.append(
            {
                'iface': iface,
                'id': host_hw.nic_identity(iface),
                'driver': info.get('driver', ''),
                'firmware': info.get('firmware-version', ''),
            }
        )
    return {'disks': disks, 'nics': nics}


def _mismatches(
    items: List[Dict[str, str]], key: str, rev: str, name: str
) -> Dict[str, Dict[str, List[str]]]:
    groups: Dict[str, Dict[str, List[str]]] = {}
    for it in items:
        if not it.get(key) or not it.get(rev):
            continue
        groups.setdefault(it[key], {}).setdefault(it[rev], []).append(
            it[name]
        )
    return {k: v for k, v in groups.items() if len(v) > 1}


def check_firmware(
    ctx: CephadmContext, inv: Optional[Dict[str, List[Dict[str, str]]]] = None
) -> List[Dict[str, Any]]:
    if inv is None:
        inv = firmware_inventory(ctx)
    results = []
    for model, revs in sorted(
        _mismatches(inv['disks'], 'model', 'rev', 'dev').items()
    ):
        results.append(
            _result(
                'firmware',
                STATUS_WARN,
                'disks of one model run different firmware',
                model,
                '; '.join(
                    '%s: %s' % (r, ','.join(d))
                    for r, d in sorted(revs.items())
                ),
            )
        )
    for ident, revs in sorted(
        _mismatches(inv['nics'], 'id', 'firmware', 'iface').items()
    ):
        results.append(
            _result(
                'firmware',
                STATUS_WARN,
                'NICs of one model run different firmware',
                ident,
                '; '.join(
                    '%s: %s' % (r, ','.join(d))
                    for r, d in sorted(revs.items())
                ),
            )
        )
    if not results:
        results.append(
            _result(
                'firmware', STATUS_OK, 'no firmware mismatches in this host'
            )
        )
    return results


def check_nic_errors(ctx: CephadmContext) -> List[Dict[str, Any]]:
    results = []
    for iface in host_hw.physical_nics():
        c = host_hw.nic_counters(iface)
        bad = {
            k: v
            for k, v in c.items()
            if k in host_hw.NIC_ERROR_COUNTERS and v
        }
        if bad:
            results.append(
                _result(
                    'nic_errors',
                    STATUS_WARN,
                    'error counters since boot are non-zero; CRC and frame '
                    'errors usually mean a bad cable, optic or port',
                    iface,
                    ','.join('%s=%d' % kv for kv in sorted(bad.items())),
                )
            )
        packets = c.get('rx_packets', 0) + c.get('tx_packets', 0)
        drops = c.get('rx_dropped', 0) + c.get('tx_dropped', 0)
        if packets and drops / packets > 0.001:
            results.append(
                _result(
                    'nic_errors',
                    STATUS_INFO,
                    'more than 0.1% of packets dropped since boot',
                    iface,
                    'rx_dropped=%d,tx_dropped=%d'
                    % (c.get('rx_dropped', 0), c.get('tx_dropped', 0)),
                )
            )
        stats = host_hw.ethtool_stats(ctx, iface)
        if stats:
            crc = any(re.search(r'crc|fcs|symbol', k, re.I) for k in stats)
            top = sorted(stats.items(), key=lambda kv: -kv[1])[:6]
            results.append(
                _result(
                    'nic_errors',
                    STATUS_WARN if crc else STATUS_INFO,
                    'driver error/drop/discard counters since boot',
                    iface,
                    ','.join('%s=%d' % kv for kv in top),
                )
            )
    return results


def check_ipmi(ctx: CephadmContext) -> List[Dict[str, Any]]:
    sel = host_hw.ipmi_sel(ctx)
    if sel is None:
        return [
            _result(
                'ipmi',
                STATUS_INFO,
                'no BMC access (ipmitool or /dev/ipmi0 missing); the SEL '
                'and sensors were not checked',
            )
        ]
    results = []
    kinds = host_hw.classify_sel(sel)
    for kind, desc in (
        ('error', 'hardware errors in the BMC event log (SEL)'),
        ('thermal', 'thermal or fan events in the BMC event log (SEL)'),
    ):
        if kinds[kind]:
            results.append(
                _result(
                    'ipmi',
                    STATUS_WARN,
                    '%d %s; newest: %s'
                    % (len(kinds[kind]), desc, kinds[kind][-1]),
                )
            )
    for s in host_hw.ipmi_sdr(ctx) or []:
        results.append(
            _result(
                'ipmi',
                STATUS_WARN if s['status'] == 'non-critical' else STATUS_FAIL,
                'sensor is %s' % s['status'],
                s['sensor'],
                s['reading'],
            )
        )
    if not results:
        results.append(
            _result(
                'ipmi',
                STATUS_OK,
                'SEL and sensors clean',
                value='%d SEL entries' % len(sel),
            )
        )
    return results


def _nvme_controllers() -> List[str]:
    return sorted(glob('/sys/class/nvme/nvme*'))


def check_power(ctx: CephadmContext) -> List[Dict[str, Any]]:
    """CPU idle states, EPP, PCIe ASPM, NVMe APST and NIC EEE."""
    results = []
    cmdline = _cmdline()

    deep = []
    for st in sorted(
        glob(os.path.join(SYS_CPU, 'cpu0', 'cpuidle', 'state*'))
    ):
        lat = _read_int(os.path.join(st, 'latency'))
        if lat is None or lat <= CSTATE_MAX_LATENCY_US:
            continue
        if _read_int(os.path.join(st, 'disable')) == 1:
            continue
        deep.append('%s(%dus)' % (_read(os.path.join(st, 'name')), lat))
    if deep:
        results.append(
            _result(
                'cstates',
                STATUS_WARN,
                'deep CPU idle states are enabled; waking from them adds '
                'latency to every I/O',
                value=','.join(deep),
                expected='none above %dus' % CSTATE_MAX_LATENCY_US,
            )
        )
    elif glob(os.path.join(SYS_CPU, 'cpu0', 'cpuidle', 'state*')):
        results.append(
            _result('cstates', STATUS_OK, 'no deep idle states enabled')
        )

    epp = {
        _read(p)
        for p in glob(
            os.path.join(
                SYS_CPU,
                'cpu[0-9]*',
                'cpufreq',
                'energy_performance_preference',
            )
        )
    }
    epp.discard(None)
    if epp and epp != {'performance'}:
        results.append(
            _result(
                'epp',
                STATUS_WARN,
                'CPU energy/performance preference favours power saving',
                value=','.join(sorted(str(e) for e in epp)),
                expected='performance',
            )
        )

    policy = _read('/sys/module/pcie_aspm/parameters/policy')
    if 'pcie_aspm=off' in cmdline:
        results.append(
            _result('aspm', STATUS_OK, 'PCIe ASPM off', value='pcie_aspm=off')
        )
    elif policy:
        p = _bracketed(policy)
        if p == 'performance':
            status, msg = STATUS_OK, 'PCIe ASPM policy'
        elif p == 'default':
            status = STATUS_INFO
            msg = (
                'PCIe ASPM follows the firmware; check it is off in the BIOS'
            )
        else:
            status = STATUS_WARN
            msg = 'PCIe ASPM adds link wake-up latency to NICs and NVMe'
        results.append(
            _result(
                'aspm',
                status,
                msg,
                value=p,
                expected='performance or pcie_aspm=off',
            )
        )

    ctrls = _nvme_controllers()
    if ctrls:
        default_ps = _read_int(
            '/sys/module/nvme_core/parameters/default_ps_max_latency_us'
        )
        apst = [
            os.path.basename(c)
            for c in ctrls
            if _read_int(
                os.path.join(c, 'power', 'pm_qos_latency_tolerance_us')
            )
            not in (0, None)
        ]
        if default_ps and apst:
            results.append(
                _result(
                    'nvme_apst',
                    STATUS_INFO,
                    'NVMe autonomous power state transitions are enabled; '
                    'some drives stall or drop off the bus on transitions',
                    ','.join(apst),
                    'default_ps_max_latency_us=%d' % default_ps,
                    'nvme_core.default_ps_max_latency_us=0',
                )
            )

    for iface in host_hw.physical_nics():
        r = host_hw._run(ctx, ['ethtool', '--show-eee', iface])
        if r and not r[1] and re.search(r'EEE status:\s*enabled', r[0]):
            results.append(
                _result(
                    'eee',
                    STATUS_WARN,
                    'Energy-Efficient Ethernet adds latency when the link wakes',
                    iface,
                    'enabled',
                    'disabled',
                )
            )
    return results


def check_numa(ctx: CephadmContext) -> List[Dict[str, Any]]:
    nodes = glob(os.path.join(SYS_NODE, 'node[0-9]*'))
    if len(nodes) < 2:
        return []
    results = []
    zr = _read_int('/proc/sys/vm/zone_reclaim_mode')
    if zr:
        results.append(
            _result(
                'numa',
                STATUS_WARN,
                'zone reclaim stalls allocations instead of using remote memory',
                'vm.zone_reclaim_mode',
                zr,
                0,
            )
        )
    nb = _read_int('/proc/sys/kernel/numa_balancing')
    if nb:
        results.append(
            _result(
                'numa',
                STATUS_INFO,
                'automatic NUMA balancing migrates pages under OSDs and adds '
                'latency jitter',
                'kernel.numa_balancing',
                nb,
                0,
            )
        )
    sockets = set()
    for line in read_file(['/proc/cpuinfo']).splitlines():
        if line.startswith('physical id'):
            sockets.add(line.split(':', 1)[1].strip())
    per_socket = len(nodes) // max(1, len(sockets))
    results.append(
        _result(
            'numa',
            STATUS_INFO,
            '%d NUMA nodes on %d sockets%s'
            % (
                len(nodes),
                len(sockets),
                ' (AMD NPS%d)' % per_socket if 'EPYC' in _cpu_model() else '',
            ),
        )
    )
    return results


def check_nic_tuning(ctx: CephadmContext) -> List[Dict[str, Any]]:
    results = []
    nics = host_hw.physical_nics()
    multiqueue = [
        i
        for i in nics
        if len(glob(os.path.join(SYS_NET, i, 'queues', 'rx-*'))) > 1
    ]
    if multiqueue:
        r = host_hw._run(ctx, ['systemctl', 'is-active', 'irqbalance'])
        if r is not None and r[0].strip() != 'active':
            results.append(
                _result(
                    'irqbalance',
                    STATUS_INFO,
                    'irqbalance is not running; without it or manual IRQ '
                    'affinity, NIC interrupts may all land on one CPU',
                    value=r[0].strip() or 'inactive',
                )
            )
    for iface in nics:
        if _read(os.path.join(SYS_NET, iface, 'operstate')) != 'up':
            continue
        r = host_hw._run(ctx, ['ethtool', '-g', iface])
        if r and not r[1]:
            maxes = re.search(r'maximums:.*?RX:\s*(\d+)', r[0], re.S)
            cur = re.search(
                r'Current hardware settings:.*?RX:\s*(\d+)', r[0], re.S
            )
            if maxes and cur and int(cur.group(1)) < int(maxes.group(1)):
                results.append(
                    _result(
                        'nic_rings',
                        STATUS_INFO,
                        'RX ring is smaller than the maximum; small rings drop '
                        'packets under bursts',
                        iface,
                        int(cur.group(1)),
                        int(maxes.group(1)),
                    )
                )
        r = host_hw._run(ctx, ['ethtool', '-k', iface])
        if r and not r[1]:
            off = [
                k
                for k in (
                    'tcp-segmentation-offload',
                    'generic-segmentation-offload',
                    'generic-receive-offload',
                )
                if re.search(r'^%s:\s*off' % k, r[0], re.M)
            ]
            if off:
                results.append(
                    _result(
                        'nic_offloads',
                        STATUS_INFO,
                        'offloads are off, which costs CPU per packet',
                        iface,
                        ','.join(off),
                        'on',
                    )
                )
    return results


CHECKS = [
    ('sysctl', check_sysctls),
    ('thp', check_thp),
    ('tuned', check_tuned),
    ('cpu_governor', check_cpu_governor),
    ('kernel', check_kernel),
    ('power', check_power),
    ('numa', check_numa),
    ('swap', check_swap),
    ('disks', check_block_devices),
    ('sizing', check_sizing),
    ('smart', check_smart),
    ('pcie', check_pcie),
    ('ipmi', check_ipmi),
    ('nic_errors', check_nic_errors),
    ('nic_tuning', check_nic_tuning),
]


def check_peers(
    ctx: CephadmContext, peers: List[str]
) -> List[Dict[str, Any]]:
    from .net_test import ping_peers

    results = []
    for r in ping_peers(ctx, peers):
        target = '%s (%s)' % (r['peer'], r['addr'])
        if 'error' in r:
            results.append(_result('ping', STATUS_FAIL, r['error'], target))
            continue
        small, big = r['small'], r['full_mtu']
        rtt = small.get('rtt_avg_ms')
        if not small['ok'] and not big['ok']:
            results.append(
                _result('ping', STATUS_FAIL, 'peer is unreachable', target)
            )
        elif small['ok'] and not big['ok']:
            results.append(
                _result(
                    'ping',
                    STATUS_FAIL,
                    'small pings work but %d-byte packets with DF set do '
                    'not: the path MTU is below the local MTU, so jumbo '
                    'frames are not configured end to end' % r['mtu'],
                    target,
                    'mtu %d via %s' % (r['mtu'], r['iface']),
                )
            )
        elif small.get('loss_pct') or big.get('loss_pct'):
            results.append(
                _result(
                    'ping',
                    STATUS_WARN,
                    'packet loss',
                    target,
                    'small %s%%, full MTU %s%%'
                    % (small.get('loss_pct'), big.get('loss_pct')),
                )
            )
        else:
            results.append(
                _result(
                    'ping',
                    STATUS_OK,
                    'reachable at MTU %d via %s' % (r['mtu'], r['iface']),
                    target,
                    '%s ms' % rtt if rtt is not None else None,
                )
            )
    return results


def check_groups() -> List[str]:
    return [name for name, _ in CHECKS] + ['firmware', 'network', 'ping']


def run_checks(
    ctx: CephadmContext,
    networks: Optional[List[str]] = None,
    skip: Optional[List[str]] = None,
    peers: Optional[List[str]] = None,
) -> Dict[str, Any]:
    skipped = set(skip or [])
    results: List[Dict[str, Any]] = []
    report: Dict[str, Any] = {}

    def run(name: str, fn: Any, *args: Any) -> None:
        if name in skipped:
            return
        try:
            results.extend(fn(ctx, *args))
        except Exception as e:
            logger.debug('check %s failed', name, exc_info=True)
            results.append(
                _result(name, STATUS_INFO, 'check could not run: %s' % e)
            )

    for name, fn in CHECKS:
        run(name, fn)
    if 'firmware' not in skipped:
        # also returned, so the mgr can compare firmware across hosts
        try:
            report['inventory'] = firmware_inventory(ctx)
        except Exception:
            logger.debug('firmware inventory failed', exc_info=True)
        run('firmware', check_firmware, report.get('inventory'))
    run('network', check_network, networks)
    if peers:
        run('ping', check_peers, peers)
    summary = {s: 0 for s in STATUS_ORDER}
    for r in results:
        summary[r['status']] += 1
    report.update({'summary': summary, 'checks': results})
    return report


def format_report(report: Dict[str, Any]) -> str:
    lines = []
    for r in report['checks']:
        detail = r['message']
        if 'value' in r:
            detail += ' (value: %s' % r['value']
            if 'expected' in r:
                detail += ', expected: %s' % r['expected']
            detail += ')'
        lines.append(
            '%-5s %-16s %-28s %s'
            % (r['status'].upper(), r['check'], r['target'], detail)
        )
    s = report['summary']
    lines.append(
        '%d ok, %d info, %d warn, %d fail'
        % (s[STATUS_OK], s[STATUS_INFO], s[STATUS_WARN], s[STATUS_FAIL])
    )
    return '\n'.join(lines)
