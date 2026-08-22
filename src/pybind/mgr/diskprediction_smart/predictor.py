"""Rules-based heuristics for disk failure prediction.

The classes defined here provide the SMART disk failure prediction module.
CLYSODiskFailurePredictor uses a rule-based model developed by CLYSO.

To predict hard drive health and deduce time to failure, the
predict function is called with 6 days worth of SMART data from the hard drive.
It will return a string to indicate disk failure status: "Good", "Warning",
"Bad", or "Unknown".
"""
import os
import json
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple


DevSmartT = Dict[str, Any]
AttrNamesT = List[str]
AttrDiffsT = List[Dict[str, int]]


class Predictor:
    @classmethod
    def create(cls, name: str) -> Optional['Predictor']:
        if name == 'clyso':
            return CLYSODiskFailurePredictor()
        else:
            return None

    def predict(self, dataset: Sequence[DevSmartT]) -> str:
        raise NotImplementedError()


class CLYSODiskFailurePredictor(Predictor):
    """Simple threshold-based failure prediction module developed at CLYSO.

       References:
           https://en.wikipedia.org/wiki/Self-Monitoring,_Analysis_and_Reporting_Technology
    """

    LOGGER = logging.getLogger()

    def predict(self, disk_days: Sequence[DevSmartT]) -> str:
        """Predict the disk failure status based on the provided SMART data.

        Arguments:
            disk_days {list} -- list in which each element is a dictionary with key,val
                                as feature name,value respectively.
                                e.g.[{'smart_1_raw': 0, 'user_capacity': 512 ...}, ...]

        Returns:
            str -- Prediction result: "Good", "Warning", "Bad", or "Unknown"
        """

        # Return Unknown if disk_days is empty
        if len(disk_days) < 1:
            return "Unknown"

        # Quickly return "Bad" if the device reports itself as FAILING
        for smart in disk_days:
            if not smart.get("smart_status", True):
                CLYSODiskFailurePredictor.LOGGER.debug(
                    "Bad: Drive failure expected in less than 24 hours. SAVE ALL DATA."
                )
                return "Bad"

        # Warn if power on hours is more than 7 years
        for smart in disk_days:
            if smart.get("smart_9_raw", 0) > 7 * 365 * 24:
                CLYSODiskFailurePredictor.LOGGER.debug(
                    f"Warning: power on hours {smart['smart_9_raw']} exceeds 7 years"
                )
                return "Warning"

        # Warn if any of the well-known ATA failure indicators are non-zero
        ata_warning_thresholds = [
            ("smart_5_raw",   1, "Reallocated_Sector_Ct"),
            ("smart_10_raw",  1, "Spin_Retry_Count"),
            ("smart_184_raw", 1, "End_to_End_Error"),
            ("smart_196_raw", 1, "Reallocation_Event_Count"),
            ("smart_197_raw", 1, "Current_Pending_Sector"),
            ("smart_198_raw", 1, "Offline_Uncorrectable"),
            ("smart_201_raw", 1, "Soft_Read_Error_Rate")
        ]
        for smart in disk_days:
            for attr, threshold, name in ata_warning_thresholds:
                if smart.get(attr, 0) >= threshold:
                    CLYSODiskFailurePredictor.LOGGER.debug(
                        f"Warning: {name} = {smart[attr]}"
                    )
                    return "Warning"

        # Warn if any normalized attr nears its threshold
        for smart in disk_days:
            for attr in smart:
                if attr.endswith("_normalized") and attr.replace("_normalized", "_threshold") in smart:
                    normalized_value = smart.get(attr, 100)
                    threshold_value = smart.get(attr.replace("_normalized", "_threshold"), 0)
                    if normalized_value <= threshold_value + 10:  # 10 is an arbitrary buffer
                        CLYSODiskFailurePredictor.LOGGER.debug(
                            f"Warning: {attr} = {normalized_value}"
                        )
                        return "Warning"

        # Warn if any NVMe attrs are problematic
        for smart in disk_days:
            if smart.get("nvme_critical_warning", 0) != 0:
                CLYSODiskFailurePredictor.LOGGER.debug(
                    f"Warning: nvme_critical_warning = {smart['nvme_critical_warning']}"
                )
                return "Warning"

            if smart.get("nvme_available_spare", 100) <= smart.get("nvme_available_spare_threshold", 10) + 10:
                CLYSODiskFailurePredictor.LOGGER.debug(
                    f"Warning: nvme_available_spare = {smart['nvme_available_spare']}"
                )
                return "Warning"

            if smart.get("nvme_percentage_used", 0) >= 90:
                CLYSODiskFailurePredictor.LOGGER.debug(
                    f"Warning: nvme_percentage_used = {smart['nvme_percentage_used']}"
                )
                return "Warning"

        # If none of the above conditions are met, return "Good"
        return "Good"
