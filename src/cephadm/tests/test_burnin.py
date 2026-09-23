import json
import os
import queue
import threading
import time

from unittest import mock

import pytest

from tests.fixtures import import_cephadm

from cephadmlib import burnin
from cephadmlib.exceptions import Error

_cephadm = import_cephadm()

MiB = 1024**2


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get())
    return out[-1]


def _plain_open(path, write):
    # tmpfs and macOS do not support O_DIRECT
    return os.open(path, os.O_RDWR if write else os.O_RDONLY)


@pytest.fixture
def small_blocks(monkeypatch):
    monkeypatch.setattr(burnin, 'SEQ_BLOCK', 64 * 1024)
    monkeypatch.setattr(burnin, 'VERIFY_WINDOW', 256 * 1024)
    monkeypatch.setattr(burnin, 'MEM_CHUNK', 1 * MiB)
    monkeypatch.setattr(burnin, '_open_direct', _plain_open)


class TestWorkers:
    def test_cpu(self):
        q = queue.Queue()
        burnin.cpu_worker(
            threading.Event(), q, 'cpu.0', time.monotonic() + 0.5
        )
        st = _drain(q)
        assert st['done'] and st['ops'] > 0 and st['errors'] == 0

    def test_cpu_detects_mismatch(self):
        results = iter(range(10**6))
        q = queue.Queue()
        with mock.patch.object(
            burnin, '_cpu_kernel', side_effect=lambda s: next(results)
        ):
            burnin.cpu_worker(
                threading.Event(), q, 'cpu.0', time.monotonic() + 0.2
            )
        assert _drain(q)['errors'] > 0

    def test_memory(self, small_blocks):
        q = queue.Queue()
        burnin.memory_worker(
            threading.Event(), q, 'memory.0', time.monotonic() + 1, 4 * MiB
        )
        st = _drain(q)
        assert st['passes'] >= 1 and st['errors'] == 0

    def test_mem_patterns_differ(self, small_blocks):
        base = os.urandom(burnin.MEM_CHUNK)
        pats = {burnin._mem_pattern(p, c, base) for p in range(4) for c in range(3)}
        assert len(pats) == 12
        assert all(len(p) == burnin.MEM_CHUNK for p in pats)

    @pytest.mark.parametrize('destructive', [False, True])
    def test_disk_seq(self, small_blocks, tmp_path, destructive):
        dev = tmp_path / 'disk'
        dev.write_bytes(b'\0' * (1 * MiB))
        q = queue.Queue()
        burnin.disk_seq_worker(
            threading.Event(), q, 'd', time.monotonic() + 0.5, str(dev), destructive
        )
        st = _drain(q)
        assert st['errors'] == 0, st['error_msgs']
        assert st['passes'] >= 1
        if destructive:
            assert st['verified_bytes'] > 0
            assert dev.read_bytes()[:8] == burnin.BLOCK_MAGIC

    def test_disk_seq_without_preadv(self, small_blocks, tmp_path, monkeypatch):
        # python 3.6 has no os.preadv/pwritev
        monkeypatch.setattr(burnin, '_HAVE_PREADV', False)
        dev = tmp_path / 'disk'
        dev.write_bytes(b'\0' * (1 * MiB))
        q = queue.Queue()
        with mock.patch('os.preadv', side_effect=AssertionError), \
                mock.patch('os.pwritev', side_effect=AssertionError):
            burnin.disk_seq_worker(
                threading.Event(), q, 'd', time.monotonic() + 0.5, str(dev), True
            )
        st = _drain(q)
        assert st['errors'] == 0, st['error_msgs']
        assert st['verified_bytes'] > 0
        assert dev.read_bytes()[:8] == burnin.BLOCK_MAGIC

    def test_verify_detects_misdirected_write(self, small_blocks, tmp_path):
        import mmap

        bs = burnin.SEQ_BLOCK
        dev = tmp_path / 'disk'
        dev.write_bytes(b'\0' * (4 * bs))
        fd = os.open(str(dev), os.O_RDWR)
        buf = mmap.mmap(-1, bs)
        template = mmap.mmap(-1, bs)
        template[:] = os.urandom(bs)
        # the block for offset 0 ends up at offset bs, and block 2 rots
        burnin._make_block(template, buf, 0, 7)
        os.pwrite(fd, bytes(buf), bs)
        burnin._make_block(template, buf, 2 * bs, 7)
        os.pwrite(fd, bytes(buf[:-1]) + bytes([buf[-1] ^ 0xFF]), 2 * bs)
        burnin._make_block(template, buf, 3 * bs, 7)
        os.pwrite(fd, bytes(buf), 3 * bs)

        rep = burnin._Reporter(queue.Queue(), 'd', 'disk', str(dev))
        burnin._verify_blocks(fd, buf, template, [0, bs, 2 * bs, 3 * bs], 7, rep)
        os.close(fd)
        assert rep.stats['error_msgs'] == [
            'verify at offset 0: block header missing',
            'verify at offset %d: found block for offset 0' % bs,
            'verify at offset %d: data mismatch' % (2 * bs),
        ]

    def test_disk_open_error(self):
        q = queue.Queue()
        burnin.disk_rand_worker(
            threading.Event(), q, 'd', time.monotonic() + 1, '/nonexistent'
        )
        st = _drain(q)
        assert st['errors'] == 1 and st['done']


class TestSelectDevices:
    @pytest.fixture(autouse=True)
    def disks(self):
        with mock.patch.object(
            burnin, 'list_block_devices', return_value=['sda', 'sdb', 'sdc']
        ), mock.patch.object(
            burnin, 'os_devices', return_value={'sda'}
        ), mock.patch.object(
            burnin,
            'data_devices',
            return_value=[
                {'path': '/dev/sdb', 'in_use': []},
                {'path': '/dev/sdc', 'in_use': ['has holders: dm-0']},
            ],
        ), mock.patch.object(
            burnin, 'device_usage', side_effect=lambda d: [] if d == 'sdb' else ['busy']
        ), mock.patch.object(
            burnin, '_signatures', return_value=''
        ), mock.patch(
            'os.path.realpath', side_effect=lambda p: p
        ):
            yield

    def test_all_available(self):
        assert burnin.select_devices(None, [], True, False) == ['/dev/sdb']

    def test_refuses_os_disk(self):
        with pytest.raises(Error, match='operating system'):
            burnin.select_devices(None, ['/dev/sda'], False, False)

    def test_refuses_unknown(self):
        with pytest.raises(Error, match='whole-disk'):
            burnin.select_devices(None, ['/dev/sdb1'], False, False)

    def test_read_only_allows_busy(self):
        assert burnin.select_devices(None, ['/dev/sdc'], False, False) == [
            '/dev/sdc'
        ]

    def test_destructive_refuses_busy(self):
        with pytest.raises(Error, match='refusing destructive test of /dev/sdc'):
            burnin.select_devices(None, ['/dev/sdc'], False, True)

    def test_destructive_refuses_signatures(self):
        with mock.patch.object(burnin, '_signatures', return_value='xfs'):
            with pytest.raises(Error, match='signatures: xfs'):
                burnin.select_devices(None, ['sdb'], False, True)


class TestRun:
    def test_run_cpu(self, tmp_path, monkeypatch):
        monkeypatch.setattr(burnin, 'STATUS_FILE', str(tmp_path / 'status.json'))
        monkeypatch.setattr(burnin, 'STATS_INTERVAL', 0.1)
        counters = iter(
            [
                {'edac_ce': 0, 'edac_ue': 0, 'throttle': 5},
                {'edac_ce': 2, 'edac_ue': 0, 'throttle': 5},
            ]
        )
        monkeypatch.setattr(burnin, 'host_counters', lambda: next(counters))
        monkeypatch.setattr(burnin, 'kernel_errors', lambda ctx, since: [])
        cfg = burnin.BurninConfig(
            duration=1,
            cpu=True,
            memory=False,
            memory_percent=10,
            devices=[],
            destructive=False,
            workers=2,
        )
        status = burnin.run_burnin(None, cfg, 'test')
        assert status['state'] == burnin.STATE_PASSED
        assert sorted(status['workers']) == ['cpu.0', 'cpu.1']
        assert all(w['done'] for w in status['workers'].values())
        assert status['warnings'] == ['EDAC correctable memory errors: +2']
        assert burnin.read_status()['state'] == burnin.STATE_PASSED
        assert burnin.read_status('test')['state'] == burnin.STATE_PASSED
        assert 'PASSED' in burnin.format_status(status)

    @pytest.mark.parametrize('edac, throttle, nnotes', [
        (True, True, 0), (False, True, 1), (False, False, 2),
    ])
    def test_run_notes_missing_counters(self, tmp_path, monkeypatch, edac, throttle, nnotes):
        monkeypatch.setattr(burnin, 'STATUS_FILE', str(tmp_path / 'status.json'))
        monkeypatch.setattr(burnin, 'kernel_errors', lambda ctx, since: [])
        monkeypatch.setattr(
            burnin, 'counters_available', lambda: {'edac': edac, 'throttle': throttle})
        cfg = burnin.BurninConfig(1, True, False, 10, [], False, workers=1)
        status = burnin.run_burnin(None, cfg, 'n')
        assert len(status['notes']) == nnotes
        assert status['state'] == burnin.STATE_PASSED
        if not edac:
            assert 'NOTE: no EDAC' in burnin.format_status(status)

    def test_history(self, tmp_path, monkeypatch):
        monkeypatch.setattr(burnin, 'STATUS_FILE', str(tmp_path / 'status.json'))
        for i in range(5):
            burnin.write_status({
                'run_id': '2026010%dT000000Z' % i, 'state': burnin.STATE_PASSED,
                'started': 's%d' % i, 'failures': ['x'] * i,
            })
        # the latest is status.json, and every run is kept by id
        assert burnin.read_status()['run_id'] == '20260104T000000Z'
        assert burnin.read_status('20260101T000000Z')['started'] == 's1'
        assert burnin.read_status('nope') is None
        with pytest.raises(Error, match='invalid run id'):
            burnin.read_status('../status')
        burnin.prune_runs(keep=3)
        runs = burnin.list_runs()
        assert [r['run_id'] for r in runs] == [
            '20260102T000000Z', '20260103T000000Z', '20260104T000000Z']
        assert runs[0]['failures'] == ['x', 'x']
        assert burnin.format_runs(runs).splitlines()[1].split()[-1] == '2'
        with pytest.raises(Error, match='no burn-in run nope'):
            burnin.current_status(None, 'nope')

    def test_current_status_dead_runner(self, tmp_path, monkeypatch):
        monkeypatch.setattr(burnin, 'STATUS_FILE', str(tmp_path / 'status.json'))
        assert burnin.current_status(None) == {'state': 'none'}
        burnin.write_status({'state': burnin.STATE_RUNNING, 'pid': 2**22 + 1})
        with mock.patch.object(burnin, 'unit_active', return_value=False):
            st = burnin.current_status(None)
        assert st['state'] == burnin.STATE_ERROR


class TestCommand:
    def _ctx(self, *args):
        return _cephadm.cephadm_init_ctx(['burnin'] + list(args))

    def test_destructive_needs_confirmation(self):
        ctx = self._ctx('start', '--devices', '/dev/sdb', '--destructive')
        with pytest.raises(Error, match='yes-i-really-mean-it'):
            _cephadm.command_burnin(ctx)

    def test_start_defaults(self):
        ctx = self._ctx('start', '--duration', '60')
        with mock.patch.object(
            burnin, 'select_devices', return_value=['/dev/sdb']
        ) as sel, mock.patch.object(burnin, 'launch_detached') as launch:
            assert _cephadm.command_burnin(ctx) == 0
        # nothing selected means everything that is safe
        sel.assert_called_once_with(ctx, [], True, False)
        args = launch.call_args[0][1]
        assert '--cpu' in args and '--memory' in args
        assert args[args.index('--devices') + 1] == '/dev/sdb'
        assert '--destructive' not in args

    def test_status_and_list(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(burnin, 'STATUS_FILE', str(tmp_path / 'status.json'))
        burnin.write_status({'run_id': 'a', 'state': burnin.STATE_PASSED, 'started': 's'})
        burnin.write_status({'run_id': 'b', 'state': burnin.STATE_FAILED, 'started': 's'})
        assert _cephadm.command_burnin(self._ctx('list', '--format', 'json')) == 0
        assert [r['run_id'] for r in json.loads(capsys.readouterr().out)] == ['a', 'b']
        ctx = self._ctx('status', '--run-id', 'a', '--format', 'json')
        assert _cephadm.command_burnin(ctx) == 0
        assert json.loads(capsys.readouterr().out)['state'] == burnin.STATE_PASSED

    def test_start_destructive(self):
        ctx = self._ctx(
            'start', '--devices', 'sdb', '--destructive', '--yes-i-really-mean-it'
        )
        with mock.patch.object(
            burnin, 'select_devices', return_value=['/dev/sdb']
        ), mock.patch.object(burnin, 'launch_detached') as launch:
            _cephadm.command_burnin(ctx)
        args = launch.call_args[0][1]
        assert '--cpu' not in args
        assert '--destructive' in args and '--yes-i-really-mean-it' in args


class TestDiskBench:
    def test_bench_worker(self, small_blocks, tmp_path):
        dev = tmp_path / 'disk'
        dev.write_bytes(b'\0' * (1 * MiB))
        q = queue.Queue()
        now = time.monotonic()
        burnin.disk_bench_worker(
            threading.Event(), q, 'b', now + 5, str(dev), now + 0.3, now + 0.6
        )
        b = _drain(q)['bench']
        assert b['seq_mb_per_sec'] > 0 and b['rand_iops'] > 0
        assert 0 <= b['lat_p50_ms'] <= b['lat_p99_ms'] <= b['lat_max_ms']

    def test_seq_resumes_and_reports_progress(self, small_blocks, tmp_path):
        bs = burnin.SEQ_BLOCK
        dev = tmp_path / 'disk'
        dev.write_bytes(b'\0' * (64 * bs))
        q = queue.Queue()
        stop = threading.Event()
        # read a handful of blocks starting in the middle of the disk
        with mock.patch.object(burnin, '_pread', side_effect=lambda fd, buf, off: (
                stop.set() if off >= 40 * bs else None) or bs):
            burnin.disk_seq_worker(
                stop, q, 'd', time.monotonic() + 5, str(dev), False, 32 * bs + 5
            )
        st = _drain(q)
        assert st['start_offset'] == 32 * bs
        assert st['offset'] == 41 * bs
        assert st['pass_progress_pct'] == round(100.0 * 9 / 64, 2)
        assert st['passes'] == 0

    def test_seq_pass_counts_from_start_offset(self, small_blocks, tmp_path):
        bs = burnin.SEQ_BLOCK
        dev = tmp_path / 'disk'
        dev.write_bytes(b'\0' * (8 * bs))
        seen = []
        stop = threading.Event()

        def pread(fd, buf, off):
            seen.append(off // bs)
            if len(seen) == 8:
                stop.set()
            return bs

        q = queue.Queue()
        with mock.patch.object(burnin, '_pread', side_effect=pread):
            burnin.disk_seq_worker(stop, q, 'd', time.monotonic() + 5, str(dev), False, 6 * bs)
        st = _drain(q)
        assert seen == [6, 7, 0, 1, 2, 3, 4, 5]
        assert st['passes'] == 1 and st['offset'] == 6 * bs

    def test_outliers(self):
        bench = {
            'sda': {'seq_mb_per_sec': 250, 'rand_iops': 100, 'lat_p99_ms': 20},
            'sdb': {'seq_mb_per_sec': 245, 'rand_iops': 98, 'lat_p99_ms': 22},
            'sdc': {'seq_mb_per_sec': 150, 'rand_iops': 99, 'lat_p99_ms': 90},
            'sdd': {'seq_mb_per_sec': 500, 'rand_iops': 5, 'lat_p99_ms': 1},
            'nvme0n1': {'seq_mb_per_sec': 3000, 'rand_iops': 9000, 'lat_p99_ms': 1},
        }
        models = {'sda': 'HDD', 'sdb': 'HDD', 'sdc': 'HDD', 'sdd': 'Other',
                  'nvme0n1': 'NVMe'}
        out = burnin.find_outliers(bench, models)
        # only sdc; single-disk models have nothing to compare with
        assert len(out) == 2
        assert out[0].startswith('sdc: sequential read 150 MB/s is 61% of the '
                                 'median of the other 2 HDD disks (247.5 MB/s)')
        assert out[1].startswith('sdc: random read p99 latency 90 ms')

    def test_run_bench_then_stress_and_save_offsets(self, small_blocks, tmp_path, monkeypatch):
        monkeypatch.setattr(burnin, 'STATUS_FILE', str(tmp_path / 'st' / 'status.json'))
        monkeypatch.setattr(burnin, 'STATS_INTERVAL', 0.1)
        monkeypatch.setattr(burnin, 'kernel_errors', lambda ctx, since: [])
        monkeypatch.setattr(burnin, 'device_info', lambda d: {'dev': d, 'model': 'M'})
        monkeypatch.setattr(burnin, 'device_key', lambda d: 'key-' + d)
        disks = []
        for n in ('da', 'db'):
            dev = tmp_path / n
            dev.write_bytes(b'\0' * (4 * MiB))
            disks.append(str(dev))
        burnin.save_offsets({'key-da': 2 * burnin.SEQ_BLOCK, 'key-other': 7})
        cfg = burnin.BurninConfig(4, False, False, 10, disks, False,
                                  workers=1, disk_bench_seconds=10)
        status = burnin.run_burnin(None, cfg, 'r')
        assert status['state'] == burnin.STATE_PASSED, status['failures']
        # a 4 s run gets 1 s of each benchmark pattern
        assert sorted(status['disk_bench']) == ['da', 'db']
        w = status['workers']
        assert w['disk.da.seq']['start_offset'] == 2 * burnin.SEQ_BLOCK
        assert w['disk.db.seq']['start_offset'] == 0
        assert w['disk.da.seq']['elapsed'] < 2.5
        offsets = burnin.load_offsets()
        assert offsets['key-other'] == 7
        assert offsets['key-da'] == w['disk.da.seq']['offset']
        text = burnin.format_status(status)
        assert 'disk benchmark' in text and 'eta' in text

    def test_from_start_ignores_offsets(self, small_blocks, tmp_path, monkeypatch):
        monkeypatch.setattr(burnin, 'STATUS_FILE', str(tmp_path / 'status.json'))
        monkeypatch.setattr(burnin, 'kernel_errors', lambda ctx, since: [])
        monkeypatch.setattr(burnin, 'device_key', lambda d: 'k')
        dev = tmp_path / 'd'
        dev.write_bytes(b'\0' * (1 * MiB))
        burnin.save_offsets({'k': 4 * burnin.SEQ_BLOCK})
        cfg = burnin.BurninConfig(1, False, False, 10, [str(dev)], False,
                                  disk_bench_seconds=0, resume=False)
        status = burnin.run_burnin(None, cfg, 'r')
        assert status['workers']['disk.d.seq']['start_offset'] == 0
        assert 'disk.d.bench' not in status['workers']
