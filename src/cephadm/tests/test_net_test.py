import json

from unittest import mock

import pytest

from tests.fixtures import import_cephadm

from cephadmlib import net_test
from cephadmlib.exceptions import Error

_cephadm = import_cephadm()

PING_OK = """PING 10.0.0.2 (10.0.0.2) 56(84) bytes of data.

--- 10.0.0.2 ping statistics ---
3 packets transmitted, 3 received, 0% packet loss, time 402ms
rtt min/avg/max/mdev = 0.101/0.155/0.201/0.041 ms
"""
PING_FRAG = """PING 10.0.0.2 (10.0.0.2) 8972(9000) bytes of data.
ping: local error: message too long, mtu=1500

--- 10.0.0.2 ping statistics ---
3 packets transmitted, 0 received, +3 errors, 100% packet loss, time 405ms
"""


class TestPing:
    def test_ping_peer(self):
        calls = []

        def fake_call(ctx, cmd, **kw):
            calls.append(cmd)
            if cmd[0] == 'ip':
                return (json.dumps([{'dst': '10.0.0.2', 'dev': 'bond0'}]), '', 0)
            if '-M' in cmd:
                return (PING_FRAG, '', 1)
            return (PING_OK, '', 0)

        with mock.patch.object(net_test, 'find_executable', return_value='x'), \
                mock.patch.object(net_test, 'call', side_effect=fake_call), \
                mock.patch.object(net_test, 'iface_mtu', return_value=9000):
            r = net_test.ping_peer(None, 'host2=10.0.0.2')
        assert (r['peer'], r['iface'], r['mtu']) == ('host2', 'bond0', 9000)
        assert r['small'] == {'ok': True, 'loss_pct': 0.0, 'rtt_avg_ms': 0.155,
                              'rtt_max_ms': 0.201}
        assert r['full_mtu'] == {
            'ok': False, 'loss_pct': 100.0,
            'error': 'ping: local error: message too long, mtu=1500'}
        big = [c for c in calls if '-M' in c][0]
        assert big[big.index('-s') + 1] == '8972'

    def test_ipv6_payload(self):
        with mock.patch.object(net_test, 'route_iface', return_value='eth0'), \
                mock.patch.object(net_test, 'iface_mtu', return_value=9000), \
                mock.patch.object(net_test, '_ping', return_value={'ok': True}) as p:
            net_test.ping_peer(None, 'fd00::2')
        assert p.call_args_list[1][0][2] == 9000 - 48

    def test_bad_address(self):
        assert 'error' in net_test.ping_peer(None, 'host2=not-an-ip')


def _iperf_json(fwd, rev=None, retr=0):
    end = {'sum_sent': {'bits_per_second': fwd * 1e9, 'retransmits': retr},
           'sum_received': {'bits_per_second': fwd * 1e9}}
    if rev is not None:
        end['sum_sent_bidir_reverse'] = {'bits_per_second': rev * 1e9, 'retransmits': 2}
        end['sum_received_bidir_reverse'] = {'bits_per_second': rev * 1e9}
    return json.dumps({'end': end})


class TestIperf:
    def _client(self, outputs, bidir=True, counters=({}, {})):
        seq = list(outputs)
        cmds = []

        def fake_call(ctx, cmd, **kw):
            cmds.append(cmd)
            if cmd[:2] == ['iperf3', '--help']:
                return ('--bidir  run in bidirectional mode' if bidir else '', '', 0)
            return (seq.pop(0), '', 0)

        nic = iter(counters)
        with mock.patch.object(net_test, 'find_executable', return_value='x'), \
                mock.patch.object(net_test, 'call', side_effect=fake_call), \
                mock.patch.object(net_test, 'route_iface', return_value='eth0'), \
                mock.patch.object(net_test, 'iface_speed', return_value=25000), \
                mock.patch.object(net_test.host_hw, 'nic_counters', side_effect=lambda i: next(nic)), \
                mock.patch.object(net_test.time, 'sleep'):
            return net_test.iperf_client(None, 'host2=10.0.0.2', 5201, 5, 4), cmds

    def test_bidir(self):
        r, cmds = self._client([_iperf_json(23.1, 11.0, retr=40)],
                               counters=({'rx_crc_errors': 1}, {'rx_crc_errors': 4}))
        assert r['mode'] == 'bidir'
        assert (r['to_peer_gbps'], r['from_peer_gbps']) == (23.1, 11.0)
        assert (r['to_peer_retransmits'], r['from_peer_retransmits']) == (40, 2)
        assert '--bidir' in cmds[-1] and '-P' in cmds[-1]
        assert r['problems'] == [
            '11.00 Gb/s from host2 is below 70% of the 25 Gb/s link',
            'rx_crc_errors increased by 3 during the test',
        ]

    def test_old_iperf_runs_each_direction(self):
        r, cmds = self._client([_iperf_json(24.0), _iperf_json(23.5)], bidir=False)
        assert r['mode'] == 'sequential'
        assert (r['to_peer_gbps'], r['from_peer_gbps']) == (24.0, 23.5)
        assert '-R' in cmds[-1] and r['problems'] == []

    def test_retries_until_server_is_up(self):
        refused = json.dumps({'error': 'unable to connect to server: Connection refused'})
        r, _ = self._client([refused, refused, _iperf_json(24.0, 24.0)])
        assert r['to_peer_gbps'] == 24.0 and 'error' not in r

    def test_error(self):
        r, _ = self._client([json.dumps({'error': 'unable to connect: No route to host'})])
        assert r['error'].startswith('unable to connect')

    def test_server(self):
        calls = []

        def fake_call(ctx, cmd, **kw):
            calls.append(cmd)
            if cmd[:2] == ['systemctl', 'is-active']:
                return ('', '', 3)
            return ('', '', 0)

        with mock.patch.object(net_test, 'find_executable', side_effect=lambda n: '/usr/bin/' + n), \
                mock.patch.object(net_test, 'call', side_effect=fake_call):
            r = net_test.iperf_server(None, 5201, 120)
        assert r['firewall_opened'] == 'firewalld'
        assert ['firewall-cmd', '--add-port=5201/tcp', '--timeout=120s'] in calls
        run = [c for c in calls if c[0] == 'systemd-run'][0]
        assert 'RuntimeMaxSec=120' in run
        assert run[-3:] == ['-s', '-p', '5201']

    def _ufw_server(self, status, run_code=0):
        calls = []

        def fake_call(ctx, cmd, **kw):
            calls.append(cmd)
            if cmd[:2] == ['systemctl', 'is-active']:
                return ('', '', 3)
            if cmd[-1] == 'status':
                return (status, '', 0)
            if cmd[0] == 'systemd-run':
                return ('', 'boom', run_code)
            return ('', '', 0)

        tools = {'iperf3': '/usr/bin/iperf3', 'systemd-run': '/usr/bin/systemd-run',
                 'ufw': '/usr/sbin/ufw'}
        with mock.patch.object(net_test, 'find_executable', side_effect=tools.get), \
                mock.patch.object(net_test, 'call', side_effect=fake_call):
            try:
                return net_test.iperf_server(None, 5201, 120), calls
            except Error:
                return None, calls

    def test_server_ufw(self):
        r, calls = self._ufw_server('Status: active\n\nTo  Action  From\n22/tcp  ALLOW  Anywhere\n')
        assert r['firewall_opened'] == 'ufw'
        assert ['/usr/sbin/ufw', 'allow', '5201/tcp', 'comment', 'ceph net-test'] in calls
        run = [c for c in calls if c[0] == 'systemd-run'][0]
        assert 'ExecStopPost=/usr/sbin/ufw delete allow 5201/tcp' in run

    def test_server_ufw_keeps_existing_rule(self):
        r, calls = self._ufw_server('Status: active\n\n5201/tcp  ALLOW  Anywhere\n')
        assert r['firewall_opened'] is False
        assert not [c for c in calls if 'allow' in c]
        assert not [c for c in calls if any('ExecStopPost' in a for a in c)]

    def test_server_ufw_inactive(self):
        r, calls = self._ufw_server('Status: inactive\n')
        assert r['firewall_opened'] is False

    def test_server_ufw_cleanup_when_start_fails(self):
        r, calls = self._ufw_server('Status: active\n', run_code=1)
        assert r is None
        assert calls[-1] == ['ufw', 'delete', 'allow', '5201/tcp']

    def test_server_needs_iperf3(self):
        with mock.patch.object(net_test, 'find_executable', return_value=None):
            with pytest.raises(Error, match='iperf3 is not installed'):
                net_test.iperf_server(None, 5201, 60)

    def test_command_json(self, capsys):
        ctx = _cephadm.cephadm_init_ctx(['net-test', 'iperf-client', '--peer', 'h=1.2.3.4'])
        with mock.patch.object(net_test, 'iperf_client', return_value={'error': 'x'}):
            assert _cephadm.command_net_test(ctx) == 1
        assert json.loads(capsys.readouterr().out) == {'error': 'x'}
