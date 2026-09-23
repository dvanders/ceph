# burnin.py - CPU, memory and disk burn-in tests for new hosts
#
# Copyright (C) 2026 Clyso Technologies Inc.
#
# This is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License version 2.1, as published by the Free Software
# Foundation.  See file COPYING.

"""Burn-in tests to shake out bad hardware before it holds data.

The tests are implemented with the python standard library only, so they
work on any host cephadm can manage without installing stress-ng or fio.
Each test runs in its own process until the requested duration expires or
the run is stopped:

cpu     hash, compress and floating point loops whose results are checked
        against a reference computed at start; a mismatch indicates a
        silent computation error.
memory  fill most of the available memory with changing patterns and read
        it back; any mismatch is a memory error.
disk    sequential and random O_DIRECT I/O across the whole device. The
        default is read-only. In destructive mode the sequential worker
        writes self-describing blocks and verifies them, which destroys any
        data on the device.

Besides the workers' own results, the run compares EDAC memory error
counters and CPU thermal throttle counters before and after, and scans the
kernel log for hardware errors.

A run is normally launched detached from the caller with systemd-run, so
the mgr only needs to start it and then poll the status file.
"""

import datetime
import errno
import hashlib
import json
import logging
import math
import mmap
import multiprocessing
import os
import random
import re
import signal
import struct
import sys
import time
import zlib

from glob import glob
from queue import Empty
from typing import Any, Dict, List, Optional, Tuple

from .call_wrappers import call, CallVerbosity
from .constants import DATA_DIR
from .context import CephadmContext
from .exceptions import Error
from .exe_utils import find_executable
from .file_utils import read_file, write_new
from .host_tuning import (
    data_devices,
    device_info,
    device_usage,
    list_block_devices,
    os_devices,
)

logger = logging.getLogger()

BURNIN_DIR = os.path.join(DATA_DIR, 'burnin')
# the latest run; every run is also kept as runs/<run_id>.json
STATUS_FILE = os.path.join(BURNIN_DIR, 'status.json')
MAX_RUNS_KEPT = 20
UNIT_NAME = 'ceph-burnin'

STATE_RUNNING = 'running'
STATE_PASSED = 'passed'
STATE_FAILED = 'failed'
STATE_STOPPED = 'stopped'
STATE_ERROR = 'error'
FINAL_STATES = (STATE_PASSED, STATE_FAILED, STATE_STOPPED, STATE_ERROR)

MiB = 1024**2
GiB = 1024**3

MEM_CHUNK = 16 * MiB
MEM_CHUNK_HEADER = struct.Struct('<QQ')  # pass, chunk
SEQ_BLOCK = 4 * MiB
RAND_BLOCK = 64 * 1024
# verify destructive writes after this much data has been written
VERIFY_WINDOW = 256 * MiB
BLOCK_MAGIC = b'CEPHBURN'
BLOCK_HEADER = struct.Struct('<8sQQ')  # magic, offset, run seed

STATS_INTERVAL = 5.0
LATENCY_SAMPLES = 10000
# per-disk benchmark before the stress phase: seconds per pattern
DISK_BENCH_SECONDS = 30
# a disk below this fraction of its peers' median is reported
OUTLIER_RATIO = 0.8
# ... and so is one whose p99 latency is above this multiple of theirs
OUTLIER_LATENCY_RATIO = 2.0
MAX_ERRORS_KEPT = 20

KERNEL_ERROR_PATTERNS = [
    r'I/O error',
    r'Machine check',
    r'Hardware Error',
    r'EDAC .*(CE|UE)',
    r'mce: ',
    r'nvme.*(timeout|reset)',
    r'blk_update_request',
    r'critical medium error',
    r'Medium Error',
    r'ata\d+.*(exception|failed command|hard resetting)',
    r'temperature above threshold',
    r'soft lockup',
    r'hung_task',
]


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%S.%fZ'
    )


##################################
# workers
#
# Every worker runs in a forked child, loops until stop is set or its
# deadline expires, and periodically puts a stats dict on the queue. The
# final stats message carries done=True.


class _Reporter:
    def __init__(self, queue: Any, name: str, kind: str, target: str):
        self.queue = queue
        self.stats: Dict[str, Any] = {
            'name': name,
            'type': kind,
            'target': target,
            'ops': 0,
            'bytes': 0,
            'errors': 0,
            'error_msgs': [],
            'passes': 0,
            'done': False,
        }
        self.started = time.monotonic()
        self.last = 0.0

    def error(self, msg: str) -> None:
        self.stats['errors'] += 1
        if len(self.stats['error_msgs']) < MAX_ERRORS_KEPT:
            self.stats['error_msgs'].append(msg)

    def maybe_report(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last < STATS_INTERVAL:
            return
        self.last = now
        elapsed = max(now - self.started, 1e-6)
        self.stats['elapsed'] = round(elapsed, 1)
        self.stats['mb_per_sec'] = round(
            self.stats['bytes'] / elapsed / MiB, 1
        )
        self.stats['ops_per_sec'] = round(self.stats['ops'] / elapsed, 1)
        self.queue.put(dict(self.stats))

    def finish(self) -> None:
        self.stats['done'] = True
        self.maybe_report(force=True)


def _cpu_kernel(seed: int) -> Tuple[str, int, float]:
    """A deterministic mixed integer/float workload."""
    rnd = random.Random(seed)
    data = bytes(rnd.getrandbits(8) for _ in range(64 * 1024))
    digest = hashlib.sha256(data * 16).hexdigest()
    crc = zlib.crc32(zlib.compress(data, 6))
    acc = 0.0
    for i in range(1, 20000):
        acc += math.sin(i * 0.001) * math.sqrt(i) / (1.0 + math.log(i))
    return digest, crc, acc


def cpu_worker(stop: Any, queue: Any, name: str, deadline: float) -> None:
    rep = _Reporter(queue, name, 'cpu', name)
    seeds = [random.getrandbits(32) for _ in range(4)]
    reference = {s: _cpu_kernel(s) for s in seeds}
    i = 0
    while not stop.is_set() and time.monotonic() < deadline:
        seed = seeds[i % len(seeds)]
        got = _cpu_kernel(seed)
        if got != reference[seed]:
            rep.error(
                'computation mismatch for seed %d: %r != %r'
                % (seed, got, reference[seed])
            )
        rep.stats['ops'] += 1
        i += 1
        if i % len(seeds) == 0:
            rep.stats['passes'] += 1
        rep.maybe_report()
    rep.finish()


def _mem_pattern(pass_no: int, chunk_no: int, base: bytes) -> bytes:
    """Pattern for one chunk; varies by pass and chunk to catch aliasing."""
    kind = pass_no % 4
    if kind == 0:
        pat = base
    elif kind == 1:
        pat = bytes(b ^ 0xFF for b in base[:4096]) * (len(base) // 4096)
    elif kind == 2:
        pat = b'\x55\xaa' * (len(base) // 2)
    else:
        pat = b'\x00\xff' * (len(base) // 2)
    # rotate by chunk number so no two chunks hold the same bytes at the
    # same offsets, and stamp each chunk so an aliased write is detected
    r = (chunk_no * 4099) % len(pat)
    pat = pat[r:] + pat[:r]
    return (
        MEM_CHUNK_HEADER.pack(pass_no, chunk_no)
        + pat[MEM_CHUNK_HEADER.size :]
    )


def memory_worker(
    stop: Any, queue: Any, name: str, deadline: float, size: int
) -> None:
    rep = _Reporter(queue, name, 'memory', '%d MiB' % (size // MiB))
    nchunks = max(1, size // MEM_CHUNK)
    try:
        buf = bytearray(nchunks * MEM_CHUNK)
    except MemoryError:
        rep.error('could not allocate %d MiB' % (size // MiB))
        rep.finish()
        return

    def expired() -> bool:
        return stop.is_set() or time.monotonic() >= deadline

    pass_no = 0
    while not expired():
        base = os.urandom(MEM_CHUNK)
        # write every chunk before verifying any of them, so errors that
        # need time (retention) or are caused by writes elsewhere show up
        for c in range(nchunks):
            if expired():
                break
            off = c * MEM_CHUNK
            buf[off : off + MEM_CHUNK] = _mem_pattern(pass_no, c, base)
            rep.stats['bytes'] += MEM_CHUNK
            rep.stats['ops'] += 1
            rep.maybe_report()
        else:
            for c in range(nchunks):
                if expired():
                    break
                off = c * MEM_CHUNK
                pat = _mem_pattern(pass_no, c, base)
                if buf[off : off + MEM_CHUNK] != pat:
                    bad = next(
                        i for i in range(MEM_CHUNK) if buf[off + i] != pat[i]
                    )
                    rep.error(
                        'pass %d: mismatch at chunk %d byte %d: '
                        'got 0x%02x want 0x%02x'
                        % (pass_no, c, bad, buf[off + bad], pat[bad])
                    )
                rep.stats['bytes'] += MEM_CHUNK
                rep.stats['ops'] += 1
                rep.maybe_report()
            else:
                rep.stats['passes'] += 1
        pass_no += 1
    del buf
    rep.finish()


# os.preadv/pwritev are python 3.7+; cephadm still runs on 3.6 (EL8).
# readv/writev after a seek also transfer straight from and to the aligned
# buffer, as O_DIRECT requires. Each worker has its own fd, so the seek is
# safe.
_HAVE_PREADV = hasattr(os, 'preadv')


def _pread(fd: int, buf: mmap.mmap, off: int) -> int:
    if _HAVE_PREADV:
        return os.preadv(fd, [buf], off)
    os.lseek(fd, off, os.SEEK_SET)
    return os.readv(fd, [buf])


def _pwrite(fd: int, buf: mmap.mmap, off: int) -> int:
    if _HAVE_PREADV:
        return os.pwritev(fd, [buf], off)
    os.lseek(fd, off, os.SEEK_SET)
    return os.writev(fd, [buf])


def _open_direct(path: str, write: bool) -> int:
    flags = os.O_RDWR if write else os.O_RDONLY
    return os.open(path, flags | getattr(os, 'O_DIRECT', 0))


def _dev_size(fd: int) -> int:
    return os.lseek(fd, 0, os.SEEK_END)


def _make_block(
    template: mmap.mmap, buf: mmap.mmap, offset: int, seed: int
) -> None:
    buf[:] = template[:]
    buf[: BLOCK_HEADER.size] = BLOCK_HEADER.pack(BLOCK_MAGIC, offset, seed)


def _percentile(sorted_vals: List[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    k = min(
        len(sorted_vals) - 1, int(round(pct / 100.0 * (len(sorted_vals) - 1)))
    )
    return sorted_vals[k]


class _Latencies:
    """Reservoir sample of latencies, so long runs use bounded memory."""

    def __init__(self, size: int = LATENCY_SAMPLES):
        self.size = size
        self.samples: List[float] = []
        self.count = 0
        self.max = 0.0

    def add(self, v: float) -> None:
        self.count += 1
        self.max = max(self.max, v)
        if len(self.samples) < self.size:
            self.samples.append(v)
        else:
            k = random.randrange(self.count)
            if k < self.size:
                self.samples[k] = v

    def summary(self) -> Dict[str, float]:
        vals = sorted(self.samples)
        return {
            'lat_p50_ms': round(_percentile(vals, 50) * 1000, 1),
            'lat_p99_ms': round(_percentile(vals, 99) * 1000, 1),
            'lat_max_ms': round(self.max * 1000, 1),
        }


def disk_bench_worker(
    stop: Any,
    queue: Any,
    name: str,
    deadline: float,
    path: str,
    seq_end: float,
    rand_end: float,
) -> None:
    """Measure one disk on its own: sequential reads, then random reads.

    The stress workers run the two patterns concurrently, which makes a
    spinning disk seek constantly; this gives the numbers each pattern gets
    alone. Sequential reads start at offset 0, the outer edge of an HDD, so
    that every disk is measured at the same place.
    """
    rep = _Reporter(queue, name, 'disk-bench', path)
    try:
        fd = _open_direct(path, False)
    except OSError as e:
        rep.error('open %s: %s' % (path, e))
        rep.finish()
        return
    bench: Dict[str, Any] = {}
    try:
        size = _dev_size(fd)
        buf = mmap.mmap(-1, SEQ_BLOCK)
        off = 0
        nbytes = 0
        t0 = time.monotonic()
        end = min(seq_end, deadline)
        while not stop.is_set() and time.monotonic() < end:
            if off + SEQ_BLOCK > size:
                off = 0
            try:
                nbytes += _pread(fd, buf, off)
            except OSError as e:
                rep.error('read at offset %d: %s' % (off, e))
            off += SEQ_BLOCK
            rep.stats['ops'] += 1
        elapsed = max(time.monotonic() - t0, 1e-6)
        bench['seq_mb_per_sec'] = round(nbytes / elapsed / MiB, 1)
        rep.stats['bytes'] += nbytes

        buf = mmap.mmap(-1, RAND_BLOCK)
        nblocks = size // RAND_BLOCK
        lat = _Latencies()
        t0 = time.monotonic()
        end = min(rand_end, deadline)
        while not stop.is_set() and time.monotonic() < end and nblocks:
            off = random.randrange(nblocks) * RAND_BLOCK
            t = time.monotonic()
            try:
                rep.stats['bytes'] += _pread(fd, buf, off)
            except OSError as e:
                rep.error('read at offset %d: %s' % (off, e))
            lat.add(time.monotonic() - t)
            rep.stats['ops'] += 1
        elapsed = max(time.monotonic() - t0, 1e-6)
        bench['rand_iops'] = round(lat.count / elapsed, 1)
        bench.update(lat.summary())
        rep.stats['bench'] = bench
    finally:
        os.close(fd)
        rep.finish()


def disk_seq_worker(
    stop: Any,
    queue: Any,
    name: str,
    deadline: float,
    path: str,
    destructive: bool,
    start_offset: int = 0,
) -> None:
    """Sequential passes over the whole device, starting at start_offset
    and wrapping around, so repeated short runs cover the whole disk."""
    rep = _Reporter(
        queue,
        name,
        'disk-seq-write' if destructive else 'disk-seq-read',
        path,
    )
    try:
        fd = _open_direct(path, destructive)
    except OSError as e:
        rep.error('open %s: %s' % (path, e))
        rep.finish()
        return
    seed = random.getrandbits(63)
    buf = mmap.mmap(-1, SEQ_BLOCK)  # page aligned, as O_DIRECT requires
    template = mmap.mmap(-1, SEQ_BLOCK)
    template[:] = os.urandom(SEQ_BLOCK)
    try:
        size = _dev_size(fd) // SEQ_BLOCK * SEQ_BLOCK
        off = start_offset // SEQ_BLOCK * SEQ_BLOCK
        if off >= size:
            off = 0
        rep.stats['device_bytes'] = size
        rep.stats['start_offset'] = off
        covered = 0  # bytes of the current pass
        pending_verify: List[int] = []
        while not stop.is_set() and time.monotonic() < deadline and size:
            try:
                if destructive:
                    _make_block(template, buf, off, seed)
                    n = _pwrite(fd, buf, off)
                    pending_verify.append(off)
                else:
                    n = _pread(fd, buf, off)
                if n != SEQ_BLOCK:
                    rep.error(
                        'short %s at %d: %d bytes'
                        % ('write' if destructive else 'read', off, n)
                    )
                rep.stats['bytes'] += n
            except OSError as e:
                rep.error(
                    '%s at offset %d: %s'
                    % ('write' if destructive else 'read', off, e)
                )
            rep.stats['ops'] += 1
            off += SEQ_BLOCK
            covered += SEQ_BLOCK
            if destructive and (
                len(pending_verify) * SEQ_BLOCK >= VERIFY_WINDOW
                or off >= size
            ):
                _verify_blocks(fd, buf, template, pending_verify, seed, rep)
                pending_verify = []
            if off >= size:
                off = 0
            if covered >= size:
                covered = 0
                rep.stats['passes'] += 1
            rep.stats['offset'] = off
            _seq_progress(rep, covered, size)
            rep.maybe_report()
        if pending_verify:
            _verify_blocks(fd, buf, template, pending_verify, seed, rep)
    finally:
        os.close(fd)
        rep.finish()


def _seq_progress(rep: _Reporter, covered: int, size: int) -> None:
    rep.stats['pass_progress_pct'] = round(100.0 * covered / size, 2)
    elapsed = time.monotonic() - rep.started
    rate = rep.stats['bytes'] / elapsed if elapsed > 0 else 0
    rep.stats['pass_eta_sec'] = int((size - covered) / rate) if rate else None


def _verify_blocks(
    fd: int,
    buf: mmap.mmap,
    template: mmap.mmap,
    offsets: List[int],
    seed: int,
    rep: _Reporter,
) -> None:
    # flush the drive's volatile cache so we verify what reached the media
    os.fsync(fd)
    body = template[BLOCK_HEADER.size :]
    for off in offsets:
        try:
            _pread(fd, buf, off)
        except OSError as e:
            rep.error('verify read at offset %d: %s' % (off, e))
            continue
        magic, got_off, got_seed = BLOCK_HEADER.unpack(
            buf[: BLOCK_HEADER.size]
        )
        if magic != BLOCK_MAGIC or got_seed != seed:
            rep.error('verify at offset %d: block header missing' % off)
        elif got_off != off:
            # the block landed somewhere else: a misdirected write
            rep.error(
                'verify at offset %d: found block for offset %d'
                % (off, got_off)
            )
        elif buf[BLOCK_HEADER.size :] != body:
            rep.error('verify at offset %d: data mismatch' % off)
        rep.stats['verified_bytes'] = (
            rep.stats.get('verified_bytes', 0) + SEQ_BLOCK
        )


def disk_rand_worker(
    stop: Any, queue: Any, name: str, deadline: float, path: str
) -> None:
    """Random reads to exercise seeks and the controller queue."""
    rep = _Reporter(queue, name, 'disk-rand-read', path)
    try:
        fd = _open_direct(path, False)
    except OSError as e:
        rep.error('open %s: %s' % (path, e))
        rep.finish()
        return
    buf = mmap.mmap(-1, RAND_BLOCK)
    lat = _Latencies()
    try:
        nblocks = _dev_size(fd) // RAND_BLOCK
        while not stop.is_set() and time.monotonic() < deadline and nblocks:
            off = random.randrange(nblocks) * RAND_BLOCK
            t = time.monotonic()
            try:
                rep.stats['bytes'] += _pread(fd, buf, off)
            except OSError as e:
                rep.error('read at offset %d: %s' % (off, e))
            lat.add(time.monotonic() - t)
            rep.stats['ops'] += 1
            if rep.stats['ops'] % 64 == 0:
                rep.stats.update(lat.summary())
            rep.maybe_report()
        rep.stats.update(lat.summary())
    finally:
        os.close(fd)
        rep.finish()


##################################
# host counters


def edac_counts() -> Dict[str, int]:
    ce = ue = 0
    for mc in glob('/sys/devices/system/edac/mc/mc*'):
        for field in ('ce_count', 'ue_count'):
            try:
                v = int(read_file([os.path.join(mc, field)]))
            except ValueError:
                continue
            if field == 'ce_count':
                ce += v
            else:
                ue += v
    return {'ce': ce, 'ue': ue}


def counters_available() -> Dict[str, bool]:
    """Whether the counters exist at all; zero is not evidence otherwise."""
    return {
        'edac': bool(glob('/sys/devices/system/edac/mc/mc*/ce_count')),
        'throttle': bool(
            glob('/sys/devices/system/cpu/cpu[0-9]*/thermal_throttle')
        ),
    }


def host_counters() -> Dict[str, int]:
    edac = edac_counts()
    return {
        'edac_ce': edac['ce'],
        'edac_ue': edac['ue'],
        'throttle': throttle_count(),
    }


def throttle_count() -> int:
    total = 0
    for p in glob(
        '/sys/devices/system/cpu/cpu[0-9]*/thermal_throttle/*_throttle_count'
    ):
        try:
            total += int(read_file([p]))
        except ValueError:
            continue
    return total


def kernel_errors(ctx: CephadmContext, since: float) -> List[str]:
    """Hardware-looking kernel log lines since the given epoch time."""
    if not find_executable('journalctl'):
        return []
    out, _, code = call(
        ctx,
        [
            'journalctl',
            '-k',
            '--no-pager',
            '-o',
            'short-iso',
            '--since',
            '@%d' % since,
        ],
        verbosity=CallVerbosity.QUIET,
        timeout=60,
    )
    if code:
        return []
    rx = re.compile('|'.join(KERNEL_ERROR_PATTERNS), re.IGNORECASE)
    return [line for line in out.splitlines() if rx.search(line)][:100]


##################################
# device selection


def select_devices(
    ctx: CephadmContext,
    devices: List[str],
    all_available: bool,
    destructive: bool,
) -> List[str]:
    """Validate the requested devices and return their /dev paths."""
    os_devs = os_devices()
    selected: List[str] = []
    if all_available:
        for d in data_devices():
            if not d['in_use']:
                selected.append(d['path'])
    known = set(list_block_devices())
    for dev in devices:
        kname = os.path.basename(os.path.realpath(dev))
        if kname not in known:
            raise Error('%s is not a whole-disk block device' % dev)
        if kname in os_devs:
            raise Error(
                '%s holds the operating system; refusing to test it' % dev
            )
        path = '/dev/' + kname
        if path not in selected:
            selected.append(path)
    if destructive:
        for path in selected:
            kname = os.path.basename(path)
            reasons = device_usage(kname)
            if reasons:
                raise Error(
                    'refusing destructive test of %s: %s'
                    % (path, '; '.join(reasons))
                )
            sig = _signatures(ctx, path)
            if sig:
                raise Error(
                    'refusing destructive test of %s: found signatures: %s '
                    '(zap the device first)' % (path, sig)
                )
    return selected


def _signatures(ctx: CephadmContext, path: str) -> str:
    if not find_executable('wipefs'):
        return ''
    # wipefs -n prints nothing when there are no signatures
    out, _, code = call(
        ctx,
        ['wipefs', '-n', '--output', 'TYPE', '--noheadings', path],
        verbosity=CallVerbosity.QUIET,
    )
    if code:
        return ''
    return ','.join(sorted(set(out.split())))


##################################
# status file


def _runs_dir() -> str:
    return os.path.join(os.path.dirname(STATUS_FILE), 'runs')


def read_status(run_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Read the latest run, or the given one."""
    if run_id:
        if os.path.basename(run_id) != run_id:
            raise Error('invalid run id %r' % run_id)
        path = os.path.join(_runs_dir(), run_id + '.json')
    else:
        path = STATUS_FILE
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_status(status: Dict[str, Any]) -> None:
    runs = _runs_dir()
    os.makedirs(runs, mode=0o700, exist_ok=True)
    status['updated'] = _now()
    paths = [STATUS_FILE]
    if status.get('run_id'):
        paths.append(os.path.join(runs, status['run_id'] + '.json'))
    for path in paths:
        with write_new(path, owner=None, perms=0o600) as f:
            json.dump(status, f, indent=2, sort_keys=True)


def list_runs() -> List[Dict[str, Any]]:
    """Summaries of the kept runs, oldest first."""
    out = []
    for path in sorted(glob(os.path.join(_runs_dir(), '*.json'))):
        try:
            with open(path) as f:
                st = json.load(f)
        except (OSError, ValueError):
            continue
        out.append(
            {
                k: st.get(k)
                for k in ('run_id', 'state', 'started', 'ended', 'failures')
            }
        )
    return out


def prune_runs(keep: int = MAX_RUNS_KEPT) -> None:
    # run ids are timestamps, so name order is age order
    paths = sorted(glob(os.path.join(_runs_dir(), '*.json')))
    for path in paths[: max(0, len(paths) - keep)]:
        try:
            os.unlink(path)
        except OSError:
            pass


##################################
# runner


class BurninConfig:
    def __init__(
        self,
        duration: int,
        cpu: bool,
        memory: bool,
        memory_percent: int,
        devices: List[str],
        destructive: bool,
        workers: int = 0,
        disk_bench_seconds: int = DISK_BENCH_SECONDS,
        resume: bool = True,
    ):
        self.duration = duration
        self.cpu = cpu
        self.memory = memory
        self.memory_percent = memory_percent
        self.devices = devices
        self.destructive = destructive
        self.workers = workers or os.cpu_count() or 1
        self.disk_bench_seconds = disk_bench_seconds
        self.resume = resume

    def to_json(self) -> Dict[str, Any]:
        return dict(self.__dict__)


def _worker_main(target: Any, start_at: float, stop: Any, *args: Any) -> None:
    # the runner owns shutdown: a ^C in the foreground or a SIGTERM from
    # systemd reaches it and it sets the stop event for everyone
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    # stress workers wait for the disk benchmark, so it measures idle disks
    while time.monotonic() < start_at:
        if stop.wait(min(1.0, start_at - time.monotonic())):
            break
    target(stop, *args)


def _offsets_file() -> str:
    return os.path.join(os.path.dirname(STATUS_FILE), 'offsets.json')


def device_key(dev: str) -> str:
    """A name for a disk that survives renumbering of /dev/sdX."""
    for rel in ('device/wwid', 'wwid', 'device/serial'):
        v = read_file([os.path.join('/sys/block', dev, rel)]).strip()
        if v and v != 'Unknown':
            return v
    return dev


def load_offsets() -> Dict[str, int]:
    try:
        with open(_offsets_file()) as f:
            data = json.load(f)
        return {str(k): int(v) for k, v in data.items()}
    except (OSError, ValueError, AttributeError):
        return {}


def save_offsets(offsets: Dict[str, int]) -> None:
    os.makedirs(os.path.dirname(_offsets_file()), mode=0o700, exist_ok=True)
    with write_new(_offsets_file(), owner=None, perms=0o600) as f:
        json.dump(offsets, f, indent=2, sort_keys=True)


def find_outliers(
    bench: Dict[str, Dict[str, Any]], models: Dict[str, str]
) -> List[str]:
    """Compare each disk's benchmark with the other disks of its model."""
    groups: Dict[str, List[str]] = {}
    for dev in sorted(bench):
        groups.setdefault(models.get(dev) or 'unknown model', []).append(dev)
    out = []
    for model, devs in sorted(groups.items()):
        if len(devs) < 2:
            continue
        for dev in devs:
            peers = [bench[d] for d in devs if d != dev]
            for key, desc, unit, higher_is_better in (
                ('seq_mb_per_sec', 'sequential read', 'MB/s', True),
                ('rand_iops', 'random read', 'IOPS', True),
                ('lat_p99_ms', 'random read p99 latency', 'ms', False),
            ):
                mine = bench[dev].get(key)
                vals = sorted(p[key] for p in peers if p.get(key))
                if not mine or not vals:
                    continue
                ref = (
                    vals[len(vals) // 2]
                    if len(vals) % 2
                    else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2
                )
                if higher_is_better:
                    bad = mine < ref * OUTLIER_RATIO
                else:
                    bad = mine > ref * OUTLIER_LATENCY_RATIO
                if bad:
                    out.append(
                        '%s: %s %s %s is %d%% of the median of the other %d '
                        '%s disks (%s %s)'
                        % (
                            dev,
                            desc,
                            mine,
                            unit,
                            round(100.0 * mine / ref),
                            len(peers),
                            model,
                            round(ref, 1),
                            unit,
                        )
                    )
    return out


def _mem_available() -> int:
    for line in read_file(['/proc/meminfo']).splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) * 1024
    return 0


def run_burnin(
    ctx: CephadmContext, cfg: BurninConfig, run_id: str
) -> Dict[str, Any]:
    """Run the burn-in in the foreground and return the final status."""
    mp = multiprocessing.get_context('fork')
    stop = mp.Event()
    queue = mp.Queue()
    deadline = time.monotonic() + cfg.duration
    start_epoch = time.time()

    status: Dict[str, Any] = {
        'run_id': run_id,
        'state': STATE_RUNNING,
        'pid': os.getpid(),
        'started': _now(),
        'config': cfg.to_json(),
        'workers': {},
        'devices': {
            os.path.basename(p): device_info(os.path.basename(p))
            for p in cfg.devices
        },
        'counters_before': host_counters(),
        'counters_available': counters_available(),
    }

    procs: List[Any] = []
    t0 = time.monotonic()
    # benchmark each disk alone (sequential, then random) before anything
    # else loads the host; never more than half of the run
    bench_each = 0.0
    if cfg.devices and cfg.disk_bench_seconds > 0:
        bench_each = min(float(cfg.disk_bench_seconds), cfg.duration / 4.0)
    stress_at = t0 + 2 * bench_each

    def spawn(
        name: str, target: Any, *args: Any, start_at: float = 0.0
    ) -> None:
        p = mp.Process(
            target=_worker_main,
            name=name,
            args=(target, start_at, stop, queue, name, deadline) + args,
        )
        p.daemon = True
        p.start()
        procs.append(p)
        status['workers'][name] = {'name': name, 'done': False}

    offsets = load_offsets() if cfg.resume else {}
    keys = {
        os.path.basename(p): device_key(os.path.basename(p))
        for p in cfg.devices
    }
    for path in cfg.devices:
        dev = os.path.basename(path)
        if bench_each:
            spawn(
                'disk.%s.bench' % dev,
                disk_bench_worker,
                path,
                t0 + bench_each,
                t0 + 2 * bench_each,
            )
        spawn(
            'disk.%s.seq' % dev,
            disk_seq_worker,
            path,
            cfg.destructive,
            offsets.get(keys[dev], 0),
            start_at=stress_at,
        )
        spawn(
            'disk.%s.rand' % dev, disk_rand_worker, path, start_at=stress_at
        )
    if cfg.cpu:
        for i in range(cfg.workers):
            spawn('cpu.%d' % i, cpu_worker, start_at=stress_at)
    if cfg.memory:
        total = _mem_available() * cfg.memory_percent // 100
        n = min(cfg.workers, 16)
        for i in range(n):
            spawn(
                'memory.%d' % i, memory_worker, total // n, start_at=stress_at
            )

    def _on_signal(signum: int, frame: Any) -> None:
        logger.info('burn-in: got signal %d, stopping', signum)
        status['state'] = STATE_STOPPED
        stop.set()

    old_handlers = {
        s: signal.signal(s, _on_signal)
        for s in (signal.SIGTERM, signal.SIGINT)
    }
    write_status(status)
    try:
        last_write = time.monotonic()
        while any(p.is_alive() for p in procs) or not queue.empty():
            try:
                msg = queue.get(timeout=1.0)
                status['workers'][msg['name']] = msg
            except Empty:
                pass
            if time.monotonic() - last_write >= STATS_INTERVAL:
                write_status(status)
                last_write = time.monotonic()
        for p in procs:
            p.join()
            if p.exitcode:
                w = status['workers'].setdefault(p.name, {'name': p.name})
                w['errors'] = w.get('errors', 0) + 1
                w.setdefault('error_msgs', []).append(
                    'worker exited with code %s' % p.exitcode
                )
    finally:
        for s, h in old_handlers.items():
            signal.signal(s, h)
        stop.set()

    # remember where each disk's sequential pass got to
    for path in cfg.devices:
        dev = os.path.basename(path)
        w = status['workers'].get('disk.%s.seq' % dev, {})
        if 'offset' in w:
            offsets[keys[dev]] = w['offset']
    if cfg.devices:
        try:
            save_offsets(offsets)
        except OSError as e:
            logger.warning('burn-in: could not save disk offsets: %s', e)

    bench = {
        os.path.basename(p): status['workers']
        .get('disk.%s.bench' % os.path.basename(p), {})
        .get('bench')
        for p in cfg.devices
    }
    bench = {d: b for d, b in bench.items() if b}
    status['disk_bench'] = bench

    before = status['counters_before']
    after = host_counters()
    status['counters_after'] = after
    kerr = kernel_errors(ctx, start_epoch)
    status['kernel_errors'] = kerr

    failures: List[str] = []
    for w in status['workers'].values():
        if w.get('errors'):
            failures.append('%s: %d errors' % (w['name'], w['errors']))
    warnings: List[str] = []
    for key, desc, is_failure in (
        ('edac_ue', 'EDAC uncorrectable memory errors', True),
        ('edac_ce', 'EDAC correctable memory errors', False),
        ('throttle', 'CPU thermal throttling events', False),
    ):
        delta = after[key] - before[key]
        if delta > 0:
            (failures if is_failure else warnings).append(
                '%s: +%d' % (desc, delta)
            )
    if kerr:
        failures.append('%d hardware errors in the kernel log' % len(kerr))
    models = {d: status['devices'].get(d, {}).get('model', '') for d in bench}
    warnings.extend(find_outliers(bench, models))
    notes: List[str] = []
    available = status['counters_available']
    if not available['edac']:
        notes.append(
            'no EDAC memory controller counters: memory errors corrected by '
            'ECC (or ECC being absent) cannot be detected on this host'
        )
    if not available['throttle']:
        notes.append(
            'no CPU thermal throttle counters: throttling cannot be detected'
        )
    status['failures'] = failures
    status['warnings'] = warnings
    status['notes'] = notes
    status['ended'] = _now()
    if status['state'] != STATE_STOPPED:
        status['state'] = STATE_FAILED if failures else STATE_PASSED
    write_status(status)
    prune_runs()
    return status


##################################
# launching and controlling a detached run


def unit_active(ctx: CephadmContext) -> bool:
    _, _, code = call(
        ctx,
        ['systemctl', 'is-active', '--quiet', UNIT_NAME],
        verbosity=CallVerbosity.QUIET,
    )
    return code == 0


def launch_detached(ctx: CephadmContext, run_args: List[str]) -> None:
    """Start `cephadm burnin run ...` as a transient systemd service."""
    if not find_executable('systemd-run'):
        raise Error('systemd-run is required to start a detached burn-in')
    if unit_active(ctx):
        raise Error(
            'a burn-in is already running; use `cephadm burnin stop` first'
        )
    # clear a failed unit left by a previous run so the name is free
    call(
        ctx,
        ['systemctl', 'reset-failed', UNIT_NAME],
        verbosity=CallVerbosity.QUIET,
    )
    cmd = [
        'systemd-run',
        '--unit',
        UNIT_NAME,
        '--description',
        'Ceph host burn-in',
        '--property',
        'KillMode=mixed',
        '--property',
        'TimeoutStopSec=120',
        '--collect',
        sys.executable,
        os.path.abspath(sys.argv[0]),
        'burnin',
        'run',
    ] + run_args
    _, err, code = call(ctx, cmd, verbosity=CallVerbosity.VERBOSE_ON_FAILURE)
    if code:
        raise Error('failed to start burn-in: %s' % err)


def stop_detached(ctx: CephadmContext) -> bool:
    if not unit_active(ctx):
        return False
    _, err, code = call(ctx, ['systemctl', 'stop', UNIT_NAME], timeout=180)
    if code:
        raise Error('failed to stop burn-in: %s' % err)
    return True


def current_status(
    ctx: CephadmContext, run_id: Optional[str] = None
) -> Dict[str, Any]:
    status = read_status(run_id)
    if status is None:
        if run_id:
            raise Error('no burn-in run %s on this host' % run_id)
        return {'state': 'none'}
    if status.get('state') == STATE_RUNNING and not unit_active(ctx):
        # the runner died without writing a final state
        pid = status.get('pid')
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError as e:
                alive = e.errno == errno.EPERM
        if not alive:
            status['state'] = STATE_ERROR
            status.setdefault('failures', []).append(
                'burn-in process exited unexpectedly'
            )
    return status


def _fmt_secs(secs: int) -> str:
    h, rem = divmod(int(secs), 3600)
    return '%dh%02dm' % (h, rem // 60) if h else '%dm%02ds' % divmod(rem, 60)


def format_status(status: Dict[str, Any]) -> str:
    if status.get('state') == 'none':
        return 'No burn-in has been run on this host'
    cfg = status.get('config', {})
    lines = [
        'run %s: %s'
        % (status.get('run_id'), status.get('state', '').upper()),
        'started %s, updated %s%s'
        % (
            status.get('started'),
            status.get('updated'),
            ', ended %s' % status['ended'] if status.get('ended') else '',
        ),
        'duration %ss, destructive: %s'
        % (cfg.get('duration'), cfg.get('destructive')),
    ]
    bench = status.get('disk_bench', {})
    if bench:
        lines.append(
            'disk benchmark (each pattern alone, %ss):'
            % cfg.get('disk_bench_seconds')
        )
        lines.append(
            '  %-10s %-20s %10s %10s %10s %10s %10s'
            % (
                'DEV',
                'MODEL',
                'SEQ MB/s',
                'RAND IOPS',
                'P50 ms',
                'P99 ms',
                'MAX ms',
            )
        )
        for dev, b in sorted(bench.items()):
            lines.append(
                '  %-10s %-20s %10s %10s %10s %10s %10s'
                % (
                    dev,
                    status.get('devices', {})
                    .get(dev, {})
                    .get('model', '')[:20],
                    b.get('seq_mb_per_sec'),
                    b.get('rand_iops'),
                    b.get('lat_p50_ms'),
                    b.get('lat_p99_ms'),
                    b.get('lat_max_ms'),
                )
            )
    workers = sorted(
        status.get('workers', {}).values(), key=lambda w: w['name']
    )
    for w in workers:
        extra = ''
        if 'pass_progress_pct' in w:
            eta = w.get('pass_eta_sec')
            extra = ' (pass %d, %.2f%% done, eta %s)' % (
                w.get('passes', 0) + 1,
                w['pass_progress_pct'],
                _fmt_secs(eta) if eta is not None else '?',
            )
        elif 'lat_p99_ms' in w:
            extra = ' (p99 %s ms, max %s ms)' % (
                w['lat_p99_ms'],
                w['lat_max_ms'],
            )
        lines.append(
            '  %-24s %-15s %-12s %8s MB/s %9s ops/s %6s errors%s'
            % (
                w['name'],
                w.get('type', 'waiting'),
                w.get('target', '')[-12:],
                w.get('mb_per_sec', '-'),
                w.get('ops_per_sec', '-'),
                w.get('errors', 0),
                extra,
            )
        )
        for m in w.get('error_msgs', [])[:3]:
            lines.append('      ' + m)
    for f in status.get('failures', []):
        lines.append('FAIL: ' + f)
    for wmsg in status.get('warnings', []):
        lines.append('WARN: ' + wmsg)
    for note in status.get('notes', []):
        lines.append('NOTE: ' + note)
    for k in status.get('kernel_errors', [])[:10]:
        lines.append('  kernel: ' + k)
    return '\n'.join(lines)


def format_runs(runs: List[Dict[str, Any]]) -> str:
    if not runs:
        return 'No burn-in has been run on this host'
    lines = ['%-18s %-8s %-28s %s' % ('RUN', 'STATE', 'STARTED', 'FAILURES')]
    for r in runs:
        lines.append(
            '%-18s %-8s %-28s %d'
            % (
                r['run_id'],
                r['state'],
                r['started'],
                len(r.get('failures') or []),
            )
        )
    return '\n'.join(lines)
