from typing import Any, Dict, Optional

import pytest

from devicehealth import predictor


# A healthy, long-serving 12TB SATA HDD with a few settled reallocated
# sectors.  Power_On_Hours normalizes to 1 and UDMA_CRC_Error_Count to 200.
# Captured from a live cluster; identifying fields removed.
REAL_TOSHIBA_HDD = {
    'model_name': 'TOSHIBA MG07ACA12TE',
    'firmware_version': '0101',
    'smart_status': {'passed': True},
    'power_on_time': {'hours': 50173},
    'ata_smart_attributes': {'table': [
        {'id': 1, 'name': 'Raw_Read_Error_Rate', 'value': 100, 'worst': 100,
         'thresh': 50, 'raw': {'value': 0, 'string': '0'}},
        {'id': 2, 'name': 'Throughput_Performance', 'value': 100, 'worst': 100,
         'thresh': 50, 'raw': {'value': 0, 'string': '0'}},
        {'id': 3, 'name': 'Spin_Up_Time', 'value': 100, 'worst': 100,
         'thresh': 1, 'raw': {'value': 7020, 'string': '7020'}},
        {'id': 4, 'name': 'Start_Stop_Count', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 3, 'string': '3'}},
        {'id': 5, 'name': 'Reallocated_Sector_Ct', 'value': 100, 'worst': 100,
         'thresh': 10, 'raw': {'value': 8, 'string': '8'}},
        {'id': 7, 'name': 'Seek_Error_Rate', 'value': 100, 'worst': 100,
         'thresh': 50, 'raw': {'value': 0, 'string': '0'}},
        {'id': 8, 'name': 'Seek_Time_Performance', 'value': 100, 'worst': 100,
         'thresh': 50, 'raw': {'value': 0, 'string': '0'}},
        {'id': 9, 'name': 'Power_On_Hours', 'value': 1, 'worst': 1,
         'thresh': 0, 'raw': {'value': 50173, 'string': '50173'}},
        {'id': 10, 'name': 'Spin_Retry_Count', 'value': 100, 'worst': 100,
         'thresh': 30, 'raw': {'value': 0, 'string': '0'}},
        {'id': 12, 'name': 'Power_Cycle_Count', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 3, 'string': '3'}},
        {'id': 23, 'name': 'Helium_Condition_Lower', 'value': 100, 'worst': 100,
         'thresh': 75, 'raw': {'value': 0, 'string': '0'}},
        {'id': 24, 'name': 'Helium_Condition_Upper', 'value': 100, 'worst': 100,
         'thresh': 75, 'raw': {'value': 0, 'string': '0'}},
        {'id': 191, 'name': 'G-Sense_Error_Rate', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 0, 'string': '0'}},
        {'id': 192, 'name': 'Power-Off_Retract_Count', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 2, 'string': '2'}},
        {'id': 193, 'name': 'Load_Cycle_Count', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 24, 'string': '24'}},
        {'id': 194, 'name': 'Temperature_Celsius', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 171799937056, 'string': '32 (Min/Max 19/40)'}},
        {'id': 196, 'name': 'Reallocated_Event_Count', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 1, 'string': '1'}},
        {'id': 197, 'name': 'Current_Pending_Sector', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 0, 'string': '0'}},
        {'id': 198, 'name': 'Offline_Uncorrectable', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 0, 'string': '0'}},
        {'id': 199, 'name': 'UDMA_CRC_Error_Count', 'value': 200, 'worst': 200,
         'thresh': 0, 'raw': {'value': 0, 'string': '0'}},
        {'id': 220, 'name': 'Disk_Shift', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 19005441, 'string': '19005441'}},
        {'id': 222, 'name': 'Loaded_Hours', 'value': 1, 'worst': 1,
         'thresh': 0, 'raw': {'value': 49459, 'string': '49459'}},
        {'id': 223, 'name': 'Load_Retry_Count', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 0, 'string': '0'}},
        {'id': 224, 'name': 'Load_Friction', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 0, 'string': '0'}},
        {'id': 226, 'name': 'Load-in_Time', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 532, 'string': '532'}},
        {'id': 240, 'name': 'Head_Flying_Hours', 'value': 100, 'worst': 100,
         'thresh': 1, 'raw': {'value': 0, 'string': '0'}},
    ]},
    'ata_pending_defects_log': {'count': 0},
}

# A healthy, long-serving enterprise SATA SSD.  End-to-End_Error_Count has
# thresh 90 against a fresh value of 100.
# Captured from a live cluster; identifying fields removed.
REAL_INTEL_SSD = {
    'model_name': 'INTEL SSDSC2KB019T8',
    'firmware_version': 'XCV10165',
    'smart_status': {'passed': True},
    'power_on_time': {'hours': 50659},
    'ata_smart_attributes': {'table': [
        {'id': 5, 'name': 'Reallocated_Sector_Ct', 'value': 99, 'worst': 99,
         'thresh': 0, 'raw': {'value': 8, 'string': '8'}},
        {'id': 9, 'name': 'Power_On_Hours', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 50659, 'string': '50659'}},
        {'id': 12, 'name': 'Power_Cycle_Count', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 84, 'string': '84'}},
        {'id': 170, 'name': 'Available_Reservd_Space', 'value': 99, 'worst': 99,
         'thresh': 10, 'raw': {'value': 0, 'string': '0'}},
        {'id': 171, 'name': 'Program_Fail_Count', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 2, 'string': '2'}},
        {'id': 172, 'name': 'Erase_Fail_Count', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 0, 'string': '0'}},
        {'id': 174, 'name': 'Unsafe_Shutdown_Count', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 84, 'string': '84'}},
        {'id': 175, 'name': 'Power_Loss_Cap_Test', 'value': 100, 'worst': 100,
         'thresh': 10, 'raw': {'value': 365072157215, 'string': '2591 (84 65535)'}},
        {'id': 183, 'name': 'SATA_Downshift_Count', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 0, 'string': '0'}},
        {'id': 184, 'name': 'End-to-End_Error_Count', 'value': 100, 'worst': 100,
         'thresh': 90, 'raw': {'value': 0, 'string': '0'}},
        {'id': 187, 'name': 'Uncorrectable_Error_Cnt', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 0, 'string': '0'}},
        {'id': 190, 'name': 'Drive_Temperature', 'value': 77, 'worst': 76,
         'thresh': 0, 'raw': {'value': 420806679, 'string': '23 (Min/Max 21/25)'}},
        {'id': 192, 'name': 'Unsafe_Shutdown_Count', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 84, 'string': '84'}},
        {'id': 194, 'name': 'Temperature_Celsius', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 23, 'string': '23'}},
        {'id': 197, 'name': 'Pending_Sector_Count', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 0, 'string': '0'}},
        {'id': 199, 'name': 'CRC_Error_Count', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 0, 'string': '0'}},
        {'id': 225, 'name': 'Host_Writes_32MiB', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 6216872, 'string': '6216872'}},
        {'id': 226, 'name': 'Workld_Media_Wear_Indic', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 5539, 'string': '5539'}},
        {'id': 227, 'name': 'Workld_Host_Reads_Perc', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 30, 'string': '30'}},
        {'id': 228, 'name': 'Workload_Minutes', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 3039559, 'string': '3039559'}},
        {'id': 232, 'name': 'Available_Reservd_Space', 'value': 99, 'worst': 99,
         'thresh': 10, 'raw': {'value': 0, 'string': '0'}},
        {'id': 233, 'name': 'Media_Wearout_Indicator', 'value': 95, 'worst': 95,
         'thresh': 0, 'raw': {'value': 0, 'string': '0'}},
        {'id': 234, 'name': 'Thermal_Throttle_Status', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 0, 'string': '0/0'}},
        {'id': 235, 'name': 'Power_Loss_Cap_Test', 'value': 100, 'worst': 100,
         'thresh': 10, 'raw': {'value': 365072157215, 'string': '2591 (84 65535)'}},
        {'id': 241, 'name': 'Host_Writes_32MiB', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 6216872, 'string': '6216872'}},
        {'id': 242, 'name': 'Host_Reads_32MiB', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 2719042, 'string': '2719042'}},
        {'id': 243, 'name': 'NAND_Writes_32MiB', 'value': 100, 'worst': 100,
         'thresh': 0, 'raw': {'value': 38589845, 'string': '38589845'}},
    ]},
    'ata_device_statistics': {'pages': [{'number': 7, 'table': [
        {'offset': 8, 'name': 'Percentage Used Endurance Indicator', 'value': 5},
    ]}]},
}


def ata(**attrs: Any) -> Dict[str, Any]:
    """
    Build a smartctl JSON document from ``a<id>=(raw, normalized, thresh)``.
    """
    table = []
    for key, (raw, normalized, thresh) in attrs.items():
        table.append({
            'id': int(key[1:]),
            'value': normalized,
            'thresh': thresh,
            'raw': {'value': raw, 'string': str(raw)},
        })
    return {
        'smart_status': {'passed': True},
        'ata_smart_attributes': {'table': table},
    }


_STAT_FLAGS = {'valid': True, 'normalized': False,
               'monitored_condition_met': False}


def devstats(**pages: Dict[int, int]) -> Dict[str, Any]:
    """
    Build an ACS Device Statistics log: devstats(p3={32: 0}, p4={8: 1}).
    """
    table = []
    for key, entries in sorted(pages.items()):
        table.append({'number': int(key[1:]), 'table': [
            {'offset': offset, 'name': 'page %s offset %d' % (key[1:], offset),
             'size': 4, 'value': value, 'flags': dict(_STAT_FLAGS)}
            for offset, value in sorted(entries.items())]})
    return {'smart_status': {'passed': True},
            'ata_device_statistics': {'pages': table}}


def nvme(**log: int) -> Dict[str, Any]:
    defaults = {'critical_warning': 0, 'available_spare': 100,
                'available_spare_threshold': 10, 'percentage_used': 1}
    defaults.update(log)
    return {'smart_status': {'passed': True},
            'nvme_smart_health_information_log': defaults}


class TestParsing:
    def test_no_samples_is_unknown(self) -> None:
        assert predictor.predict([]).status == predictor.UNKNOWN

    def test_unrecognized_sample_is_unknown(self) -> None:
        # no data is not evidence of health
        assert predictor.predict([{}]).status == predictor.UNKNOWN
        assert predictor.predict([{'model_name': 'X', 'user_capacity': {'bytes': 1}}]) \
            .status == predictor.UNKNOWN

    @pytest.mark.parametrize('string,expected', [
        ('0', 0),
        ('12345', 12345),
        # temperatures and packed counters decode to their leading field
        ('35 (Min/Max 20/45)', 35),
        ('0 0 0', 0),
        ('3906 (52 202 0)', 3906),
    ])
    def test_raw_value_decoding(self, string: str, expected: int) -> None:
        attr = {'id': 5, 'value': 100, 'thresh': 10,
                'raw': {'value': 999, 'string': string}}
        assert predictor._attr_raw_value(attr) == expected

    def test_raw_value_falls_back_to_integer(self) -> None:
        attr = {'id': 5, 'value': 100, 'thresh': 10,
                'raw': {'value': 7, 'string': 'unparseable'}}
        assert predictor._attr_raw_value(attr) == 7


class TestSmartctlErrors:
    """
    Error stubs stored by block_device_get_metrics() when smartctl fails.
    """

    def blob(self, **extra: Any) -> Dict[str, Any]:
        doc = {
            'error': 'smartctl failed',
            'dev': '/dev/nvme0n1',
            'smartctl_error_code': -22,
            'smartctl_output': 'smartctl 7.1 2019-12-30 r5022 [x86_64-linux]\n'
                               'Smartctl open device: /dev/nvme0n1 failed: '
                               'Permission denied\n',
            'nvme_vendor': 'samsung',
            'nvme_smart_health_information_add_log_error': 'nvme returned 1',
            'nvme_smart_health_information_add_log_error_code': -1,
        }
        doc.update(extra)
        return doc

    def test_error_blob_is_unknown_not_good(self) -> None:
        assert predictor.predict([self.blob()]).status == predictor.UNKNOWN

    def test_error_blob_explains_itself(self) -> None:
        reasons = predictor.predict([self.blob()]).reasons
        assert len(reasons) == 1
        assert 'smartctl_error_code -22' in reasons[0]
        assert 'Permission denied' in reasons[0]

    def test_a_whole_history_of_error_blobs_is_unknown(self) -> None:
        result = predictor.predict([self.blob()] * 180)
        assert result.status == predictor.UNKNOWN
        assert 'smartctl' in result.reasons[0]

    def test_invalid_json_stub_is_also_explained(self) -> None:
        # the other stub block_device_get_metrics() can store
        blob = {'error': 'smartctl returned invalid JSON', 'dev': '/dev/sdb',
                'output': 'garbage\nnot json'}
        result = predictor.predict([blob])
        assert result.status == predictor.UNKNOWN
        assert 'invalid JSON' in result.reasons[0]

    def test_a_real_sample_beside_error_blobs_still_counts(self) -> None:
        # scraping recovered: the newest sample is real, older ones are stubs
        samples = [ata(a197=(8, 100, 0)), self.blob(), self.blob()]
        assert predictor.predict(samples).status == predictor.WARNING

    def test_error_blob_does_not_mask_a_newer_failure(self) -> None:
        samples = [{'smart_status': {'passed': False}}, self.blob()]
        assert predictor.predict(samples).status == predictor.BAD


class TestSelfAssessment:
    def test_failed_self_assessment_is_bad(self) -> None:
        assert predictor.predict([{'smart_status': {'passed': False}}]).status \
            == predictor.BAD

    def test_only_the_newest_sample_counts(self) -> None:
        # an old failed sample in the window does not keep the device Bad
        samples = [{'smart_status': {'passed': True}},
                   {'smart_status': {'passed': False}}]
        assert predictor.predict(samples).status == predictor.GOOD


class TestAtaRules:
    def test_clean_drive_is_good(self) -> None:
        assert predictor.predict([ata(a5=(0, 200, 140), a197=(0, 100, 0))]).status \
            == predictor.GOOD

    def test_stable_reallocated_sectors_do_not_warn(self) -> None:
        sample = ata(a5=(3, 200, 140))
        assert predictor.predict([sample, sample]).status == predictor.GOOD

    def test_growing_reallocated_sectors_warn(self) -> None:
        result = predictor.predict([ata(a5=(3, 200, 140)), ata(a5=(0, 200, 140))])
        assert result.status == predictor.WARNING
        assert 'grew by 3' in result.reasons[0]

    def test_growth_needs_two_samples(self) -> None:
        assert predictor.predict([ata(a5=(100, 200, 140))]).status \
            == predictor.GOOD

    def test_a_stable_count_is_good(self) -> None:
        # below COUNTER_BACKSTOP only growth counts; see TestBackstop
        sample = ata(a5=(100, 190, 140))
        assert predictor.predict([sample, sample]).status == predictor.GOOD

    def test_a_large_count_that_is_still_growing_warns(self) -> None:
        result = predictor.predict([ata(a5=(3800, 190, 140)),
                                    ata(a5=(3790, 190, 140))])
        assert result.status == predictor.WARNING
        assert 'grew by 10' in result.reasons[0]

    def test_a_counter_that_went_backwards_is_not_growth(self) -> None:
        result = predictor.predict([ata(a5=(0, 200, 140)), ata(a5=(3, 200, 140))])
        assert result.status == predictor.GOOD

    @pytest.mark.parametrize('attr,grew', [
        ('a10', 1),   # Spin_Retry_Count
        ('a184', 1),  # End-to-End_Error
        ('a187', 1),  # Reported_Uncorrect
        ('a188', 2),  # Command_Timeout
        ('a198', 1),  # Offline_Uncorrectable
    ])
    def test_growing_defect_counters_warn(self, attr: str, grew: int) -> None:
        newest = ata(**{attr: (grew + 5, 100, 0)})
        oldest = ata(**{attr: (5, 100, 0)})
        result = predictor.predict([newest, oldest])
        assert result.status == predictor.WARNING, result.reasons

    @pytest.mark.parametrize('attr', ['a10', 'a184', 'a187', 'a188', 'a198'])
    def test_stable_defect_counters_are_good(self, attr: str) -> None:
        sample = ata(**{attr: (5, 100, 0)})
        assert predictor.predict([sample, sample]).status == predictor.GOOD

    def test_a_single_command_timeout_does_not_warn(self) -> None:
        result = predictor.predict([ata(a188=(3, 100, 0)),
                                    ata(a188=(2, 100, 0))])
        assert result.status == predictor.GOOD

    def test_pending_sectors_are_a_gauge_not_a_counter(self) -> None:
        sample = ata(a197=(8, 100, 0))
        result = predictor.predict([sample, sample])
        assert result.status == predictor.WARNING
        assert 'is 8' in result.reasons[0]

    def test_pending_sectors_draining_still_warns(self) -> None:
        # growth is negative here; the absolute rule is what matters
        result = predictor.predict([ata(a197=(4, 100, 0)),
                                    ata(a197=(9, 100, 0))])
        assert result.status == predictor.WARNING

    def test_temperature_attribute_is_ignored(self) -> None:
        # Airflow_Temperature_Cel normalizes as 100 - degrees_C
        assert predictor.predict([ata(a190=(45, 55, 45))]).status == predictor.GOOD

    def test_vendor_specific_attributes_are_ignored(self) -> None:
        # Seagate reports enormous raw values for 1 and 7 by design.
        assert predictor.predict([ata(a1=(145436280, 118, 6),
                                      a7=(3906, 90, 30))]).status == predictor.GOOD

    def test_normalized_value_at_vendor_threshold_is_bad(self) -> None:
        assert predictor.predict([ata(a5=(600, 140, 140))]).status == predictor.BAD

    def test_fresh_drive_with_a_high_vendor_threshold_is_good(self) -> None:
        # Intel DC S4510: 184 ships at 100 with thresh 90
        assert predictor.predict([ata(a184=(0, 100, 90))]).status == predictor.GOOD

    @pytest.mark.parametrize('normalized,thresh,expected', [
        # fresh value 100 (Intel S4510 184, thresh 90 -> span 10)
        (100, 90, predictor.GOOD),
        (92, 90, predictor.GOOD),
        (91, 90, predictor.WARNING),
        (90, 90, predictor.BAD),
        # fresh value 100 (Toshiba MG07ACA 5, thresh 10 -> span 90)
        (100, 10, predictor.GOOD),
        (20, 10, predictor.GOOD),
        (19, 10, predictor.WARNING),
        (10, 10, predictor.BAD),
        # fresh value 200 (thresh 140 -> span 60); the span must come from
        # the fresh value, not the current one, or this never fires
        (200, 140, predictor.GOOD),
        (147, 140, predictor.GOOD),
        (146, 140, predictor.WARNING),
        (140, 140, predictor.BAD),
    ])
    def test_margin_is_judged_against_the_whole_range(
            self, normalized: int, thresh: int, expected: str) -> None:
        result = predictor.predict([ata(a5=(0, normalized, thresh))])
        assert result.status == expected, result.reasons

    def test_normalized_span_infers_the_fresh_value(self) -> None:
        assert predictor._normalized_span(100, 90) == 10
        assert predictor._normalized_span(100, 10) == 90
        assert predictor._normalized_span(146, 140) == 60      # fresh 200
        assert predictor._normalized_span(210, 140) == 113     # fresh 253

    def test_zero_vendor_threshold_is_not_a_threshold(self) -> None:
        # thresh 0 means the vendor set none
        assert predictor.predict([ata(a5=(0, 5, 0))]).status == predictor.GOOD

    def test_age_alone_is_not_a_failure(self) -> None:
        # ~8 years of power-on hours on an otherwise clean drive.
        assert predictor.predict([ata(a9=(70080, 80, 0), a5=(0, 200, 140))]).status \
            == predictor.GOOD


class TestNvmeRules:
    def test_healthy_nvme_is_good(self) -> None:
        assert predictor.predict([nvme()]).status == predictor.GOOD

    def test_temperature_warning_bit_is_a_warning(self) -> None:
        assert predictor.predict([nvme(critical_warning=0x02)]).status \
            == predictor.WARNING

    @pytest.mark.parametrize('bit', [0x04, 0x08])
    def test_terminal_warning_bits_are_bad(self, bit: int) -> None:
        assert predictor.predict([nvme(critical_warning=bit)]).status \
            == predictor.BAD

    def test_spare_at_threshold_is_bad(self) -> None:
        assert predictor.predict([nvme(available_spare=10)]).status == predictor.BAD

    def test_spare_near_threshold_warns(self) -> None:
        assert predictor.predict([nvme(available_spare=18)]).status \
            == predictor.WARNING

    def test_percentage_used_warns(self) -> None:
        assert predictor.predict([nvme(percentage_used=95)]).status \
            == predictor.WARNING

    def test_growing_media_errors_warn(self) -> None:
        result = predictor.predict([nvme(media_errors=3), nvme(media_errors=1)])
        assert result.status == predictor.WARNING
        assert 'grew by 2' in result.reasons[0]

    def test_stable_media_errors_are_good(self) -> None:
        sample = nvme(media_errors=2)
        assert predictor.predict([sample, sample]).status == predictor.GOOD


class TestScsiRules:
    def scsi(self, defects: Optional[int] = None,
             uncorrected: Optional[int] = None) -> Dict[str, Any]:
        data: Dict[str, Any] = {'smart_status': {'passed': True}}
        if defects is not None:
            data['scsi_grown_defect_list'] = defects
        if uncorrected is not None:
            data['scsi_error_counter_log'] = {
                'read': {'total_uncorrected_errors': uncorrected}}
        return data

    def test_sas_drive_without_counters_is_not_silently_good(self) -> None:
        # smart_status alone is health data, so this is Good; but a SAS
        # document with nothing at all in it must stay Unknown.
        assert predictor.predict([{'device': {'protocol': 'SCSI'}}]).status \
            == predictor.UNKNOWN

    def test_stable_grown_defects_do_not_warn(self) -> None:
        sample = self.scsi(defects=3)
        assert predictor.predict([sample, sample]).status == predictor.GOOD

    def test_growing_grown_defects_warn(self) -> None:
        assert predictor.predict([self.scsi(defects=3), self.scsi(defects=0)]).status \
            == predictor.WARNING

    def test_many_but_stable_grown_defects_are_good(self) -> None:
        sample = self.scsi(defects=250)
        assert predictor.predict([sample, sample]).status == predictor.GOOD

    def test_growing_uncorrected_errors_warn(self) -> None:
        result = predictor.predict([self.scsi(uncorrected=4),
                                    self.scsi(uncorrected=1)])
        assert result.status == predictor.WARNING
        assert 'grew by 3' in result.reasons[0]

    def test_stable_uncorrected_errors_are_good(self) -> None:
        sample = self.scsi(uncorrected=4)
        assert predictor.predict([sample, sample]).status == predictor.GOOD


class TestWear:
    def wear(self, pct: int) -> Dict[str, Any]:
        return {'smart_status': {'passed': True},
                'ata_device_statistics': {
                    'pages': [{'number': 7,
                               'table': [{'offset': 8, 'value': pct}]}]}}

    def test_young_ssd_is_good(self) -> None:
        assert predictor.predict([self.wear(20)]).status == predictor.GOOD

    def test_worn_ssd_warns(self) -> None:
        result = predictor.predict([self.wear(95)])
        assert result.status == predictor.WARNING
        assert '95%' in result.reasons[0]


class TestVerdict:
    def test_the_worst_finding_wins(self) -> None:
        sample = ata(a197=(8, 100, 0))
        sample['smart_status'] = {'passed': False}
        result = predictor.predict([sample])
        assert result.status == predictor.BAD
        # ...and the reasons lead with it, so an operator sees why first.
        assert result.reasons[0] == 'device failed its own SMART self-assessment'
        assert len(result.reasons) == 2

    def test_a_good_verdict_has_no_reasons(self) -> None:
        assert predictor.predict([ata(a5=(0, 200, 140))]).reasons == []

    def test_every_finding_is_reported(self) -> None:
        result = predictor.predict([nvme(critical_warning=0x02, percentage_used=95)])
        assert result.status == predictor.WARNING
        assert len(result.reasons) == 2


def test_life_expectancy_map_matches_thresholds() -> None:
    from devicehealth.module import LIFE_EXPECTANCY, Module

    defaults = {opt['name']: opt['default'] for opt in Module.MODULE_OPTIONS}
    mark_out = defaults['mark_out_threshold']
    warn = defaults['warn_threshold']

    bad_max = LIFE_EXPECTANCY[predictor.BAD][1]
    warn_max = LIFE_EXPECTANCY[predictor.WARNING][1]
    good_min = LIFE_EXPECTANCY[predictor.GOOD][0]
    assert bad_max is not None and bad_max < mark_out
    assert warn_max is not None and mark_out < warn_max <= warn
    assert good_min > warn
    assert LIFE_EXPECTANCY[predictor.GOOD][1] is None


def test_unknown_has_no_life_expectancy() -> None:
    from devicehealth.module import LIFE_EXPECTANCY

    assert predictor.UNKNOWN not in LIFE_EXPECTANCY


class TestGoodRecordReuse:

    def dev(self, minimum: str = '', maximum: str = '') -> Dict[str, Any]:
        return {'devid': 'X', 'life_expectancy_min': minimum,
                'life_expectancy_max': maximum}

    def stamp(self, days: int) -> str:
        from datetime import datetime, timedelta, timezone
        when = datetime.now(timezone.utc) + timedelta(days=days)
        return when.strftime('%Y-%m-%dT%H:%M:%S.%f%z')

    def test_fresh_good_record_is_reused(self) -> None:
        from devicehealth.module import Module
        assert Module._is_still_good(self.dev(minimum=self.stamp(30)))

    def test_expired_good_record_is_rewritten(self) -> None:
        from devicehealth.module import Module
        assert not Module._is_still_good(self.dev(minimum=self.stamp(-1)))

    def test_device_with_no_record_is_written(self) -> None:
        from devicehealth.module import Module
        assert not Module._is_still_good(self.dev())

    def test_unset_upper_bound_as_dumped_by_the_mgr(self) -> None:
        # DeviceState::dump() writes an unset bound as '0.000000'
        from devicehealth.module import Module
        assert Module._is_still_good(
            self.dev(minimum=self.stamp(30), maximum='0.000000'))

    def test_record_with_an_upper_bound_is_rewritten(self) -> None:
        # a previous Warning or Bad must be overwritten
        from devicehealth.module import Module
        assert not Module._is_still_good(
            self.dev(minimum=self.stamp(14), maximum=self.stamp(42)))

    def test_unparseable_record_is_rewritten(self) -> None:
        from devicehealth.module import Module
        assert not Module._is_still_good(self.dev(minimum='not a date'))


class TestPredictAllDevices:

    def build(self, samples: Any, mode: str = 'smart',
              **dev_fields: Any) -> Any:
        from unittest import mock

        from devicehealth.module import Module

        module = Module('devicehealth', 0, 0)
        module.prediction_window = 7 * 86400
        dev = {'devid': 'D1', 'daemons': ['osd.0'],
               'life_expectancy_min': '', 'life_expectancy_max': ''}
        dev.update(dev_fields)
        module.get = mock.Mock(return_value={'devices': [dev]})
        module.get_ceph_option = mock.Mock(return_value=mode)
        # newest first on the way out, so number the stamps descending
        metrics = {'2026010%d-000000' % (len(samples) - i): sample
                   for i, sample in enumerate(samples)}
        module.get_recent_device_metrics = mock.Mock(return_value=metrics)
        module._set_device_life_expectancy = mock.Mock(return_value=0)
        module._reset_device_life_expectancy = mock.Mock(return_value=0)
        module.set_device_health_status = mock.Mock(return_value=None)
        return module

    def days(self, module: Any) -> Any:
        """The (from, to) offsets in days that were recorded, or None."""
        from datetime import datetime, timezone

        from devicehealth.module import LIFE_EXPECTANCY_FORMAT

        if not module._set_device_life_expectancy.called:
            return None
        _, from_date, to_date = module._set_device_life_expectancy.call_args[0]
        now = datetime.now(timezone.utc)

        def offset(value: Optional[str]) -> Optional[float]:
            if value is None:
                return None
            when = datetime.strptime(value, LIFE_EXPECTANCY_FORMAT) \
                .replace(tzinfo=timezone.utc)
            return round((when - now).total_seconds() / 86400)

        return offset(from_date), offset(to_date)

    def test_bad_device_gets_a_life_expectancy(self) -> None:
        # a lower bound of 0 must still be recorded
        module = self.build([{'smart_status': {'passed': False}}])
        module.predict_all_devices()
        assert self.days(module) == (0, 13)
        module._reset_device_life_expectancy.assert_not_called()

    def test_bad_device_falls_inside_mark_out_threshold(self) -> None:
        module = self.build([{'smart_status': {'passed': False}}])
        module.predict_all_devices()
        _, to_days = self.days(module)
        defaults = {opt['name']: opt['default']
                    for opt in type(module).MODULE_OPTIONS}
        assert to_days * 86400 < defaults['mark_out_threshold']

    def test_warning_device_gets_a_bounded_range(self) -> None:
        module = self.build([ata(a197=(8, 100, 0))])
        module.predict_all_devices()
        assert self.days(module) == (14, 42)

    def test_good_device_gets_an_open_ended_range(self) -> None:
        module = self.build([ata(a5=(0, 200, 140))])
        module.predict_all_devices()
        assert self.days(module) == (43, None)

    def test_good_device_with_a_fresh_record_is_left_alone(self) -> None:
        from datetime import datetime, timedelta, timezone
        future = (datetime.now(timezone.utc) + timedelta(days=30)) \
            .strftime('%Y-%m-%dT%H:%M:%S.%f%z')
        module = self.build([ata(a5=(0, 200, 140))],
                            life_expectancy_min=future)
        module.predict_all_devices()
        module._set_device_life_expectancy.assert_not_called()
        module._reset_device_life_expectancy.assert_not_called()

    def test_the_verdict_itself_reaches_the_device(self) -> None:
        module = self.build([ata(a197=(8, 100, 0))])
        module.predict_all_devices()
        module.set_device_health_status.assert_called_once_with('D1',
                                                               'Warning')

    def test_a_good_device_left_alone_still_reports_its_verdict(self) -> None:
        from datetime import datetime, timedelta, timezone
        future = (datetime.now(timezone.utc) + timedelta(days=30)) \
            .strftime('%Y-%m-%dT%H:%M:%S.%f%z')
        module = self.build([ata(a5=(0, 200, 140))],
                            life_expectancy_min=future)
        module.predict_all_devices()
        module._set_device_life_expectancy.assert_not_called()
        module.set_device_health_status.assert_called_once_with('D1', 'Good')

    def test_unknown_device_retracts_a_stale_record(self) -> None:
        module = self.build([{}], life_expectancy_min='2020-01-01T00:00:00.000000+0000')
        module.predict_all_devices()
        module._reset_device_life_expectancy.assert_called_once_with('D1')
        module._set_device_life_expectancy.assert_not_called()

    def test_unknown_device_with_no_record_costs_nothing(self) -> None:
        module = self.build([{}])
        module.predict_all_devices()
        module._reset_device_life_expectancy.assert_not_called()
        module._set_device_life_expectancy.assert_not_called()

    def test_unclaimed_devices_are_skipped(self) -> None:
        module = self.build([{'smart_status': {'passed': False}}], daemons=[])
        module.predict_all_devices()
        module._set_device_life_expectancy.assert_not_called()

    def test_disabled_mode_does_nothing(self) -> None:
        module = self.build([{'smart_status': {'passed': False}}], mode='none')
        assert module.predict_all_devices() == (0, '', '')
        module._set_device_life_expectancy.assert_not_called()

    def test_growth_uses_the_oldest_sample_in_the_window(self) -> None:
        module = self.build([ata(a5=(4, 200, 140)), ata(a5=(2, 200, 140)),
                             ata(a5=(0, 200, 140))])
        module.predict_all_devices()
        assert self.days(module) == (14, 42)

    def test_explain_health_reports_the_reasons(self) -> None:
        module = self.build([ata(a197=(8, 100, 0))])
        r, out, err = module.explain_health('D1')
        assert r == 0
        assert out.startswith('D1: Warning')
        assert 'Current_Pending_Sector' in out

    def test_explain_health_lists_disabled_rules(self) -> None:
        import json
        from unittest import mock
        module = self.build([ata(a197=(8, 100, 0))])
        module.get_store = mock.Mock(return_value=json.dumps({
            'ruleset': 'site', 'profiles': [{
                'name': 'quiet', 'match': {'model_name': '*'},
                'ata': {'197': {'disabled': True}}}]}))
        doc = ata(a197=(8, 100, 0))
        doc['model_name'] = 'M1'
        module.get_recent_device_metrics = mock.Mock(
            return_value={'20260101-000000': doc})
        r, out, err = module.explain_health('D1')
        assert out.startswith('D1: Good')
        assert 'disabled: Current_Pending_Sector (ATA 197) by profile quiet' \
            in out

    def test_explain_health_flags_an_inactive_mode(self) -> None:
        module = self.build([ata(a197=(8, 100, 0))], mode='none')
        r, out, err = module.explain_health('D1')
        assert r == 0
        assert 'not being acted on' in out


class TestRealHardware:
    """
    Documents captured from healthy drives in a live cluster.
    """

    def test_healthy_hdd_with_settled_reallocations_is_good(self) -> None:
        result = predictor.predict([REAL_TOSHIBA_HDD, REAL_TOSHIBA_HDD])
        assert result.status == predictor.GOOD, result.reasons

    def test_healthy_ssd_is_good(self) -> None:
        result = predictor.predict([REAL_INTEL_SSD, REAL_INTEL_SSD])
        assert result.status == predictor.GOOD, result.reasons

    def test_hdd_reallocations_growing_warns(self) -> None:
        # the same drive, had those sectors appeared over the window
        import copy
        older = copy.deepcopy(REAL_TOSHIBA_HDD)
        for attr in older['ata_smart_attributes']['table']:
            if attr['id'] == 5:
                attr['raw'] = {'value': 0, 'string': '0'}
        result = predictor.predict([REAL_TOSHIBA_HDD, older])
        assert result.status == predictor.WARNING
        assert 'grew by 8' in result.reasons[0]

    def test_real_ssd_endurance_is_read(self) -> None:
        # ata_device_statistics page 7 offset 8 == 5% used
        assert predictor.parse_sample(REAL_INTEL_SSD).wear == 0.05

    def test_real_hdd_has_no_endurance_page(self) -> None:
        assert predictor.parse_sample(REAL_TOSHIBA_HDD).wear is None

    def test_real_raw_strings_all_parse(self) -> None:
        for doc in (REAL_TOSHIBA_HDD, REAL_INTEL_SSD):
            sample = predictor.parse_sample(doc)
            for attr in doc['ata_smart_attributes']['table']:
                if attr['id'] in predictor.ATA_RULES:
                    assert attr['id'] in sample.ata_raw, attr

    def test_power_on_hours_normalizing_to_one_is_not_a_failure(self) -> None:
        sample = predictor.parse_sample(REAL_TOSHIBA_HDD)
        assert sample.ata_normalized[9] == 1
        assert sample.ata_threshold[9] == 0
        assert predictor.predict([REAL_TOSHIBA_HDD]).status == predictor.GOOD

    def test_udma_crc_fresh_value_is_two_hundred(self) -> None:
        sample = predictor.parse_sample(REAL_TOSHIBA_HDD)
        assert sample.ata_normalized[199] == 200


class TestDeviceStatistics:

    def test_clean_statistics_are_good(self) -> None:
        doc = devstats(p3={32: 0, 48: 0, 56: 0}, p4={8: 0, 16: 0})
        assert predictor.predict([doc, doc]).status == predictor.GOOD

    @pytest.mark.parametrize('page,offset,value', [
        (3, 32, 16),    # reallocated sectors, as observed in a live fleet
        (3, 48, 5),     # mechanical start failures
        (4, 8, 8),      # reported uncorrectable errors
        (4, 16, 14),    # resets between acceptance and completion
    ])
    def test_a_stable_count_below_the_backstop_is_good(
            self, page: int, offset: int, value: int) -> None:
        doc = devstats(**{'p%d' % page: {offset: value}})
        assert predictor.predict([doc, doc]).status == predictor.GOOD

    @pytest.mark.parametrize('page,offset,grew,expected', [
        (3, 32, 1, predictor.WARNING),   # any reallocation is movement
        (3, 48, 1, predictor.WARNING),
        (4, 8, 1, predictor.WARNING),
        (4, 16, 1, predictor.GOOD),      # one reset is not a pattern
        (4, 16, 2, predictor.WARNING),
    ])
    def test_growth_thresholds(self, page: int, offset: int, grew: int,
                               expected: str) -> None:
        newest = devstats(**{'p%d' % page: {offset: grew + 20}})
        oldest = devstats(**{'p%d' % page: {offset: 20}})
        result = predictor.predict([newest, oldest])
        assert result.status == expected, result.reasons

    def test_growth_is_reported(self) -> None:
        newest = devstats(p3={32: 4})
        oldest = devstats(p3={32: 0})
        result = predictor.predict([newest, oldest])
        assert result.status == predictor.WARNING
        assert 'grew by 4' in result.reasons[0]

    def test_reallocation_candidates_are_a_gauge(self) -> None:
        # the standardised Current_Pending_Sector, also a gauge
        doc = devstats(p3={56: 2})
        result = predictor.predict([doc, doc])
        assert result.status == predictor.WARNING
        assert 'is 2' in result.reasons[0]

    def test_invalid_statistics_are_ignored(self) -> None:
        doc = devstats(p4={8: 99})
        doc['ata_device_statistics']['pages'][0]['table'][0]['flags']['valid'] = False
        assert predictor.parse_sample(doc).dev_stats == {}
        # smart_status alone is health data, so Good rather than Unknown
        assert predictor.predict([doc]).status == predictor.GOOD

    def test_page_without_a_table_is_harmless(self) -> None:
        # a spinning disk still advertises the solid state page, with no table
        doc = {'smart_status': {'passed': True},
               'ata_device_statistics': {'pages': [
                   {'number': 7, 'name': 'Solid State Device Statistics',
                    'revision': 1}]}}
        assert predictor.predict([doc]).status == predictor.GOOD

    def test_statistics_supersede_the_attribute(self) -> None:
        # a drive reporting both must not be flagged twice for one defect
        def doc(value: int) -> Dict[str, Any]:
            out = devstats(p3={32: value})
            out['ata_smart_attributes'] = {'table': [
                {'id': 5, 'name': 'Reallocated_Sector_Ct', 'value': 100,
                 'worst': 100, 'thresh': 10,
                 'raw': {'value': value, 'string': str(value)}}]}
            return out
        result = predictor.predict([doc(12), doc(8)])
        assert result.status == predictor.WARNING
        assert len(result.reasons) == 1
        assert 'device statistics' in result.reasons[0]

    def test_superseding_keeps_the_vendor_threshold_rule(self) -> None:
        doc = devstats(p3={32: 0})
        doc['ata_smart_attributes'] = {'table': [
            {'id': 5, 'name': 'Reallocated_Sector_Ct', 'value': 10,
             'worst': 10, 'thresh': 10,
             'raw': {'value': 0, 'string': '0'}}]}
        assert predictor.predict([doc]).status == predictor.BAD

    def test_attribute_still_applies_without_statistics(self) -> None:
        result = predictor.predict([ata(a5=(12, 100, 10)),
                                    ata(a5=(8, 100, 10))])
        assert result.status == predictor.WARNING
        assert 'ATA 5' in result.reasons[0]

    def test_monitored_condition_is_recorded_not_ruled_on(self) -> None:
        # captured for later analysis; no rule reads it yet
        doc = devstats(p3={32: 0})
        doc['ata_device_statistics']['pages'][0]['table'][0]['flags'][
            'monitored_condition_met'] = True
        sample = predictor.parse_sample(doc)
        assert sample.monitored
        assert predictor.predict([doc]).status == predictor.GOOD


class TestHelium:
    """
    Helium is judged on its normalized margin only: WDC/HGST at 22 (thresh
    25), Toshiba at 23 and 24 (thresh 75).
    """

    @staticmethod
    def helium(aid: int, normalized: int, thresh: int) -> Dict[str, Any]:
        return {'smart_status': {'passed': True}, 'ata_smart_attributes': {
            'table': [{'id': aid, 'name': 'Helium', 'value': normalized,
                       'worst': normalized, 'thresh': thresh,
                       'raw': {'value': 0, 'string': '0'}}]}}

    @pytest.mark.parametrize('aid', [23, 24])
    @pytest.mark.parametrize('normalized,expected', [
        (100, predictor.GOOD),
        (80, predictor.GOOD),
        (77, predictor.WARNING),
        (75, predictor.BAD),
        (60, predictor.BAD),
    ])
    def test_helium_condition(self, aid: int, normalized: int,
                              expected: str) -> None:
        result = predictor.predict([self.helium(aid, normalized, 75)])
        assert result.status == expected, result.reasons

    @pytest.mark.parametrize('normalized,expected', [
        (100, predictor.GOOD),   # a full drive, as every observed one is
        (40, predictor.GOOD),
        (33, predictor.GOOD),
        (32, predictor.WARNING),  # 10% of the way from 100 down to 25
        (26, predictor.WARNING),
        (25, predictor.BAD),      # the vendor's own threshold
        (20, predictor.BAD),
    ])
    def test_helium_level(self, normalized: int, expected: str) -> None:
        result = predictor.predict([self.helium(22, normalized, 25)])
        assert result.status == expected, result.reasons

    def test_a_full_helium_level_is_not_confused_with_a_counter(self) -> None:
        doc = self.helium(22, 100, 25)
        doc['ata_smart_attributes']['table'][0]['raw'] = {'value': 100,
                                                          'string': '100'}
        assert predictor.predict([doc]).status == predictor.GOOD

    def test_a_packed_helium_raw_is_not_confused_with_a_counter(self) -> None:
        # many HGST/WDC drives pack current and worst levels: 0x640064
        packed = (100 << 16) | 100
        assert packed > predictor.COUNTER_BACKSTOP
        doc = self.helium(22, 100, 25)
        doc['ata_smart_attributes']['table'][0]['raw'] = {
            'value': packed, 'string': str(packed)}
        assert predictor.predict([doc]).status == predictor.GOOD


class TestBackstop:
    """
    COUNTER_BACKSTOP catches damage older than the prediction window.
    """

    def stat(self, value: int) -> Dict[str, Any]:
        return devstats(p3={32: value})

    def test_stable_damage_above_the_backstop_warns(self) -> None:
        doc = self.stat(predictor.COUNTER_BACKSTOP)
        result = predictor.predict([doc, doc])
        assert result.status == predictor.WARNING
        assert 'predates the sample window' in result.reasons[0]

    def test_just_below_the_backstop_stays_good(self) -> None:
        doc = self.stat(predictor.COUNTER_BACKSTOP - 1)
        assert predictor.predict([doc, doc]).status == predictor.GOOD

    @pytest.mark.parametrize('value,expected', [
        # the nonzero reallocation counts observed across two live hosts
        (16, predictor.GOOD),
        (1008, predictor.WARNING),
        (3800, predictor.WARNING),
    ])
    def test_observed_fleet_values(self, value: int, expected: str) -> None:
        doc = self.stat(value)
        assert predictor.predict([doc, doc]).status == expected

    def test_growth_is_preferred_over_the_backstop(self) -> None:
        result = predictor.predict([self.stat(3800), self.stat(3790)])
        assert result.status == predictor.WARNING
        assert len(result.reasons) == 1
        assert 'grew by 10' in result.reasons[0]

    def test_the_backstop_reaches_a_single_sample(self) -> None:
        assert predictor.predict([self.stat(3800)]).status == predictor.WARNING

    def test_backstop_applies_to_ata_attributes(self) -> None:
        sample = ata(a5=(3800, 100, 10))
        assert predictor.predict([sample, sample]).status == predictor.WARNING

    def test_backstop_applies_to_scsi(self) -> None:
        doc = {'smart_status': {'passed': True}, 'scsi_grown_defect_list': 900}
        result = predictor.predict([doc, doc])
        assert result.status == predictor.WARNING
        assert 'grown defect list' in result.reasons[0]

    def test_backstop_applies_to_nvme_media_errors(self) -> None:
        doc = nvme(media_errors=900)
        result = predictor.predict([doc, doc])
        assert result.status == predictor.WARNING
        assert 'media errors' in result.reasons[0]

    def test_the_backstop_is_not_a_tuned_threshold(self) -> None:
        assert predictor.COUNTER_BACKSTOP >= 100
