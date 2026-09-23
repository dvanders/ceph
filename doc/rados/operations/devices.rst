.. _devices:

Device Management
=================

Device management allows Ceph to address hardware failure. Ceph tracks hardware
storage devices (HDDs, SSDs) to see which devices are managed by which daemons.
Ceph also collects health metrics about these devices. By doing so, Ceph can
provide tools that predict hardware failure and can automatically respond to
hardware failure.

Device tracking
---------------

To see a list of the storage devices that are in use, run the following
command:

.. prompt:: bash $

   ceph device ls

Alternatively, to list devices by daemon or by host, run a command of one of
the following forms:

.. prompt:: bash $

   ceph device ls-by-daemon <daemon>
   ceph device ls-by-host <host>

To see information about the location of a specific device and about how the
device is being consumed, run a command of the following form:

.. prompt:: bash $

   ceph device info <devid>

Identifying physical devices
----------------------------

To make the replacement of failed disks easier and less error-prone, you can
(in some cases) "blink" the drive's LEDs on hardware enclosures by running a
command of the following form::

  device light on|off <devid> [ident|fault] [--force]

.. note:: Using this command to blink the lights might not work. Whether it
   works will depend upon such factors as your kernel revision, your SES
   firmware, or the setup of your HBA.

The ``<devid>`` parameter is the device identification. To retrieve this
information, run the following command:

.. prompt:: bash $

   ceph device ls

The ``[ident|fault]`` parameter determines which kind of light will blink.  By
default, the `identification` light is used.

.. note:: This command works only if the Cephadm or the Rook
   :ref:`orchestrator <orchestrator-cli-module>` module is enabled.  To see
   which orchestrator module is enabled, run the following command:

   .. prompt:: bash $

      ceph orch status

The command that makes the drive's LEDs blink is `lsmcli`. To customize this
command, configure it via a Jinja2 template by running commands of the
following forms::

   ceph config-key set mgr/cephadm/blink_device_light_cmd "<template>"
   ceph config-key set mgr/cephadm/<host>/blink_device_light_cmd "lsmcli local-disk-{{ ident_fault }}-led-{{'on' if on else 'off'}} --path '{{ path or dev }}'"

The following arguments can be used to customize the Jinja2 template:

* ``on``
    A boolean value.
* ``ident_fault``
    A string that contains `ident` or `fault`.
* ``dev``
    A string that contains the device ID: for example, `SanDisk_X400_M.2_2280_512GB_162924424784`.
* ``path``
    A string that contains the device path: for example, `/dev/sda`.

.. _enabling-monitoring:

Enabling monitoring
-------------------

Ceph can also monitor the health metrics associated with your device. For
example, SATA drives implement a standard called SMART that provides a wide
range of internal metrics about the device's usage and health (for example: the
number of hours powered on, the number of power cycles, the number of
unrecoverable read errors). Other device types such as SAS and NVMe present a
similar set of metrics (via slightly different standards).  All of these
metrics can be collected by Ceph via the ``smartctl`` tool.

You can enable or disable health monitoring by running one of the following
commands:

.. prompt:: bash $

   ceph device monitoring on
   ceph device monitoring off

Scraping
--------

If monitoring is enabled, device metrics will be scraped automatically at
regular intervals. To configure that interval, run a command of the following
form:

.. prompt:: bash $

   ceph config set mgr mgr/devicehealth/scrape_frequency <seconds>

By default, device metrics are scraped once every 24 hours.

To manually scrape all devices, run the following command:
   
.. prompt:: bash $

   ceph device scrape-health-metrics

To scrape a single device, run a command of the following form:

.. prompt:: bash $

   ceph device scrape-health-metrics <device-id>

To scrape a single daemon's devices, run a command of the following form:

.. prompt:: bash $

   ceph device scrape-daemon-health-metrics <who>

To retrieve the stored health metrics for a device (optionally for a specific
timestamp),  run a command of the following form:

.. prompt:: bash $

   ceph device get-health-metrics <devid> [sample-timestamp]

Failure prediction
------------------

Ceph can predict drive life expectancy and device failures by analyzing the
health metrics that it collects. The prediction modes are as follows:

* *none*: disable device failure prediction.
* *smart*: use the rule-based SMART predictor built into the ``devicehealth``
  module. See :ref:`devicehealth-smart-prediction` below.
* *local*: use a pre-trained machine-learning model from the
  ``diskprediction_local`` module. See :ref:`diskprediction`.

To configure the prediction mode, run a command of the following form:

.. prompt:: bash $

   ceph config set global device_failure_prediction_mode <mode>

Under normal conditions, failure prediction runs periodically in the
background.  For this reason, life expectancy values might be populated only
after a significant amount of time has passed.  The life expectancy of all
devices is displayed in the output of the following command:

.. prompt:: bash $

   ceph device ls

To see the metadata of a specific device, run a command of the following form:

.. prompt:: bash $

   ceph device info <devid>

To explicitly force prediction of a specific device's life expectancy, run a
command of the following form:

.. prompt:: bash $

   ceph device predict-life-expectancy <devid>

In addition to Ceph's internal device failure prediction, you might have an
external source of information about device failures. To inform Ceph of a
specific device's life expectancy, run a command of the following form:

.. prompt:: bash $

   ceph device set-life-expectancy <devid> <from> [<to>]

Life expectancies are expressed as a time interval. This means that the
uncertainty of the life expectancy can be expressed in the form of a range of
time, and perhaps a wide range of time. The interval's end can be left
unspecified.

.. _devicehealth-smart-prediction:

The SMART predictor
-------------------

The ``smart`` mode applies a small set of rules to the SMART data that
``devicehealth`` already collects. It needs no additional mgr module or model.
Each device gets one of four verdicts:

+-------------+---------------------------------+-------------------------------+
| Verdict     | Meaning                         | Life expectancy recorded      |
+=============+=================================+===============================+
| ``Good``    | No rule matched.                | More than six weeks.          |
+-------------+---------------------------------+-------------------------------+
| ``Warning`` | A defect counter is growing, or | Two to six weeks. Raises a    |
|             | a value is near its limit.      | health warning.               |
+-------------+---------------------------------+-------------------------------+
| ``Bad``     | The device failed a check       | Less than two weeks. Marks    |
|             | outright.                       | the OSD ``out`` if            |
|             |                                 | ``self_heal`` is enabled.     |
+-------------+---------------------------------+-------------------------------+
| ``Unknown`` | No usable SMART data.           | None. Clears any earlier      |
|             |                                 | value.                        |
+-------------+---------------------------------+-------------------------------+

``ceph device ls`` shows the verdict in the ``HEALTH`` column and the recorded
band in ``LIFE EXPECTANCY``:

.. prompt:: bash $

   ceph device ls

::

   DEVICE                    HOST:DEV    DAEMONS  WEAR  HEALTH   LIFE EXPECTANCY
   WDC_WUH721816ALE6L4_XXXX  ceph01:sdb  osd.3          Bad      <13d
   TOSHIBA_MG08ACA16TE_YYYY  ceph02:sdf  osd.7          Warning  2w to 6w
   ST16000NM001G_ZZZZ        ceph03:sdd  osd.11         Good     >6w

``mark_out_threshold`` and ``warn_threshold`` act on ``LIFE EXPECTANCY``,
which is also where ``ceph device set-life-expectancy`` writes.

The verdicts are risk tiers, not countdowns. On the Backblaze Q1 2026
drive-stats data (351,095 drives and 1,030 failures over 90 days):

* 4.4% of ``Warning`` devices failed within six weeks, compared with 0.05% of
  unflagged devices.
* 11% of ``Bad`` devices failed within two weeks, compared with 0.02% of
  unflagged devices. This is based on a sample of 62 devices.

Read ``Bad`` as "move the data off this device first", not as "this device
has two weeks left".

Defect counters are judged on growth. The predictor compares the newest sample
with the oldest one inside ``mgr/devicehealth/prediction_window`` (30 days by
default). A drive with a stable count of old reallocated sectors stays
``Good``, and a warning clears once the counter stops growing. As a result:

* Counter rules need at least two samples.
* Damage older than the window shows no growth. To catch it, any defect
  counter at or above 256 warns regardless of growth. Healthy drives read zero
  or single digits and damaged ones read hundreds or more, so the exact value
  is not critical. This warning does not clear on its own. Once the device's
  OSDs are ``out`` and drained, it moves to ``DEVICE_HEALTH_REPLACE``.

The window has no effect beyond ``mgr/devicehealth/retention_period``, which
limits how long samples are kept.

A device is ``Bad`` when:

* it fails its own SMART self-assessment,
* an attribute's normalized value has reached its vendor threshold, or
* an NVMe device reports degraded reliability or read-only media, or its
  available spare has reached its threshold.

A device is ``Warning`` when:

* one of these counters grows: ``Reallocated_Sector_Ct`` (ATA 5),
  ``Spin_Retry_Count`` (10), ``End-to-End_Error`` (184),
  ``Reported_Uncorrect`` (187), ``Command_Timeout`` (188, by 2 or more) or
  ``Offline_Uncorrectable`` (198),
* ``Current_Pending_Sector`` (197) is nonzero. This is a gauge that falls as
  sectors are remapped, so its current value is what counts,
* an attribute has used 90% of its margin to its vendor threshold,
* a SAS device's grown defect list or uncorrected error counts grow,
* an NVMe device's media errors grow, it sets any other critical warning bit,
  its available spare is within 10 points of its threshold, or its percentage
  used is 90 or more, or
* the device has used 90% of its rated write endurance.

The vendor-threshold margin is a fraction of the attribute's whole range, not
a fixed number of points, because some drives ship with a threshold of 90
against a fresh value of 100.

Attributes whose meaning varies by vendor, such as ``Raw_Read_Error_Rate`` or
temperature, are ignored, as is power-on time. A rule does not apply to a
drive that does not report its attribute. Rules for a specific model can be
added with a ruleset (see below).

When a drive reports the ACS Device Statistics log, these statistics are used
in place of the matching attributes:

+-------+--------+------------------------------------------+---------------+
| Page  | Offset | Statistic                                | Replaces      |
+=======+========+==========================================+===============+
| 3     | 32     | Reallocated Logical Sectors              | ATA 5         |
+-------+--------+------------------------------------------+---------------+
| 3     | 48     | Mechanical Start Failures                | ATA 10        |
+-------+--------+------------------------------------------+---------------+
| 3     | 56     | Reallocation Candidate Logical Sectors   | ATA 197       |
+-------+--------+------------------------------------------+---------------+
| 4     | 8      | Reported Uncorrectable Errors            | ATA 187       |
+-------+--------+------------------------------------------+---------------+
| 4     | 16     | Resets Between Command Acceptance and    | ATA 188       |
|       |        | Completion                               |               |
+-------+--------+------------------------------------------+---------------+
| 7     | 8      | Percentage Used Endurance Indicator      | wear level    |
+-------+--------+------------------------------------------+---------------+

A statistic is used only when the drive marks it valid. The replaced
attribute's vendor-threshold margin is still checked.

Helium drives are judged only on the helium attribute's margin to its vendor
threshold: ``Helium_Level`` (ATA 22, WDC and HGST, threshold 25), or
``Helium_Condition_Lower`` and ``Helium_Condition_Upper`` (ATA 23 and 24,
Toshiba, threshold 75). Their raw values are levels rather than counts, and
are ignored.

To see why a device got its verdict, run a command of the following form:

.. prompt:: bash $

   ceph device explain-health <devid>

::

   WDC_WUH721816ALE6L4_XXXXXXXX: Warning
     - Current_Pending_Sector (ATA 197) is 8, at or above the threshold of 1
     - Reallocated_Sector_Ct (ATA 5) grew by 4 over the sample window, to 12

Rulesets
--------

The built-in rules use only counters that mean the same thing across drive
models. To add rules for a particular model, or to flag a known-bad firmware,
load a ruleset:

.. prompt:: bash #

   ceph device set-predictor-ruleset -i ruleset.json
   ceph device get-predictor-ruleset
   ceph device rm-predictor-ruleset

A ruleset is a JSON document that starts from the built-in rules. For
example:

.. code-block:: json

   {
     "ruleset": "example-2026.08",
     "defaults": {"counter_backstop": 256},
     "profiles": [
       {
         "name": "toshiba-mg07",
         "match": {"model_name": "TOSHIBA MG07ACA*"},
         "defaults": {"normalized_headroom_fraction": 0.25},
         "ata": {"1": {"name": "Raw_Read_Error_Rate", "growth": 1}},
         "device_statistics": [
           {"name": "Spindle Speed Deviations",
            "page": 3, "offset": 64, "growth": 1}
         ]
       }
     ]
   }

Top-level keys (only ``ruleset`` is required):

``ruleset``
   A name, reported by ``ceph device explain-health``.
``defaults``
   Tunables to change, from the table below.
``ata``
   SMART attribute rules, keyed by attribute id as a string.
``device_statistics``
   ACS Device Statistics rules, as an array.
``profiles``
   Rules for matching devices only. Each has a ``name``, a ``match``, and any
   of ``defaults``, ``ata``, ``device_statistics`` and ``findings``.

An ``ata`` or ``device_statistics`` rule is added to the built-in rules, or
replaces the built-in rule with the same attribute id, or the same page and
offset. A rule has these keys:

``name``
   Required. The name used in verdicts.
``growth``
   Warn when the counter grows by at least this much within the prediction
   window. At least 1. Most counters want this.
``absolute``
   Warn when the counter's current value is at least this much. Use this only
   for gauges such as ``Current_Pending_Sector``.
``page``, ``offset``
   Required for ``device_statistics``: the location of the statistic.
``supersedes``
   Only for ``device_statistics``: the SMART attribute id whose raw counter
   this statistic replaces.

A rule with neither ``absolute`` nor ``growth`` treats the raw value as a
level rather than a count. Only its vendor-threshold margin is checked, and
``counter_backstop`` does not apply. The helium rules are defined this way.

``defaults`` may set these tunables, in a ruleset or a profile:

+----------------------------------+---------+---------------------------------+
| Key                              | Default | Meaning                         |
+==================================+=========+=================================+
| ``counter_backstop``             | 256     | Warn on any defect counter at   |
|                                  |         | or above this value.            |
+----------------------------------+---------+---------------------------------+
| ``normalized_headroom_fraction`` | 0.1     | Warn when the margin to the     |
|                                  |         | vendor threshold is at most     |
|                                  |         | this fraction of the range.     |
+----------------------------------+---------+---------------------------------+
| ``wear_warning``                 | 0.9     | Warn at this fraction of rated  |
|                                  |         | write endurance.                |
+----------------------------------+---------+---------------------------------+
| ``nvme_spare_headroom``          | 10      | Warn when NVMe available spare  |
|                                  |         | is within this many points of   |
|                                  |         | its threshold.                  |
+----------------------------------+---------+---------------------------------+
| ``nvme_used_warning``            | 90      | Warn at this NVMe               |
|                                  |         | ``percentage_used``.            |
+----------------------------------+---------+---------------------------------+
| ``scsi_defect_growth``           | 1       | SAS grown defect list growth    |
|                                  |         | that warns.                     |
+----------------------------------+---------+---------------------------------+
| ``scsi_error_growth``            | 1       | SAS uncorrected error growth    |
|                                  |         | that warns.                     |
+----------------------------------+---------+---------------------------------+

A profile's ``match`` may name ``model_name``, ``model_family``,
``firmware_version``, ``vendor`` and ``product``. Each is a case-insensitive
shell-style pattern. Every named field must match, and a field the device does
not report never matches. Profiles are applied in order, and later ones win.

A profile can also assert a verdict from the device's identity alone:

.. code-block:: json

   {
     "ruleset": "example-2026.08",
     "profiles": [
       {
         "name": "st4000nm-sc61",
         "match": {"model_name": "ST4000NM*", "firmware_version": "SC61"},
         "findings": [
           {"status": "Bad",
            "reason": "SC61 can lose writes on power loss; use SC62"}
         ]
       }
     ]
   }

These findings apply even when the device returns no usable SMART data. Only
``Warning`` and ``Bad`` can be asserted, so a ruleset cannot hide a failure.
Findings are allowed only in profiles.

``ceph device explain-health`` reports the ruleset and profiles behind a
verdict::

   Toshiba_MG07ACA14TE_XXXXXXXX: Warning
     - Spindle Speed Deviations (device statistics page 3) grew by 3 ...
   ruleset: example-2026.08 (profile: toshiba-mg07)

A ruleset is validated when it is loaded. Unknown keys and values that would
flag every device, such as a growth of 0, are rejected. If a stored ruleset
later fails to load, for example after a downgrade, the module logs an error
and uses the built-in rules.

.. note:: A ruleset changes which OSDs ``self_heal`` marks ``out``. The rate
   limits under `Automatic Migration`_ still apply.

Health alerts
-------------

The ``mgr/devicehealth/warn_threshold`` configuration option controls the
health check for an expected device failure. If the device is expected to fail
within the specified time interval, an alert is raised.

Once every OSD on a device is ``out`` and drained, the device is reported
under ``DEVICE_HEALTH_REPLACE`` instead of ``DEVICE_HEALTH``, because it only
needs replacing. A device shared by several OSDs stays under
``DEVICE_HEALTH`` while any of them is still in service.

To check the stored life expectancy of all devices and generate any appropriate
health alert, run the following command:

.. prompt:: bash $

   ceph device check-health

Automatic Migration
-------------------

The ``mgr/devicehealth/self_heal`` option (enabled by default) automatically
migrates data away from devices that are expected to fail soon. If this option
is enabled, the module marks such devices ``out`` so that automatic migration
will occur.

.. note:: The ``mon_osd_min_in_ratio`` configuration option can help prevent
   this process from cascading to total failure. If the "self heal" module
   marks ``out`` so many OSDs that the ratio value of ``mon_osd_min_in_ratio``
   is exceeded, then the cluster raises the ``DEVICE_HEALTH_TOOMANY`` health
   check. For instructions on what to do in this situation, see
   :ref:`DEVICE_HEALTH_TOOMANY<rados_health_checks_device_health_toomany>`.

The ``mgr/devicehealth/mark_out_threshold`` configuration option specifies the
time interval for automatic migration. If a device is expected to fail within
the specified time interval, it will be automatically marked ``out``.

Rate limiting
~~~~~~~~~~~~~

``mon_osd_min_in_ratio`` is a floor, not a rate limit: on its own it allows a
large part of a cluster to be marked ``out`` in one pass. Because predictions
and scrapes can be wrong, self-heal is also rate limited:

``mgr/devicehealth/mark_out_max_concurrent`` (default 1)
   How many devices self-heal may have draining at once. Another device is not
   marked ``out`` until these have drained. ``0`` keeps the health checks but
   never marks anything ``out``.
``mgr/devicehealth/mark_out_min_interval`` (default one hour)
   The minimum time between mark outs. This matters because an OSD with no
   data drains immediately.

The limits count devices, not OSDs. OSDs that share a device, for example
several OSDs with ``block.db`` on one NVMe drive, are marked ``out`` together
and count as one device.

Self-heal does not drain two devices on the same host at once. This spreads
the backfill load, and protects CRUSH rules whose failure domain is the OSD.

A device with more OSDs than ``mon_osd_min_in_ratio`` allows is not evacuated
and raises ``DEVICE_HEALTH_TOOMANY``. Self-heal moves on to the next device.

An OSD stops counting against the limits once it has drained, is marked back
``in``, or is removed. The accounting is kept in the module's store, so it
survives a mgr restart or failover.
