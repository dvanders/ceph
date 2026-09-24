.. _orchestrator-cli-host-management:

===============
Host Management
===============

Listing Hosts
=============

Run a command of this form to list hosts associated with the cluster:

.. prompt:: bash #

   ceph orch host ls [--format yaml] [--host-pattern <name>] [--label <label>] [--host-status <status>] [--detail]

In commands of this form, the arguments ``host-pattern``, ``label``, and
``host-status`` are optional and are used for filtering.

- ``host-pattern`` is a regular expression that matches against hostnames and returns only
  matching hosts.
- ``label`` returns only hosts with the specified label.
- ``host-status`` returns only hosts with the specified status (currently
  ``offline`` or ``maintenance``).
- Any combination of these filtering flags is valid. It is possible to filter
  against name, label and status simultaneously, or to filter against any
  proper subset of name, label and status.

The ``detail`` parameter provides more host-related information for cephadm-based
clusters. For example:

.. prompt:: bash #

   ceph orch host ls --detail

.. code-block:: console

    HOSTNAME     ADDRESS         LABELS  STATUS  VENDOR/MODEL                           CPU    HDD      SSD  NIC
    ceph-master  192.168.122.73  _admin          QEMU (Standard PC (Q35 + ICH9, 2009))  4C/4T  4/1.6TB  -    1
    1 hosts in cluster


.. _cephadm-adding-hosts:

Adding Hosts
============

Hosts must have these :ref:`cephadm-host-requirements` installed.
Hosts without all the necessary requirements will fail to be added to the cluster.

To add each new host to the cluster, perform two steps:

#. Install the cluster's public SSH key in the new host's root user's ``authorized_keys`` file:

   .. prompt:: bash #

      ssh-copy-id -f -i /etc/ceph/ceph.pub root@<new-host>

   For example:

   .. prompt:: bash #

      ssh-copy-id -f -i /etc/ceph/ceph.pub root@host2
      ssh-copy-id -f -i /etc/ceph/ceph.pub root@host3

#. Tell Ceph that the new node is part of the cluster:

   .. prompt:: bash #

      ceph orch host add <newhost> [<ip>] [<label1> ...]

   For example:

   .. prompt:: bash #

      ceph orch host add host2 10.10.0.102
      ceph orch host add host3 10.10.0.103

   It is best to explicitly provide the host IP address.  If an address is
   not provided, then the host name will be immediately resolved via
   DNS and the result will be used.

   One or more labels can also be included to immediately label the
   new host.  For example, by default the ``_admin`` label will make
   cephadm maintain a copy of the ``ceph.conf`` file and a
   ``client.admin`` keyring file in ``/etc/ceph``:

   .. prompt:: bash #

      ceph orch host add host4 10.10.0.104 --labels _admin


.. _cephadm-host-precheck:

Checking Host Tuning
====================

Besides the hard requirements checked by ``cephadm check-host``, cephadm can
run advisory checks of how well a host is tuned for Ceph:

.. prompt:: bash #

   ceph cephadm host-precheck <host> [--addr <ip>] [--format json]

The host does not need to have been added to the cluster yet, as long as it
has the cluster's SSH key. The checks cover:

* ``sysctl`` settings such as ``vm.swappiness``, ``kernel.pid_max``,
  ``fs.aio-max-nr`` and the TCP buffer limits. Settings that cephadm applies
  itself when it deploys an OSD, and settings that matter only on busy or
  fast hosts (such as the TCP buffer limits), are reported as ``info``. Use
  :ref:`cephadm-os-tuning-profiles` to apply them.
* the kernel command line: on AMD EPYC, whether the IOMMU is off
  (``amd_iommu=off``), which avoids a large loss of network and NVMe
  throughput; and whether CPU vulnerability mitigations are on.
  ``mitigations=off`` is recommended for dedicated OSD hosts, but not for
  hosts that also run hypervisors or other clients, so it is reported as
  ``info``.
* transparent huge pages, the active ``tuned`` profile, the CPU frequency
  governor and swap (``zram`` swap is not counted, as it is compressed
  memory rather than disk).
* the I/O scheduler of each non-OS disk (``mq-deadline`` for HDDs, ``none``
  for flash), HDDs with volatile write cache enabled, and disks that are
  already in use.
* NIC link speed and duplex, degraded bonds, and bond or VLAN members with a
  smaller MTU than the interface stacked on them.
* whether the host has an address in each configured ``public_network`` and
  ``cluster_network``.
* whether the host has enough memory and CPU threads for one OSD on each of
  its data disks. Mounted disks are not counted.
* power management: deep CPU idle states (C-states) that take more than 10
  microseconds to exit, a CPU energy/performance preference other than
  ``performance``, PCIe ASPM, NVMe autonomous power state transitions
  (APST), and Energy-Efficient Ethernet on NICs. Each of these adds latency
  to I/O. See :ref:`cephadm-host-tune` to turn them off.
* NUMA: ``vm.zone_reclaim_mode`` and automatic NUMA balancing on hosts with
  more than one NUMA node, and the node layout (NPS) on AMD EPYC.
* NIC tuning: ``irqbalance`` not running on a host with multi-queue NICs,
  RX rings smaller than their maximum, and segmentation or receive offloads
  that are off.
* disk health from SMART (with ``smartctl``): the overall health status,
  reallocated, pending and offline-uncorrectable sectors, interface CRC
  errors (usually a bad cable or backplane), the ATA error log, failed
  self-tests, and for NVMe the critical warning, media errors, spare and
  endurance.
* PCIe links of NICs, NVMe drives and storage controllers that trained at a
  lower width or speed than the device supports, and whether the slot is
  what limits them.
* firmware: disks or NICs of one model that run different firmware. When
  run from the mgr, disk firmware is also compared with the other hosts in
  the cluster.
* NIC error counters (CRC, frame, FIFO, missed, carrier) and driver drop and
  discard counters from ``ethtool -S``.
* the BMC (with ``ipmitool``): hardware errors and thermal events in the
  system event log, and sensors in a critical or non-critical state.
* when run from the mgr, pings of every other host, on each Ceph network,
  with small packets and with packets of the full interface MTU and the
  don't-fragment bit set. If only the small pings get through, jumbo frames
  are not configured end to end.

Checks that need ``smartctl``, ``ethtool`` or ``ipmitool`` report ``info``
when the tool is not installed on the host.

Each result is ``ok``, ``info``, ``warn`` or ``fail``. The checks can also be
run directly on a host with ``cephadm host-precheck``, which accepts
``--network <cidr>``, ``--peer [<name>=]<ip>``, ``--skip <group>`` and
``--fail-on-warn``.

cephadm also runs the checks on every host once a day, and raises the
``CEPHADM_HOST_PRECHECK`` health warning for hosts with ``warn`` or ``fail``
results. These options control it:

* ``mgr/cephadm/host_precheck_interval``: how often, in seconds (default
  86400; 0 turns the periodic check and the health warning off).
* ``mgr/cephadm/host_precheck_health_level``: ``warn`` (default) raises the
  warning for ``warn`` and ``fail`` results, ``fail`` only for ``fail``.
* ``mgr/cephadm/host_precheck_skip``: check groups to skip, comma-separated,
  for problems that are known and accepted (for example ``kernel`` for CPU
  mitigations, or ``nic_tuning``).
* ``mgr/cephadm/host_precheck_max_peers``: how many other hosts each host
  pings in the periodic check (default 32; 0 for all).

``ceph orch host add`` runs these checks against every new host and appends
any ``warn`` or ``fail`` results to its output. The
``mgr/cephadm/host_precheck_on_add`` option controls this:

* ``warn`` (default): add the host and report the problems.
* ``enforce``: refuse to add a host that has any ``warn`` or ``fail`` result.
* ``off``: do not run the checks.

.. prompt:: bash #

   ceph config set mgr mgr/cephadm/host_precheck_on_add enforce

.. _cephadm-host-tune:

Tuning a Host for Latency
=========================

``host-tune`` fixes what host-precheck reports: power management, sysctls,
services and missing tools:

.. prompt:: bash #

   ceph cephadm host-tune <host> [--apply] [--cmdline] [--iommu-off] [--mitigations-off]
       [--no-packages] [--nic-rings]
   ceph cephadm host-tune <host> --remove

Without ``--apply`` it only shows what it would change. With ``--apply`` it
sets the ``performance`` CPU governor and energy preference, disables CPU
idle states that take more than 10 microseconds to exit, sets the PCIe ASPM
policy to ``performance``, disables NVMe APST, sets transparent huge pages
to ``madvise``, turns off Energy-Efficient Ethernet, sets ``mq-deadline`` on
HDDs and ``none`` on flash, and on NUMA hosts sets ``vm.zone_reclaim_mode``
and ``kernel.numa_balancing`` to 0. It persists the sysctl values that
host-precheck recommends, such as ``vm.swappiness`` and the TCP buffer
limits; values only move toward the recommendation, and the settings cephadm
applies when it deploys an OSD are left to cephadm.

It also installs the tools that host-precheck and the burn-in use, if they
are missing: ``smartmontools``, ``nvme-cli``, ``ethtool``, ``tuned``,
``irqbalance``, ``ipmitool`` (only on hosts with a BMC), ``stress-ng`` and
``iperf3``. Each package is installed on its own, so one that the configured
repositories do not provide (for example ``stress-ng`` on EL without EPEL)
does not stop the others; host-tune never adds repositories.
``--no-packages`` skips this. It enables ``tuned`` and ``irqbalance``, and
sets tuned's profile to ``throughput-performance`` unless tuned already runs
a performance profile such as ``network-latency`` or a custom profile from
``/etc/tuned``. On hosts with a BMC it loads the ``ipmi_si`` and
``ipmi_devintf`` kernel modules at boot.

``--nic-rings`` also grows NIC RX and TX rings to their maximum. This
briefly resets the link on many drivers, so it is not done by default.

The settings apply immediately and again at every boot, from the
``ceph-host-tune`` systemd unit, a udev rule and a ``sysctl.d`` file.

``--cmdline`` also adds kernel arguments, which take effect at the next
reboot: ``processor.max_cstate=1 intel_idle.max_cstate=0 pcie_aspm=off
nvme_core.default_ps_max_latency_us=0``. On RHEL-family hosts they are added
with ``grubby``. On hosts without ``grubby`` (Debian, Ubuntu, SUSE) they are
added to ``GRUB_CMDLINE_LINUX`` in ``/etc/default/grub``, of which a copy is
kept as ``/etc/default/grub.ceph-host-tune.bak``, and the grub configuration
is regenerated with ``update-grub`` or ``grub2-mkconfig``. On hosts with
another boot loader, the output lists the arguments to add by hand. ``--iommu-off`` adds
``amd_iommu=off`` (AMD EPYC only), and ``--mitigations-off`` adds
``mitigations=off``.

.. warning::

   ``mitigations=off`` removes the kernel's protection against CPU
   vulnerabilities such as Spectre and Meltdown. Use it only on dedicated
   OSD hosts, not on hosts that also run hypervisors or other clients.

``--remove`` removes the unit, script, udev rule, ``sysctl.d`` and
``modules-load.d`` files. The runtime settings stay until the next reboot,
installed packages and enabled services stay, and kernel arguments are left
as they are; the output shows how to remove them.

.. _cephadm-host-burnin:

Burning In a Host
=================

Before a new host holds data, you can stress its CPUs, memory and disks to
find hardware that fails early. The burn-in runs in the background on the
host as the transient systemd unit ``ceph-burnin``, needs nothing beyond
python, and does not require the host to have been added to the cluster.

A burn-in refuses to start on a host that runs any Ceph process, including
containerized daemons.

.. prompt:: bash #

   ceph cephadm burnin start <host> [--duration <seconds>] [--cpu] [--memory]
       [--devices <dev> ...] [--all-available-devices] [--memory-percent <pct>]
   ceph cephadm burnin status <host> [--run-id <id>] [--format json]
   ceph cephadm burnin ls <host>
   ceph cephadm burnin stop <host>

With no test selected, a burn-in runs all three for an hour (``--duration
3600``):

* ``cpu`` and ``memory``: if ``stress-ng`` is installed on the host (see
  :ref:`cephadm-host-tune`), it runs all of its CPU and memory (``vm``)
  methods with ``--verify``, and any verification failure fails the burn-in. Otherwise, or with ``--engine
  builtin``, one process per CPU runs hashing, compression and floating
  point loops, checking every result against a reference, and memory
  workers fill ``--memory-percent`` (default 70) of the available memory
  with changing patterns and read it back.
* ``disk``: sequential and random ``O_DIRECT`` reads of every unused non-OS
  device, reporting throughput, latency and I/O errors.

Before the stress phase, each disk is benchmarked on its own: sequential
reads from the start of the disk, then random 64 KiB reads, for
``--disk-bench-seconds`` (default 30) each, and at most a quarter of the run
each. CPU and memory stress wait until the benchmark is done, so the numbers
show what the disk can do alone. In the stress phase, the two patterns run
at the same time, which makes a spinning disk seek constantly. A disk whose
benchmark is below 80% of the median of the other disks of the same model,
or whose p99 latency is more than twice theirs, is reported as a warning.

A full sequential pass over a large HDD takes more than a day. The status
shows each disk's pass progress and estimated time to complete it, and the
next burn-in resumes each disk where the previous one stopped, so a series
of shorter runs also covers the whole surface. Pass ``--from-start`` to start
at the beginning instead.

When the run ends, these are compared with their values at the start: the
EDAC memory error counters, the CPU thermal throttling counters, the BMC
event log (with ``ipmitool``), the SMART error counters of the tested disks,
and the NIC error counters. The kernel log is searched for hardware errors.
The run fails if any worker saw an error, an uncorrectable memory error was
counted, the BMC logged a hardware error, a tested disk gained reallocated,
pending or uncorrectable sectors, or the kernel logged a hardware error.
Correctable memory errors, thermal throttling or BMC thermal events, SMART
CRC errors and NIC errors are reported as warnings. If the host has no EDAC or thermal throttle counters,
for example because it has no ECC memory, the result says so, because zero
errors then proves nothing.

``burnin status`` shows the latest run. The last 20 runs are kept in
``/var/lib/ceph/burnin/runs/`` on the host: ``burnin ls`` lists them and
``burnin status --run-id <id>`` shows one of them.

To write to the disks as well as read them, pass ``--destructive``. The
sequential worker then writes blocks that record their own offset, and
reads them back to detect corruption and misdirected writes.

.. warning::

   ``--destructive`` destroys all data on the tested devices. It requires
   ``--yes-i-really-mean-it`` and refuses any device that holds the operating
   system, is mounted, has partitions or holders such as LVM, or has a
   filesystem or other signature. Zap devices first with
   ``ceph orch device zap``.

.. prompt:: bash #

   ceph cephadm burnin start host5 --duration 86400 --all-available-devices --destructive --yes-i-really-mean-it

The same tests can be run directly on a host with ``cephadm burnin
start|run|status|stop``. ``run`` stays in the foreground and exits non-zero
if the burn-in fails.

To test the network between a host and the rest of the cluster, run:

.. prompt:: bash #

   ceph cephadm burnin network <host> [--peers <host> ...] [--duration 10] [--parallel 4]

For each other host (or each of ``--peers``), on each Ceph network they
share, cephadm starts an ``iperf3`` server on the peer and runs a
bidirectional test from the host. It reports the throughput in each
direction and the TCP retransmits, and flags a direction below 70% of the
link speed and NIC error counters that increased during the test. The host
must not run any Ceph daemons, and ``iperf3`` must be installed on both
hosts. If ``firewalld`` or ``ufw`` is active on the peer, the iperf3 port (5201 by
default) is opened for the duration of the test only.

.. _cephadm-removing-hosts:

Removing Hosts
==============

A host can safely be removed from the cluster after all daemons are removed
from it.

To drain all daemons from a host, run a command of the following form:

.. prompt:: bash #

   ceph orch host drain <host>

The ``_no_schedule`` and ``_no_conf_keyring`` labels will be applied to the
host. See :ref:`cephadm-special-host-labels`.

If you want to drain daemons but leave the managed ``ceph.conf`` and keyring
files on the host, you may pass the ``--keep-conf-keyring`` flag to the
drain command:

.. prompt:: bash #

   ceph orch host drain <host> --keep-conf-keyring

This will apply the ``_no_schedule`` label to the host but not the
``_no_conf_keyring`` label.

All OSDs on the host will be scheduled to be removed. You can check
progress of the OSD removal operation with the following command:

.. prompt:: bash #

   ceph orch osd rm status

See :ref:`cephadm-osd-removal` for more details about OSD removal.

The ``orch host drain`` command also supports a ``--zap-osd-devices``
flag. Setting this flag while draining a host will cause cephadm to zap
the devices of the OSDs it is removing as part of the drain process.

.. prompt:: bash #

   ceph orch host drain <host> --zap-osd-devices

Run a command of the following form to determine whether any daemons are
still on the host:

.. prompt:: bash #

   ceph orch ps <host>

After all daemons have been removed from the host, remove the host from the
cluster by running a command of the following form:

.. prompt:: bash #

   ceph orch host rm <host>


Offline Host Removal
--------------------

If a host is offline and cannot be recovered, it can be removed from the
cluster by running a command of the following form:

.. prompt:: bash #

   ceph orch host rm <host> --offline --force

.. warning:: This can potentially cause data loss. This command forcefully
   purges OSDs from the cluster by calling ``osd purge-actual`` for each OSD.
   Any service specs that still contain this host should be manually updated.
   For more information, see :ref:`orchestrator-cli-service-spec`.


.. _orchestrator-host-labels:

Host Labels
===========

The orchestrator supports assigning labels to hosts. Labels
are free-form and have no particular meaning by themselves. Each host
can have multiple labels. They can be used to specify the placement
of daemons. For more information, see :ref:`orch-placement-by-labels`.

Labels can be added when adding a host with the ``--labels`` flag:

.. prompt:: bash #

   ceph orch host add my_hostname --labels=my_label1
   ceph orch host add my_hostname --labels=my_label1,my_label2

To add a label to an existing host, run:

.. prompt:: bash #

   ceph orch host label add my_hostname my_label

To remove a label, run:

.. prompt:: bash #

   ceph orch host label rm my_hostname my_label


.. _cephadm-special-host-labels:

Special Host Labels
-------------------

The following host labels have a special meaning to cephadm.  All start with ``_``.

* ``_no_schedule``: *Do not schedule or deploy daemons on this host*.

  This label prevents cephadm from deploying daemons on this host.  If it is added to
  an existing host that already contains Ceph daemons, it will cause cephadm to move
  those daemons elsewhere (except OSDs, which are not removed automatically).

* ``_no_conf_keyring``: *Do not deploy config files or keyrings on this host*.

  This label is effectively the same as ``_no_schedule`` but instead of working for
  daemons it works for client keyrings and ``ceph.conf`` files that are being managed
  by cephadm.

* ``_no_autotune_memory``: *Do not autotune memory on this host*.

  This label will prevent daemon memory from being tuned even when the
  :confval:`osd_memory_target_autotune` or similar option is enabled for one or more daemons
  on that host.

* ``_admin``: *Distribute config files and keyrings to this host*.

  By default, an ``_admin`` label is applied to the first host in the cluster (where
  bootstrap was originally run), and the ``client.admin`` key is set to be distributed
  to that host via the ``ceph orch client-keyring ...`` function.  Adding this label
  to additional hosts will normally cause cephadm to deploy configuration and keyring files
  in ``/etc/ceph``. Starting from versions 16.2.10 (Pacific) and 17.2.1 (Quincy), in
  addition to the default location ``/etc/ceph/`` cephadm also stores config and keyring
  files in the ``/var/lib/ceph/<fsid>/config`` directory.


Maintenance Mode
================

Putting a host into *maintenance mode* stops all Ceph daemons on the host.
Run a command of the following form to put a host into maintenance mode or
to take a host out of maintenance mode:

.. prompt:: bash #

   ceph orch host maintenance enter <hostname> [--force] [--yes-i-really-mean-it]
   ceph orch host maintenance exit <hostname> [--force] [--offline]

* Adding the ``--force`` flag to the ``enter`` command allows the user to
  bypass warnings (but not alerts).
* Adding the ``--yes-i-really-mean-it`` flag to the ``enter`` command
  bypasses all safety checks and makes an attempt to force the host into
  maintenance mode.
* Adding the ``--force`` and ``--offline`` flags to the ``exit`` command
  causes cephadm to mark hosts that are in maintenance mode and offline as
  no longer in maintenance mode. Note that if the host comes online, the
  Ceph daemons on the host will remain in the stopped state. The ``--force``
  and ``--offline`` flags of the ``exit`` command are meant to be run on
  hosts that are in maintenance mode and that are permanently offline prior
  to the removal of those hosts from cephadm management by running the
  ``ceph orch host rm`` command.

.. warning:: Using the ``--yes-i-really-mean-it`` flag to force the host to
   enter maintenance mode can cause loss of data availability, breakdown of
   the Monitor quorum due to too few running Monitors, unresponsive Manager
   module commands (such as ``ceph orch . . .`` commands), and other issues.
   Use this flag only if you're absolutely certain that you know what you're
   doing.

See also :ref:`cephadm-fqdn`.


Rescanning Host Devices
=======================

Some servers and external enclosures may not register device removal or insertion with the
kernel. In these scenarios, you'll need to perform a device rescan on the appropriate host.
A rescan is typically non-disruptive, and can be performed with the following CLI command:

.. prompt:: bash #

   ceph orch host rescan <hostname> [--with-summary]

The ``with-summary`` flag provides a breakdown of the number of HBAs found and scanned, together
with any that failed:

.. prompt:: bash #

   ceph orch host rescan rh9-ceph1 --with-summary

.. code-block:: console

   Ok. 2 adapters detected: 2 rescanned, 0 skipped, 0 failed (0.32s)


Creating many Hosts at Once
===========================

Many hosts can be added at once using ``ceph orch apply -i`` by submitting
a multi-document YAML file:

.. code-block:: yaml

    service_type: host
    hostname: node-00
    addr: 192.168.0.10
    labels:
    - example1
    - example2
    ---
    service_type: host
    hostname: node-01
    addr: 192.168.0.11
    labels:
    - grafana
    ---
    service_type: host
    hostname: node-02
    addr: 192.168.0.12

This can be combined with :ref:`service specifications<orchestrator-cli-service-spec>`
to create a cluster spec file to deploy a whole cluster in one command.
Command of the form ``cephadm bootstrap --apply-spec`` can be used to also do
this during bootstrap.

Cluster SSH Keys must be copied to hosts prior to adding them,
see :ref:`cephadm-ssh-configuration` below.


Setting the Initial CRUSH Location of a Host
============================================

Hosts can contain a ``location`` identifier which will instruct cephadm to
create a new CRUSH host bucket located in the specified hierarchy.
You can specify more than one element of the tree when doing so (for
instance if you want to ensure that the rack that a host is being
added to is also added to the default bucket), for example:

.. code-block:: yaml

    service_type: host
    hostname: node-00
    addr: 192.168.0.10
    location:
      root: default
      rack: rack1

.. note::

  The ``location`` attribute will affect only the initial CRUSH location.
  Subsequent changes to the ``location`` property will be ignored.
  Removing a host will not remove an associated CRUSH bucket unless the
  ``--rm-crush-entry`` flag is provided to the ``orch host rm`` command.

See also :ref:`crush_map_default_types`.


Removing a Host from the CRUSH Map
==================================

The ``ceph orch host rm`` command has support for removing the associated host bucket
from the CRUSH map. This is done by providing the ``--rm-crush-entry`` flag.

.. prompt:: bash #

   ceph orch host rm host1 --rm-crush-entry

When this flag is specified, cephadm will attempt to remove the host bucket
from the CRUSH map as part of the host removal process. Note that if
it fails to do so, cephadm will report the failure and the host will remain under
cephadm control.

.. note::

  Removal from the CRUSH map will fail if there are OSDs deployed on the
  host. If you would like to remove all the host's OSDs as well, please start
  by using the ``ceph orch host drain`` command to do so. Once the OSDs
  have been removed, then you may direct cephadm to remove the CRUSH bucket
  along with the host using the ``--rm-crush-entry`` flag.


.. _cephadm-os-tuning-profiles:

OS Tuning Profiles
==================

Cephadm can be used to manage operating system tuning profiles that apply
``sysctl`` settings to sets of hosts.

To do so, create a YAML spec file in the following format:

.. code-block:: yaml

    profile_name: 23-mon-host-profile
    placement:
      hosts:
        - mon-host-01
        - mon-host-02
    settings:
      fs.file-max: 1000000
      vm.swappiness: '13'

Apply the tuning profile by running a command of the following form:

.. prompt:: bash #

   ceph orch tuned-profile apply -i <tuned-profile-file-name>

This profile is written to a file under ``/etc/sysctl.d/`` on each host
specified in the ``placement`` block, and then ``sysctl --system`` is
run on the host.

.. note::

  The exact filename that the profile is written to within ``/etc/sysctl.d/``
  is ``<profile-name>-cephadm-tuned-profile.conf``, where ``<profile-name>`` is
  the ``profile_name`` setting that you specify in the YAML spec. We suggest
  naming these profiles following the usual ``sysctl.d`` `NN-xxxxx` convention. Because
  sysctl settings are applied in lexicographical order (sorted by the filename
  in which the setting is specified), you may want to carefully choose
  the ``profile_name`` in your spec so that it is applied before or after other
  configuration files. Careful selection ensures that values supplied here override or
  do not override those in other ``sysctl.d`` files as desired.

.. note::

  These settings are applied only at the host level, and are not specific
  to any particular daemon or container.

.. note::

  Applying tuning profiles is idempotent when the ``--no-overwrite`` option is
  passed. Moreover, if the ``--no-overwrite`` option is passed, existing
  profiles with the same name are not overwritten.


Viewing Profiles
----------------

Run the following command to view all the profiles that cephadm currently manages:

.. prompt:: bash #

   ceph orch tuned-profile ls

.. note::

  To make modifications and re-apply a profile, pass ``--format yaml`` to the
  ``tuned-profile ls`` command. The ``tuned-profile ls --format yaml`` command
  presents the profiles in a format that is easy to copy and re-apply.


Removing Profiles
-----------------

To remove a previously applied profile, run a command of the following form:

.. prompt:: bash #

   ceph orch tuned-profile rm <profile-name>

When a profile is removed, cephadm cleans up the file previously written
to ``/etc/sysctl.d``, and then ``sysctl --system`` is run on the host.


Modifying Profiles
------------------

Profiles can be modified by re-applying a YAML spec with the same name as the
profile that you want to modify, but settings within existing profiles can be
adjusted with the following commands.

To add or modify a setting in an existing profile:

.. prompt:: bash #

   ceph orch tuned-profile add-setting <profile-name> <setting-name> <value>

To remove a setting from an existing profile:

.. prompt:: bash #

   ceph orch tuned-profile rm-setting <profile-name> <setting-name>

.. note::

  Modifying the placement requires re-applying a profile with the same name.
  Remember that profiles are tracked by their names, so when a profile with the
  same name as an existing profile is applied, it overwrites the old profile
  unless the ``--no-overwrite`` flag is passed.


.. _cephadm-ssh-configuration:

SSH Configuration
=================

Cephadm uses SSH to connect to remote hosts.  SSH uses a key to authenticate
with those hosts in a secure way.


Default Behavior
----------------

Cephadm stores an SSH key in the :ref:`Monitor configuration database <ceph-conf-database>` that is used to
connect to remote hosts.  When the cluster is bootstrapped, this SSH
key is generated automatically and no additional configuration
is necessary.

The currently stored SSH key can be deleted with:

.. prompt:: bash #

   ceph cephadm clear-key


A *new* SSH key can be generated with:

.. prompt:: bash #

   ceph cephadm generate-key

The public portion of the SSH key can be retrieved with:

.. prompt:: bash #

   ceph cephadm get-pub-key

You can make use of an existing key by directly importing it with:

.. prompt:: bash #

   ceph config-key set mgr/cephadm/ssh_identity_key -i <key>
   ceph config-key set mgr/cephadm/ssh_identity_pub -i <pub>

You will then need to restart the Manager daemon to reload the
configuration with:

.. prompt:: bash #

   ceph mgr fail


.. _cephadm-ssh-user:

Configuring a Different SSH User
--------------------------------

Cephadm must be able to log into all the Ceph cluster nodes as a user
that has enough privileges to download container images, start containers
and execute commands without prompting for a password. If you do not want
to use the "root" user (default option in cephadm), you must provide
cephadm the name of the user that is going to be used to perform all the
cephadm operations. Run a command of the following form:

.. prompt:: bash #

   ceph cephadm set-user <user>

The ``set-user`` command automatically configures the specified user on all cluster
hosts by calling ``cephadm setup-ssh-user`` on each host. This command includes the following:

- Setting up passwordless sudo access for non-root users
- Authorizing the cluster's SSH public key for the user

If you have already manually configured the user on all hosts, you can skip
the automatic setup by using the ``--skip-pre-steps`` flag:

.. prompt:: bash #

   ceph cephadm set-user <user> --skip-pre-steps

For manual setup of SSH users on individual hosts, you can use the
``cephadm setup-ssh-user`` command directly:

.. prompt:: bash #

   cephadm setup-ssh-user --ssh-user <user> --ssh-pub-key <public_key>

This command validates that the user exists, configures passwordless sudo access,
and authorizes the SSH public key.


Customizing the SSH Configuration
---------------------------------

Cephadm generates an appropriate ``ssh_config`` file that is
used for connecting to remote hosts.  The configuration looks
something like this::

  Host *
  User root
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null

There are two ways to customize this configuration for your environment:

#. Import a customized configuration file that will be stored
   by the Monitor with:

   .. prompt:: bash #

      ceph cephadm set-ssh-config -i <ssh_config_file>

   To remove a customized SSH config and revert back to the default behavior:

   .. prompt:: bash #

      ceph cephadm clear-ssh-config

#. You can configure a file location for the SSH configuration file with:

   .. prompt:: bash #

      ceph config set mgr mgr/cephadm/ssh_config_file <path>

   We do *not recommend* this approach.  The path name must be
   visible to *any* Manager daemon, and cephadm runs all daemons as
   containers. That means that the file must either be placed
   inside a customized container image for your deployment, or
   manually distributed to the Manager data directory
   (``/var/lib/ceph/<cluster-fsid>/mgr.<id>`` on the host, visible at
   ``/var/lib/ceph/mgr/ceph-<id>`` from inside the container).


Setting up CA-signed Keys for the Cluster
-----------------------------------------

Cephadm also supports using CA signed keys for SSH authentication
across cluster nodes. In this setup, instead of needing a private
key and a public key, we instead need a private key and a certificate
created by signing the public key with a CA key. For more information
on setting up nodes for authentication using a CA-signed key, see
:ref:`cephadm-bootstrap-ca-signed-keys`. Once you have your private
key and signed certificate, they can be set up for cephadm to use by running
commands of the following form:

.. prompt:: bash #

   ceph config-key set mgr/cephadm/ssh_identity_key -i <private-key-file>
   ceph config-key set mgr/cephadm/ssh_identity_cert -i <signed-cert-file>


.. _cephadm-fqdn:

Fully Qualified Domain Names vs Bare Host Names
===============================================

.. note::

  Cephadm demands that the name of the host given via ``ceph orch host add``
  equals the output of ``hostname`` on remote hosts.

Otherwise cephadm can't be sure that names returned by
``ceph * metadata`` match the hosts known to cephadm. This might result
in a :ref:`cephadm-stray-host` warning.

When configuring new hosts, there are two **valid** ways to set the
``hostname`` of a host:

#. Using the bare host name. In this case:

   - ``hostname`` returns the bare host name.
   - ``hostname -f`` returns the FQDN.

#. Using the fully qualified domain name as the host name. In this case:

   - ``hostname`` returns the FQDN.
   - ``hostname -s`` return the bare host name.

Note that ``man hostname`` recommends ``hostname`` to return the bare
host name:

    The FQDN (Fully Qualified Domain Name) of the system is the
    name that the resolver(3) returns for the host name, for example
    ``ursula.example.com``. It is usually the short hostname followed by the DNS
    domain name (the part after the first dot). You can check the FQDN
    using ``hostname --fqdn`` or the domain name using ``dnsdomainname``.

    .. code-block:: none

          You cannot change the FQDN with hostname or dnsdomainname.

          The recommended method of setting the FQDN is to make the hostname
          be an alias for the fully qualified name using /etc/hosts, DNS, or
          NIS. For example, if the hostname was "ursula", one might have
          a line in /etc/hosts which reads

                 127.0.1.1    ursula.example.com ursula

Which means, ``man hostname`` recommends ``hostname`` to return the bare
host name. This in turn means that Ceph will return the bare host names
when executing ``ceph * metadata``. This in turn means cephadm also
requires the bare host name when adding a host to the cluster:
``ceph orch host add <bare-name>``.

Sudo Hardening
==============

Cephadm supports sudo hardening to enhance security by restricting sudo privilege
escalation for non-root SSH users. When sudo hardening is enabled, cephadm uses the
``cephadm_invoker.py`` script to securely execute cephadm commands with controlled
privilege escalation.

Enabling Sudo Hardening
-----------------------

To enable sudo hardening for the entire cluster, use the following command:

.. prompt:: bash #

  ceph cephadm prepare-host-and-enable-sudo-hardening <user>

This command performs a comprehensive sudo hardening setup:

1. **Host Preparation**: Prepares all cluster hosts for sudo hardening by:
   - Installing/upgrading cephadm RPM with the invoker script
   - Configuring restricted sudo access for non-root users
   - Setting up SSH key authorization

2. **SSH User Configuration**: Sets the specified user for cluster SSH operations

3. **Global Enablement**: Enables sudo hardening cluster-wide

The ``<user>`` parameter specifies which non-root user should be configured for SSH access.
This user will have restricted sudo access configured through the sudoers file.

You can manually prepare a host for sudo hardening using:

.. prompt:: bash #

  cephadm prepare-host-sudo-hardening --ssh-user <user> --ssh-pub-key <pub_key>

.. note:: During initial host addition, the root user is used for setup. After
   Sudo hardening is enabled, the specified non-root user with restricted sudo
   access will be used for ongoing operations.

Sudo Hardening Workflow
-----------------------

When sudo hardening is enabled, the following workflow is used:

1. **Host Addition**: Before adding a new host with ``ceph orch host add``,
   the cluster SSH key needs to be added to this user's ``authorized_keys``
   file and non-root users must have passwordless sudo access.
2. **Command Execution**: Instead of executing cephadm directly, commands are
   routed through ``cephadm_invoker.py``
3. **Binary Verification**: The invoker validates the cephadm binary's hash before execution
4. **Secure Execution**: Commands are executed with restricted permissions

The ``cephadm_invoker.py`` script provides the following subcommands:

- ``run``: Execute cephadm binary with hash verification
- ``deploy_cephadm_binary``: Deploy cephadm binary to final location
- ``check_existence``: Check if a file exists

Sudo Access Restrictions
-------------------------

Sudo hardening restricts sudo access for non-root users to enhance security. When a
host is prepared for sudo hardening, the sudoers configuration is modified to limit
the commands that can be executed with sudo. This prevents unauthorized command
execution while still allowing necessary cephadm operations.

The sudoers configuration restricts access to only the ``cephadm_invoker.py`` script
and essential system commands, providing a secure execution environment.

Security Benefits
-----------------
Sudo hardening provides the following security benefits:

1. The invoker validates the cephadm binary's hash
2. If validation fails, it signals for binary redeployment
3. Commands are executed with restricted sudo permissions
4. All operations are logged for security auditing


Disabling Sudo Hardening
------------------------

To disable sudo hardening:

.. prompt:: bash #

  ceph config set mgr mgr/cephadm/sudo_hardening false

.. note:: Disabling sudo hardening does not automatically revert host configurations.
   Hosts that were prepared for sudo hardening will retain the invoker setup.
