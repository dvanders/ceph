#!/usr/bin/env python3
"""
Score the devicehealth SMART predictor against Backblaze drive-stats data.

Usage:

  score_smart_predictor.py data_Q1_2026.zip
  score_smart_predictor.py data_Q1_2026.zip --ruleset candidate.json

The data is a quarter of daily CSVs from
https://www.backblaze.com/cloud-storage/resources/hard-drive-test-data,
read directly from the zip.  Each drive is judged on each day as
devicehealth would judge it: the newest sample against the oldest one in
the prediction window.

With --ruleset, the candidate ruleset is scored next to the built-in rules,
and the drives whose outcome changes are counted.

Limitations of the data, which the results inherit:

- Only ATA attributes are recorded: no device statistics, SAS or NVMe
  counters, and no SMART self-assessment.
- Vendor thresholds are not recorded.  The helium thresholds (25 for ATA 22,
  75 for 23 and 24) are supplied here; other vendor-threshold rules cannot
  fire.
- Command_Timeout (188) packs three 16-bit counters into its raw value.  It
  is decoded to the low word, the leading field smartctl prints.
- Backblaze retires some drives because of these same attributes, which
  inflates recall.  A drive that fails after the quarter counts as a false
  positive, which understates precision.
"""

import argparse
import bisect
import csv
import importlib.util
import io
import json
import os
import statistics
import sys
import zipfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

PREDICTOR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         '..', 'pybind', 'mgr', 'devicehealth', 'predictor.py')

# counters for the "any nonzero counter" comparison
COUNTER_IDS = (5, 10, 184, 187, 188, 197, 198)
HELIUM_THRESHOLDS = {22: 25, 23: 75, 24: 75}
DECODE = {188: lambda raw: raw & 0xFFFF}

# follow-up after a drive first enters a tier: six weeks for Warning, two for
# Bad, matching the life expectancy bands
TIER_HORIZON_DAYS = {'Warning': 42, 'Bad': 14}


def load_predictor(path: str) -> Any:
    spec = importlib.util.spec_from_file_location('predictor', path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def to_int(value: str) -> Optional[int]:
    if value == '':
        return None
    try:
        return int(value)
    except ValueError:
        return int(float(value))


class Drive:
    __slots__ = ('model', 'first_day', 'last_day', 'failed_day', 'history',
                 'first_flag', 'first_tier', 'first_nonzero', 'helium_low',
                 'last_counters_zero')

    def __init__(self, model: str, day: int, nrulesets: int) -> None:
        self.model = model
        self.first_day = day
        self.last_day = day
        self.failed_day: Optional[int] = None
        # (day, values) at each day the values changed
        self.history: List[Tuple[int, Tuple[Optional[int], ...]]] = []
        # per ruleset: first day flagged, and first day in each tier
        self.first_flag: List[Optional[int]] = [None] * nrulesets
        self.first_tier: List[Dict[str, int]] = [{} for _ in range(nrulesets)]
        self.first_nonzero: Optional[int] = None
        self.helium_low = False
        self.last_counters_zero = True


class Scorer:
    def __init__(self, predictor: Any, rulesets: Sequence[Any],
                 window_days: int) -> None:
        self.p = predictor
        self.rulesets = rulesets
        self.window = window_days
        ids = set(COUNTER_IDS) | set(HELIUM_THRESHOLDS)
        for rs in rulesets:
            ids |= set(rs.base.ata)
            for profile in rs.profiles:
                ids |= set(profile.ata) | set(profile.disabled_ata)
        self.ids = sorted(ids)
        self.drives: Dict[str, Drive] = {}
        # (model, newest, oldest) -> the verdict of each ruleset
        self.cache: Dict[Any, Tuple[str, ...]] = {}
        self.by_model = any(rs.profiles for rs in rulesets)
        self.days = 0
        self.drive_days = 0

    def document(self, model: str,
                 values: Tuple[Optional[int], ...]) -> Dict[str, Any]:
        table = []
        for i, aid in enumerate(self.ids):
            raw, normalized = values[2 * i], values[2 * i + 1]
            if raw is None and normalized is None:
                continue
            table.append({'id': aid, 'value': normalized,
                          'thresh': HELIUM_THRESHOLDS.get(aid, 0),
                          'raw': {'value': raw,
                                  'string': '' if raw is None else str(raw)}})
        return {'model_name': model, 'ata_smart_attributes': {'table': table}}

    def verdicts(self, model: str, newest: Tuple[Optional[int], ...],
                 oldest: Optional[Tuple[Optional[int], ...]]
                 ) -> Tuple[str, ...]:
        key = (model if self.by_model else '', newest, oldest)
        statuses = self.cache.get(key)
        if statuses is None:
            samples = [self.document(model, newest)]
            if oldest is not None:
                samples.append(self.document(model, oldest))
            statuses = tuple(self.p.predict(samples, rs).status
                             for rs in self.rulesets)
            self.cache[key] = statuses
        return statuses

    def day(self, day: int, rows: Any) -> None:
        header = next(rows)
        col = {name: i for i, name in enumerate(header)}
        cols = [(col.get('smart_%d_raw' % aid), col.get('smart_%d_normalized' % aid))
                for aid in self.ids]
        i_serial, i_model, i_failure = col['serial_number'], col['model'], col['failure']
        pos = {aid: n for n, aid in enumerate(self.ids)}
        counter_pos = [2 * pos[aid] for aid in COUNTER_IDS]
        helium_pos = [(2 * pos[aid] + 1, t) for aid, t in HELIUM_THRESHOLDS.items()]
        for row in rows:
            values: List[Optional[int]] = []
            for aid, (ri, ni) in zip(self.ids, cols):
                raw = to_int(row[ri]) if ri is not None else None
                if raw is not None and aid in DECODE:
                    raw = DECODE[aid](raw)
                values.append(raw)
                values.append(to_int(row[ni]) if ni is not None else None)
            v = tuple(values)
            serial = row[i_serial]
            drive = self.drives.get(serial)
            if drive is None:
                drive = Drive(row[i_model], day, len(self.rulesets))
                self.drives[serial] = drive
            drive.last_day = day
            self.drive_days += 1
            if row[i_failure] == '1':
                drive.failed_day = day
            if not drive.history or drive.history[-1][1] != v:
                drive.history.append((day, v))

            # the oldest sample in the window, as devicehealth fetches it
            start = max(drive.first_day, day - self.window)
            days = [d for d, _ in drive.history]
            at = bisect.bisect_right(days, start) - 1
            del drive.history[:at]
            oldest = drive.history[0][1] if start < day else None

            for index, status in enumerate(
                    self.verdicts(drive.model, v, oldest)):
                if status in ('Warning', 'Bad'):
                    if drive.first_flag[index] is None:
                        drive.first_flag[index] = day
                    drive.first_tier[index].setdefault(status, day)

            nonzero = any(v[k] for k in counter_pos)
            if nonzero and drive.first_nonzero is None:
                drive.first_nonzero = day
            drive.last_counters_zero = not nonzero
            if any(v[k] is not None and v[k] <= t for k, t in helium_pos):
                drive.helium_low = True
        self.days = max(self.days, day + 1)

    def run(self, path: str, max_days: Optional[int] = None) -> None:
        with zipfile.ZipFile(path) as z:
            names = sorted(n for n in z.namelist() if n.endswith('.csv'))
            names = names[:max_days]
            for day, name in enumerate(names):
                print('reading %s' % name, file=sys.stderr)
                with z.open(name) as raw:
                    self.day(day, csv.reader(io.TextIOWrapper(
                        raw, encoding='utf-8', newline='')))

    @staticmethod
    def detection(drives: Sequence[Drive], flag_day: Any) -> Dict[str, Any]:
        failed = [d for d in drives if d.failed_day is not None]
        flagged = [d for d in drives if flag_day(d) is not None]
        caught = [d for d in failed
                  if flag_day(d) is not None and flag_day(d) <= d.failed_day]
        base = len(failed) / len(drives)
        precision = (len([d for d in flagged if d.failed_day is not None])
                     / len(flagged)) if flagged else 0.0
        return {
            'flagged_drives': len(flagged),
            'flagged_fraction': len(flagged) / len(drives),
            'recall': len(caught) / len(failed) if failed else 0.0,
            'median_lead_days': statistics.median(
                d.failed_day - flag_day(d) for d in caught) if caught else None,
            'lift': precision / base if base else None,
            'missed_with_no_signal': (
                len([d for d in failed if d not in caught
                     and d.last_counters_zero])
                / max(1, len(failed) - len(caught))),
        }

    def tiers(self, index: int) -> Dict[str, Any]:
        drives = list(self.drives.values())
        end = self.days - 1
        out = {}
        for tier, horizon in TIER_HORIZON_DAYS.items():
            def rate(entries: List[Tuple[Drive, int]]) -> Dict[str, Any]:
                entries = [(d, day) for d, day in entries if day + horizon <= end]
                failed = [d for d, day in entries if d.failed_day is not None
                          and day <= d.failed_day <= day + horizon]
                return {'drives': len(entries),
                        'failed_within_horizon':
                            len(failed) / len(entries) if entries else None}
            out[tier] = {
                'horizon_days': horizon,
                'entered': rate([(d, d.first_tier[index][tier]) for d in drives
                                 if tier in d.first_tier[index]]),
                'never_flagged': rate([(d, d.first_day) for d in drives
                                       if d.first_flag[index] is None]),
            }
        return out

    def report(self) -> Dict[str, Any]:
        drives = list(self.drives.values())
        failed = [d for d in drives if d.failed_day is not None]
        helium = [d for d in drives if d.helium_low]
        base = len(failed) / len(drives)
        helium_failed = [d for d in helium if d.failed_day is not None]
        out: Dict[str, Any] = {
            'predictor': self.p.__file__,
            'days': self.days,
            'drives': len(drives),
            'drive_days': self.drive_days,
            'failures': len(failed),
            'window_days': self.window,
            'nonzero_counter': self.detection(drives, lambda d: d.first_nonzero),
            'helium_at_threshold': {
                'drives': len(helium),
                'lift': (len(helium_failed) / len(helium) / base)
                if helium and base else None,
            },
            'rulesets': [],
        }
        for index, rs in enumerate(self.rulesets):
            result = self.detection(drives, lambda d: d.first_flag[index])
            result['name'] = rs.name
            result['tiers'] = self.tiers(index)
            out['rulesets'].append(result)
        if len(self.rulesets) == 2:
            def caught(d: Drive, i: int) -> bool:
                return (d.first_flag[i] is not None and d.failed_day is not None
                        and d.first_flag[i] <= d.failed_day)
            out['changes'] = {
                'newly_flagged_drives': len([
                    d for d in drives
                    if d.first_flag[0] is None and d.first_flag[1] is not None]),
                'no_longer_flagged_drives': len([
                    d for d in drives
                    if d.first_flag[0] is not None and d.first_flag[1] is None]),
                'newly_caught_failures': len([
                    d for d in failed if not caught(d, 0) and caught(d, 1)]),
                'newly_missed_failures': len([
                    d for d in failed if caught(d, 0) and not caught(d, 1)]),
            }
        return out


def pct(x: Optional[float]) -> str:
    return '-' if x is None else '%.1f%%' % (100 * x)


def times(x: Optional[float]) -> str:
    return '-' if x is None else '%.1fx' % x


def print_report(r: Dict[str, Any]) -> None:
    print('%d drives, %d drive-days, %d failures over %d days; '
          'prediction window %d days'
          % (r['drives'], r['drive_days'], r['failures'], r['days'],
             r['window_days']))
    print()
    columns = [(rs['name'], rs) for rs in r['rulesets']]
    columns.append(('any nonzero counter', r['nonzero_counter']))
    width = max(22, *(len(name) + 2 for name, _ in columns))
    rows = [
        ('drives flagged', lambda c: pct(c['flagged_fraction'])),
        ('failures flagged', lambda c: pct(c['recall'])),
        ('median days of warning', lambda c: '-' if c['median_lead_days'] is None
         else '%g' % c['median_lead_days']),
        ('lift over base rate', lambda c: times(c['lift'])),
        ('missed, no counter set', lambda c: pct(c['missed_with_no_signal'])),
    ]
    print(' ' * 26 + ''.join(name.ljust(width) for name, _ in columns))
    for label, fmt in rows:
        print(label.ljust(26) + ''.join(fmt(c).ljust(width) for _, c in columns))
    for rs in r['rulesets']:
        print()
        print('%s, first entry into each tier (drives with full follow-up):'
              % rs['name'])
        for tier, t in rs['tiers'].items():
            print('  %-8s %s of %d failed within %d days; %s of %d never-flagged'
                  % (tier, pct(t['entered']['failed_within_horizon']),
                     t['entered']['drives'], t['horizon_days'],
                     pct(t['never_flagged']['failed_within_horizon']),
                     t['never_flagged']['drives']))
    h = r['helium_at_threshold']
    print()
    print('helium at or below vendor threshold: %d drives, %s the base rate'
          % (h['drives'], times(h['lift'])))
    if 'changes' in r:
        c = r['changes']
        print()
        print('%s compared with %s:' % (r['rulesets'][1]['name'],
                                        r['rulesets'][0]['name']))
        print('  %d drives newly flagged, %d no longer flagged'
              % (c['newly_flagged_drives'], c['no_longer_flagged_drives']))
        print('  %d failures newly caught, %d newly missed'
              % (c['newly_caught_failures'], c['newly_missed_failures']))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split('\n\n')[0].strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('data', help='Backblaze drive-stats zip for a quarter')
    parser.add_argument('--ruleset', help='candidate ruleset JSON to compare')
    parser.add_argument('--window-days', type=int, default=30,
                        help='prediction window (default 30)')
    parser.add_argument('--predictor', default=PREDICTOR,
                        help='predictor.py to score (default: this tree)')
    parser.add_argument('--max-days', type=int,
                        help='read only the first N days, for a quick trial')
    parser.add_argument('--json', action='store_true',
                        help='print the results as JSON')
    args = parser.parse_args()

    predictor = load_predictor(args.predictor)
    rulesets = [predictor.BUILTIN_RULESET]
    if args.ruleset:
        with open(args.ruleset) as f:
            rulesets.append(predictor.load_ruleset(json.load(f)))
    scorer = Scorer(predictor, rulesets, args.window_days)
    scorer.run(args.data, args.max_days)
    report = scorer.report()
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)
    return 0


if __name__ == '__main__':
    sys.exit(main())
