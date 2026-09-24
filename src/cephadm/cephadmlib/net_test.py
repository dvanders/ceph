# net_test.py - reachability, path MTU and throughput tests between hosts
#
# Copyright (C) 2026 Clyso Technologies Inc.
#
# This is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License version 2.1, as published by the Free Software
# Foundation.  See file COPYING.

"""Tests of the network between this host and its peers.

ping_peers() pings each peer twice: with small packets, and with packets
of the local interface's full MTU and the don't-fragment bit set. When the
small ping works and the large one does not, something on the path (a
switch port, a peer NIC, a VLAN) has a smaller MTU than this host: the
classic half-configured jumbo frames problem, which makes large Ceph
messages hang while pings and small requests work.

iperf3 tests need a server on the peer. iperf_server() runs one as a
transient systemd unit that stops by itself after a timeout, and
iperf_client() runs a bidirectional test against it.
"""

import ipaddress
import json
import logging
import os
import re
import time

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from .call_wrappers import call, CallVerbosity
from .context import CephadmContext
from .exceptions import Error
from .exe_utils import find_executable
from . import host_hw

logger = logging.getLogger()

IPERF_PORT = 5201
IPERF_UNIT = 'ceph-iperf3'
# a direction below this fraction of the link speed is reported
IPERF_MIN_LINK_FRACTION = 0.7
PING_COUNT = 3
PING_PARALLEL = 16


def parse_peer(peer: str) -> Tuple[str, str]:
    """'name=addr' or 'addr' -> (name, addr)."""
    if '=' in peer:
        name, addr = peer.split('=', 1)
        return name, addr
    return peer, peer


def route_iface(ctx: CephadmContext, addr: str) -> Optional[str]:
    if not find_executable('ip'):
        return None
    out, _, code = call(
        ctx, ['ip', '-j', 'route', 'get', addr], verbosity=CallVerbosity.QUIET
    )
    if code:
        return None
    try:
        routes = json.loads(out)
        return routes[0].get('dev') if routes else None
    except (ValueError, IndexError, AttributeError):
        return None


def iface_mtu(iface: Optional[str]) -> int:
    if not iface:
        return 1500
    try:
        with open(os.path.join(host_hw.SYS_NET, iface, 'mtu')) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 1500


def iface_speed(iface: Optional[str]) -> Optional[int]:
    """Link speed in Mb/s, following a bond or VLAN to its members."""
    if not iface:
        return None
    base = os.path.join(host_hw.SYS_NET, iface)
    try:
        with open(os.path.join(base, 'speed')) as f:
            v = int(f.read().strip())
        if v > 0:
            return v
    except (OSError, ValueError):
        pass
    return None


def _ping(
    ctx: CephadmContext, addr: str, size: Optional[int]
) -> Dict[str, Any]:
    cmd = ['ping', '-q', '-n', '-c', str(PING_COUNT), '-i', '0.2', '-W', '2']
    if ipaddress.ip_address(addr).version == 6:
        cmd.append('-6')
    if size is not None:
        cmd += ['-M', 'do', '-s', str(size)]
    out, err, code = call(
        ctx, cmd + [addr], verbosity=CallVerbosity.QUIET, timeout=30
    )
    r: Dict[str, Any] = {'ok': code == 0}
    m = re.search(r'(\d+) packets transmitted, (\d+) received', out)
    if m:
        sent, recv = int(m.group(1)), int(m.group(2))
        r['loss_pct'] = (
            round(100.0 * (sent - recv) / sent, 1) if sent else 100
        )
    m = re.search(r'= [\d.]+/([\d.]+)/([\d.]+)/', out)
    if m:
        r['rtt_avg_ms'] = float(m.group(1))
        r['rtt_max_ms'] = float(m.group(2))
    if code and not m:
        lines = (err + '\n' + out).strip().splitlines()
        # e.g. "ping: local error: message too long, mtu=1500"
        why = [
            ln
            for ln in lines
            if re.search(r'error|unreachable|unknown', ln, re.I)
        ]
        r['error'] = (why or lines or ['no reply'])[0].strip()
    return r


def ping_peer(ctx: CephadmContext, peer: str) -> Dict[str, Any]:
    name, addr = parse_peer(peer)
    try:
        version = ipaddress.ip_address(addr).version
    except ValueError:
        return {'peer': name, 'addr': addr, 'error': 'not an IP address'}
    iface = route_iface(ctx, addr)
    mtu = iface_mtu(iface)
    # IP + ICMP headers
    payload = mtu - (48 if version == 6 else 28)
    return {
        'peer': name,
        'addr': addr,
        'iface': iface,
        'mtu': mtu,
        'small': _ping(ctx, addr, None),
        'full_mtu': _ping(ctx, addr, payload),
    }


def ping_peers(ctx: CephadmContext, peers: List[str]) -> List[Dict[str, Any]]:
    if not peers or not find_executable('ping'):
        return []
    with ThreadPoolExecutor(max_workers=PING_PARALLEL) as pool:
        return list(pool.map(lambda p: ping_peer(ctx, p), peers))


##################################
# iperf3


def _unit(port: int) -> str:
    return '%s-%d' % (IPERF_UNIT, port)


def _unit_active(ctx: CephadmContext, unit: str) -> bool:
    _, _, code = call(
        ctx,
        ['systemctl', 'is-active', '--quiet', unit],
        verbosity=CallVerbosity.QUIET,
    )
    return code == 0


def iperf_server(
    ctx: CephadmContext, port: int, timeout: int
) -> Dict[str, Any]:
    """Start an iperf3 server that exits by itself after timeout seconds."""
    for tool in ('iperf3', 'systemd-run'):
        if not find_executable(tool):
            raise Error('%s is not installed on this host' % tool)
    unit = _unit(port)
    if _unit_active(ctx, unit):
        raise Error('an iperf3 server is already running on port %d' % port)
    call(
        ctx,
        ['systemctl', 'reset-failed', unit],
        verbosity=CallVerbosity.QUIET,
    )
    opened, props = _open_port(ctx, port, timeout)
    _, err, code = call(
        ctx,
        [
            'systemd-run',
            '--unit',
            unit,
            '--description',
            'Ceph network test (iperf3 server)',
            '--collect',
            '--property',
            'RuntimeMaxSec=%d' % timeout,
        ]
        + props
        + [
            find_executable('iperf3') or 'iperf3',
            '-s',
            '-p',
            str(port),
        ],
        verbosity=CallVerbosity.VERBOSE_ON_FAILURE,
    )
    if code:
        if opened == 'ufw':
            # the unit never ran, so its ExecStopPost will not either
            call(
                ctx,
                ['ufw', 'delete', 'allow', '%d/tcp' % port],
                verbosity=CallVerbosity.QUIET,
            )
        raise Error('failed to start iperf3 server: %s' % err)
    return {
        'unit': unit,
        'port': port,
        'timeout': timeout,
        'firewall_opened': opened,
    }


def _open_port(
    ctx: CephadmContext, port: int, timeout: int
) -> Tuple[Any, List[str]]:
    """Open the port in the active firewall for the server's lifetime.

    Returns what was opened and extra systemd-run properties that close it
    again when the server unit stops, however it stops.
    """
    if find_executable('firewall-cmd'):
        _, _, code = call(
            ctx, ['firewall-cmd', '--state'], verbosity=CallVerbosity.QUIET
        )
        if code == 0:
            # a runtime rule that firewalld removes again by itself
            _, _, code = call(
                ctx,
                [
                    'firewall-cmd',
                    '--add-port=%d/tcp' % port,
                    '--timeout=%ds' % timeout,
                ],
                verbosity=CallVerbosity.QUIET,
            )
            return ('firewalld' if code == 0 else False), []
    ufw = find_executable('ufw')
    if ufw:
        out, _, code = call(
            ctx, [ufw, 'status'], verbosity=CallVerbosity.QUIET
        )
        if code == 0 and re.search(r'^Status:\s*active', out, re.M):
            rule = '%d/tcp' % port
            if re.search(r'^%s\s+ALLOW' % re.escape(rule), out, re.M):
                # already allowed: leave the operator's rule alone
                return False, []
            _, _, code = call(
                ctx,
                [ufw, 'allow', rule, 'comment', 'ceph net-test'],
                verbosity=CallVerbosity.QUIET,
            )
            if code == 0:
                # ufw rules do not expire: delete it when the unit stops
                return 'ufw', [
                    '--property',
                    'ExecStopPost=%s delete allow %s' % (ufw, rule),
                ]
    return False, []


def iperf_server_stop(ctx: CephadmContext, port: int) -> bool:
    unit = _unit(port)
    if not _unit_active(ctx, unit):
        return False
    call(ctx, ['systemctl', 'stop', unit], verbosity=CallVerbosity.QUIET)
    return True


def _bidir_supported(ctx: CephadmContext) -> bool:
    out, err, _ = call(
        ctx, ['iperf3', '--help'], verbosity=CallVerbosity.QUIET
    )
    return '--bidir' in out + err


def _iperf_run(
    ctx: CephadmContext, args: List[str], duration: int
) -> Dict[str, Any]:
    # the server may still be starting: retry a refused connection briefly
    for attempt in range(5):
        out, err, code = call(
            ctx,
            ['iperf3', '-J'] + args,
            verbosity=CallVerbosity.QUIET,
            timeout=duration + 30,
        )
        try:
            data = json.loads(out)
        except ValueError:
            data = {'error': (err or out).strip() or 'exit %d' % code}
        if 'refused' in str(data.get('error', '')) and attempt < 4:
            time.sleep(1)
            continue
        return data
    return data


def _gbps(d: Dict[str, Any], key: str) -> Optional[float]:
    v = d.get('end', {}).get(key, {}).get('bits_per_second')
    return round(v / 1e9, 2) if isinstance(v, (int, float)) else None


def _retrans(d: Dict[str, Any], key: str) -> Optional[int]:
    v = d.get('end', {}).get(key, {}).get('retransmits')
    return v if isinstance(v, int) else None


def iperf_client(
    ctx: CephadmContext,
    peer: str,
    port: int,
    duration: int,
    parallel: int,
) -> Dict[str, Any]:
    """Bidirectional throughput test against an iperf3 server on peer."""
    if not find_executable('iperf3'):
        raise Error('iperf3 is not installed on this host')
    name, addr = parse_peer(peer)
    iface = route_iface(ctx, addr)
    before = host_hw.nic_counters(iface) if iface else {}
    base = [
        '-c',
        addr,
        '-p',
        str(port),
        '-t',
        str(duration),
        '-P',
        str(parallel),
    ]
    result: Dict[str, Any] = {
        'peer': name,
        'addr': addr,
        'iface': iface,
        'link_mbps': iface_speed(iface),
        'duration': duration,
        'parallel': parallel,
    }
    if _bidir_supported(ctx):
        d = _iperf_run(ctx, base + ['--bidir'], duration)
        if d.get('error'):
            result['error'] = d['error']
            return result
        result['to_peer_gbps'] = _gbps(d, 'sum_received')
        result['from_peer_gbps'] = _gbps(d, 'sum_received_bidir_reverse')
        result['to_peer_retransmits'] = _retrans(d, 'sum_sent')
        result['from_peer_retransmits'] = _retrans(
            d, 'sum_sent_bidir_reverse'
        )
        result['mode'] = 'bidir'
    else:
        # iperf3 before 3.7: one direction at a time
        fwd = _iperf_run(ctx, base, duration)
        rev = _iperf_run(ctx, base + ['-R'], duration)
        err = fwd.get('error') or rev.get('error')
        if err:
            result['error'] = err
            return result
        result['to_peer_gbps'] = _gbps(fwd, 'sum_received')
        result['from_peer_gbps'] = _gbps(rev, 'sum_received')
        result['to_peer_retransmits'] = _retrans(fwd, 'sum_sent')
        result['from_peer_retransmits'] = _retrans(rev, 'sum_sent')
        result['mode'] = 'sequential'
    after = host_hw.nic_counters(iface) if iface else {}
    result['nic_errors'] = {
        k: after[k] - before.get(k, 0)
        for k in host_hw.NIC_ERROR_COUNTERS + host_hw.NIC_DROP_COUNTERS
        if k in after and after[k] > before.get(k, 0)
    }
    result['problems'] = iperf_problems(result)
    return result


def iperf_problems(r: Dict[str, Any]) -> List[str]:
    out = []
    link = r.get('link_mbps')
    for key, desc in (('to_peer_gbps', 'to'), ('from_peer_gbps', 'from')):
        v = r.get(key)
        if (
            v is not None
            and link
            and v * 1000 < link * IPERF_MIN_LINK_FRACTION
        ):
            out.append(
                '%.2f Gb/s %s %s is below %d%% of the %d Gb/s link'
                % (
                    v,
                    desc,
                    r['peer'],
                    100 * IPERF_MIN_LINK_FRACTION,
                    link // 1000,
                )
            )
    for k, v in sorted(r.get('nic_errors', {}).items()):
        out.append('%s increased by %d during the test' % (k, v))
    return out
