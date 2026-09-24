# host_hw.py - hardware health probes shared by host-precheck and burnin
#
# Copyright (C) 2026 Clyso Technologies Inc.
#
# This is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License version 2.1, as published by the Free Software
# Foundation.  See file COPYING.

"""Read-only probes of disk, PCIe, NIC and BMC health.

Everything here degrades gracefully: a probe whose tool (smartctl,
ethtool, ipmitool) is not installed returns None, and callers report that
rather than failing.
"""

import json
import logging
import os
import re

from glob import glob
from typing import Any, Dict, List, Optional, Tuple

from .call_wrappers import call, CallVerbosity
from .context import CephadmContext
from .exe_utils import find_executable
from .file_utils import read_file

logger = logging.getLogger()

SYS_BLOCK = '/sys/block'
SYS_NET = '/sys/class/net'
SYS_PCI = '/sys/bus/pci/devices'
TOOL_TIMEOUT = 60


def _read(path: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    v = read_file([path])
    return None if v == 'Unknown' else v


def _run(
    ctx: CephadmContext, cmd: List[str], timeout: int = TOOL_TIMEOUT
) -> Optional[Tuple[str, int]]:
    """Run a tool if installed; None if it is not."""
    if not find_executable(cmd[0]):
        return None
    out, _, code = call(
        ctx, cmd, verbosity=CallVerbosity.QUIET, timeout=timeout
    )
    return out, code


##################################
# SMART

# ATA attributes whose raw value should be zero on a healthy disk:
# (id, name, severity)
ATA_BAD_ATTRS = [
    (5, 'reallocated sectors', 'warn'),
    (10, 'spin retries', 'warn'),
    (184, 'end-to-end errors', 'fail'),
    (187, 'reported uncorrectable errors', 'warn'),
    (196, 'reallocation events', 'warn'),
    (197, 'pending sectors', 'fail'),
    (198, 'offline uncorrectable sectors', 'fail'),
    (199, 'interface CRC errors (check the cable or backplane)', 'warn'),
]

# counters compared before and after a burn-in
SMART_COUNTERS = {
    5: 'reallocated_sectors',
    187: 'reported_uncorrect',
    197: 'pending_sectors',
    198: 'offline_uncorrectable',
    199: 'crc_errors',
}


def smartctl(ctx: CephadmContext, dev: str) -> Optional[Dict[str, Any]]:
    """`smartctl -j -a` for a device, or None if smartctl is missing."""
    r = _run(ctx, ['smartctl', '-j', '-a', '/dev/' + dev])
    if r is None:
        return None
    out, code = r
    try:
        data = json.loads(out)
    except ValueError:
        return {'_error': 'unparsable smartctl output (exit %d)' % code}
    data['_exit'] = code
    return data


def _ata_raw(data: Dict[str, Any], attr_id: int) -> Optional[int]:
    table = data.get('ata_smart_attributes', {}).get('table', [])
    for a in table:
        if a.get('id') == attr_id:
            raw = a.get('raw', {}).get('value')
            if isinstance(raw, int):
                # several vendors pack extra data into the high bytes
                return raw & 0xFFFFFFFF
    return None


def smart_counters(data: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """Error counters that should never increase."""
    if not data:
        return {}
    out = {}
    for attr_id, name in SMART_COUNTERS.items():
        v = _ata_raw(data, attr_id)
        if v is not None:
            out[name] = v
    nvme = data.get('nvme_smart_health_information_log')
    if nvme:
        for key in ('media_errors', 'num_err_log_entries'):
            if isinstance(nvme.get(key), int):
                out[key] = nvme[key]
    scsi = data.get('scsi_grown_defect_list')
    if isinstance(scsi, int):
        out['grown_defects'] = scsi
    err_log = data.get('ata_smart_error_log', {})
    for kind in ('extended', 'summary'):
        if isinstance(err_log.get(kind, {}).get('count'), int):
            out['error_log_count'] = err_log[kind]['count']
            break
    return out


def smart_problems(data: Dict[str, Any]) -> List[Tuple[str, str]]:
    """(severity, message) for everything worrying in a smartctl report."""
    out: List[Tuple[str, str]] = []
    if '_error' in data:
        return [('info', data['_error'])]
    status = data.get('smart_status', {})
    if status.get('passed') is False:
        out.append(('fail', 'SMART overall health check FAILED'))
    for attr_id, name, sev in ATA_BAD_ATTRS:
        v = _ata_raw(data, attr_id)
        if v:
            out.append((sev, '%d %s' % (v, name)))
    for a in data.get('ata_smart_attributes', {}).get('table', []):
        if a.get('when_failed'):
            out.append(
                (
                    'fail',
                    'attribute %s failed %s'
                    % (a.get('name'), a.get('when_failed')),
                )
            )
    err_log = data.get('ata_smart_error_log', {})
    for kind in ('extended', 'summary'):
        count = err_log.get(kind, {}).get('count')
        if count:
            out.append(('warn', '%d errors in the ATA error log' % count))
            break
    for t in (
        data.get('ata_smart_self_test_log', {})
        .get('standard', {})
        .get('table', [])[:1]
    ):
        if t.get('status', {}).get('passed') is False:
            out.append(
                (
                    'fail',
                    'last self-test failed: %s'
                    % t.get('status', {}).get('string', ''),
                )
            )
    nvme = data.get('nvme_smart_health_information_log')
    if nvme:
        if nvme.get('critical_warning'):
            out.append(
                (
                    'fail',
                    'NVMe critical warning 0x%x' % nvme['critical_warning'],
                )
            )
        if nvme.get('media_errors'):
            out.append(
                ('fail', '%d NVMe media errors' % nvme['media_errors'])
            )
        spare = nvme.get('available_spare')
        thresh = nvme.get('available_spare_threshold')
        if (
            isinstance(spare, int)
            and isinstance(thresh, int)
            and spare < thresh
        ):
            out.append(
                (
                    'fail',
                    'available spare %d%% is below the %d%% threshold'
                    % (spare, thresh),
                )
            )
        used = nvme.get('percentage_used')
        if isinstance(used, int) and used >= 80:
            out.append(('warn', 'endurance %d%% used' % used))
        if nvme.get('num_err_log_entries'):
            out.append(
                (
                    'info',
                    '%d entries in the NVMe error log'
                    % nvme['num_err_log_entries'],
                )
            )
    defects = data.get('scsi_grown_defect_list')
    if defects:
        out.append(('warn', '%d grown defects' % defects))
    counters = data.get('scsi_error_counter_log', {})
    for op in ('read', 'write', 'verify'):
        unc = counters.get(op, {}).get('total_uncorrected_errors')
        if unc:
            out.append(('fail', '%d uncorrected %s errors' % (unc, op)))
    return out


##################################
# firmware inventory


def disk_firmware(dev: str) -> Dict[str, str]:
    """Model and firmware revision as the kernel reports them.

    This is the same source gather-facts uses, so results can be compared
    with other hosts' facts.
    """
    base = os.path.join(SYS_BLOCK, dev, 'device')
    model = _read(os.path.join(base, 'model')) or ''
    rev = _read(os.path.join(base, 'rev')) or _read(
        os.path.join(base, 'firmware_rev')
    )
    return {'dev': dev, 'model': model.strip(), 'rev': (rev or '').strip()}


def ethtool_info(ctx: CephadmContext, iface: str) -> Optional[Dict[str, str]]:
    r = _run(ctx, ['ethtool', '-i', iface])
    if r is None or r[1]:
        return None
    info = {}
    for line in r[0].splitlines():
        if ':' in line:
            k, v = line.split(':', 1)
            info[k.strip()] = v.strip()
    return info


def nic_identity(iface: str) -> str:
    """PCI vendor:device of a NIC, used to group identical NICs."""
    base = os.path.join(SYS_NET, iface, 'device')
    vendor = _read(os.path.join(base, 'vendor')) or '?'
    device = _read(os.path.join(base, 'device')) or '?'
    return '%s:%s' % (vendor, device)


def physical_nics() -> List[str]:
    if not os.path.isdir(SYS_NET):
        return []
    return sorted(
        i
        for i in os.listdir(SYS_NET)
        if os.path.exists(os.path.join(SYS_NET, i, 'device'))
    )


##################################
# PCIe links

# PCI classes worth checking: NICs, NVMe, SAS/RAID/SATA HBAs
PCI_CLASSES = ('0x02', '0x0108', '0x0104', '0x0107', '0x0106')


def _gts(v: Optional[str]) -> float:
    m = re.match(r'([\d.]+)\s*GT/s', v or '')
    return float(m.group(1)) if m else 0.0


def _pci_label(addr: str) -> str:
    base = os.path.join(SYS_PCI, addr)
    names = [
        os.path.basename(p) for p in glob(os.path.join(base, 'net', '*'))
    ]
    names += [
        os.path.basename(p) for p in glob(os.path.join(base, 'nvme', '*'))
    ]
    return '%s (%s)' % (addr, ','.join(names)) if names else addr


def pci_links() -> List[Dict[str, Any]]:
    """PCIe link state of NICs, NVMe drives and storage controllers."""
    out = []
    for path in sorted(glob(os.path.join(SYS_PCI, '*'))):
        cls = _read(os.path.join(path, 'class')) or ''
        if not cls.startswith(PCI_CLASSES):
            continue
        # skip virtual functions, which report the physical function's link
        if os.path.exists(os.path.join(path, 'physfn')):
            continue
        try:
            cur_w = int(_read(os.path.join(path, 'current_link_width')) or 0)
            max_w = int(_read(os.path.join(path, 'max_link_width')) or 0)
        except ValueError:
            continue
        cur_s = _read(os.path.join(path, 'current_link_speed'))
        max_s = _read(os.path.join(path, 'max_link_speed'))
        if not max_w or not max_s:
            continue
        addr = os.path.basename(path)
        # the port above the device may be what limits the link
        parent = os.path.dirname(os.path.realpath(path))
        up_w = _read(os.path.join(parent, 'max_link_width'))
        up_s = _read(os.path.join(parent, 'max_link_speed'))
        out.append(
            {
                'addr': addr,
                'label': _pci_label(addr),
                'class': cls,
                'cur_width': cur_w,
                'max_width': max_w,
                'cur_speed': _gts(cur_s),
                'max_speed': _gts(max_s),
                'upstream_width': int(up_w) if up_w and up_w.isdigit() else 0,
                'upstream_speed': _gts(up_s),
            }
        )
    return out


##################################
# NIC error counters

# counters that indicate a physical problem when they are non-zero
NIC_ERROR_COUNTERS = [
    'rx_crc_errors',
    'rx_frame_errors',
    'rx_fifo_errors',
    'rx_missed_errors',
    'rx_over_errors',
    'rx_length_errors',
    'tx_carrier_errors',
    'tx_aborted_errors',
    'tx_fifo_errors',
    'tx_heartbeat_errors',
    'tx_window_errors',
]
NIC_DROP_COUNTERS = ['rx_dropped', 'tx_dropped']
# driver counters (ethtool -S) worth reporting when non-zero
ETHTOOL_BAD = re.compile(r'(crc|discard|drop|err|fcs|symbol|timeout)', re.I)
ETHTOOL_IGNORE = re.compile(
    r'(xdp|prio\d_|_pp_|csum_(none|unnecessary))', re.I
)


def nic_counters(iface: str) -> Dict[str, int]:
    out = {}
    base = os.path.join(SYS_NET, iface, 'statistics')
    for name in (
        NIC_ERROR_COUNTERS
        + NIC_DROP_COUNTERS
        + [
            'rx_packets',
            'tx_packets',
        ]
    ):
        v = _read(os.path.join(base, name))
        if v is not None and v.isdigit():
            out[name] = int(v)
    return out


def ethtool_stats(
    ctx: CephadmContext, iface: str
) -> Optional[Dict[str, int]]:
    """Non-zero driver error/drop counters from `ethtool -S`."""
    r = _run(ctx, ['ethtool', '-S', iface])
    if r is None or r[1]:
        return None
    out = {}
    for line in r[0].splitlines():
        if ':' not in line:
            continue
        k, v = line.rsplit(':', 1)
        k, v = k.strip(), v.strip()
        if not v.isdigit() or not int(v):
            continue
        if ETHTOOL_BAD.search(k) and not ETHTOOL_IGNORE.search(k):
            out[k] = int(v)
    return out


##################################
# BMC (IPMI)

SEL_ERROR = re.compile(
    r'(uncorrectable|ecc|memory|processor|machine check|ierr|mce|'
    r'pci|critical|non-recoverable|drive fault|power supply|failure)',
    re.I,
)
SEL_THERMAL = re.compile(r'(thermal|throttl|temperature|fan)', re.I)
SEL_IGNORE = re.compile(
    r'(log area reset|cleared|event logging disabled)', re.I
)
# `ipmitool sdr` status column
SDR_BAD = {'cr': 'critical', 'nr': 'non-recoverable', 'nc': 'non-critical'}


def ipmi_available() -> bool:
    return bool(find_executable('ipmitool')) and any(
        os.path.exists(p)
        for p in ('/dev/ipmi0', '/dev/ipmi/0', '/dev/ipmidev/0')
    )


def ipmi_sel(ctx: CephadmContext) -> Optional[List[str]]:
    """`ipmitool sel elist` lines, or None without a usable BMC."""
    if not ipmi_available():
        return None
    r = _run(ctx, ['ipmitool', 'sel', 'elist'])
    if r is None or r[1]:
        return None
    return [line.strip() for line in r[0].splitlines() if '|' in line]


def classify_sel(lines: List[str]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {'error': [], 'thermal': []}
    for line in lines:
        if SEL_IGNORE.search(line):
            continue
        if SEL_THERMAL.search(line):
            out['thermal'].append(line)
        elif SEL_ERROR.search(line):
            out['error'].append(line)
    return out


def ipmi_sdr(ctx: CephadmContext) -> Optional[List[Dict[str, str]]]:
    """Sensors that are not ok, or None without a usable BMC."""
    if not ipmi_available():
        return None
    r = _run(ctx, ['ipmitool', 'sdr'])
    if r is None or r[1]:
        return None
    out = []
    for line in r[0].splitlines():
        fields = [f.strip() for f in line.split('|')]
        if len(fields) >= 3 and fields[2] in SDR_BAD:
            out.append(
                {
                    'sensor': fields[0],
                    'reading': fields[1],
                    'status': SDR_BAD[fields[2]],
                }
            )
    return out
