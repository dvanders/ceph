"""
Rule-based device failure prediction from smartctl output.

Uses only attributes that mean the same thing across vendors (ATA 5, 10,
184, 187, 188, 197, 198 and the helium levels), plus SAS and NVMe health
counters.  Vendor-specific attributes such as 1, 7, 199 and 201 are ignored.

*absolute* rules look at the newest sample; *growth* rules compare the
newest and oldest samples in the window.  Defect counters use growth, so a
drive with a few long-settled reallocated sectors is not flagged.
"""

import fnmatch
from typing import (Any, Dict, List, Mapping, NamedTuple, Optional, Sequence,
                    Set, Tuple, TypeVar)

K = TypeVar('K')

# Verdicts, in increasing order of severity.  UNKNOWN means no usable data.
UNKNOWN = 'Unknown'
GOOD = 'Good'
WARNING = 'Warning'
BAD = 'Bad'

_SEVERITY = {UNKNOWN: 0, GOOD: 1, WARNING: 2, BAD: 3}


class AtaRule(NamedTuple):
    name: str
    absolute: Optional[int]  # newest sample >= this -> Warning
    growth: Optional[int]    # increase across the window >= this -> Warning


# Raw counters that mean the same thing across vendors, judged on growth.
# Current_Pending_Sector is a gauge (it falls as sectors are remapped), so it
# also has an absolute rule.
ATA_RULES: Dict[int, AtaRule] = {
    5: AtaRule('Reallocated_Sector_Ct', absolute=None, growth=1),
    10: AtaRule('Spin_Retry_Count', absolute=None, growth=1),
    184: AtaRule('End-to-End_Error', absolute=None, growth=1),
    187: AtaRule('Reported_Uncorrect', absolute=None, growth=1),
    188: AtaRule('Command_Timeout', absolute=None, growth=2),
    197: AtaRule('Current_Pending_Sector', absolute=1, growth=1),
    198: AtaRule('Offline_Uncorrectable', absolute=None, growth=1),
    # Helium: WDC/HGST report a level at 22 (threshold 25), Toshiba a pair at
    # 23 and 24 (threshold 75).  The raw value is a level, not a count, so
    # only the normalized margin is judged.
    22: AtaRule('Helium_Level', absolute=None, growth=None),
    23: AtaRule('Helium_Condition_Lower', absolute=None, growth=None),
    24: AtaRule('Helium_Condition_Upper', absolute=None, growth=None),
}


class DevStat(NamedTuple):
    name: str
    page: int
    offset: int
    absolute: Optional[int]
    growth: Optional[int]
    supersedes: Optional[int]  # ATA attribute whose raw rules this replaces


# Standardised counters from the ACS Device Statistics log (GP Log 0x04),
# preferred over the attributes they supersede.  Many enterprise HDDs lack
# attributes 187 and 188 but report these.  Only entries flagged valid are
# read.  The reallocation candidate count is a gauge, like
# Current_Pending_Sector.
DEVICE_STATISTICS: Tuple[DevStat, ...] = (
    DevStat('Reallocated Logical Sectors', 3, 32, None, 1, 5),
    DevStat('Mechanical Start Failures', 3, 48, None, 1, 10),
    DevStat('Reallocation Candidate Logical Sectors', 3, 56, 1, 1, 197),
    DevStat('Reported Uncorrectable Errors', 4, 8, None, 1, 187),
    DevStat('Resets Between Command Acceptance and Completion',
            4, 16, None, 2, 188),
)

# Page 7 offset 8 is the percentage-used endurance indicator, read via
# get_ata_wear_level() rather than as a counter rule.
WEAR_STATISTIC = (7, 8)

# Warn when the normalized value is within this fraction of the range from
# fresh to the vendor threshold.  A fixed number of points would not work:
# the Intel DC S4510 ships attribute 184 at 100 with a threshold of 90.
# Applied only to ATA_RULES; temperature attributes would otherwise trip it.
NORMALIZED_HEADROOM_FRACTION = 0.1

# Possible fresh normalized values.  smartctl does not report which one a
# drive uses, so take the smallest that is >= the current value.
NORMALIZED_FRESH_VALUES = (100, 200, 253)

# Fraction of rated endurance, from device statistics page 7, that warns.
WEAR_WARNING = 0.9

# NVMe SMART / Health Information log, critical warning (byte 0).
NVME_CRITICAL_WARNING_BITS: List[Tuple[int, str, str]] = [
    (0x01, 'available spare capacity has fallen below the threshold', WARNING),
    (0x02, 'temperature is outside of an operating range', WARNING),
    (0x04, 'reliability degraded due to media or internal errors', BAD),
    (0x08, 'media has been placed in read-only mode', BAD),
    (0x10, 'volatile memory backup device has failed', WARNING),
    (0x20, 'persistent memory region is read-only or unreliable', WARNING),
]

NVME_SPARE_HEADROOM = 10  # percent above available_spare_threshold
NVME_USED_WARNING = 90    # percentage_used; WEAR_WARNING for NVMe

SCSI_DEFECT_GROWTH = 1   # grown defect list entries added over the window
SCSI_ERROR_GROWTH = 1    # uncorrected errors added over the window

# Any defect counter at or above this warns, to catch damage older than the
# prediction window.  Observed counts are either single digits or in the
# hundreds and up, so this sits in the gap rather than being tuned.
COUNTER_BACKSTOP = 256


class Rules(NamedTuple):
    """The rules in effect for one device, after profiles are resolved."""
    ata: Mapping[int, AtaRule]
    device_statistics: Tuple[DevStat, ...]
    # (status, reason) asserted from the device's identity alone
    findings: Tuple[Tuple[str, str], ...]
    counter_backstop: int
    normalized_headroom_fraction: float
    wear_warning: float
    nvme_spare_headroom: int
    nvme_used_warning: int
    scsi_defect_growth: int
    scsi_error_growth: int
    # rules turned off by a profile, for explain-health
    disabled: Tuple[str, ...] = ()


# The tunable scalars, by the name a ruleset document uses for each.
SCALARS: Tuple[str, ...] = (
    'counter_backstop', 'normalized_headroom_fraction', 'wear_warning',
    'nvme_spare_headroom', 'nvme_used_warning', 'scsi_defect_growth',
    'scsi_error_growth',
)

BUILTIN_RULES = Rules(
    ata=ATA_RULES,
    device_statistics=DEVICE_STATISTICS,
    findings=(),
    counter_backstop=COUNTER_BACKSTOP,
    normalized_headroom_fraction=NORMALIZED_HEADROOM_FRACTION,
    wear_warning=WEAR_WARNING,
    nvme_spare_headroom=NVME_SPARE_HEADROOM,
    nvme_used_warning=NVME_USED_WARNING,
    scsi_defect_growth=SCSI_DEFECT_GROWTH,
    scsi_error_growth=SCSI_ERROR_GROWTH,
)

# smartctl fields a profile may match on: identity only, never state
MATCH_FIELDS: Tuple[str, ...] = (
    'model_name', 'model_family', 'firmware_version', 'vendor', 'product',
)


class Profile(NamedTuple):
    """Rule overrides that apply only to the devices a pattern matches."""
    name: str
    match: Mapping[str, str]
    ata: Mapping[int, AtaRule]
    device_statistics: Tuple[DevStat, ...]
    findings: Tuple[Tuple[str, str], ...]
    scalars: Mapping[str, Any]
    # rules this profile turns off: ATA id -> name, (page, offset) -> name
    disabled_ata: Mapping[int, str] = {}
    disabled_stats: Mapping[Tuple[int, int], str] = {}

    def matches(self, data: Dict[str, Any]) -> bool:
        # every named field must match; a missing field never matches
        for field, pattern in self.match.items():
            value = data.get(field)
            if not isinstance(value, str):
                return False
            if not fnmatch.fnmatch(value.lower(), pattern.lower()):
                return False
        return True


class Ruleset(NamedTuple):
    """A base set of rules plus any per-model overrides."""
    name: str
    base: Rules
    profiles: Tuple[Profile, ...]

    def resolve(self, data: Dict[str, Any]) -> Tuple[Rules, List[str]]:
        """The rules for this device, and the profiles that shaped them."""
        rules = self.base
        applied: List[str] = []
        for profile in self.profiles:
            if not profile.matches(data):
                continue
            applied.append(profile.name)
            ata = dict(rules.ata)
            ata.update(profile.ata)
            stats = {(d.page, d.offset): d for d in rules.device_statistics}
            for stat in profile.device_statistics:
                stats[(stat.page, stat.offset)] = stat
            disabled = list(rules.disabled)
            for aid, name in sorted(profile.disabled_ata.items()):
                rule = ata.pop(aid, None)
                disabled.append('%s (ATA %d) by profile %s'
                                % (name or (rule.name if rule else '?'),
                                   aid, profile.name))
            for key, name in sorted(profile.disabled_stats.items()):
                stat = stats.pop(key, None)
                disabled.append('%s (device statistics page %d offset %d) '
                                'by profile %s'
                                % (name or (stat.name if stat else '?'),
                                   key[0], key[1], profile.name))
            rules = rules._replace(
                ata=ata,
                device_statistics=tuple(stats[k] for k in sorted(stats)),
                findings=rules.findings + profile.findings,
                disabled=tuple(disabled),
                **profile.scalars)
        return rules, applied


BUILTIN_RULESET = Ruleset(name='builtin', base=BUILTIN_RULES, profiles=())


class Prediction(NamedTuple):
    """A verdict plus the human-readable reasons that produced it."""
    status: str
    reasons: List[str]
    # the ruleset and profiles that produced it
    ruleset: str = 'builtin'
    profiles: Tuple[str, ...] = ()
    disabled: Tuple[str, ...] = ()


class Sample(NamedTuple):
    """One scrape, reduced to the fields the rules care about."""
    passed: Optional[bool]
    ata_raw: Dict[int, int]
    ata_normalized: Dict[int, int]
    ata_threshold: Dict[int, int]
    dev_stats: Dict[Tuple[int, int], int]
    monitored: List[str]
    nvme: Dict[str, int]
    scsi: Dict[str, int]
    wear: Optional[float]

    @property
    def has_data(self) -> bool:
        return bool(self.passed is not None or self.ata_raw or self.nvme
                    or self.scsi or self.dev_stats or self.wear is not None)


class RulesetError(ValueError):
    """A ruleset document was rejected."""


# (type, min, max) per scalar.  The bounds keep a new, healthy device from
# being flagged: counters at zero, wear and percentage used near 0%, NVMe
# spare at 100% against a typical threshold of 10%, and normalized values at
# their fresh value.
_SCALAR_LIMITS: Dict[str, Tuple[type, Any, Any]] = {
    'counter_backstop': (int, 1, None),
    'normalized_headroom_fraction': (float, 0.0, 0.5),
    'wear_warning': (float, 0.5, 1.0),
    'nvme_spare_headroom': (int, 0, 50),
    'nvme_used_warning': (int, 50, 255),
    'scsi_defect_growth': (int, 1, None),
    'scsi_error_growth': (int, 1, None),
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RulesetError(message)


def _known_keys(where: str, doc: Any, allowed: Sequence[str]) -> None:
    _require(isinstance(doc, dict), '%s must be an object' % where)
    unknown = sorted(set(doc) - set(allowed))
    _require(not unknown, '%s has unknown key(s): %s'
             % (where, ', '.join(unknown)))


def _load_scalars(where: str, doc: Any) -> Dict[str, Any]:
    if doc is None:
        return {}
    _known_keys(where, doc, SCALARS)
    out: Dict[str, Any] = {}
    for key, value in doc.items():
        want, low, high = _SCALAR_LIMITS[key]
        if want is float:
            _require(isinstance(value, (int, float))
                     and not isinstance(value, bool),
                     '%s/%s must be a number' % (where, key))
            value = float(value)
        else:
            _require(isinstance(value, int) and not isinstance(value, bool),
                     '%s/%s must be an integer' % (where, key))
        _require(low is None or value >= low,
                 '%s/%s must be at least %s' % (where, key, low))
        _require(high is None or value <= high,
                 '%s/%s must be at most %s' % (where, key, high))
        out[key] = value
    return out


def _load_counter(where: str, doc: Any) -> Tuple[Optional[int], Optional[int]]:
    absolute = doc.get('absolute')
    growth = doc.get('growth')
    for label, value in (('absolute', absolute), ('growth', growth)):
        if value is None:
            continue
        _require(isinstance(value, int) and not isinstance(value, bool),
                 '%s/%s must be an integer or null' % (where, label))
        _require(value >= 1, '%s/%s must be at least 1' % (where, label))
    return absolute, growth


def _load_disabled(at: str, entry: Dict[str, Any], allow: bool,
                   extra: Sequence[str] = ()) -> bool:
    """True if this rule entry turns a rule off."""
    disabled = entry.get('disabled', False)
    _require(isinstance(disabled, bool), '%s/disabled must be true or false' % at)
    if not disabled:
        return False
    _require(allow, '%s/disabled is only allowed in a profile, which says '
                    'which devices it applies to' % at)
    _known_keys(at, entry, ('name', 'disabled') + tuple(extra))
    name = entry.get('name', '')
    _require(isinstance(name, str), '%s/name must be a string' % at)
    return True


def _require_threshold(at: str, absolute: Optional[int],
                       growth: Optional[int], old: Any) -> None:
    # a rule without thresholds treats the raw value as a level; replacing a
    # counter rule that way would silently stop judging the counter
    _require(absolute is not None or growth is not None or old is None
             or (old.absolute is None and old.growth is None),
             '%s has neither absolute nor growth, which would stop judging '
             'the counter "%s"; to turn a rule off, set "disabled": true in '
             'a profile' % (at, old.name if old else ''))


def _load_ata(where: str, doc: Any, base: Mapping[int, AtaRule],
              allow_disable: bool
              ) -> Tuple[Dict[int, AtaRule], Dict[int, str]]:
    if doc is None:
        return {}, {}
    _require(isinstance(doc, dict), '%s must be an object' % where)
    out: Dict[int, AtaRule] = {}
    disabled: Dict[int, str] = {}
    for key, entry in doc.items():
        try:
            aid = int(key)
        except (TypeError, ValueError):
            raise RulesetError('%s has a non-numeric attribute id: %r'
                               % (where, key))
        _require(0 <= aid <= 255,
                 '%s/%s is not a SMART attribute id' % (where, key))
        at = '%s/%s' % (where, key)
        _known_keys(at, entry, ('name', 'absolute', 'growth', 'disabled'))
        if _load_disabled(at, entry, allow_disable):
            disabled[aid] = entry.get('name', '')
            continue
        name = entry.get('name')
        _require(isinstance(name, str) and name != '',
                 '%s/name must be a non-empty string' % at)
        absolute, growth = _load_counter(at, entry)
        _require_threshold(at, absolute, growth, base.get(aid))
        out[aid] = AtaRule(name, absolute, growth)
    return out, disabled


def _load_device_statistics(
        where: str, doc: Any, base: Sequence[DevStat], allow_disable: bool
) -> Tuple[Tuple[DevStat, ...], Dict[Tuple[int, int], str]]:
    if doc is None:
        return (), {}
    _require(isinstance(doc, list), '%s must be an array' % where)
    known = {(d.page, d.offset): d for d in base}
    out = []
    disabled: Dict[Tuple[int, int], str] = {}
    for i, entry in enumerate(doc):
        at = '%s[%d]' % (where, i)
        _known_keys(at, entry, ('name', 'page', 'offset', 'absolute',
                                'growth', 'supersedes', 'disabled'))
        for field in ('page', 'offset'):
            value = entry.get(field)
            _require(isinstance(value, int) and not isinstance(value, bool)
                     and value >= 0,
                     '%s/%s must be a non-negative integer' % (at, field))
        key = (entry['page'], entry['offset'])
        if _load_disabled(at, entry, allow_disable, ('page', 'offset')):
            disabled[key] = entry.get('name', '')
            continue
        name = entry.get('name')
        _require(isinstance(name, str) and name != '',
                 '%s/name must be a non-empty string' % at)
        supersedes = entry.get('supersedes')
        _require(supersedes is None
                 or (isinstance(supersedes, int)
                     and not isinstance(supersedes, bool)
                     and 0 <= supersedes <= 255),
                 '%s/supersedes must be a SMART attribute id or null' % at)
        absolute, growth = _load_counter(at, entry)
        _require_threshold(at, absolute, growth, known.get(key))
        out.append(DevStat(name, entry['page'], entry['offset'],
                           absolute, growth, supersedes))
    return tuple(out), disabled


def _load_findings(where: str, doc: Any) -> Tuple[Tuple[str, str], ...]:
    """Verdicts a profile asserts from the device's identity alone.

    Only Warning and Bad are allowed, so a ruleset cannot hide a failure.
    """
    if doc is None:
        return ()
    _require(isinstance(doc, list), '%s must be an array' % where)
    out = []
    for i, entry in enumerate(doc):
        at = '%s[%d]' % (where, i)
        _known_keys(at, entry, ('status', 'reason'))
        status = entry.get('status')
        _require(status in (WARNING, BAD),
                 '%s/status must be "%s" or "%s"' % (at, WARNING, BAD))
        reason = entry.get('reason')
        _require(isinstance(reason, str) and reason != '',
                 '%s/reason must be a non-empty string saying why' % at)
        out.append((status, reason))
    return tuple(out)


def load_ruleset(doc: Any) -> Ruleset:
    """Build a :class:`Ruleset` from a parsed ruleset document.

    Raises :class:`RulesetError` if the document is invalid.
    """
    # 'findings' are only allowed in a profile, or they would match every
    # device
    _known_keys('ruleset document', doc,
                ('ruleset', 'defaults', 'ata', 'device_statistics',
                 'profiles'))
    name = doc.get('ruleset')
    _require(isinstance(name, str) and name != '',
             'ruleset must be a non-empty string naming this ruleset')

    ata = dict(BUILTIN_RULES.ata)
    ata.update(_load_ata('ata', doc.get('ata'), BUILTIN_RULES.ata, False)[0])
    stats = {(d.page, d.offset): d for d in BUILTIN_RULES.device_statistics}
    for stat in _load_device_statistics(
            'device_statistics', doc.get('device_statistics'),
            BUILTIN_RULES.device_statistics, False)[0]:
        stats[(stat.page, stat.offset)] = stat
    base = BUILTIN_RULES._replace(
        ata=ata,
        device_statistics=tuple(stats[k] for k in sorted(stats)),
        **_load_scalars('defaults', doc.get('defaults')))

    profiles = []
    raw_profiles = doc.get('profiles') or []
    _require(isinstance(raw_profiles, list), 'profiles must be an array')
    for i, entry in enumerate(raw_profiles):
        at = 'profiles[%d]' % i
        _known_keys(at, entry, ('name', 'match', 'defaults', 'ata',
                                'device_statistics', 'findings'))
        pname = entry.get('name')
        _require(isinstance(pname, str) and pname != '',
                 '%s/name must be a non-empty string' % at)
        match = entry.get('match')
        _known_keys('%s/match' % at, match, MATCH_FIELDS)
        _require(bool(match), '%s/match must name at least one field, or the '
                              'profile would apply to every device' % at)
        for field, pattern in match.items():
            _require(isinstance(pattern, str) and pattern != '',
                     '%s/match/%s must be a non-empty pattern' % (at, field))
        p_ata, p_ata_off = _load_ata('%s/ata' % at, entry.get('ata'),
                                     base.ata, True)
        p_stats, p_stats_off = _load_device_statistics(
            '%s/device_statistics' % at, entry.get('device_statistics'),
            base.device_statistics, True)
        profiles.append(Profile(
            name=pname,
            match=dict(match),
            ata=p_ata,
            device_statistics=p_stats,
            findings=_load_findings('%s/findings' % at,
                                    entry.get('findings')),
            scalars=_load_scalars('%s/defaults' % at, entry.get('defaults')),
            disabled_ata=p_ata_off,
            disabled_stats=p_stats_off))

    return Ruleset(name=name, base=base, profiles=tuple(profiles))


def dump_ruleset(ruleset: Ruleset) -> Dict[str, Any]:
    """Render a ruleset back to the document form load_ruleset() accepts."""
    def counters(rule: Any) -> Dict[str, Any]:
        return {'absolute': rule.absolute, 'growth': rule.growth}

    def stats(entries: Sequence[DevStat]) -> List[Dict[str, Any]]:
        return [dict(name=d.name, page=d.page, offset=d.offset,
                     supersedes=d.supersedes, **counters(d))
                for d in entries]

    return {
        'ruleset': ruleset.name,
        'defaults': {key: getattr(ruleset.base, key) for key in SCALARS},
        'ata': {str(aid): dict(name=rule.name, **counters(rule))
                for aid, rule in sorted(ruleset.base.ata.items())},
        'device_statistics': stats(ruleset.base.device_statistics),
        'profiles': [{'name': p.name,
                      'match': dict(p.match),
                      'defaults': dict(p.scalars),
                      'findings': [{'status': st, 'reason': why}
                                   for st, why in p.findings],
                      'ata': dict(
                          [(str(aid), dict(name=rule.name, **counters(rule)))
                           for aid, rule in sorted(p.ata.items())]
                          + [(str(aid), {'name': name, 'disabled': True})
                             for aid, name in sorted(p.disabled_ata.items())]),
                      'device_statistics': stats(p.device_statistics)
                      + [{'name': name, 'page': page, 'offset': offset,
                          'disabled': True}
                         for (page, offset), name
                         in sorted(p.disabled_stats.items())]}
                     for p in ruleset.profiles],
    }


def get_ata_wear_level(data: Dict[Any, Any]) -> Optional[float]:
    """
    Extract wear level (as float) from smartctl -x --json output for SATA SSD
    """
    for page in data.get("ata_device_statistics", {}).get("pages", []):
        if page is None or page.get("number") != 7:
            continue
        for item in page.get("table", []):
            if item["offset"] == 8:
                return item["value"] / 100.0
    return None


def get_nvme_wear_level(data: Dict[Any, Any]) -> Optional[float]:
    """
    Extract wear level (as float) from smartctl -x --json output for NVME SSD
    """
    pct_used = data.get("nvme_smart_health_information_log", {}).get("percentage_used")
    if pct_used is None:
        return None
    return pct_used / 100.0


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _attr_raw_value(attr: Dict[str, Any]) -> Optional[int]:
    """
    The raw value of an ATA attribute.

    Prefer the leading field of the decoded string (e.g. '35 (Min/Max
    20/45)', or '0 0 0' for Command_Timeout) over the packed 48-bit integer.
    """
    raw = attr.get('raw', {})
    if isinstance(raw, dict):
        string = raw.get('string')
        if string is not None:
            token = str(string).split(' ')[0].replace(',', '')
            if token.lstrip('-').isdigit():
                return int(token)
        return _int(raw.get('value'))
    return None


def smartctl_error(data: Dict[str, Any]) -> Optional[str]:
    """
    Describe the failure if this sample is an error stub.

    block_device_get_metrics() stores one when smartctl fails, e.g.

        {"error": "smartctl failed", "dev": "/dev/sdb",
         "smartctl_error_code": -22, "smartctl_output": "..."}
    """
    error = data.get('error')
    if not error:
        return None
    code = data.get('smartctl_error_code')
    detail = '%s' % error
    if code is not None:
        detail += ' (smartctl_error_code %s)' % code
    output = str(data.get('smartctl_output') or data.get('output') or '')
    lines = output.strip().splitlines()
    if lines:
        detail += ': %s' % lines[-1].strip()[:160]
    return detail


def parse_sample(data: Dict[str, Any]) -> Sample:
    """Reduce one raw smartctl JSON document to a :class:`Sample`."""
    ata_raw: Dict[int, int] = {}
    ata_normalized: Dict[int, int] = {}
    ata_threshold: Dict[int, int] = {}

    for attr in data.get('ata_smart_attributes', {}).get('table', []):
        aid = _int(attr.get('id'))
        if aid is None:
            continue
        raw_value = _attr_raw_value(attr)
        if raw_value is not None:
            ata_raw[aid] = raw_value
        normalized = _int(attr.get('value'))
        if normalized is not None:
            ata_normalized[aid] = normalized
        threshold = _int(attr.get('thresh'))
        if threshold is not None:
            ata_threshold[aid] = threshold

    nvme: Dict[str, int] = {}
    nvme_log = data.get('nvme_smart_health_information_log', {})
    for key in ('critical_warning', 'available_spare',
                'available_spare_threshold', 'percentage_used',
                'media_errors'):
        value = _int(nvme_log.get(key))
        if value is not None:
            nvme[key] = value

    scsi: Dict[str, int] = {}
    defects = _int(data.get('scsi_grown_defect_list'))
    if defects is not None:
        scsi['grown_defect_list'] = defects
    counters = data.get('scsi_error_counter_log', {})
    for key in ('read', 'write', 'verify'):
        errors = _int(counters.get(key, {}).get('total_uncorrected_errors'))
        if errors is not None:
            scsi['%s_uncorrected' % key] = errors

    dev_stats: Dict[Tuple[int, int], int] = {}
    monitored: List[str] = []
    for page in data.get('ata_device_statistics', {}).get('pages', []):
        if not isinstance(page, dict):
            continue
        number = _int(page.get('number'))
        # a supported-but-empty page carries no table at all, e.g. the solid
        # state page on a spinning disk
        for item in page.get('table') or []:
            if not isinstance(item, dict) or number is None:
                continue
            offset = _int(item.get('offset'))
            value = _int(item.get('value'))
            raw_flags = item.get('flags')
            flags: Dict[str, Any] = {}
            if isinstance(raw_flags, dict):
                flags = raw_flags
            if offset is None or value is None or not flags.get('valid'):
                continue
            dev_stats[(number, offset)] = value
            if flags.get('monitored_condition_met'):
                label = item.get('name')
                if not label:
                    label = 'page %d offset %d' % (number, offset)
                monitored.append(str(label))

    passed = data.get('smart_status', {}).get('passed')
    if not isinstance(passed, bool):
        passed = None

    # NVMe endurance is percentage_used, which the NVMe rules already cover
    wear = get_ata_wear_level(data)

    return Sample(passed=passed,
                  ata_raw=ata_raw,
                  ata_normalized=ata_normalized,
                  ata_threshold=ata_threshold,
                  dev_stats=dev_stats,
                  monitored=monitored,
                  nvme=nvme,
                  scsi=scsi,
                  wear=wear)


def _growth(newest: Mapping[K, int], oldest: Mapping[K, int],
            key: K) -> Optional[int]:
    """
    How much a counter grew across the window, or None if unknown.  A
    counter that went backwards (firmware reset, reused device id) is 0.
    """
    new = newest.get(key)
    old = oldest.get(key)
    if new is None or old is None:
        return None
    return max(0, new - old)


def _normalized_span(normalized: int, threshold: int) -> int:
    """Size of the range this attribute must fall through before failing."""
    for fresh in NORMALIZED_FRESH_VALUES:
        if normalized <= fresh:
            return fresh - threshold
    return normalized - threshold


def _counter_finding(label: str,
                     absolute: Optional[int],
                     growth: Optional[int],
                     value: Optional[int],
                     grew: Optional[int],
                     backstop: int = COUNTER_BACKSTOP
                     ) -> Optional[Tuple[str, str]]:
    """
    Judge one counter, returning at most one finding.

    'absolute' is set only for gauges such as the pending sector count.
    Growth is reported in preference to the backstop.  A rule with neither
    threshold marks a raw value that is not a count (the helium levels, whose
    raw may be packed, e.g. 6553700 for 100/100), so the backstop is skipped.
    """
    if value is None or (absolute is None and growth is None):
        return None
    if absolute is not None and value >= absolute:
        return (WARNING, '%s is %d, at or above the threshold of %d'
                % (label, value, absolute))
    if growth is not None and grew is not None and grew >= growth:
        return (WARNING, '%s grew by %d over the sample window, to %d'
                % (label, grew, value))
    if value >= backstop:
        return (WARNING, '%s is %d, which is damage that predates the sample '
                         'window' % (label, value))
    return None


def _check_self_assessment(newest: Sample) -> List[Tuple[str, str]]:
    if newest.passed is False:
        return [(BAD, 'device failed its own SMART self-assessment')]
    return []


def _check_device_statistics(
        newest: Sample, oldest: Sample, rules: Rules = BUILTIN_RULES
) -> Tuple[List[Tuple[str, str]], Set[int]]:
    """
    Apply the ACS Device Statistics rules.

    Returns the findings and the ATA attribute ids they supersede, so a
    drive reporting both is not flagged twice.
    """
    findings = []
    superseded: Set[int] = set()
    for stat in rules.device_statistics:
        key = (stat.page, stat.offset)
        value = newest.dev_stats.get(key)
        if value is None:
            continue
        if stat.supersedes is not None:
            superseded.add(stat.supersedes)
        finding = _counter_finding(
            '%s (device statistics page %d)' % (stat.name, stat.page),
            stat.absolute, stat.growth, value,
            _growth(newest.dev_stats, oldest.dev_stats, key),
            rules.counter_backstop)
        if finding:
            findings.append(finding)
    return findings, superseded


def _check_ata(newest: Sample, oldest: Sample,
               superseded: Optional[Set[int]] = None,
               rules: Rules = BUILTIN_RULES) -> List[Tuple[str, str]]:
    findings = []
    superseded = superseded or set()
    for aid, rule in sorted(rules.ata.items()):
        # a superseding statistic replaces the raw counter, but the
        # normalized threshold is still checked below
        value = None if aid in superseded else newest.ata_raw.get(aid)
        grew = None if aid in superseded \
            else _growth(newest.ata_raw, oldest.ata_raw, aid)
        finding = _counter_finding('%s (ATA %d)' % (rule.name, aid),
                                   rule.absolute, rule.growth, value, grew,
                                   rules.counter_backstop)
        if finding:
            findings.append(finding)

        # a vendor threshold of zero means none is set
        threshold = newest.ata_threshold.get(aid, 0)
        normalized = newest.ata_normalized.get(aid)
        if normalized is None or threshold <= 0:
            continue
        if normalized <= threshold:
            findings.append((BAD, '%s (ATA %d) normalized value %d has '
                                  'reached the vendor threshold of %d'
                             % (rule.name, aid, normalized, threshold)))
            continue
        # see NORMALIZED_HEADROOM_FRACTION
        span = _normalized_span(normalized, threshold)
        margin = normalized - threshold
        if span > 0 and margin <= span * rules.normalized_headroom_fraction:
            findings.append((WARNING, '%s (ATA %d) normalized value %d has '
                                      'consumed %d%% of its margin to the '
                                      'vendor threshold of %d'
                             % (rule.name, aid, normalized,
                                round(100 * (1 - float(margin) / span)),
                                threshold)))
    return findings


def _check_wear(newest: Sample,
                rules: Rules = BUILTIN_RULES) -> List[Tuple[str, str]]:
    if newest.wear is not None and newest.wear >= rules.wear_warning:
        return [(WARNING, 'device has consumed %d%% of its rated endurance'
                 % round(newest.wear * 100))]
    return []


def _check_nvme(newest: Sample, oldest: Sample,
                rules: Rules = BUILTIN_RULES) -> List[Tuple[str, str]]:
    if not newest.nvme:
        return []
    findings = []

    warning = newest.nvme.get('critical_warning', 0)
    for bit, description, status in NVME_CRITICAL_WARNING_BITS:
        if warning & bit:
            findings.append((status, 'NVMe critical warning: %s' % description))
    unknown_bits = warning & ~0x3f
    if unknown_bits:
        findings.append((WARNING, 'NVMe critical warning: unrecognized bits '
                                  '0x%02x set' % unknown_bits))

    spare = newest.nvme.get('available_spare')
    spare_threshold = newest.nvme.get('available_spare_threshold', 10)
    if spare is not None:
        if spare <= spare_threshold:
            findings.append((BAD, 'NVMe available spare %d%% has reached the '
                                  'threshold of %d%%' % (spare, spare_threshold)))
        elif spare <= spare_threshold + rules.nvme_spare_headroom:
            findings.append((WARNING, 'NVMe available spare %d%% is close to '
                                      'the threshold of %d%%'
                             % (spare, spare_threshold)))

    used = newest.nvme.get('percentage_used')
    if used is not None and used >= rules.nvme_used_warning:
        findings.append((WARNING, 'NVMe percentage used is %d%%' % used))

    finding = _counter_finding(
        'NVMe media errors', None, 1, newest.nvme.get('media_errors'),
        _growth(newest.nvme, oldest.nvme, 'media_errors'),
        rules.counter_backstop)
    if finding:
        findings.append(finding)

    return findings


def _check_scsi(newest: Sample, oldest: Sample,
                rules: Rules = BUILTIN_RULES) -> List[Tuple[str, str]]:
    if not newest.scsi:
        return []
    findings = []

    finding = _counter_finding(
        'SCSI grown defect list', None, rules.scsi_defect_growth,
        newest.scsi.get('grown_defect_list'),
        _growth(newest.scsi, oldest.scsi, 'grown_defect_list'),
        rules.counter_backstop)
    if finding:
        findings.append(finding)

    for key in ('read', 'write', 'verify'):
        field = '%s_uncorrected' % key
        finding = _counter_finding(
            'SCSI %s uncorrected errors' % key, None,
            rules.scsi_error_growth, newest.scsi.get(field),
            _growth(newest.scsi, oldest.scsi, field),
            rules.counter_backstop)
        if finding:
            findings.append(finding)

    return findings


def predict(samples: Sequence[Dict[str, Any]],
            ruleset: Optional[Ruleset] = None) -> Prediction:
    """
    Predict a device's health from its recent SMART samples.

    Arguments:
        samples -- raw smartctl JSON documents for one device, *newest
                   first*.  A single sample is enough for the absolute
                   rules; the growth rules need at least two.
        ruleset -- rules to judge it by, defaulting to the built-in set.

    Returns a :class:`Prediction` whose status is one of ``Good``,
    ``Warning``, ``Bad`` or ``Unknown``.
    """
    ruleset = ruleset or BUILTIN_RULESET
    if not samples:
        return Prediction(UNKNOWN, ['no SMART data has been collected for '
                                    'this device'], ruleset.name)

    # match profiles on the newest sample, so a firmware upgrade takes effect
    rules, applied = ruleset.resolve(samples[0])

    def verdict(findings: List[Tuple[str, str]]) -> Prediction:
        status = max((f[0] for f in findings), key=lambda s: _SEVERITY[s])
        findings.sort(key=lambda f: _SEVERITY[f[0]], reverse=True)
        return Prediction(status, [reason for _, reason in findings],
                          ruleset.name, tuple(applied), rules.disabled)

    parsed = [s for s in (parse_sample(sample) for sample in samples)
              if s.has_data]
    if not parsed:
        # identity findings apply even without health data
        if rules.findings:
            return verdict(list(rules.findings))
        # report smartctl errors, so a broken scrape is not mistaken for
        # one that never ran
        for sample in samples:
            error = smartctl_error(sample)
            if error:
                return Prediction(UNKNOWN, ['smartctl did not return health '
                                            'data: %s' % error], ruleset.name)
        return Prediction(UNKNOWN, ['no recognizable SMART health data in the '
                                    'collected samples'], ruleset.name)

    newest, oldest = parsed[0], parsed[-1]
    findings: List[Tuple[str, str]] = list(rules.findings)
    findings += _check_self_assessment(newest)
    stat_findings, superseded = _check_device_statistics(newest, oldest, rules)
    findings += stat_findings
    findings += _check_ata(newest, oldest, superseded, rules)
    findings += _check_wear(newest, rules)
    findings += _check_nvme(newest, oldest, rules)
    findings += _check_scsi(newest, oldest, rules)

    if not findings:
        return Prediction(GOOD, [], ruleset.name, tuple(applied),
                          rules.disabled)
    return verdict(findings)
