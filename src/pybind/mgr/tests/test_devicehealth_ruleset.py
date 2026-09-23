# type: ignore
"""Tests for the loadable predictor ruleset."""

import json

import pytest

from devicehealth import predictor


def base_doc(**kw):
    doc = {'ruleset': 'test'}
    doc.update(kw)
    return doc


def rejects(doc, fragment):
    with pytest.raises(predictor.RulesetError) as exc:
        predictor.load_ruleset(doc)
    assert fragment in str(exc.value)


# ---------------------------------------------------------------------------
# round trip
# ---------------------------------------------------------------------------

def test_the_builtin_ruleset_round_trips():
    doc = predictor.dump_ruleset(predictor.BUILTIN_RULESET)
    doc['ruleset'] = 'round-trip'
    loaded = predictor.load_ruleset(doc)
    assert dict(loaded.base.ata) == dict(predictor.BUILTIN_RULES.ata)
    assert loaded.base.device_statistics \
        == predictor.BUILTIN_RULES.device_statistics
    for scalar in predictor.SCALARS:
        assert getattr(loaded.base, scalar) \
            == getattr(predictor.BUILTIN_RULES, scalar)


def test_a_dumped_ruleset_is_json_serializable():
    json.dumps(predictor.dump_ruleset(predictor.BUILTIN_RULESET))


# ---------------------------------------------------------------------------
# refusing bad documents
# ---------------------------------------------------------------------------

def test_a_ruleset_must_be_named():
    rejects({}, 'must be a non-empty string')


def test_unknown_top_level_keys_are_refused():
    rejects(base_doc(rules={}), 'unknown key')


def test_unknown_scalars_are_refused():
    rejects(base_doc(defaults={'backstop': 10}), 'unknown key')


@pytest.mark.parametrize('scalar,value', [
    ('counter_backstop', 0),            # would condemn every device
    ('scsi_defect_growth', 0),          # a counter cannot grow by less than 1
    ('scsi_error_growth', 0),
    ('normalized_headroom_fraction', 1.5),
    ('wear_warning', -0.1),
    ('nvme_used_warning', 0),
    ('nvme_spare_headroom', 101),
])
def test_scalars_outside_their_range_are_refused(scalar, value):
    rejects(base_doc(defaults={scalar: value}), scalar)


def test_a_boolean_is_not_an_integer():
    rejects(base_doc(defaults={'counter_backstop': True}),
            'must be an integer')


def test_an_ata_rule_needs_a_name():
    rejects(base_doc(ata={'5': {'growth': 1}}), 'name')


def test_a_zero_growth_ata_rule_is_refused():
    rejects(base_doc(ata={'5': {'name': 'x', 'growth': 0}}), 'at least 1')


def test_a_non_numeric_attribute_id_is_refused():
    rejects(base_doc(ata={'five': {'name': 'x', 'growth': 1}}),
            'non-numeric attribute id')


def test_an_out_of_range_attribute_id_is_refused():
    rejects(base_doc(ata={'999': {'name': 'x', 'growth': 1}}),
            'not a SMART attribute id')


def test_a_device_statistic_needs_a_page_and_offset():
    rejects(base_doc(device_statistics=[{'name': 'x', 'growth': 1}]), 'page')


def test_device_statistics_must_be_an_array():
    rejects(base_doc(device_statistics={}), 'must be an array')


def test_a_profile_must_match_something():
    rejects(base_doc(profiles=[{'name': 'p', 'match': {}}]),
            'at least one field')


def test_a_profile_cannot_match_an_unknown_field():
    rejects(base_doc(profiles=[{'name': 'p',
                                'match': {'serial_number': '*'}}]),
            'unknown key')


def test_a_profile_cannot_match_on_device_state():
    rejects(base_doc(profiles=[{'name': 'p',
                                'match': {'temperature': '*'}}]),
            'unknown key')


# ---------------------------------------------------------------------------
# profile resolution
# ---------------------------------------------------------------------------

TOSHIBA = {'model_name': 'TOSHIBA MG07ACA14TE', 'firmware_version': '0104'}
SAMSUNG = {'model_name': 'SAMSUNG MZQLB1T9HAJR-00007'}


def with_profile(**kw):
    profile = {'name': 'p', 'match': {'model_name': 'TOSHIBA MG07ACA*'}}
    profile.update(kw)
    return predictor.load_ruleset(base_doc(profiles=[profile]))


def test_a_profile_applies_only_to_what_it_matches():
    rs = with_profile(defaults={'counter_backstop': 4})
    assert rs.resolve(TOSHIBA)[0].counter_backstop == 4
    assert rs.resolve(SAMSUNG)[0].counter_backstop \
        == predictor.COUNTER_BACKSTOP


def test_matching_is_case_insensitive():
    rs = with_profile(defaults={'counter_backstop': 4})
    assert rs.resolve({'model_name': 'toshiba mg07aca14te'})[0] \
        .counter_backstop == 4


def test_every_named_field_must_match():
    rs = predictor.load_ruleset(base_doc(profiles=[
        {'name': 'p',
         'match': {'model_name': 'TOSHIBA*', 'firmware_version': '9999'},
         'defaults': {'counter_backstop': 4}}]))
    assert rs.resolve(TOSHIBA)[0].counter_backstop \
        == predictor.COUNTER_BACKSTOP


def test_a_field_the_device_does_not_report_never_matches():
    rs = predictor.load_ruleset(base_doc(profiles=[
        {'name': 'p', 'match': {'vendor': '*'},
         'defaults': {'counter_backstop': 4}}]))
    assert rs.resolve(TOSHIBA)[0].counter_backstop \
        == predictor.COUNTER_BACKSTOP


def test_later_profiles_win():
    rs = predictor.load_ruleset(base_doc(profiles=[
        {'name': 'first', 'match': {'model_name': 'TOSHIBA*'},
         'defaults': {'counter_backstop': 4}},
        {'name': 'second', 'match': {'model_name': '*MG07*'},
         'defaults': {'counter_backstop': 8}}]))
    rules, applied = rs.resolve(TOSHIBA)
    assert rules.counter_backstop == 8
    assert applied == ['first', 'second']


def test_a_profile_can_add_an_attribute_rule():
    rs = with_profile(ata={'1': {'name': 'Raw_Read_Error_Rate', 'growth': 1}})
    rules, _ = rs.resolve(TOSHIBA)
    assert rules.ata[1].name == 'Raw_Read_Error_Rate'
    assert 1 not in rs.resolve(SAMSUNG)[0].ata


def test_a_profile_can_replace_a_device_statistic():
    rs = with_profile(device_statistics=[
        {'name': 'Reallocated Logical Sectors', 'page': 3, 'offset': 32,
         'absolute': 1, 'growth': 1, 'supersedes': 5}])
    rules, _ = rs.resolve(TOSHIBA)
    replaced = [d for d in rules.device_statistics
                if (d.page, d.offset) == (3, 32)]
    assert len(replaced) == 1                  # replaced, not duplicated
    assert replaced[0].absolute == 1
    assert len(rules.device_statistics) \
        == len(predictor.BUILTIN_RULES.device_statistics)


def test_the_builtin_is_never_mutated_by_a_profile():
    rs = with_profile(ata={'1': {'name': 'x', 'growth': 1}})
    rs.resolve(TOSHIBA)
    assert 1 not in predictor.BUILTIN_RULES.ata
    assert 1 not in predictor.ATA_RULES


# ---------------------------------------------------------------------------
# the engine actually uses the ruleset
# ---------------------------------------------------------------------------

def dev_stat_sample(model, page, offset, value):
    return {'model_name': model,
            'ata_device_statistics': {'pages': [
                {'number': page,
                 'table': [{'offset': offset, 'value': value,
                            'flags': {'valid': True}}]}]}}


def test_a_ruleset_can_add_a_rule_the_builtin_does_not_have():
    rs = with_profile(device_statistics=[
        {'name': 'Spindle Speed Deviations', 'page': 3, 'offset': 64,
         'growth': 1}])
    newest = dev_stat_sample('TOSHIBA MG07ACA14TE', 3, 64, 5)
    oldest = dev_stat_sample('TOSHIBA MG07ACA14TE', 3, 64, 2)

    assert predictor.predict([newest, oldest]).status == predictor.GOOD
    result = predictor.predict([newest, oldest], rs)
    assert result.status == predictor.WARNING
    assert 'Spindle Speed Deviations' in result.reasons[0]


def test_a_profile_only_changes_the_devices_it_matches():
    rs = with_profile(device_statistics=[
        {'name': 'Spindle Speed Deviations', 'page': 3, 'offset': 64,
         'growth': 1}])
    newest = dev_stat_sample('SAMSUNG MZQLB1T9HAJR-00007', 3, 64, 5)
    oldest = dev_stat_sample('SAMSUNG MZQLB1T9HAJR-00007', 3, 64, 2)
    assert predictor.predict([newest, oldest], rs).status == predictor.GOOD


def test_a_ruleset_can_lower_the_backstop():
    rs = predictor.load_ruleset(base_doc(defaults={'counter_backstop': 8}))
    sample = {'model_name': 'X', 'ata_smart_attributes': {'table': [
        {'id': 5, 'value': 100, 'thresh': 10, 'raw': {'value': 16}}]}}
    assert predictor.predict([sample]).status == predictor.GOOD
    assert predictor.predict([sample], rs).status == predictor.WARNING


def test_the_verdict_names_the_ruleset_and_profile():
    rs = with_profile(defaults={'counter_backstop': 8})
    sample = dict(TOSHIBA, smart_status={'passed': True})
    result = predictor.predict([sample], rs)
    assert result.status == predictor.GOOD
    assert result.ruleset == 'test'
    assert result.profiles == ('p',)


def test_a_verdict_with_no_data_names_the_ruleset_but_no_profile():
    # nothing was judged, so no profile shaped the outcome
    rs = with_profile(defaults={'counter_backstop': 8})
    result = predictor.predict([dict(TOSHIBA)], rs)
    assert result.status == predictor.UNKNOWN
    assert result.ruleset == 'test'
    assert result.profiles == ()


def test_a_builtin_verdict_says_so():
    result = predictor.predict([{'model_name': 'X',
                                 'smart_status': {'passed': True}}])
    assert result.ruleset == predictor.BUILTIN_NAME
    assert result.profiles == ()


def test_an_unknown_verdict_still_names_the_ruleset():
    rs = predictor.load_ruleset(base_doc())
    assert predictor.predict([], rs).ruleset == 'test'
    assert predictor.predict([{}], rs).ruleset == 'test'


# ---------------------------------------------------------------------------
# the module keeps working when the stored ruleset does not
# ---------------------------------------------------------------------------

def module_with(stored):
    from devicehealth.module import Module
    m = Module.__new__(Module)
    m._ruleset_cache = None
    m._ruleset_raw = None
    m._ruleset_error = None
    m.store = {} if stored is None else {'ruleset': stored}
    m.get_store = lambda k, d=None: m.store.get(k, d)
    m.set_store = lambda k, v: (m.store.pop(k, None) if v is None
                                else m.store.__setitem__(k, v))
    return m


def test_no_stored_ruleset_means_the_builtin():
    assert module_with(None)._ruleset().name == predictor.BUILTIN_NAME


def test_a_stored_ruleset_is_used():
    stored = json.dumps({'ruleset': 'clyso-2026.08'})
    assert module_with(stored)._ruleset().name == 'clyso-2026.08'


@pytest.mark.parametrize('stored,why', [
    ('{ not json', 'unparseable'),
    ('[]', 'not an object'),
    (json.dumps({'ruleset': 'bad', 'defaults': {'counter_backstop': 0}}),
     'a backstop that would condemn everything'),
    (json.dumps({'ruleset': 'bad', 'ata': {'5': {'name': 'x', 'growth': 0}}}),
     'a growth rule that always fires'),
])
def test_an_unusable_stored_ruleset_falls_back_to_the_builtin(stored, why,
                                                              caplog):
    m = module_with(stored)
    assert m._ruleset().name == predictor.BUILTIN_NAME, why
    assert 'falling back to the built-in rules' in caplog.text
    r, out, err = m.do_get_predictor_ruleset()
    assert r == 0 and 'cannot be loaded' in err


def test_the_ruleset_is_cached_but_notices_a_change():
    m = module_with(json.dumps({'ruleset': 'first'}))
    assert m._ruleset() is m._ruleset()
    m.store['ruleset'] = json.dumps({'ruleset': 'second'})
    assert m._ruleset().name == 'second'


# ---------------------------------------------------------------------------
# known-bad models and firmware: verdicts from the device's identity alone
# ---------------------------------------------------------------------------

def known_bad(status='Bad', reason='firmware SC61 can lose writes',
              match=None):
    return predictor.load_ruleset(base_doc(profiles=[
        {'name': 'known-bad',
         'match': match or {'model_name': 'ST4000NM*',
                            'firmware_version': 'SC61'},
         'findings': [{'status': status, 'reason': reason}]}]))


HEALTHY_ST4000 = {'model_name': 'ST4000NM0033', 'firmware_version': 'SC61',
                  'smart_status': {'passed': True}}


def test_a_known_bad_firmware_condemns_a_healthy_drive():
    result = predictor.predict([HEALTHY_ST4000], known_bad())
    assert result.status == predictor.BAD
    assert result.reasons == ['firmware SC61 can lose writes']
    assert result.profiles == ('known-bad',)


def test_the_same_model_on_fixed_firmware_is_unaffected():
    fixed = dict(HEALTHY_ST4000, firmware_version='SC62')
    assert predictor.predict([fixed], known_bad()).status == predictor.GOOD


def test_the_builtin_ruleset_has_no_opinion_about_firmware():
    assert predictor.predict([HEALTHY_ST4000]).status == predictor.GOOD


def test_a_known_bad_model_is_reported_without_any_smart_data():
    rs = known_bad(status='Warning', reason='reports no usable SMART data',
                   match={'model_name': 'XYZ-*'})
    blind = {'model_name': 'XYZ-1000', 'firmware_version': 'A1'}
    result = predictor.predict([blind], rs)
    assert result.status == predictor.WARNING
    assert result.reasons == ['reports no usable SMART data']
    assert predictor.predict([blind]).status == predictor.UNKNOWN


def test_an_identity_finding_joins_the_counter_findings():
    failing = dict(HEALTHY_ST4000, smart_status={'passed': False})
    result = predictor.predict([failing], known_bad(status='Warning'))
    assert result.status == predictor.BAD          # the worst one wins
    assert len(result.reasons) == 2
    assert 'self-assessment' in result.reasons[0]  # Bad sorts first


def test_findings_from_several_profiles_accumulate():
    rs = predictor.load_ruleset(base_doc(profiles=[
        {'name': 'model', 'match': {'model_name': 'ST4000NM*'},
         'findings': [{'status': 'Warning', 'reason': 'model is suspect'}]},
        {'name': 'firmware', 'match': {'firmware_version': 'SC61'},
         'findings': [{'status': 'Bad', 'reason': 'firmware is worse'}]}]))
    result = predictor.predict([HEALTHY_ST4000], rs)
    assert result.status == predictor.BAD
    assert len(result.reasons) == 2
    assert result.profiles == ('model', 'firmware')


def test_a_finding_cannot_be_asserted_for_every_device():
    # a finding with nothing to match on would condemn the whole cluster
    rejects(base_doc(findings=[{'status': 'Bad', 'reason': 'everything'}]),
            'unknown key')


def test_a_ruleset_cannot_assert_that_a_device_is_healthy():
    rejects(base_doc(profiles=[{'name': 'p', 'match': {'vendor': 'a'},
                                'findings': [{'status': 'Good',
                                              'reason': 'fine'}]}]),
            'must be "Warning" or "Bad"')


def test_an_unknown_verdict_cannot_be_asserted_either():
    rejects(base_doc(profiles=[{'name': 'p', 'match': {'vendor': 'a'},
                                'findings': [{'status': 'Unknown',
                                              'reason': 'dunno'}]}]),
            'must be "Warning" or "Bad"')


def test_a_finding_must_say_why():
    rejects(base_doc(profiles=[{'name': 'p', 'match': {'vendor': 'a'},
                                'findings': [{'status': 'Bad'}]}]),
            'non-empty string')


def test_findings_round_trip():
    rs = known_bad()
    doc = predictor.dump_ruleset(rs)
    again = predictor.load_ruleset(doc)
    assert again.profiles[0].findings == rs.profiles[0].findings


# ---------------------------------------------------------------------------
# scalar limits: no accepted value may flag a new, healthy device
# ---------------------------------------------------------------------------

def fresh_devices():
    """A new SATA SSD, NVMe drive and HDD, all healthy."""
    ssd = {'smart_status': {'passed': True},
           'ata_device_statistics': {'pages': [{'number': 7, 'table': [
               {'offset': 8, 'value': 1, 'flags': {'valid': True}}]}]}}
    nvme = {'smart_status': {'passed': True},
            'nvme_smart_health_information_log': {
                'critical_warning': 0, 'available_spare': 100,
                'available_spare_threshold': 10, 'percentage_used': 1,
                'media_errors': 0}}
    hdd = {'smart_status': {'passed': True},
           'ata_smart_attributes': {'table': [
               {'id': 5, 'value': 100, 'thresh': 10,
                'raw': {'value': 0, 'string': '0'}},
               {'id': 184, 'value': 100, 'thresh': 90,
                'raw': {'value': 0, 'string': '0'}}]}}
    return [ssd, nvme, hdd]


@pytest.mark.parametrize('scalar,value', [
    ('wear_warning', 0.0),
    ('nvme_used_warning', 1),
    ('nvme_spare_headroom', 100),
    ('normalized_headroom_fraction', 1.0),
])
def test_scalars_that_would_flag_a_new_device_are_refused(scalar, value):
    rejects(base_doc(defaults={scalar: value}), scalar)


@pytest.mark.parametrize('scalar', sorted(predictor.SCALARS))
def test_the_most_aggressive_accepted_value_leaves_new_devices_good(scalar):
    _, low, high = predictor._SCALAR_LIMITS[scalar]
    # thresholds that warn when a value is high are most aggressive at their
    # minimum; margins that warn when a value is close are at their maximum
    value = high if scalar in ('normalized_headroom_fraction',
                               'nvme_spare_headroom') else low
    rs = predictor.load_ruleset(base_doc(defaults={scalar: value}))
    for doc in fresh_devices():
        assert predictor.predict([doc, doc], rs).status == predictor.GOOD, \
            (scalar, value, doc)


# ---------------------------------------------------------------------------
# turning rules off
# ---------------------------------------------------------------------------

def pending(model='TOSHIBA MG07ACA14TE', value=50):
    return {'model_name': model, 'smart_status': {'passed': True},
            'ata_smart_attributes': {'table': [
                {'id': 197, 'value': 100, 'thresh': 0,
                 'raw': {'value': value, 'string': str(value)}}]}}


def test_a_rule_without_thresholds_cannot_replace_a_counter():
    rejects(base_doc(ata={'197': {'name': 'Current_Pending_Sector'}}),
            '"disabled": true')
    rejects(base_doc(profiles=[{
        'name': 'p', 'match': {'model_name': 'X*'},
        'ata': {'197': {'name': 'Current_Pending_Sector'}}}]),
        '"disabled": true')
    rejects(base_doc(device_statistics=[
        {'name': 'x', 'page': 3, 'offset': 56}]), '"disabled": true')


def test_a_new_level_rule_needs_no_thresholds():
    rs = predictor.load_ruleset(base_doc(ata={'230': {'name': 'Level'}}))
    assert rs.base.ata[230] == predictor.AtaRule('Level', None, None)


def test_a_profile_can_disable_a_rule_for_its_devices():
    rs = predictor.load_ruleset(base_doc(profiles=[{
        'name': 'noisy-197', 'match': {'model_name': 'TOSHIBA*'},
        'ata': {'197': {'disabled': True}}}]))
    result = predictor.predict([pending()], rs)
    assert result.status == predictor.GOOD
    assert result.disabled == \
        ('Current_Pending_Sector (ATA 197) by profile noisy-197',)
    # other models are still judged
    other = predictor.predict([pending(model='WDC WUH721816ALE6L4')], rs)
    assert other.status == predictor.WARNING
    assert other.disabled == ()


def test_a_profile_can_disable_a_device_statistic():
    rs = predictor.load_ruleset(base_doc(profiles=[{
        'name': 'p', 'match': {'model_name': 'X*'},
        'device_statistics': [{'page': 3, 'offset': 56, 'disabled': True}]}]))
    rules, _ = rs.resolve({'model_name': 'X1'})
    assert (3, 56) not in {(d.page, d.offset) for d in rules.device_statistics}
    assert 'page 3 offset 56' in rules.disabled[0]


def test_disabling_is_only_allowed_in_a_profile():
    rejects(base_doc(ata={'197': {'disabled': True}}),
            'only allowed in a profile')


def test_a_disabled_rule_takes_no_thresholds():
    rejects(base_doc(profiles=[{
        'name': 'p', 'match': {'model_name': 'X*'},
        'ata': {'197': {'disabled': True, 'growth': 1}}}]), 'unknown key')


def test_disabled_rules_round_trip():
    doc = base_doc(profiles=[{
        'name': 'p', 'match': {'model_name': 'X*'},
        'ata': {'197': {'name': 'Current_Pending_Sector', 'disabled': True}},
        'device_statistics': [{'page': 3, 'offset': 56, 'disabled': True}]}])
    first = predictor.load_ruleset(doc)
    again = predictor.load_ruleset(predictor.dump_ruleset(first))
    assert again.profiles[0].disabled_ata == {197: 'Current_Pending_Sector'}
    assert again.profiles[0].disabled_stats == {(3, 56): ''}


# ---------------------------------------------------------------------------
# versioning the built-in rules
# ---------------------------------------------------------------------------

# One digest per built-in ruleset name.  When the built-in rules change, this
# test fails: give BUILTIN_NAME in predictor.py a new name and add its digest
# here, so explain-health tells the old and new rules apart.
BUILTIN_DIGESTS = {
    'builtin-2026.09':
        'b6339f5d8ce140a3b5ca0b8fc61ace1e71cc0b7440d56ef7b2e9469a8ea86aaf',
}


def builtin_digest():
    import hashlib
    doc = predictor.dump_ruleset(predictor.BUILTIN_RULESET)
    del doc['ruleset']
    # engine constants that the ruleset document does not carry
    doc['nvme_critical_warning_bits'] = predictor.NVME_CRITICAL_WARNING_BITS
    doc['normalized_fresh_values'] = predictor.NORMALIZED_FRESH_VALUES
    doc['wear_statistic'] = predictor.WEAR_STATISTIC
    return hashlib.sha256(
        json.dumps(doc, sort_keys=True).encode()).hexdigest()


def test_the_builtin_name_changes_with_the_builtin_rules():
    digest = builtin_digest()
    assert BUILTIN_DIGESTS.get(predictor.BUILTIN_NAME) == digest, (
        'the built-in rules differ from those recorded for %s: change '
        'BUILTIN_NAME and record %s for it in BUILTIN_DIGESTS'
        % (predictor.BUILTIN_NAME, digest))


def test_builtin_names_are_reserved():
    rejects(base_doc(ruleset='builtin-local'), 'reserved')
    rejects(base_doc(ruleset='Builtin'), 'reserved')
    dumped = predictor.dump_ruleset(predictor.BUILTIN_RULESET)
    rejects(dumped, 'reserved')
