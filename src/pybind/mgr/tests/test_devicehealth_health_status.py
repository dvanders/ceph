# type: ignore
"""Tests for the verdict devicehealth publishes to 'ceph device ls',
via _apply_prediction() on an uninitialised Module."""

from devicehealth import predictor
from devicehealth.module import Module


def apply_to(dev, status):
    """Apply a verdict to one device, returning what the module wrote.

    :return: (health statuses written, life expectancies written,
              life expectancies cleared)
    """
    m = Module.__new__(Module)
    written, expectancy, cleared = [], [], []
    m.set_device_health_status = \
        lambda devid, s: written.append((devid, s))
    m._set_device_life_expectancy = \
        lambda devid, f, t=None: (expectancy.append((devid, f, t)), 0)[1]
    m._reset_device_life_expectancy = \
        lambda devid: cleared.append(devid)

    class FakeLog:
        def info(self, *args):
            pass

    m._module_logger = FakeLog()
    m._apply_prediction(dev, status)
    return written, expectancy, cleared


def device(devid='Model_Serial', **kw):
    d = {'devid': devid}
    d.update(kw)
    return d


def test_a_bad_verdict_is_published():
    written, expectancy, _ = apply_to(device(), predictor.BAD)
    assert written == [('Model_Serial', 'Bad')]
    # the band is still recorded for the thresholds
    assert len(expectancy) == 1


def test_a_warning_verdict_is_published():
    written, _, _ = apply_to(device(), predictor.WARNING)
    assert written == [('Model_Serial', 'Warning')]


def test_a_good_verdict_is_published():
    written, _, _ = apply_to(device(), predictor.GOOD)
    assert written == [('Model_Serial', 'Good')]


def test_an_unchanged_verdict_is_not_rewritten():
    written, _, _ = apply_to(device(health_status='Good'), predictor.GOOD)
    assert written == []


def test_a_changed_verdict_is_rewritten():
    written, _, _ = apply_to(device(health_status='Good'), predictor.WARNING)
    assert written == [('Model_Serial', 'Warning')]


def test_unknown_clears_a_recorded_verdict():
    written, _, _ = apply_to(device(health_status='Bad'), predictor.UNKNOWN)
    assert written == [('Model_Serial', '')]


def test_unknown_writes_nothing_when_there_is_no_verdict():
    written, _, _ = apply_to(device(), predictor.UNKNOWN)
    assert written == []


def test_a_good_verdict_that_need_not_be_refreshed_is_still_published():
    # the verdict is published before the early return for a fresh Good
    dev = device(life_expectancy_min='2999-01-01T00:00:00.000000+0000')
    written, expectancy, _ = apply_to(dev, predictor.GOOD)
    assert written == [('Model_Serial', 'Good')]
    assert expectancy == []
