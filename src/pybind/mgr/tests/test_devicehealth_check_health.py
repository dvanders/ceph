# type: ignore
"""Tests for check_health(), run on an uninitialised Module with self.get()
stubbed."""

import json
from datetime import datetime, timedelta, timezone

from devicehealth.module import (DEVICE_HEALTH, DEVICE_HEALTH_IN_USE,
                                 DEVICE_HEALTH_REPLACE,
                                 DEVICE_HEALTH_TOOMANY, Module)


class FakeLog(object):
    def _ignore(self, *args: object) -> None:
        pass

    info = debug = warning = error = _ignore


def device(devid='Model_Serial', daemons=('osd.0',), days=7, host='node1'):
    """A device predicted to fail inside both thresholds."""
    when = datetime.now(timezone.utc) + timedelta(days=days)
    return {
        'devid': devid,
        'daemons': list(daemons),
        'location': [{'host': host, 'dev': 'sda'}],
        'life_expectancy_min': when.strftime('%Y-%m-%dT%H:%M:%S.%f%z'),
        'life_expectancy_max': when.strftime('%Y-%m-%dT%H:%M:%S.%f%z'),
    }


def run_check(devices, osds_in, num_pgs, self_heal=True,
              max_concurrent=1, min_interval=0, store=None, hosts=None):
    """Run check_health() and return the health checks it set.

    :param osds_in: {osd_id: bool}
    :param num_pgs: {osd_id: int}; an id absent here is absent from osd_stats
    :param store: pre-existing module store, e.g. a mark_out_history
    :param hosts: {osd_id: host} used to place each device
    """
    m = Module.__new__(Module)
    m._logger = FakeLog()   # MgrModule.log is a read-only property
    m.mark_out_threshold = 86400 * 7 * 2
    m.warn_threshold = 86400 * 7 * 6
    m.self_heal = self_heal
    m.mark_out_max_concurrent = max_concurrent
    m.mark_out_min_interval = min_interval
    m.marked_out = []
    m.store = dict(store or {})
    m.get_store = lambda k, default=None: m.store.get(k, default)
    m.set_store = lambda k, v: m.store.__setitem__(k, v)

    data = {
        'config': {'mon_osd_min_in_ratio': '0.75'},
        'devices': {'devices': devices},
        'osd_map': {'osds': [{'osd': int(i), 'in': int(v)}
                             for i, v in osds_in.items()]},
        'osd_stats': {'osd_stats': [{'osd': int(i), 'num_pgs': n}
                                    for i, n in num_pgs.items()]},
    }
    m.get = lambda what: data[what]
    m.mark_out_etc = lambda osd_ids: m.marked_out.extend(osd_ids)

    checks = {}
    m.set_health_checks = lambda c: checks.update(c)
    assert m.check_health() == (0, "", "")
    return m, checks


def test_device_in_service_warns_as_failing():
    _, checks = run_check([device(daemons=['osd.0'])],
                          osds_in={'0': True}, num_pgs={'0': 42})
    assert DEVICE_HEALTH in checks
    assert DEVICE_HEALTH_REPLACE not in checks
    assert 'life expectancy between' in checks[DEVICE_HEALTH]['detail'][0]


def test_out_and_drained_device_awaits_replacement():
    m, checks = run_check([device(daemons=['osd.0'])],
                          osds_in={'0': False}, num_pgs={'0': 0})
    assert DEVICE_HEALTH not in checks
    assert checks[DEVICE_HEALTH_REPLACE]['summary'] \
        == '1 device(s) awaiting replacement'
    assert checks[DEVICE_HEALTH_REPLACE]['detail'] \
        == ['Model_Serial (node1:sda); osd.0 marked out and drained']
    # nothing left for self-heal to do
    assert m.marked_out == []


def test_out_but_still_holding_pgs_is_not_a_replacement():
    _, checks = run_check([device(daemons=['osd.0'])],
                          osds_in={'0': False}, num_pgs={'0': 3})
    assert DEVICE_HEALTH in checks
    assert DEVICE_HEALTH_REPLACE not in checks
    assert checks[DEVICE_HEALTH_IN_USE]['detail'] \
        == ['osd.0 is marked out but still has 3 PG(s)']


def test_unknown_pg_count_is_not_evidence_of_draining():
    # osd.0 is absent from osd_stats, so get_osd_num_pgs() returns -1
    _, checks = run_check([device(daemons=['osd.0'])],
                          osds_in={'0': False}, num_pgs={})
    assert DEVICE_HEALTH in checks
    assert DEVICE_HEALTH_REPLACE not in checks


def test_one_drained_osd_does_not_silence_a_shared_device():
    _, checks = run_check([device(daemons=['osd.0', 'osd.1'])],
                          osds_in={'0': False, '1': True},
                          num_pgs={'0': 0, '1': 90})
    assert DEVICE_HEALTH in checks
    assert DEVICE_HEALTH_REPLACE not in checks


def test_every_osd_out_and_drained_on_a_shared_device():
    _, checks = run_check([device(daemons=['osd.0', 'osd.1'])],
                          osds_in={'0': False, '1': False},
                          num_pgs={'0': 0, '1': 0})
    assert DEVICE_HEALTH not in checks
    assert checks[DEVICE_HEALTH_REPLACE]['detail'] \
        == ['Model_Serial (node1:sda); osd.0,osd.1 marked out and drained']


def test_a_device_shared_with_a_mon_is_still_in_service():
    # a mon has no notion of being 'out', so the device is still in use
    _, checks = run_check([device(daemons=['osd.0', 'mon.a'])],
                          osds_in={'0': False}, num_pgs={'0': 0})
    assert DEVICE_HEALTH in checks
    assert DEVICE_HEALTH_REPLACE not in checks


def test_device_with_no_osds_at_all_is_still_in_service():
    _, checks = run_check([device(daemons=['mon.a'])],
                          osds_in={}, num_pgs={})
    assert DEVICE_HEALTH in checks
    assert DEVICE_HEALTH_REPLACE not in checks


def test_disabling_self_heal_still_silences_in_use():
    # documented in health-checks.rst as the way to silence this check
    m, checks = run_check([device(daemons=['osd.0'])],
                          osds_in={'0': False}, num_pgs={'0': 3},
                          self_heal=False)
    assert DEVICE_HEALTH_IN_USE not in checks
    assert m.marked_out == []


def test_replacement_is_reported_with_self_heal_disabled():
    # unlike DEVICE_HEALTH_IN_USE, this state is about what the
    # administrator has already done, not about what self-heal did
    _, checks = run_check([device(daemons=['osd.0'])],
                          osds_in={'0': False}, num_pgs={'0': 0},
                          self_heal=False)
    assert DEVICE_HEALTH not in checks
    assert checks[DEVICE_HEALTH_REPLACE]['count'] == 1


def test_self_heal_still_marks_out_a_failing_osd():
    m, checks = run_check([device(daemons=['osd.0'])],
                          osds_in={'0': True, '1': True, '2': True,
                                   '3': True},
                          num_pgs={'0': 42})
    assert m.marked_out == ['0']
    assert DEVICE_HEALTH in checks


def test_mixed_fleet_separates_the_two_states():
    devices = [device('Failing', ['osd.0']), device('Drained', ['osd.1'])]
    _, checks = run_check(devices,
                          osds_in={'0': True, '1': False},
                          num_pgs={'0': 42, '1': 0})
    assert checks[DEVICE_HEALTH]['count'] == 1
    assert 'Failing' in checks[DEVICE_HEALTH]['detail'][0]
    assert checks[DEVICE_HEALTH_REPLACE]['count'] == 1
    assert 'Drained' in checks[DEVICE_HEALTH_REPLACE]['detail'][0]


# ---------------------------------------------------------------------------
# self-heal throttling
# ---------------------------------------------------------------------------

def failing(n, host='node1'):
    """n devices, each on its own OSD, all predicted to fail imminently."""
    return [device('Device_%d' % i, ['osd.%d' % i], days=1, host=host)
            for i in range(n)]


def all_in(n):
    return {str(i): True for i in range(n)}


def spread(n):
    """n OSDs on n distinct hosts."""
    return {str(i): 'node%d' % i for i in range(n)}


def test_only_one_osd_is_marked_out_per_pass_by_default():
    devices = [device('Device_%d' % i, ['osd.%d' % i], days=1,
                      host='node%d' % i) for i in range(20)]
    m, _ = run_check(devices, osds_in=all_in(40),
                     num_pgs={str(i): 100 for i in range(40)})
    assert len(m.marked_out) == 1


def test_concurrency_limit_is_configurable():
    devices = [device('Device_%d' % i, ['osd.%d' % i], days=1,
                      host='node%d' % i) for i in range(20)]
    m, _ = run_check(devices, osds_in=all_in(40),
                     num_pgs={str(i): 100 for i in range(40)},
                     max_concurrent=3)
    assert len(m.marked_out) == 3


def test_zero_concurrency_marks_nothing_out():
    m, checks = run_check(failing(5), osds_in=all_in(20),
                          num_pgs={str(i): 100 for i in range(20)},
                          max_concurrent=0)
    assert m.marked_out == []
    # the devices are still reported, only the automatic action is withheld
    assert checks[DEVICE_HEALTH]['count'] == 5


def test_a_draining_osd_holds_its_slot():
    # osd.9 was marked out earlier and still has PGs
    history = json.dumps({'9': {'at': 0.0, 'host': 'node9'}})
    m, _ = run_check(failing(5), osds_in={**all_in(20), '9': False},
                     num_pgs={**{str(i): 100 for i in range(20)}, '9': 40},
                     store={'mark_out_history': history})
    assert m.marked_out == []


def test_a_finished_drain_releases_its_slot():
    history = json.dumps({'9': {'at': 0.0, 'host': 'node9'}})
    m, _ = run_check(failing(5), osds_in={**all_in(20), '9': False},
                     num_pgs={**{str(i): 100 for i in range(20)}, '9': 0},
                     store={'mark_out_history': history})
    assert len(m.marked_out) == 1


def test_an_unknown_pg_count_keeps_holding_the_slot():
    # osd.9 is out and absent from osd_stats: we cannot tell if it drained
    history = json.dumps({'9': {'at': 0.0, 'host': 'node9'}})
    m, _ = run_check(failing(5), osds_in={**all_in(20), '9': False},
                     num_pgs={str(i): 100 for i in range(20)},
                     store={'mark_out_history': history})
    assert m.marked_out == []


def test_an_osd_brought_back_in_is_forgotten():
    # the administrator returned osd.9 to service, so it is no longer ours
    history = json.dumps({'9': {'at': 0.0, 'host': 'node9'}})
    m, _ = run_check(failing(5), osds_in=all_in(20),
                     num_pgs={str(i): 100 for i in range(20)},
                     store={'mark_out_history': history})
    assert len(m.marked_out) == 1


def test_a_purged_osd_does_not_block_forever():
    # osd.99 is gone from the osdmap entirely
    history = json.dumps({'99': {'at': 0.0, 'host': 'node9'}})
    m, _ = run_check(failing(5), osds_in=all_in(20),
                     num_pgs={str(i): 100 for i in range(20)},
                     store={'mark_out_history': history})
    assert len(m.marked_out) == 1


def test_minimum_interval_defers_a_recent_mark_out():
    recent = datetime.now(timezone.utc).timestamp() - 60
    history = json.dumps({'9': {'at': recent, 'host': 'node9'}})
    m, _ = run_check(failing(5), osds_in={**all_in(20), '9': False},
                     # drained instantly, so only the interval can throttle
                     num_pgs={**{str(i): 100 for i in range(20)}, '9': 0},
                     min_interval=3600,
                     store={'mark_out_history': history})
    assert m.marked_out == []


def test_minimum_interval_expires():
    old = datetime.now(timezone.utc).timestamp() - 7200
    history = json.dumps({'9': {'at': old, 'host': 'node9'}})
    m, _ = run_check(failing(5), osds_in={**all_in(20), '9': False},
                     num_pgs={**{str(i): 100 for i in range(20)}, '9': 0},
                     min_interval=3600,
                     store={'mark_out_history': history})
    assert len(m.marked_out) == 1


def test_two_osds_on_one_host_are_not_drained_together():
    # both failing devices are on node1; only one may go at a time
    devices = failing(4, host='node1')
    m, _ = run_check(devices, osds_in=all_in(20),
                     num_pgs={str(i): 100 for i in range(20)},
                     max_concurrent=3)
    assert len(m.marked_out) == 1


def test_osds_on_distinct_hosts_may_drain_together():
    devices = [device('Device_%d' % i, ['osd.%d' % i], days=1,
                      host='node%d' % i) for i in range(4)]
    m, _ = run_check(devices, osds_in=all_in(20),
                     num_pgs={str(i): 100 for i in range(20)},
                     max_concurrent=3)
    assert len(m.marked_out) == 3


def test_a_host_already_draining_is_skipped():
    history = json.dumps({'9': {'at': 0.0, 'host': 'node1'}})
    devices = [device('OnBusyHost', ['osd.0'], days=1, host='node1'),
               device('Elsewhere', ['osd.1'], days=1, host='node2')]
    m, _ = run_check(devices, osds_in={**all_in(20), '9': False},
                     num_pgs={**{str(i): 100 for i in range(20)}, '9': 40},
                     max_concurrent=2,
                     store={'mark_out_history': history})
    assert m.marked_out == ['1']


def test_the_mark_out_is_recorded_for_the_next_pass():
    m, _ = run_check(failing(5), osds_in=all_in(20),
                     num_pgs={str(i): 100 for i in range(20)})
    history = json.loads(m.store['mark_out_history'])
    assert list(history) == m.marked_out
    assert history[m.marked_out[0]]['host'] == 'node1'
    assert history[m.marked_out[0]]['at'] > 0


def test_a_corrupt_history_does_not_wedge_self_heal():
    m, _ = run_check(failing(5), osds_in=all_in(20),
                     num_pgs={str(i): 100 for i in range(20)},
                     store={'mark_out_history': 'not json'})
    assert len(m.marked_out) == 1


def test_the_worst_device_is_marked_out_first():
    devices = [device('Later', ['osd.0'], days=13, host='node0'),
               device('Sooner', ['osd.1'], days=1, host='node1')]
    m, _ = run_check(devices, osds_in=all_in(20),
                     num_pgs={str(i): 100 for i in range(20)})
    assert m.marked_out == ['1']


# ---------------------------------------------------------------------------
# hybrid OSDs: several OSDs sharing one device, e.g. block.db on a common NVMe
# ---------------------------------------------------------------------------

def shared_db(n=12, failing=True, host='node1'):
    """One NVMe carrying block.db for n OSDs, plus each OSD's own HDD."""
    nvme = device('NVME_db', ['osd.%d' % i for i in range(n)], host=host)
    if not failing:
        del nvme['life_expectancy_min'], nvme['life_expectancy_max']
    hdds = [device('HDD_%d' % i, ['osd.%d' % i], host=host) for i in range(n)]
    for h in hdds:
        del h['life_expectancy_min'], h['life_expectancy_max']
    return [nvme] + hdds


def test_a_failing_shared_device_is_evacuated_in_one_go():
    m, checks = run_check(shared_db(12), osds_in=padded(all_in(12)),
                          num_pgs={str(i): 100 for i in range(12)})
    assert sorted(int(x) for x in m.marked_out) == list(range(12))
    assert checks[DEVICE_HEALTH]['count'] == 1      # one device, not twelve


def test_a_shared_device_counts_as_one_slot():
    # twelve OSDs going out together is one device's worth of self-heal
    devices = shared_db(12) + [device('HDD_far', ['osd.99'], host='node9')]
    m, _ = run_check(devices, osds_in=padded({**all_in(12), '99': True}),
                     num_pgs={**{str(i): 100 for i in range(12)}, '99': 100},
                     max_concurrent=2)
    assert sorted(int(x) for x in m.marked_out) == list(range(12)) + [99]


def test_a_draining_shared_device_holds_exactly_one_slot():
    # all twelve OSDs of the device are still draining; that is one device
    history = json.dumps({str(i): {'at': 0.0, 'host': 'node1',
                                   'devid': 'NVME_db'} for i in range(12)})
    devices = shared_db(12) + [device('HDD_far', ['osd.99'], host='node9')]
    m, _ = run_check(devices,
                     osds_in=padded({**{str(i): False for i in range(12)},
                                     '99': True}),
                     num_pgs={**{str(i): 50 for i in range(12)}, '99': 100},
                     max_concurrent=2,
                     store={'mark_out_history': history})
    assert m.marked_out == ['99']


def test_the_whole_group_is_recorded_under_its_device():
    m, _ = run_check(shared_db(12), osds_in=padded(all_in(12)),
                     num_pgs={str(i): 100 for i in range(12)})
    history = json.loads(m.store['mark_out_history'])
    assert len(history) == 12
    assert set(rec['devid'] for rec in history.values()) == {'NVME_db'}


def test_a_group_too_large_for_the_ratio_does_not_stall_the_rest():
    # 20 OSDs on one shared device cannot go out without breaching the ratio,
    # but the single-OSD device behind it can
    big = shared_db(20, host='node1')
    devices = big + [device('HDD_far', ['osd.99'], host='node9')]
    osds = {str(i): True for i in range(20)}
    m, checks = run_check(devices, osds_in={**osds, '99': True},
                          num_pgs={**{str(i): 100 for i in range(20)},
                                   '99': 100})
    assert m.marked_out == ['99']
    assert DEVICE_HEALTH_TOOMANY in checks


def test_a_shared_device_awaits_replacement_only_when_fully_drained():
    n = 12
    # eleven of twelve drained is not enough
    _, checks = run_check(shared_db(n),
                          osds_in={str(i): i == 11 for i in range(n)},
                          num_pgs={str(i): 0 if i < 11 else 100
                                   for i in range(n)})
    assert DEVICE_HEALTH in checks
    assert DEVICE_HEALTH_REPLACE not in checks

    _, checks = run_check(shared_db(n),
                          osds_in={str(i): False for i in range(n)},
                          num_pgs={str(i): 0 for i in range(n)})
    assert DEVICE_HEALTH not in checks
    assert checks[DEVICE_HEALTH_REPLACE]['count'] == 1


def padded(osds, total=120):
    """Add healthy in OSDs so mon_osd_min_in_ratio is not the limit here."""
    filler = {str(i): True for i in range(1000, 1000 + total)}
    filler.update(osds)
    return filler


def test_a_shared_device_holds_up_the_rest_of_the_cluster():
    # a draining shared device uses the only slot
    devices = shared_db(12) + [device('HDD_far', ['osd.99'], host='node9')]
    history = json.dumps({'0': {'at': 0.0, 'host': 'node1'}})
    m, _ = run_check(devices,
                     osds_in=padded({**all_in(12), '0': False, '99': True}),
                     num_pgs={**{str(i): 100 for i in range(12)}, '0': 50,
                              '99': 100},
                     store={'mark_out_history': history})
    assert m.marked_out == []


def test_raising_the_limit_lets_a_far_away_device_through():
    devices = shared_db(12) + [device('HDD_far', ['osd.99'], host='node9')]
    history = json.dumps({'0': {'at': 0.0, 'host': 'node1'}})
    m, _ = run_check(devices,
                     osds_in=padded({**all_in(12), '0': False, '99': True}),
                     num_pgs={**{str(i): 100 for i in range(12)}, '0': 50,
                              '99': 100},
                     max_concurrent=2,
                     store={'mark_out_history': history})
    # node1 is busy draining, so only the device on node9 is eligible
    assert m.marked_out == ['99']
