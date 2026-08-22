# type: ignore
from datetime import datetime

from devicehealth.module import Module, TIME_FORMAT


def test_sample_timestamps_round_trip():
    # a timestamp printed by 'device get-health-metrics' must select the
    # same sample when passed back, whatever the mgr's timezone
    for epoch in (0, 1700000000, 1780000000):
        stamp = datetime.utcfromtimestamp(epoch).strftime(TIME_FORMAT)
        assert Module._t2epoch(None, stamp) == epoch
    assert Module._t2epoch(None, None) == 0
