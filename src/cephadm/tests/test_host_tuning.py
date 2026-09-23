from unittest import mock

import pytest

from tests.fixtures import import_cephadm

from cephadmlib import host_tuning as ht

_cephadm = import_cephadm()

GiB = 1024**3


def _by(results, check, target=None):
    return [
        r
        for r in results
        if r['check'] == check and (target is None or r['target'] == target)
    ]


def _add_disk(fs, dev, sectors, rotational, scheduler, parts=(), holders=()):
    base = '/sys/block/%s' % dev
    fs.create_file(base + '/dev', contents='8:0')
    fs.create_file(base + '/size', contents=str(sectors))
    fs.create_file(base + '/removable', contents='0')
    fs.create_file(base + '/ro', contents='0')
    fs.create_file(base + '/queue/rotational', contents=str(int(rotational)))
    fs.create_file(base + '/queue/scheduler', contents=scheduler)
    fs.create_file(base + '/device/model', contents='Model-%s' % dev)
    fs.create_dir(base + '/holders')
    for p in parts:
        fs.create_file('%s/%s/partition' % (base, p), contents='1')
        fs.create_dir('%s/%s/holders' % (base, p))
    for h in holders:
        fs.create_dir('%s/holders/%s' % (base, h))
        fs.create_file('/sys/block/%s/dev' % h, contents='253:0')
        fs.create_dir('/sys/block/%s/holders' % h)


@pytest.fixture
def host(fs):
    """sda: OS disk, sdb: hdd, sdc: hdd with LVM, nvme0n1: flash."""
    tb = 2 * 1024**4 // 512
    _add_disk(fs, 'sda', tb, True, '[mq-deadline] none', parts=['sda1', 'sda2'])
    _add_disk(fs, 'sdb', tb, True, 'mq-deadline [bfq] none')
    _add_disk(fs, 'sdc', tb, True, '[mq-deadline] none', holders=['dm-0'])
    _add_disk(fs, 'nvme0n1', tb, False, '[none] mq-deadline')
    fs.create_file('/sys/block/sdb/queue/write_cache', contents='write back')
    # a tiny device that must not count as an OSD candidate
    _add_disk(fs, 'sdd', 1024, False, '[none]')
    fs.create_file(
        '/proc/mounts',
        contents='/dev/sda2 / xfs rw 0 0\n'
        '/dev/sda1 /boot xfs rw 0 0\n'
        'proc /proc proc rw 0 0\n',
    )
    fs.create_file(
        '/proc/swaps', contents='Filename Type Size Used Priority\n'
    )
    fs.create_file(
        '/proc/meminfo',
        contents='MemTotal:       16384000 kB\n'
        'MemAvailable:   12000000 kB\n',
    )
    yield fs


class TestDevices:
    def test_os_devices(self, host):
        assert ht.os_devices() == {'sda'}

    def test_data_devices(self, host):
        devs = {d['dev']: d for d in ht.data_devices()}
        assert sorted(devs) == ['nvme0n1', 'sdb', 'sdc']
        assert devs['sdb']['rotational']
        assert not devs['nvme0n1']['rotational']
        assert devs['sdb']['in_use'] == []
        assert devs['sdc']['in_use'] == ['has holders: dm-0']

    def test_device_usage(self, host):
        reasons = ht.device_usage('sda')
        assert 'sda2 mounted at /' in reasons
        assert 'has partitions: sda1,sda2' in reasons

    def test_mounted_dm_holder(self, host):
        host.create_file('/dev/dm-0')
        with open('/proc/mounts', 'a') as f:
            f.write('/dev/dm-0 /srv xfs rw 0 0\n')
        assert 'dm-0 mounted at /srv' in ht.device_usage('sdc')


class TestChecks:
    def test_sysctls(self, host):
        vals = {
            'fs.aio-max-nr': '65536',
            'kernel.pid_max': '4194304',
            'vm.swappiness': '60',
            'net.core.netdev_max_backlog': '1000',
            'net.ipv4.tcp_rmem': '4096\t131072\t6291456',
            'net.ipv4.tcp_wmem': '4096\t16384\t33554432',
        }
        for k, v in vals.items():
            host.create_file('/proc/sys/' + k.replace('.', '/'), contents=v)
        res = {r['target']: r for r in ht.check_sysctls(None)}
        # managed by cephadm: informational only
        assert res['fs.aio-max-nr']['status'] == ht.STATUS_INFO
        assert res['kernel.pid_max']['status'] == ht.STATUS_OK
        assert res['vm.swappiness']['status'] == ht.STATUS_WARN
        # distribution defaults that are only worth raising on fast hosts
        assert res['net.ipv4.tcp_rmem']['status'] == ht.STATUS_INFO
        assert res['net.ipv4.tcp_rmem']['value'] == 6291456
        assert res['net.ipv4.tcp_wmem']['status'] == ht.STATUS_OK
        assert res['net.core.netdev_max_backlog']['status'] == ht.STATUS_INFO
        assert res['net.core.somaxconn']['message'] == 'not present'
        assert 'kernel.threads-max' not in res

    @pytest.mark.parametrize(
        'thp, status',
        [
            ('[always] madvise never', ht.STATUS_WARN),
            ('always [madvise] never', ht.STATUS_OK),
            ('always madvise [never]', ht.STATUS_OK),
        ],
    )
    def test_thp(self, host, thp, status):
        host.create_file(ht.THP_PATH, contents=thp)
        assert ht.check_thp(None)[0]['status'] == status

    @pytest.mark.parametrize(
        'out, code, status',
        [
            ('Current active profile: throughput-performance\n', 0, ht.STATUS_OK),
            ('Current active profile: balanced\n', 0, ht.STATUS_WARN),
            ('Current active profile: my-ceph\n', 0, ht.STATUS_INFO),
            ('No current active profile.\n', 1, ht.STATUS_WARN),
        ],
    )
    def test_tuned(self, out, code, status):
        with mock.patch.object(ht, 'find_executable', return_value='x'), \
                mock.patch.object(ht, 'call', return_value=(out, '', code)):
            assert ht.check_tuned(None)[0]['status'] == status

    def test_tuned_missing(self):
        with mock.patch.object(ht, 'find_executable', return_value=None):
            assert ht.check_tuned(None)[0]['status'] == ht.STATUS_INFO

    def test_cpu_governor(self, host):
        for i, gov in enumerate(['performance', 'powersave', 'powersave']):
            host.create_file(
                '/sys/devices/system/cpu/cpu%d/cpufreq/scaling_governor' % i,
                contents=gov,
            )
        r = ht.check_cpu_governor(None)[0]
        assert r['status'] == ht.STATUS_WARN
        assert r['value'] == 'performance(1 cpus),powersave(2 cpus)'

    def test_cpu_governor_absent(self, host):
        assert ht.check_cpu_governor(None)[0]['status'] == ht.STATUS_INFO

    def test_block_devices(self, host):
        res = ht.check_block_devices(None)
        sched = {r['target']: r for r in _by(res, 'io_scheduler')}
        assert sorted(sched) == ['nvme0n1', 'sdb', 'sdc']
        assert sched['sdb']['status'] == ht.STATUS_WARN
        assert sched['sdb']['value'] == 'bfq'
        assert sched['sdc']['status'] == ht.STATUS_OK
        assert sched['nvme0n1']['status'] == ht.STATUS_OK
        assert _by(res, 'write_cache', 'sdb')
        assert _by(res, 'disk_available', 'sdc')

    def test_sizing(self, host):
        with mock.patch('os.cpu_count', return_value=4):
            res = {r['check']: r for r in ht.check_sizing(None)}
        # 3 data disks * 4 GiB + 8 GiB base > 15 GiB of RAM
        assert res['memory']['status'] == ht.STATUS_WARN
        assert res['memory']['expected'] == '>= 20 GiB'
        # 2 + 2 hdd + 2 for one flash device
        assert res['cpu']['status'] == ht.STATUS_WARN
        assert res['cpu']['expected'] == '>= 6 threads'

    def test_sizing_skips_mounted_disks(self, host):
        # sdb holds /srv: not an OSD candidate. sdc's LVM may be an OSD.
        host.create_file('/dev/sdb')
        with open('/proc/mounts', 'a') as f:
            f.write('/dev/sdb /srv xfs rw 0 0\n')
        with mock.patch('os.cpu_count', return_value=4):
            res = {r['check']: r for r in ht.check_sizing(None)}
        assert res['memory']['message'].startswith('2 data devices')
        assert res['cpu']['message'] == '1 hdd and 1 flash data devices'

    @pytest.mark.parametrize(
        'mem_kb, status',
        [
            # a 16 GiB host reports a little less than 16 GiB in MemTotal
            (16 * 1024 * 1024 - 400 * 1024, ht.STATUS_OK),
            (14 * 1024 * 1024, ht.STATUS_WARN),
        ],
    )
    def test_sizing_memory_tolerance(self, host, mem_kb, status):
        # one data disk: 8 GiB base + 4 GiB, so ask for 16 via two disks
        host.remove_object('/sys/block/sdc')
        host.remove_object('/sys/block/nvme0n1')
        with open('/proc/meminfo', 'w') as f:
            f.write('MemTotal: %d kB\n' % mem_kb)
        with mock.patch.object(ht, 'MEM_BASE', 12 * GiB):
            res = {r['check']: r for r in ht.check_sizing(None)}
        assert res['memory']['expected'] == '>= 16 GiB'
        assert res['memory']['status'] == status

    @pytest.mark.parametrize(
        'swaps, status, value',
        [
            ('', ht.STATUS_OK, None),
            ('/dev/zram0 partition 8388604 0 100\n', ht.STATUS_OK, '/dev/zram0'),
            (
                '/dev/zram0 partition 8388604 0 100\n'
                '/dev/sda3 partition 8388604 0 -2\n',
                ht.STATUS_INFO,
                '/dev/sda3',
            ),
        ],
    )
    def test_swap(self, host, swaps, status, value):
        with open('/proc/swaps', 'a') as f:
            f.write(swaps)
        r = ht.check_swap(None)[0]
        assert r['status'] == status
        assert r.get('value') == value


def _add_nic(fs, name, mtu=1500, state='up', speed=None, physical=True,
             duplex='full', lower=()):
    base = '/sys/class/net/%s' % name
    fs.create_file(base + '/mtu', contents=str(mtu))
    fs.create_file(base + '/operstate', contents=state)
    if speed is not None:
        fs.create_file(base + '/speed', contents=str(speed))
        fs.create_file(base + '/duplex', contents=duplex)
    if physical:
        fs.create_dir(base + '/device')
    for low in lower:
        fs.create_dir(base + '/lower_' + low)
    return base


class TestNetwork:
    def test_bond_and_mtu(self, fs):
        _add_nic(fs, 'lo', mtu=65536)
        _add_nic(fs, 'eno1', mtu=9000, speed=25000)
        _add_nic(fs, 'eno2', mtu=1500, speed=1000, duplex='half', state='down')
        _add_nic(fs, 'veth1234', mtu=1500, physical=False)
        bond = _add_nic(fs, 'bond0', mtu=9000, physical=False,
                        lower=['eno1', 'eno2'])
        fs.create_file(bond + '/bonding/mode', contents='802.3ad 4')

        res = ht.check_network(None)
        assert _by(res, 'nic_speed', 'eno1')[0]['status'] == ht.STATUS_OK
        # eno2 is down, so its speed and duplex are not judged
        assert not _by(res, 'nic_speed', 'eno2')
        assert not _by(res, 'nic_duplex')
        bond_r = _by(res, 'bond', 'bond0')[0]
        assert bond_r['status'] == ht.STATUS_WARN
        assert 'eno2' in bond_r['message']
        mtu = _by(res, 'mtu')
        assert [r['target'] for r in mtu] == ['eno2']
        assert not [r for r in res if r['target'].startswith(('lo', 'veth'))]

    def test_slow_nic(self, fs):
        _add_nic(fs, 'eth0', speed=1000, duplex='half')
        res = ht.check_network(None)
        assert _by(res, 'nic_speed', 'eth0')[0]['status'] == ht.STATUS_WARN
        assert _by(res, 'nic_duplex', 'eth0')[0]['status'] == ht.STATUS_WARN

    def test_ceph_networks(self, fs):
        _add_nic(fs, 'eth0', mtu=9000, speed=25000)
        nets = {'10.0.0.0/24': {'eth0': ['10.0.0.5']}}
        with mock.patch(
            'cephadmlib.host_facts.list_networks', return_value=nets
        ):
            res = ht.check_network(
                None, ['10.0.0.0/24', '192.168.0.0/16', 'bogus']
            )
        by_net = {r['target']: r for r in _by(res, 'ceph_network')}
        assert by_net['10.0.0.0/24']['status'] == ht.STATUS_OK
        assert by_net['10.0.0.0/24']['value'] == 'mtu 9000'
        assert by_net['192.168.0.0/16']['status'] == ht.STATUS_FAIL
        assert by_net['bogus']['status'] == ht.STATUS_FAIL


class TestReport:
    def test_run_checks_isolates_failures(self, host):
        def boom(ctx):
            raise RuntimeError('kaboom')

        with mock.patch.object(ht, 'CHECKS', [('thp', boom)]):
            rep = ht.run_checks(None, skip=['network'])
        assert rep['checks'] == [
            {
                'check': 'thp',
                'status': ht.STATUS_INFO,
                'target': '',
                'message': 'check could not run: kaboom',
            }
        ]
        assert rep['summary'][ht.STATUS_INFO] == 1
        assert ht.format_report(rep).endswith('0 ok, 1 info, 0 warn, 0 fail')

    def test_command(self, host, capsys):
        report = {
            'summary': {'ok': 0, 'info': 0, 'warn': 1, 'fail': 0},
            'checks': [ht._result('thp', ht.STATUS_WARN, 'x')],
        }
        with mock.patch('cephadm.run_tuning_checks', return_value=report):
            ctx = _cephadm.cephadm_init_ctx(['host-precheck', '--format', 'json'])
            assert _cephadm.command_host_precheck(ctx) == 0
            ctx = _cephadm.cephadm_init_ctx(['host-precheck', '--fail-on-warn'])
            assert _cephadm.command_host_precheck(ctx) == 1


class TestKernel:
    EPYC = 'processor : 0\nvendor_id : AuthenticAMD\nmodel name : AMD EPYC 9454P 48-Core Processor\n'
    XEON = 'processor : 0\nvendor_id : GenuineIntel\nmodel name : Intel(R) Xeon(R) Gold 6338\n'

    def _host(self, fs, cpuinfo, cmdline, iommu=True, vulns=None):
        fs.create_file('/proc/cpuinfo', contents=cpuinfo)
        fs.create_file('/proc/cmdline', contents=cmdline)
        if iommu:
            fs.create_dir('/sys/class/iommu/ivhd0')
        for name, state in (vulns or {}).items():
            fs.create_file(
                '/sys/devices/system/cpu/vulnerabilities/' + name, contents=state)

    @pytest.mark.parametrize('cmdline, iommu, status', [
        ('ro quiet', True, ht.STATUS_WARN),
        ('ro amd_iommu=off', True, ht.STATUS_OK),
        ('ro iommu=off', True, ht.STATUS_OK),
        ('ro iommu=pt', True, ht.STATUS_INFO),
        ('ro quiet', False, ht.STATUS_OK),
    ])
    def test_epyc_iommu(self, fs, cmdline, iommu, status):
        self._host(fs, self.EPYC, cmdline, iommu)
        assert _by(ht.check_kernel(None), 'iommu')[0]['status'] == status

    def test_iommu_only_checked_on_epyc(self, fs):
        self._host(fs, self.XEON, 'ro quiet')
        assert not _by(ht.check_kernel(None), 'iommu')

    def test_mitigations(self, fs):
        self._host(fs, self.XEON, 'ro quiet', vulns={
            'spectre_v2': 'Mitigation: Enhanced / Automatic IBRS',
            'meltdown': 'Not affected',
            'retbleed': 'Mitigation: untrained return thunk',
        })
        r = _by(ht.check_kernel(None), 'cpu_mitigations')[0]
        assert r['status'] == ht.STATUS_INFO
        assert r['value'] == '2 mitigated: retbleed,spectre_v2'
        assert 'hypervisors' in r['message']

    def test_mitigations_off(self, fs):
        self._host(fs, self.XEON, 'ro mitigations=off',
                   vulns={'spectre_v2': 'Vulnerable'})
        assert _by(ht.check_kernel(None), 'cpu_mitigations')[0]['status'] == ht.STATUS_OK
