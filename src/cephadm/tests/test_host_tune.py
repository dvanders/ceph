import os
import subprocess

from unittest import mock

import pytest

from tests.fixtures import import_cephadm

from cephadmlib import host_tune
from cephadmlib.exceptions import Error

_cephadm = import_cephadm()


@pytest.fixture
def host(fs):
    cpu = '/sys/devices/system/cpu/cpu0'
    fs.create_file(cpu + '/cpufreq/scaling_governor', contents='powersave')
    fs.create_file(cpu + '/cpufreq/energy_performance_preference', contents='balance_power')
    for i, (name, lat, dis) in enumerate((('POLL', 0, 0), ('C1', 2, 0), ('C6', 170, 0))):
        base = cpu + '/cpuidle/state%d' % i
        fs.create_file(base + '/name', contents=name)
        fs.create_file(base + '/latency', contents=str(lat))
        fs.create_file(base + '/disable', contents=str(dis))
    fs.create_file('/sys/module/pcie_aspm/parameters/policy', contents='[default] performance')
    fs.create_file('/sys/class/nvme/nvme0/power/pm_qos_latency_tolerance_us', contents='100000')
    fs.create_file('/sys/kernel/mm/transparent_hugepage/enabled', contents='[always] madvise never')
    for n in (0, 1):
        fs.create_dir('/sys/devices/system/node/node%d' % n)
    fs.create_file('/proc/sys/vm/zone_reclaim_mode', contents='1')
    fs.create_file('/proc/sys/kernel/numa_balancing', contents='0')
    fs.create_file('/proc/cmdline', contents='ro quiet pcie_aspm=off')
    fs.create_file('/proc/cpuinfo', contents='model name : AMD EPYC 9454P\n')
    # pyfakefs has no working fchmod
    with mock.patch('os.fchmod'):
        yield fs


def _no_tools(name):
    return None


class TestPlan:
    def test_plan(self, host):
        with mock.patch.object(host_tune, 'find_executable', side_effect=_no_tools), \
                mock.patch.object(host_tune, 'call', return_value=('', '', 1)), \
                mock.patch.object(host_tune.host_hw, 'physical_nics', return_value=[]):
            items = {i['setting']: i for i in host_tune.plan(None, cmdline=True, iommu_off=True)}
        assert items['cpu governor']['current'] == 'powersave' and items['cpu governor']['change']
        assert items['deep C-states']['current'] == 'enabled: C6'
        assert items['NVMe APST']['current'] == 'enabled: nvme0'
        assert items['transparent hugepages']['target'] == 'madvise'
        assert items['sysctl vm.zone_reclaim_mode']['change']
        assert not items['sysctl kernel.numa_balancing']['change']
        cmd = items['kernel command line']
        # pcie_aspm=off is already on the command line
        assert cmd['reboot'] and cmd['current'] == 'missing 4 argument(s)'
        assert 'amd_iommu=off' in cmd['detail']
        assert 'NIC Energy-Efficient Ethernet' not in items  # no NIC has EEE on
        assert items['install smartmontools']['current'] == 'missing'
        assert 'install ipmitool' not in items  # no BMC
        assert items['service tuned']['target'] == 'enabled, active'
        assert items['tuned profile']['current'] == 'not installed'
        text = host_tune.format_plan(list(items.values()))
        assert 'change (reboot)' in text

    def test_iommu_off_needs_amd(self, host):
        with open('/proc/cpuinfo', 'w') as f:
            f.write('model name : Intel(R) Xeon(R) Gold 6338\n')
        with pytest.raises(Error, match='AMD EPYC'):
            host_tune.plan(None, cmdline=True, iommu_off=True)


class TestApply:
    def test_apply_and_remove(self, host):
        calls = []

        def fake_call(ctx, cmd, **kw):
            calls.append(cmd)
            return ('', '', 0)

        with mock.patch.object(host_tune, 'find_executable',
                               side_effect=lambda n: '/usr/sbin/' + n if n == 'grubby' else None), \
                mock.patch.object(host_tune, 'call', side_effect=fake_call):
            done = host_tune.apply(None, cmdline=True, mitigations_off=True,
                                   packages=False)
        assert all(d['ok'] for d in done), done
        for path in host_tune.MANAGED_FILES:
            # the modules file only on hosts with a BMC
            assert os.path.exists(path) == (path != host_tune.MODULES_FILE)
        assert [host_tune.SCRIPT] in calls
        assert ['systemctl', 'enable', host_tune.UNIT] in calls
        assert ['sysctl', '-p', host_tune.SYSCTL_FILE] in calls
        grubby = [c for c in calls if c[0] == 'grubby'][0]
        # already present on the command line: not added again
        assert grubby[2] == ('--args=processor.max_cstate=1 intel_idle.max_cstate=0 '
                             'nvme_core.default_ps_max_latency_us=0 mitigations=off')
        with open(host_tune.SYSCTL_FILE) as f:
            assert 'vm.zone_reclaim_mode = 0' in f.read()

        with mock.patch.object(host_tune, 'find_executable',
                               side_effect=lambda n: '/usr/sbin/' + n if n == 'grubby' else None), \
                mock.patch.object(host_tune, 'call', side_effect=fake_call):
            done = host_tune.remove(None)
        for path in host_tune.MANAGED_FILES:
            assert not os.path.exists(path)
        assert ['systemctl', 'disable', host_tune.UNIT] in calls
        assert 'grubby --update-kernel=ALL --remove-args' in done[-1]['step']

    def test_no_grubby(self, host):
        with mock.patch.object(host_tune, 'find_executable', side_effect=_no_tools), \
                mock.patch.object(host_tune, 'call', return_value=('', '', 0)):
            done = host_tune.apply(None, cmdline=True, packages=False)
        last = done[-1]
        assert not last['ok'] and 'neither grubby nor /etc/default/grub' in last['error']


@pytest.mark.parametrize('rings', [False, True])
def test_script_is_valid_shell(tmp_path, rings):
    script = tmp_path / 's.sh'
    script.write_text(host_tune.script_body(rings))
    assert subprocess.run(['sh', '-n', str(script)]).returncode == 0
    body = script.read_text()
    assert '"${n##*/}"' in body and "'\\[always\\]'" in body
    assert ('ethtool -G' in body) == rings


def test_rings_awk_picks_maximums(tmp_path):
    # run the ring snippet against a fake ethtool
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    (bindir / 'ethtool').write_text(
        '#!/bin/sh\n'
        'if [ "$1" = -g ]; then printf "Ring parameters for $2:\\nPre-set maximums:\\n'
        'RX:\\t\\t8192\\nRX Mini:\\tn/a\\nTX:\\t\\t4096\\nCurrent hardware settings:\\n'
        'RX:\\t\\t1024\\nTX:\\t\\t4096\\n"; else echo "$@" >> %s/log; fi\n'
        % tmp_path)
    (bindir / 'ethtool').chmod(0o755)
    net = tmp_path / 'net'
    (net / 'eth0' / 'device').mkdir(parents=True)
    (net / 'lo').mkdir()
    body = host_tune.RINGS_BODY.replace('/sys/class/net', str(net))
    env = dict(os.environ, PATH='%s:%s' % (bindir, os.environ['PATH']))
    subprocess.run(['sh', '-c', body], env=env, check=True)
    assert (tmp_path / 'log').read_text() == '-G eth0 rx 8192 tx 4096\n'


class TestSysctls:
    def test_targets(self, host):
        for k, v in (('vm/swappiness', '60'), ('net/ipv4/tcp_rmem', '4096\t131072\t6291456'),
                     ('net/core/somaxconn', '4096'), ('fs/aio-max-nr', '65536')):
            host.create_file('/proc/sys/' + k, contents=v)
        t = {x['key']: x for x in host_tune.sysctl_targets()}
        assert t['vm.swappiness']['target'] == '10'
        assert t['net.ipv4.tcp_rmem'] == {
            'key': 'net.ipv4.tcp_rmem', 'current': '4096 131072 6291456',
            'target': '4096 131072 16777216'}
        # already fine, and managed by cephadm when it deploys an OSD
        assert 'net.core.somaxconn' not in t and 'fs.aio-max-nr' not in t
        # numa host
        assert t['vm.zone_reclaim_mode']['target'] == '0'

    def test_persisted_stay(self, host):
        host.create_file('/proc/sys/vm/swappiness', contents='10')
        host.create_file(host_tune.SYSCTL_FILE, contents='# x\nvm.swappiness = 10\n')
        t = {x['key']: x for x in host_tune.sysctl_targets()}
        assert t['vm.swappiness']['target'] == '10'

    def test_loads_before_cephadm(self):
        # cephadm's own files are 90-ceph-<fsid>-<daemon>.conf
        assert os.path.basename(host_tune.SYSCTL_FILE) < '90-ceph-'


class FakePackager:
    def __init__(self, fail=()):
        self.installed = []
        self.fail = fail
        self.updated = False

    def update(self):
        self.updated = True

    def install(self, ls):
        if ls[0] in self.fail:
            raise RuntimeError('Error: Unable to find a match: %s' % ls[0])
        self.installed.extend(ls)


class YumDnf(FakePackager):
    pass


class Zypper(FakePackager):
    pass


class TestPackages:
    def test_missing_tools(self, host):
        with mock.patch.object(host_tune, 'find_executable',
                               side_effect=lambda n: '/x' if n == 'ethtool' else None):
            names = [t['binary'] for t in host_tune.missing_tools()]
            assert 'ethtool' not in names and 'ipmitool' not in names
            host.create_file(host_tune.DMI_IPMI)
            assert 'ipmitool' in [t['binary'] for t in host_tune.missing_tools()]

    def _install(self, host, packager):
        done = []
        with mock.patch.object(host_tune, 'find_executable', side_effect=_no_tools), \
                mock.patch('cephadmlib.packagers.create_packager', return_value=packager):
            host_tune._install_tools(None, lambda what, err: done.append((what, err)))
        return done

    def test_best_effort(self, host):
        p = YumDnf(fail=('stress-ng',))
        done = self._install(host, p)
        assert p.updated
        assert p.installed == ['smartmontools', 'nvme-cli', 'ethtool', 'tuned',
                               'irqbalance', 'iperf3']
        errs = {w: e for w, e in done if e}
        assert list(errs) == ['install stress-ng (burn-in CPU and memory stress)']
        assert 'EPEL' in errs['install stress-ng (burn-in CPU and memory stress)']
        assert 'Unable to find a match' in errs['install stress-ng (burn-in CPU and memory stress)']

    def test_zypper_names(self, host):
        p = Zypper()
        self._install(host, p)
        assert 'iperf' in p.installed and 'iperf3' not in p.installed

    def test_unknown_distro(self, host):
        with mock.patch.object(host_tune, 'find_executable', side_effect=_no_tools), \
                mock.patch('cephadmlib.packagers.create_packager', side_effect=Error('Distro x not supported')):
            done = []
            host_tune._install_tools(None, lambda w, e: done.append((w, e)))
        assert done == [('install packages', 'no package manager support: Distro x not supported')]

    def test_apply_services_and_ipmi(self, host):
        host.create_file(host_tune.DMI_IPMI)
        calls = []

        def fake_call(ctx, cmd, **kw):
            calls.append(cmd)
            return ('', '', 0)

        with mock.patch.object(host_tune, 'find_executable',
                               side_effect=lambda n: '/x' if n in ('tuned-adm', 'irqbalance') else None), \
                mock.patch.object(host_tune, 'call', side_effect=fake_call):
            done = host_tune.apply(None, packages=False)
        assert all(d['ok'] for d in done), done
        assert ['systemctl', 'enable', '--now', 'tuned'] in calls
        assert ['systemctl', 'enable', '--now', 'irqbalance'] in calls
        assert ['modprobe', '-a', 'ipmi_si', 'ipmi_devintf'] in calls
        with open(host_tune.MODULES_FILE) as f:
            assert f.read().splitlines()[1:] == ['ipmi_si', 'ipmi_devintf']


def test_format_plan_aligns_and_truncates():
    items = [
        host_tune._change('boot-time unit', 'not installed', 'installed',
                          detail=host_tune.UNIT),
        host_tune._change('kernel command line', 'x' * 60, 'set', reboot=True),
    ]
    lines = host_tune.format_plan(items).splitlines()
    col = lines[0].index('CURRENT')
    assert lines[1].index('not installed') == col
    assert lines[3].index('x') == col
    assert '...' in lines[3] and lines[3].endswith('change (reboot)')
    assert lines[2].strip() == host_tune.UNIT



UBUNTU_GRUB = """GRUB_DEFAULT=0
GRUB_TIMEOUT=0
GRUB_CMDLINE_LINUX_DEFAULT="quiet splash"
GRUB_CMDLINE_LINUX=""
"""
SUSE_GRUB = """GRUB_DISTRIBUTOR=
GRUB_CMDLINE_LINUX_DEFAULT="splash=silent mitigations=auto quiet"
GRUB_CMDLINE_LINUX='pcie_aspm=off'
GRUB_TERMINAL=console
"""


class TestDefaultGrub:
    def _apply(self, host, grub, tools=('update-grub',), **kw):
        host.create_file(host_tune.GRUB_DEFAULT, contents=grub)
        calls = []

        def fake_call(ctx, cmd, **k):
            calls.append(cmd)
            return ('', '', 0)

        with mock.patch.object(host_tune, 'find_executable',
                               side_effect=lambda n: '/usr/sbin/' + n if n in tools else None), \
                mock.patch.object(host_tune, 'call', side_effect=fake_call):
            done = host_tune.apply(None, cmdline=True, packages=False, **kw)
        with open(host_tune.GRUB_DEFAULT) as f:
            return done, calls, f.read()

    def test_ubuntu(self, host):
        done, calls, text = self._apply(host, UBUNTU_GRUB, mitigations_off=True)
        assert done[-1]['ok'], done[-1]
        # pcie_aspm=off is already on the running kernel's command line
        assert ('GRUB_CMDLINE_LINUX="processor.max_cstate=1 intel_idle.max_cstate=0 '
                'nvme_core.default_ps_max_latency_us=0 mitigations=off"\n') in text
        assert 'GRUB_CMDLINE_LINUX_DEFAULT="quiet splash"' in text
        assert ['update-grub'] in calls
        with open(host_tune.GRUB_BACKUP) as f:
            assert f.read() == UBUNTU_GRUB
        assert 'reboot needed' in done[-1]['step']

    def test_suse_single_quotes_and_mkconfig(self, host):
        host.create_file('/boot/grub2/grub.cfg')
        with open('/proc/cmdline', 'w') as f:
            f.write('ro quiet')
        done, calls, text = self._apply(host, SUSE_GRUB, tools=('grub2-mkconfig',))
        assert ('GRUB_CMDLINE_LINUX="pcie_aspm=off processor.max_cstate=1 '
                'intel_idle.max_cstate=0 nvme_core.default_ps_max_latency_us=0"') in text
        assert 'GRUB_TERMINAL=console' in text
        assert ['grub2-mkconfig', '-o', '/boot/grub2/grub.cfg'] in calls

    def test_missing_line_and_idempotent(self, host):
        done, _, text = self._apply(host, 'GRUB_TIMEOUT=5\n')
        assert text.endswith('GRUB_CMDLINE_LINUX="processor.max_cstate=1 '
                             'intel_idle.max_cstate=0 nvme_core.default_ps_max_latency_us=0"\n')
        # applying again (before a reboot) adds nothing twice, keeps the first backup
        added = host_tune._edit_default_grub(['processor.max_cstate=1', 'mitigations=off'])
        assert added == ['mitigations=off']
        with open(host_tune.GRUB_DEFAULT) as f:
            assert f.read().count('processor.max_cstate=1') == 1
        with open(host_tune.GRUB_BACKUP) as f:
            assert f.read() == 'GRUB_TIMEOUT=5\n'

    def test_remove_hint(self, host):
        host.create_file(host_tune.GRUB_DEFAULT, contents=UBUNTU_GRUB)
        host.create_file(host_tune.GRUB_BACKUP, contents=UBUNTU_GRUB)
        with mock.patch.object(host_tune, 'find_executable',
                               side_effect=lambda n: '/usr/sbin/update-grub' if n == 'update-grub' else None), \
                mock.patch.object(host_tune, 'call', return_value=('', '', 0)):
            done = host_tune.remove(None)
        assert 'GRUB_CMDLINE_LINUX in /etc/default/grub (the original is ' in done[-1]['step']
        assert done[-1]['step'].endswith('and run update-grub')

    def test_plan_names_method(self, host):
        host.create_file(host_tune.GRUB_DEFAULT, contents=UBUNTU_GRUB)
        with mock.patch.object(host_tune, 'find_executable',
                               side_effect=lambda n: '/usr/sbin/update-grub' if n == 'update-grub' else None), \
                mock.patch.object(host_tune.host_hw, 'physical_nics', return_value=[]):
            items = {i['setting']: i for i in host_tune.plan(None, cmdline=True, packages=False)}
        assert items['kernel command line']['detail'].endswith('(via /etc/default/grub)')


@pytest.mark.parametrize('active, custom, switch', [
    (None, False, True),
    ('balanced', False, True),
    ('virtual-guest', False, True),
    ('network-latency', False, False),
    ('throughput-performance', False, False),
    ('ceph-osd', True, False),
])
def test_tuned_needs_profile(fs, active, custom, switch):
    if custom:
        fs.create_dir('/etc/tuned/' + active)
    assert host_tune.tuned_needs_profile(active) == switch
