# type: ignore
"""Tests for src/script/score_smart_predictor.py, on a synthetic zip."""

import importlib.util
import os
import zipfile

import pytest

SCRIPT = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'script',
                      'score_smart_predictor.py')

COLUMNS = ['date', 'serial_number', 'model', 'capacity_bytes', 'failure']
for aid in (5, 22, 188, 197, 200):
    COLUMNS += ['smart_%d_normalized' % aid, 'smart_%d_raw' % aid]


@pytest.fixture(scope='module')
def score():
    spec = importlib.util.spec_from_file_location('score', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row(day, serial, model='MODEL A', failure=0, **attrs):
    values = {'date': '2026-01-%02d' % (day + 1), 'serial_number': serial,
              'model': model, 'capacity_bytes': '1', 'failure': str(failure)}
    for key, value in attrs.items():
        kind, aid = key[0], key[1:]
        values['smart_%s_%s' % (aid, 'raw' if kind == 'r' else 'normalized')] \
            = str(value)
    return ','.join(values.get(c, '') for c in COLUMNS)


def make_zip(path, days):
    with zipfile.ZipFile(path, 'w') as z:
        for day, rows in enumerate(days):
            body = '\n'.join([','.join(COLUMNS)] + rows) + '\n'
            z.writestr('q/2026-01-%02d.csv' % (day + 1), body)


def run(score, tmp_path, days, ruleset=None):
    path = str(tmp_path / 'q.zip')
    make_zip(path, days)
    predictor = score.load_predictor(score.PREDICTOR)
    rulesets = [predictor.BUILTIN_RULESET]
    if ruleset:
        rulesets.append(predictor.load_ruleset(ruleset))
    scorer = score.Scorer(predictor, rulesets, window_days=30)
    scorer.run(path)
    return scorer.report()


def fleet():
    """Four drives over three days:

    - grows: reallocated sectors grow on day 1, fails on day 2
    - settled: a constant 8 reallocated sectors, healthy
    - quiet: all zero, fails on day 2 with no warning
    - timeouts: Command_Timeout packed as 0x0001_0001_0001, constant
    """
    packed = (1 << 32) | (1 << 16) | 1
    return [
        [row(0, 'grows', r5=0), row(0, 'settled', r5=8), row(0, 'quiet', r5=0),
         row(0, 'timeouts', r188=packed)],
        [row(1, 'grows', r5=4), row(1, 'settled', r5=8), row(1, 'quiet', r5=0),
         row(1, 'timeouts', r188=packed)],
        [row(2, 'grows', r5=4, failure=1), row(2, 'settled', r5=8),
         row(2, 'quiet', r5=0, failure=1), row(2, 'timeouts', r188=packed)],
    ]


def test_growth_is_caught_and_settled_counts_are_not(score, tmp_path):
    r = run(score, tmp_path, fleet())
    assert (r['drives'], r['drive_days'], r['failures'], r['days']) \
        == (4, 12, 2, 3)
    builtin = r['rulesets'][0]
    assert builtin['flagged_drives'] == 1          # only 'grows'
    assert builtin['recall'] == 0.5
    assert builtin['median_lead_days'] == 1        # flagged day 1, failed day 2
    assert builtin['missed_with_no_signal'] == 1.0  # 'quiet' had no counters


def test_any_nonzero_counter_flags_settled_drives(score, tmp_path):
    r = run(score, tmp_path, fleet())
    # grows, settled and timeouts; 188 decodes to its low word, 1
    assert r['nonzero_counter']['flagged_drives'] == 3


def test_a_candidate_ruleset_is_compared(score, tmp_path):
    candidate = {'ruleset': 'site-test', 'profiles': [{
        'name': 'p', 'match': {'model_name': 'MODEL A'},
        'ata': {'5': {'disabled': True}}}]}
    r = run(score, tmp_path, fleet(), candidate)
    assert r['rulesets'][1]['name'] == 'site-test'
    assert r['rulesets'][1]['flagged_drives'] == 0
    assert r['changes'] == {'newly_flagged_drives': 0,
                            'no_longer_flagged_drives': 1,
                            'newly_caught_failures': 0,
                            'newly_missed_failures': 1}


def test_helium_at_threshold_is_counted(score, tmp_path):
    days = [[row(0, 'he', n22=25, r22=25), row(0, 'ok', n22=100, r22=100)]]
    r = run(score, tmp_path, days)
    assert r['helium_at_threshold']['drives'] == 1
    assert r['rulesets'][0]['flagged_drives'] == 1  # Bad at the threshold
