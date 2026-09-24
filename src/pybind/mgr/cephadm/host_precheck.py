# host_precheck.py - run cephadm host-precheck on hosts and track results
#
# Copyright (C) 2026 Clyso Technologies Inc.
#
# This is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License version 2.1, as published by the Free Software
# Foundation.  See file COPYING.

import datetime
import ipaddress
import json
import logging
import random
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

from ceph.utils import datetime_now, datetime_to_str, str_to_datetime
from orchestrator import OrchestratorError

if TYPE_CHECKING:
    from .module import CephadmOrchestrator

logger = logging.getLogger(__name__)

STORE_KEY = 'host_precheck'
HEALTH_CHECK = 'CEPHADM_HOST_PRECHECK'
# problems listed per host in the health detail
MAX_DETAIL_PER_HOST = 10


def format_problem(r: Dict[str, Any]) -> str:
    line = '%s %s' % (r['status'].upper(), r['check'])
    if r.get('target'):
        line += ' ' + r['target']
    line += ': ' + r.get('message', '')
    if 'value' in r:
        line += ' (value: %s' % r['value']
        if 'expected' in r:
            line += ', expected: %s' % r['expected']
        line += ')'
    return line


def problems(report: Dict[str, Any], level: str = 'warn') -> List[str]:
    wanted = ('warn', 'fail') if level == 'warn' else ('fail',)
    return [
        format_problem(r)
        for r in report.get('checks', [])
        if r.get('status') in wanted
    ]


class HostPrecheck:
    """Runs host-precheck and keeps the latest result per host."""

    def __init__(self, mgr: 'CephadmOrchestrator'):
        self.mgr = mgr
        self.results: Dict[str, Dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        try:
            self.results = json.loads(self.mgr.get_store(STORE_KEY) or '{}')
        except ValueError:
            self.results = {}

    def save(self) -> None:
        self.mgr.set_store(STORE_KEY, json.dumps(self.results))

    ##################################
    # inputs

    def ceph_networks(self) -> List[str]:
        nets: List[str] = []
        for opt in ('public_network', 'cluster_network'):
            val = str(self.mgr.get_foreign_ceph_option('mon', opt) or '')
            for n in val.split(','):
                n = n.strip()
                if '/' in n and n not in nets:
                    nets.append(n)
        return nets

    def peers(self, host: str, limit: Optional[int] = None) -> List[str]:
        """'name=ip' of the other hosts, on each Ceph network they are on."""
        nets = []
        for n in self.ceph_networks():
            try:
                nets.append(ipaddress.ip_network(n, strict=False))
            except ValueError:
                pass
        from . import utils

        out: List[str] = []
        others = [
            h for h in self.mgr.inventory.keys()
            if h != host
            and h not in self.mgr.offline_hosts
            and self.mgr.inventory._inventory.get(h, {}).get('status', '').lower() != 'maintenance'
        ]
        if limit and len(others) > limit:
            others = random.sample(others, limit)
        for h in sorted(others):
            addrs: List[str] = []
            try:
                addrs.append(utils.resolve_ip(self.mgr.inventory.get_addr(h)))
            except OrchestratorError:
                pass
            for iface_map in self.mgr.cache.networks.get(h, {}).values():
                for ips in iface_map.values():
                    for ip in ips:
                        try:
                            ipa = ipaddress.ip_address(ip)
                        except ValueError:
                            continue
                        if any(ipa in n for n in nets) and ip not in addrs:
                            addrs.append(ip)
            out.extend('%s=%s' % (h, a) for a in addrs)
        return out

    def skip_groups(self) -> List[str]:
        return [g.strip() for g in str(self.mgr.host_precheck_skip or '').split(',') if g.strip()]

    ##################################
    # running

    def run(
        self,
        host: str,
        addr: Optional[str] = None,
        max_peers: Optional[int] = None,
    ) -> Dict[str, Any]:
        from .serve import CephadmServe
        from .utils import cephadmNoImage

        args = ['--format', 'json']
        for net in self.ceph_networks():
            args += ['--network', net]
        for peer in self.peers(host, max_peers):
            args += ['--peer', peer]
        for group in self.skip_groups():
            args += ['--skip', group]
        with self.mgr.async_timeout_handler(host, 'cephadm host-precheck'):
            out, err, code = self.mgr.wait_async(
                CephadmServe(self.mgr)._run_cephadm(
                    host, cephadmNoImage, 'host-precheck', args,
                    addr=addr, error_ok=True, no_fsid=True))
        try:
            report = json.loads(''.join(out))
        except ValueError:
            report = None
        if not isinstance(report, dict) or 'checks' not in report:
            raise OrchestratorError(
                f'host-precheck failed on {host} (exit {code}):\n' + '\n'.join(err))
        if 'firmware' not in self.skip_groups():
            self.add_cluster_firmware(host, report)
        return report

    def add_cluster_firmware(self, host: str, report: Dict[str, Any]) -> None:
        """Compare this host's disk firmware with the other hosts' facts."""
        cluster: Dict[str, Dict[str, Set[str]]] = {}
        for h, facts in self.mgr.cache.facts.items():
            if h == host:
                continue
            for d in (facts.get('hdd_list') or []) + (facts.get('flash_list') or []):
                model = str(d.get('model') or '').strip()
                rev = str(d.get('rev') or '').strip()
                if model and rev and rev != 'Unknown':
                    cluster.setdefault(model, {}).setdefault(rev, set()).add(h)
        mine: Dict[str, Dict[str, List[str]]] = {}
        for d in report.get('inventory', {}).get('disks', []):
            if d.get('model') and d.get('rev'):
                mine.setdefault(d['model'], {}).setdefault(d['rev'], []).append(d['dev'])
        added = []
        for model, revs in sorted(mine.items()):
            others = cluster.get(model)
            if not others or set(revs) <= set(others):
                continue
            added.append({
                'check': 'firmware',
                'status': 'warn',
                'target': model,
                'message': 'disk firmware differs from the other hosts',
                'value': 'this host: %s; other hosts: %s' % (
                    '; '.join('%s (%s)' % (r, ','.join(d)) for r, d in sorted(revs.items())),
                    '; '.join('%s (%d hosts)' % (r, len(h)) for r, h in sorted(others.items())),
                ),
            })
        report['checks'].extend(added)
        summary = report.setdefault('summary', {})
        summary['warn'] = summary.get('warn', 0) + len(added)

    def record(self, host: str, report: Optional[Dict[str, Any]], error: str = '') -> None:
        entry: Dict[str, Any] = {'last': datetime_to_str(datetime_now())}
        if report is not None:
            entry['problems'] = problems(report, 'warn')
            entry['fail'] = problems(report, 'fail')
            entry['summary'] = report.get('summary', {})
        if error:
            entry['error'] = error
        self.results[host] = entry
        self.save()

    def needs_run(self, host: str) -> bool:
        interval = int(self.mgr.host_precheck_interval or 0)
        if interval <= 0:
            return False
        last = self.results.get(host, {}).get('last')
        if not last:
            return True
        try:
            age = datetime_now() - str_to_datetime(last)
        except ValueError:
            return True
        return age > datetime.timedelta(seconds=interval)

    def run_periodic(self, host: str) -> None:
        try:
            report = self.run(
                host,
                addr=self.mgr.inventory.get_addr(host),
                max_peers=int(self.mgr.host_precheck_max_peers or 0) or None,
            )
        except Exception as e:
            logger.info('host-precheck of %s failed: %s', host, e)
            self.record(host, None, str(e))
            return
        self.record(host, report)

    def update_health(self) -> None:
        level = self.mgr.host_precheck_health_level
        detail: List[str] = []
        hosts = 0
        for host in sorted(self.results):
            if host not in self.mgr.inventory:
                continue
            entry = self.results[host]
            probs = entry.get('fail' if level == 'fail' else 'problems', [])
            if entry.get('error'):
                probs = ['host-precheck could not run: %s' % entry['error']]
            if not probs:
                continue
            hosts += 1
            for p in probs[:MAX_DETAIL_PER_HOST]:
                detail.append('host %s: %s' % (host, p))
            if len(probs) > MAX_DETAIL_PER_HOST:
                detail.append('host %s: ... and %d more' % (
                    host, len(probs) - MAX_DETAIL_PER_HOST))
        if hosts and int(self.mgr.host_precheck_interval or 0) > 0:
            self.mgr.set_health_warning(
                HEALTH_CHECK,
                '%d host(s) have host-precheck problems' % hosts,
                hosts,
                detail)
        else:
            self.mgr.remove_health_warning(HEALTH_CHECK)
