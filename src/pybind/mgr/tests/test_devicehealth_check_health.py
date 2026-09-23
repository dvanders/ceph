# type: ignore
"""Tests for check_health(), run on an uninitialised Module with self.get()
stubbed."""

from datetime import datetime, timedelta, timezone

from devicehealth.module import (DEVICE_HEALTH, DEVICE_HEALTH_IN_USE,
                                 DEVICE_HEALTH_REPLACE, Module)


class FakeLog(object):
    def _ignore(self, *args: object) -> None:
        pass

    info = debug = warning = error = _ignore


def device(devid='Model_Serial', daemons=('osd.0',), days=7):
    """A device predicted to fail inside both thresholds."""
    when = datetime.now(timezone.utc) + timedelta(days=days)
    return {
        'devid': devid,
        'daemons': list(daemons),
        'location': [{'host': 'node1', 'dev': 'sda'}],
        'life_expectancy_min': when.strftime('%Y-%m-%dT%H:%M:%S.%f%z'),
        'life_expectancy_max': when.strftime('%Y-%m-%dT%H:%M:%S.%f%z'),
    }


def run_check(devices, osds_in, num_pgs, self_heal=True):
    """Run check_health() and return the health checks it set.

    :param osds_in: {osd_id: bool}
    :param num_pgs: {osd_id: int}; an id absent here is absent from osd_stats
    """
    m = Module.__new__(Module)
    m._logger = FakeLog()   # MgrModule.log is a read-only property
    m.mark_out_threshold = 86400 * 7 * 2
    m.warn_threshold = 86400 * 7 * 6
    m.self_heal = self_heal
    m.marked_out = []

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
