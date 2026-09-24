import json

import pytest

from cephadm.serve import CephadmServe
from orchestrator import HostSpec, OrchestratorError

from .fixtures import wait, with_host, async_side_effect
from tests import mock


REPORT = {
    'summary': {'ok': 1, 'info': 0, 'warn': 1, 'fail': 0},
    'checks': [
        {'check': 'thp', 'status': 'ok', 'target': '', 'message': 'transparent hugepages',
         'value': 'madvise'},
        {'check': 'io_scheduler', 'status': 'warn', 'target': 'sdb', 'message': 'hdd',
         'value': 'bfq', 'expected': 'mq-deadline'},
    ],
}
CLEAN = {
    'summary': {'ok': 1, 'info': 0, 'warn': 0, 'fail': 0},
    'checks': REPORT['checks'][:1],
}


def _fake_cephadm(report, calls=None):
    async def run(self, host, entity, cmd, args, **kwargs):
        if calls is not None:
            calls.append((cmd, list(args), kwargs.get('addr')))
        if cmd == 'host-precheck':
            return [json.dumps(report)], [''], 0
        if cmd == 'gather-facts':
            return ['{}'], [''], 0
        if cmd == 'burnin':
            if args[0] == 'status':
                return [json.dumps({'state': 'running'})], [''], 0
            if args[0] == 'list':
                return ['[]'], [''], 0
            return ['started'], [''], 0
        return ['[]'], [''], 0
    return run


def _add(cephadm_module, name='test'):
    with mock.patch('cephadm.utils.resolve_ip', return_value='1::4'):
        return wait(cephadm_module, cephadm_module.add_host(HostSpec(hostname=name)))


class TestHostPrecheckOnAdd:

    def test_warn_reports_problems(self, cephadm_module):
        with mock.patch.object(CephadmServe, '_run_cephadm', _fake_cephadm(REPORT)):
            msg = _add(cephadm_module)
        assert msg.startswith("Added host 'test' with addr '1::4'\n")
        assert 'host-precheck found 1 issue(s)' in msg
        assert 'WARN io_scheduler sdb: hdd (value: bfq, expected: mq-deadline)' in msg
        assert 'test' in cephadm_module.inventory

    def test_clean_host(self, cephadm_module):
        with mock.patch.object(CephadmServe, '_run_cephadm', _fake_cephadm(CLEAN)):
            assert _add(cephadm_module) == "Added host 'test' with addr '1::4'"

    def test_enforce_refuses(self, cephadm_module):
        cephadm_module.host_precheck_on_add = 'enforce'
        with mock.patch.object(CephadmServe, '_run_cephadm', _fake_cephadm(REPORT)):
            with pytest.raises(OrchestratorError, match='failed host-precheck'):
                _add(cephadm_module)
        assert 'test' not in cephadm_module.inventory

    def test_enforce_refuses_when_precheck_cannot_run(self, cephadm_module):
        cephadm_module.host_precheck_on_add = 'enforce'
        with mock.patch.object(CephadmServe, '_run_cephadm', _fake_cephadm('garbage')):
            with pytest.raises(OrchestratorError, match='host-precheck of test failed'):
                _add(cephadm_module)

    def test_warn_tolerates_precheck_failure(self, cephadm_module):
        with mock.patch.object(CephadmServe, '_run_cephadm', _fake_cephadm('garbage')):
            msg = _add(cephadm_module)
        assert 'host-precheck could not run' in msg
        assert 'test' in cephadm_module.inventory

    def test_off(self, cephadm_module):
        cephadm_module.host_precheck_on_add = 'off'
        calls = []
        with mock.patch.object(CephadmServe, '_run_cephadm', _fake_cephadm(REPORT, calls)):
            assert _add(cephadm_module) == "Added host 'test' with addr '1::4'"
        assert 'host-precheck' not in [c[0] for c in calls]

    def test_readd_skips_precheck(self, cephadm_module):
        cephadm_module.host_precheck_on_add = 'enforce'
        with mock.patch.object(CephadmServe, '_run_cephadm', _fake_cephadm(CLEAN)):
            _add(cephadm_module)
        with mock.patch.object(CephadmServe, '_run_cephadm', _fake_cephadm(REPORT)):
            assert _add(cephadm_module) == "Added host 'test' with addr '1::4'"

    def test_passes_ceph_networks(self, cephadm_module):
        calls = []
        opts = {'public_network': '10.0.0.0/24, 10.1.0.0/24', 'cluster_network': ''}
        with mock.patch.object(CephadmServe, '_run_cephadm', _fake_cephadm(CLEAN, calls)), \
                mock.patch.object(cephadm_module, 'get_foreign_ceph_option',
                                  side_effect=lambda _e, k: opts[k]):
            _add(cephadm_module)
        args = [c[1] for c in calls if c[0] == 'host-precheck'][0]
        assert args == ['--format', 'json',
                        '--network', '10.0.0.0/24', '--network', '10.1.0.0/24']


class TestHostPrecheckCommand:

    def test_plain(self, cephadm_module):
        with mock.patch.object(CephadmServe, '_run_cephadm', _fake_cephadm(REPORT)):
            r = cephadm_module.host_precheck('newhost')
        assert r.retval == 0
        assert 'io_scheduler' in r.stdout and 'mq-deadline' in r.stdout
        assert r.stdout.endswith('1 ok, 0 info, 1 warn, 0 fail')

    def test_json(self, cephadm_module):
        from orchestrator.module import Format
        with mock.patch.object(CephadmServe, '_run_cephadm', _fake_cephadm(REPORT)):
            r = cephadm_module.host_precheck('newhost', format=Format.json)
        assert json.loads(r.stdout) == REPORT

    def test_error(self, cephadm_module):
        with mock.patch.object(CephadmServe, '_run_cephadm',
                               side_effect=async_side_effect(([''], ['ERROR: boom'], 1))):
            r = cephadm_module.host_precheck('newhost')
        assert r.retval == 1
        assert 'boom' in r.stderr


class TestBurnin:

    def test_start_args(self, cephadm_module):
        calls = []
        with mock.patch.object(CephadmServe, '_run_cephadm', _fake_cephadm(CLEAN, calls)):
            with with_host(cephadm_module, 'test', refresh_hosts=False):
                calls.clear()
                r = cephadm_module.burnin_start('test', duration=60, devices=['/dev/sdb'])
                assert r.retval == 0 and r.stdout == 'started'
        assert calls[0] == (
            'burnin',
            ['start', '--duration', '60', '--memory-percent', '70', '--devices', '/dev/sdb'],
            '1::4',
        )

    def test_start_bench_and_from_start(self, cephadm_module):
        calls = []
        with mock.patch.object(CephadmServe, '_run_cephadm', _fake_cephadm(CLEAN, calls)):
            cephadm_module.burnin_start('newhost', disk_bench_seconds=0, from_start=True)
        assert calls[0][1][-3:] == ['--disk-bench-seconds', '0', '--from-start']

    def test_destructive_needs_confirmation(self, cephadm_module):
        calls = []
        with mock.patch.object(CephadmServe, '_run_cephadm', _fake_cephadm(CLEAN, calls)):
            r = cephadm_module.burnin_start('newhost', destructive=True)
            assert r.retval != 0 and 'yes-i-really-mean-it' in r.stderr
            r = cephadm_module.burnin_start('newhost', destructive=True,
                                            yes_i_really_mean_it=True,
                                            all_available_devices=True)
            assert r.retval == 0
        # a host that is not managed yet is reached by its name
        assert calls == [(
            'burnin',
            ['start', '--duration', '3600', '--memory-percent', '70',
             '--all-available-devices', '--destructive', '--yes-i-really-mean-it'],
            'newhost',
        )]

    def test_status_and_errors(self, cephadm_module):
        from orchestrator.module import Format
        with mock.patch.object(CephadmServe, '_run_cephadm', _fake_cephadm(CLEAN)):
            r = cephadm_module.burnin_status('newhost', format=Format.json)
        assert json.loads(r.stdout) == {'state': 'running'}
        with mock.patch.object(
            CephadmServe, '_run_cephadm',
            side_effect=async_side_effect(
                ([''], ['INFO: x\nERROR: a burn-in is already running'], 1))):
            r = cephadm_module.burnin_start('newhost')
        assert r.retval == 1
        assert r.stderr == 'a burn-in is already running'

    def test_status_run_id_and_ls(self, cephadm_module):
        from orchestrator.module import Format
        calls = []
        with mock.patch.object(CephadmServe, '_run_cephadm', _fake_cephadm(CLEAN, calls)):
            cephadm_module.burnin_status('newhost', run_id='20260101T000000Z')
            cephadm_module.burnin_ls('newhost')
            r = cephadm_module.burnin_ls('newhost', format=Format.yaml)
        assert [c[1] for c in calls] == [
            ['status', '--run-id', '20260101T000000Z'],
            ['list'],
            ['list', '--format', 'json'],
        ]
        assert r.retval == 0


FAIL_REPORT = {
    'summary': {'ok': 0, 'info': 0, 'warn': 1, 'fail': 1},
    'checks': REPORT['checks'][1:] + [
        {'check': 'smart', 'status': 'fail', 'target': 'sdc', 'message': '2 pending sectors'}],
    'inventory': {'disks': [{'dev': 'sdb', 'model': 'WD140EDGZ', 'rev': '0A85'}]},
}


def _add_quiet(m, name):
    saved = m.host_precheck_on_add
    m.host_precheck_on_add = 'off'
    try:
        with mock.patch.object(CephadmServe, '_run_cephadm', _fake_cephadm(CLEAN)):
            _add(m, name)
    finally:
        m.host_precheck_on_add = saved


class TestPeriodic:

    def test_daily_run_and_health(self, cephadm_module):
        m = cephadm_module
        _add_quiet(m, 'test')
        assert m.precheck.needs_run('test')
        with mock.patch.object(CephadmServe, '_run_cephadm', _fake_cephadm(FAIL_REPORT)):
            m.precheck.run_periodic('test')
        assert not m.precheck.needs_run('test')
        m.precheck.update_health()
        hc = m.health_checks['CEPHADM_HOST_PRECHECK']
        assert hc['count'] == 1
        assert hc['detail'] == [
            'host test: WARN io_scheduler sdb: hdd (value: bfq, expected: mq-deadline)',
            'host test: FAIL smart sdc: 2 pending sectors',
        ]
        # only failures
        m.host_precheck_health_level = 'fail'
        m.precheck.update_health()
        assert m.health_checks['CEPHADM_HOST_PRECHECK']['detail'] == [
            'host test: FAIL smart sdc: 2 pending sectors']
        # a clean run clears it
        with mock.patch.object(CephadmServe, '_run_cephadm', _fake_cephadm(CLEAN)):
            m.precheck.run_periodic('test')
        m.precheck.update_health()
        assert 'CEPHADM_HOST_PRECHECK' not in m.health_checks

    def test_interval(self, cephadm_module):
        m = cephadm_module
        _add_quiet(m, 'test')
        m.precheck.record('test', CLEAN)
        m.host_precheck_interval = 1
        m.precheck.results['test']['last'] = '2020-01-01T00:00:00.000000Z'
        assert m.precheck.needs_run('test')
        m.host_precheck_interval = 0
        assert not m.precheck.needs_run('test')

    def test_error_is_reported(self, cephadm_module):
        m = cephadm_module
        _add_quiet(m, 'test')
        with mock.patch.object(CephadmServe, '_run_cephadm', _fake_cephadm('garbage')):
            m.precheck.run_periodic('test')
        m.precheck.update_health()
        assert m.health_checks['CEPHADM_HOST_PRECHECK']['detail'][0].startswith(
            'host test: host-precheck could not run')

    def test_results_survive_restart(self, cephadm_module):
        m = cephadm_module
        m.precheck.record('test', FAIL_REPORT)
        m.precheck.results = {}
        m.precheck.load()
        assert m.precheck.results['test']['fail'] == ['FAIL smart sdc: 2 pending sectors']

    def test_skip_groups_are_passed(self, cephadm_module):
        m = cephadm_module
        m.host_precheck_skip = 'smart, kernel'
        calls = []
        with mock.patch.object(CephadmServe, '_run_cephadm', _fake_cephadm(CLEAN, calls)):
            m.precheck.run('newhost')
        args = calls[0][1]
        assert args[-4:] == ['--skip', 'smart', '--skip', 'kernel']


class TestPeersAndFirmware:

    def test_peers(self, cephadm_module):
        m = cephadm_module
        for h in ('a', 'b', 'c'):
            _add_quiet(m, h)
        m.cache.networks['b'] = {'10.1.0.0/24': {'eth1': ['10.1.0.2']},
                                 '192.168.0.0/24': {'eth2': ['192.168.0.9']}}
        m.offline_hosts.add('c')
        opts = {'public_network': '1::/64', 'cluster_network': '10.1.0.0/24'}
        with mock.patch.object(m, 'get_foreign_ceph_option', side_effect=lambda _e, k: opts[k]), \
                mock.patch('cephadm.utils.resolve_ip', return_value='1::4'):
            assert m.precheck.peers('a') == ['b=1::4', 'b=10.1.0.2']

    def test_cluster_firmware(self, cephadm_module):
        m = cephadm_module
        m.cache.facts = {
            'h1': {'hdd_list': [{'model': 'WD140EDGZ', 'rev': '0A81'}]},
            'h2': {'hdd_list': [{'model': 'WD140EDGZ', 'rev': '0A81'}],
                   'flash_list': [{'model': 'SAMSUNG', 'rev': 'Unknown'}]},
        }
        report = json.loads(json.dumps(FAIL_REPORT))
        report['inventory']['disks'].append({'dev': 'sdd', 'model': 'WD140EDGZ', 'rev': '0A81'})
        report['inventory']['disks'].append({'dev': 'nvme0n1', 'model': 'SAMSUNG', 'rev': 'X'})
        m.precheck.add_cluster_firmware('new', report)
        added = [c for c in report['checks'] if c['check'] == 'firmware']
        assert added == [{
            'check': 'firmware', 'status': 'warn', 'target': 'WD140EDGZ',
            'message': 'disk firmware differs from the other hosts',
            'value': 'this host: 0A81 (sdd); 0A85 (sdb); other hosts: 0A81 (2 hosts)'}]
        assert report['summary']['warn'] == 2


class TestNetworkBurnin:

    def _fake(self, calls, client=None):
        async def run(self, host, entity, cmd, args, **kwargs):
            calls.append((host, cmd, list(args)))
            if cmd == 'net-test' and args[0] == 'iperf-client':
                return [json.dumps(client or {
                    'peer': 'b', 'addr': '1::5', 'iface': 'eth0', 'link_mbps': 25000,
                    'to_peer_gbps': 23.5, 'from_peer_gbps': 23.1,
                    'to_peer_retransmits': 3, 'from_peer_retransmits': 0,
                    'problems': []})], [''], 0
            if cmd == 'net-test':
                return ['{}'], [''], 0
            if cmd == 'gather-facts':
                return ['{}'], [''], 0
            return ['[]'], [''], 0
        return run

    def test_network(self, cephadm_module):
        m = cephadm_module
        for h in ('a', 'b'):
            _add_quiet(m, h)
        calls = []
        with mock.patch.object(CephadmServe, '_run_cephadm', self._fake(calls)), \
                mock.patch('cephadm.utils.resolve_ip', return_value='1::5'):
            r = m.burnin_network('a', duration=3)
        assert r.retval == 0, r.stderr
        net = [(h, a[0]) for h, c, a in calls if c == 'net-test']
        assert net == [('b', 'iperf-server'), ('a', 'iperf-client'), ('b', 'iperf-stop')]
        assert '23.5 Gb/s' in r.stdout and '0 of 1 tests had problems' in r.stdout

    def test_network_problems(self, cephadm_module):
        m = cephadm_module
        for h in ('a', 'b'):
            _add_quiet(m, h)
        with mock.patch.object(CephadmServe, '_run_cephadm', self._fake([], client={
                'peer': 'b', 'error': 'unable to connect to server: No route to host'})), \
                mock.patch('cephadm.utils.resolve_ip', return_value='1::5'):
            r = m.burnin_network('a')
        assert r.retval == 1 and 'No route to host' in r.stdout

    def test_refuses_host_with_daemons(self, cephadm_module):
        m = cephadm_module
        _add_quiet(m, 'a')
        with mock.patch.object(m.cache, 'get_daemons_by_host',
                               return_value=[mock.Mock(name='d', **{'name.return_value': 'osd.1'})]):
            r = m.burnin_network('a')
        assert r.retval != 0 and 'osd.1' in r.stderr


class TestHostTune:

    def test_args(self, cephadm_module):
        calls = []
        with mock.patch.object(CephadmServe, '_run_cephadm', _fake_cephadm(CLEAN, calls)):
            r = cephadm_module.host_tune('newhost', apply=True, cmdline=True, iommu_off=True)
            assert r.retval == 0
            assert cephadm_module.host_tune('newhost', apply=True, remove=True).retval != 0
        assert calls == [('host-tune', ['--apply', '--cmdline', '--iommu-off'], 'newhost')]
