from unittest import mock

import pytest

from tests.fixtures import import_cephadm

from cephadmlib import host_hw
from cephadmlib import host_tuning as ht

_cephadm = import_cephadm()


def _attr(attr_id, raw, when_failed=''):
    return {'id': attr_id, 'name': 'a%d' % attr_id, 'raw': {'value': raw},
            'when_failed': when_failed}


ATA_OK = {
    'model_name': 'WDC WD140EDGZ-11', 'firmware_version': '85.00A85',
    'smart_status': {'passed': True},
    'temperature': {'current': 34},
    'ata_smart_attributes': {'table': [_attr(5, 0), _attr(197, 0), _attr(199, 0)]},
    'ata_smart_error_log': {'summary': {'count': 0}},
}
ATA_BAD = {
    'smart_status': {'passed': False},
    'ata_smart_attributes': {'table': [
        _attr(5, 8), _attr(197, 2), _attr(199, 0x100000011), _attr(1, 0, 'now')]},
    'ata_smart_error_log': {'extended': {'count': 3}},
    'ata_smart_self_test_log': {'standard': {'table': [
        {'status': {'passed': False, 'string': 'Completed: read failure'}}]}},
}
NVME_BAD = {
    'smart_status': {'passed': True},
    'nvme_smart_health_information_log': {
        'critical_warning': 4, 'media_errors': 1, 'num_err_log_entries': 12,
        'available_spare': 5, 'available_spare_threshold': 10,
        'percentage_used': 91},
}


class TestSmart:
    def test_healthy(self):
        assert host_hw.smart_problems(ATA_OK) == []

    def test_ata_problems(self):
        probs = host_hw.smart_problems(ATA_BAD)
        assert ('fail', 'SMART overall health check FAILED') in probs
        assert ('warn', '8 reallocated sectors') in probs
        assert ('fail', '2 pending sectors') in probs
        # vendor data in the high bytes is ignored
        assert ('warn', '17 interface CRC errors (check the cable or backplane)') in probs
        assert ('fail', 'attribute a1 failed now') in probs
        assert ('warn', '3 errors in the ATA error log') in probs
        assert ('fail', 'last self-test failed: Completed: read failure') in probs

    def test_nvme_problems(self):
        probs = dict((m, s) for s, m in host_hw.smart_problems(NVME_BAD))
        assert probs['NVMe critical warning 0x4'] == 'fail'
        assert probs['1 NVMe media errors'] == 'fail'
        assert probs['available spare 5% is below the 10% threshold'] == 'fail'
        assert probs['endurance 91% used'] == 'warn'
        assert probs['12 entries in the NVMe error log'] == 'info'

    def test_counters(self):
        assert host_hw.smart_counters(ATA_BAD) == {
            'reallocated_sectors': 8, 'pending_sectors': 2, 'crc_errors': 17,
            'error_log_count': 3}
        assert host_hw.smart_counters(NVME_BAD) == {
            'media_errors': 1, 'num_err_log_entries': 12}
        assert host_hw.smart_counters(None) == {}

    def test_smartctl_missing(self):
        with mock.patch.object(host_hw, 'find_executable', return_value=None):
            assert host_hw.smartctl(None, 'sda') is None

    def test_smartctl_parses_json_despite_exit_code(self):
        # smartctl sets bits in its exit code for disk problems
        with mock.patch.object(host_hw, 'find_executable', return_value='x'), \
                mock.patch.object(host_hw, 'call', return_value=('{"smart_status": {}}', '', 64)):
            assert host_hw.smartctl(None, 'sda') == {'smart_status': {}, '_exit': 64}


def _pci(fs, addr, cls, cur_w, max_w, cur_s, max_s, parent='/sys/devices/pci0000:00/0000:00:01.0'):
    real = '%s/%s' % (parent, addr)
    for k, v in (('class', cls), ('current_link_width', cur_w), ('max_link_width', max_w),
                 ('current_link_speed', cur_s), ('max_link_speed', max_s)):
        fs.create_file('%s/%s' % (real, k), contents=str(v))
    fs.create_symlink('/sys/bus/pci/devices/' + addr, real)
    return real


class TestPci:
    def test_links(self, fs):
        fs.create_file('/sys/devices/pci0000:00/0000:00:01.0/max_link_width', contents='8')
        fs.create_file('/sys/devices/pci0000:00/0000:00:01.0/max_link_speed',
                       contents='16.0 GT/s PCIe')
        nic = _pci(fs, '0000:41:00.0', '0x020000', 8, 16, '16.0 GT/s PCIe', '16.0 GT/s PCIe')
        fs.create_dir(nic + '/net/ens1f0')
        _pci(fs, '0000:42:00.0', '0x010802', 4, 4, '8.0 GT/s PCIe', '16.0 GT/s PCIe')
        _pci(fs, '0000:43:00.0', '0x030000', 1, 16, '2.5 GT/s', '16.0 GT/s')  # GPU
        links = {l['addr']: l for l in host_hw.pci_links()}
        assert sorted(links) == ['0000:41:00.0', '0000:42:00.0']
        assert links['0000:41:00.0']['label'] == '0000:41:00.0 (ens1f0)'
        res = {r['target']: r for r in ht.check_pcie(None)}
        # x8 in an x8 slot: limited by the slot
        assert 'slot or upstream' in res['0000:41:00.0 (ens1f0)']['message']
        assert res['0000:41:00.0 (ens1f0)']['value'] == 'x8 16.0 GT/s'
        assert 'trained below' in res['0000:42:00.0']['message']
        assert res['0000:42:00.0']['expected'] == 'x4 16.0 GT/s'


class TestNic:
    def test_counters_and_check(self, fs):
        base = '/sys/class/net/eth0'
        fs.create_dir(base + '/device')
        for k, v in (('rx_crc_errors', 7), ('rx_dropped', 50), ('tx_dropped', 0),
                     ('rx_packets', 10000), ('tx_packets', 10000), ('rx_frame_errors', 0)):
            fs.create_file('%s/statistics/%s' % (base, k), contents=str(v))
        assert host_hw.nic_counters('eth0')['rx_crc_errors'] == 7
        ethtool = 'NIC statistics:\n     rx_discards_phy: 12\n     rx_packets: 5\n' \
                  '     rx_fcs_errors: 3\n     rx_xdp_drop: 9\n     tx_errors: 0\n'
        with mock.patch.object(host_hw, 'find_executable', return_value='x'), \
                mock.patch.object(host_hw, 'call', return_value=(ethtool, '', 0)):
            res = ht.check_nic_errors(None)
        assert [r['status'] for r in res] == ['warn', 'info', 'warn']
        assert res[0]['value'] == 'rx_crc_errors=7'
        assert res[1]['value'] == 'rx_dropped=50,tx_dropped=0'
        assert res[2]['value'] == 'rx_discards_phy=12,rx_fcs_errors=3'


class TestIpmi:
    SEL = [
        '   1 | 01/02/2026 | 10:00:00 | Event Logging Disabled #0x07 | Log area reset/cleared | Asserted',
        '   2 | 01/02/2026 | 11:00:00 | Memory #0x87 | Correctable ECC | Asserted',
        '   3 | 01/03/2026 | 12:00:00 | Temperature #0x30 | Upper Critical going high | Asserted',
        '   4 | 01/03/2026 | 12:05:00 | Power Unit #0x01 | Power off/down | Asserted',
    ]
    SDR = 'CPU1 Temp        | 98 degrees C      | cr\nFAN1             | 0 RPM             | nc\n' \
          'PS1 Status       | 0x01              | ok\nDIMM A1          | no reading        | ns\n'

    def test_classify(self):
        k = host_hw.classify_sel(self.SEL)
        assert [e.split('|')[0].strip() for e in k['error']] == ['2']
        assert [e.split('|')[0].strip() for e in k['thermal']] == ['3']

    def test_check(self):
        def run(ctx, cmd, **kw):
            return ('\n'.join(self.SEL), '', 0) if cmd[1] == 'sel' else (self.SDR, '', 0)

        with mock.patch.object(host_hw, 'ipmi_available', return_value=True), \
                mock.patch.object(host_hw, 'find_executable', return_value='x'), \
                mock.patch.object(host_hw, 'call', side_effect=run):
            res = ht.check_ipmi(None)
        assert [(r['status'], r.get('target')) for r in res] == [
            ('warn', ''), ('warn', ''), ('fail', 'CPU1 Temp'), ('warn', 'FAN1')]
        assert res[0]['message'].startswith('1 hardware errors in the BMC event log')

    def test_no_bmc(self):
        with mock.patch.object(host_hw, 'ipmi_available', return_value=False):
            assert ht.check_ipmi(None)[0]['status'] == 'info'


class TestFirmware:
    def test_mismatch(self, fs):
        for dev, model, rev in (('sda', 'WD140EDGZ', '0A81'), ('sdb', 'WD140EDGZ', '0A81'),
                                ('sdc', 'WD140EDGZ', '0A85'), ('sdd', 'OTHER', 'X')):
            fs.create_file('/sys/block/%s/dev' % dev, contents='8:0')
            fs.create_file('/sys/block/%s/device/model' % dev, contents=model)
            fs.create_file('/sys/block/%s/device/rev' % dev, contents=rev)
        fs.create_file('/sys/block/nvme0n1/dev', contents='259:0')
        fs.create_file('/sys/block/nvme0n1/device/model', contents='SAMSUNG')
        fs.create_file('/sys/block/nvme0n1/device/firmware_rev', contents='GDC5902Q')
        for i, fw in (('eth0', '22.31.1014'), ('eth1', '22.35.1012')):
            fs.create_dir('/sys/class/net/%s/device' % i)
            fs.create_file('/sys/class/net/%s/device/vendor' % i, contents='0x15b3')
            fs.create_file('/sys/class/net/%s/device/device' % i, contents='0x101d')
        fws = {'eth0': '22.31.1014', 'eth1': '22.35.1012'}
        with mock.patch.object(host_hw, 'ethtool_info',
                               side_effect=lambda ctx, i: {'driver': 'mlx5_core',
                                                           'firmware-version': fws[i]}):
            inv = ht.firmware_inventory(None)
            res = ht.check_firmware(None, inv)
        assert {'dev': 'nvme0n1', 'model': 'SAMSUNG', 'rev': 'GDC5902Q'} in inv['disks']
        assert [(r['target'], r['value']) for r in res] == [
            ('WD140EDGZ', '0A81: sda,sdb; 0A85: sdc'),
            ('0x15b3:0x101d', '22.31.1014: eth0; 22.35.1012: eth1'),
        ]


class TestSmartCheck:
    def test_check_smart(self, fs):
        for dev in ('sda', 'sdb', 'vda'):
            fs.create_file('/sys/block/%s/dev' % dev, contents='8:0')
        data = {'sda': ATA_OK, 'sdb': ATA_BAD, 'vda': {'_exit': 4}}
        with mock.patch.object(host_hw, 'find_executable', return_value='x'), \
                mock.patch.object(host_hw, 'smartctl', side_effect=lambda ctx, d: data[d]):
            res = ht.check_smart(None)
        by_dev = {}
        for r in res:
            by_dev.setdefault(r['target'], []).append(r['status'])
        assert by_dev['sda'] == ['ok']
        assert 'fail' in by_dev['sdb'] and 'warn' in by_dev['sdb']
        assert by_dev['vda'] == ['info']

    def test_smartctl_missing(self):
        with mock.patch.object(host_hw, 'find_executable', return_value=None):
            r = ht.check_smart(None)
        assert len(r) == 1 and 'smartmontools' in r[0]['message']
