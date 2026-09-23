"""
Device health monitoring
"""

import calendar
import errno
import json
from mgr_module import MgrModule, CommandResult, MgrModuleRecoverDB, CLIRequiresDB, Option, MgrDBNotReady
import rados
import re
from threading import Event
from datetime import datetime, timedelta, timezone
from typing import cast, Any, Dict, List, Optional, Sequence, Tuple, TYPE_CHECKING, Union

from .cli import DevicehealthCLICommand
from . import predictor
from .predictor import get_ata_wear_level, get_nvme_wear_level

TIME_FORMAT = '%Y%m%d-%H%M%S'
LIFE_EXPECTANCY_FORMAT = '%Y-%m-%dT%H:%M:%S'

DEVICE_HEALTH = 'DEVICE_HEALTH'
DEVICE_HEALTH_IN_USE = 'DEVICE_HEALTH_IN_USE'
DEVICE_HEALTH_REPLACE = 'DEVICE_HEALTH_REPLACE'
DEVICE_HEALTH_TOOMANY = 'DEVICE_HEALTH_TOOMANY'
HEALTH_MESSAGES = {
    DEVICE_HEALTH: '%d device(s) expected to fail soon',
    DEVICE_HEALTH_IN_USE: '%d daemon(s) expected to fail soon and still contain data',
    DEVICE_HEALTH_REPLACE: '%d device(s) awaiting replacement',
    DEVICE_HEALTH_TOOMANY: 'Too many daemons are expected to fail soon',
}

DAY = 86400
WEEK = 7 * DAY

# Life expectancy recorded per verdict, as (from, to) seconds from now; a
# 'to' of None leaves the upper bound open.  Bad falls inside the default
# mark_out_threshold, Warning inside warn_threshold only.
LIFE_EXPECTANCY: Dict[str, Tuple[int, Optional[int]]] = {
    predictor.BAD: (0, 2 * WEEK - DAY),
    predictor.WARNING: (2 * WEEK, 6 * WEEK),
    predictor.GOOD: (6 * WEEK + DAY, None),
}


class Module(MgrModule):
    CLICommand = DevicehealthCLICommand

    # latest (if db does not exist)
    SCHEMA = [
        """
        CREATE TABLE Device (
            devid TEXT PRIMARY KEY
        ) WITHOUT ROWID;
        """,
        """
        CREATE TABLE DeviceHealthMetrics (
            time DATETIME DEFAULT (strftime('%s', 'now')),
            devid TEXT NOT NULL REFERENCES Device (devid),
            raw_smart TEXT NOT NULL,
            PRIMARY KEY (time, devid)
        );
        """
    ]

    SCHEMA_VERSIONED = [
        # v1
        [
            """
            CREATE TABLE Device (
            devid TEXT PRIMARY KEY
            ) WITHOUT ROWID;
            """,
            """
            CREATE TABLE DeviceHealthMetrics (
                time DATETIME DEFAULT (strftime('%s', 'now')),
                devid TEXT NOT NULL REFERENCES Device (devid),
                raw_smart TEXT NOT NULL,
                PRIMARY KEY (time, devid)
            );
            """,
        ]
    ]

    MODULE_OPTIONS = [
        Option(
            name='enable_monitoring',
            default=True,
            type='bool',
            desc='monitor device health metrics',
            runtime=True,
        ),
        Option(
            name='scrape_frequency',
            default=86400,
            type='secs',
            desc='how frequently to scrape device health metrics',
            runtime=True,
        ),
        Option(
            name='pool_name',
            default='device_health_metrics',
            type='str',
            desc='name of pool in which to store device health metrics',
            runtime=True,
        ),
        Option(
            name='retention_period',
            default=(86400 * 180),
            type='secs',
            desc='how long to retain device health metrics',
            runtime=True,
        ),
        Option(
            name='mark_out_threshold',
            default=(86400 * 7 * 2),  # 2 weeks
            type='secs',
            desc='automatically mark OSD if it may fail before this long',
            runtime=True,
        ),
        Option(
            name='warn_threshold',
            default=(86400 * 7 * 6),  # 6 weeks
            type='secs',
            desc='raise health warning if OSD may fail before this long',
            runtime=True,
        ),
        Option(
            name='self_heal',
            default=True,
            type='bool',
            desc='preemptively heal cluster around devices that may fail',
            runtime=True,
        ),
        Option(
            name='mark_out_max_concurrent',
            default=1,
            type='int',
            min=0,
            desc='how many devices self-heal may have draining at once',
            long_desc='OSDs sharing a device count as one.  Set to 0 to keep '
                      'the health checks but never mark an OSD out.',
            runtime=True,
        ),
        Option(
            name='mark_out_min_interval',
            default=3600,
            type='secs',
            desc='minimum time between self-heal mark out actions',
            long_desc='Needed because an OSD with no data drains '
                      'instantly, which the concurrency limit cannot slow.',
            runtime=True,
        ),
        Option(
            name='sleep_interval',
            default=600,
            type='secs',
            desc='how frequently to wake up and check device health',
            runtime=True,
        ),
        Option(
            name='prediction_window',
            default=(86400 * 30),
            type='secs',
            desc='how far back the smart predictor looks for counter growth',
            long_desc='Defect counters warn when they grow within this '
                      'window.  Has no effect beyond retention_period.',
            runtime=True,
        ),
    ]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super(Module, self).__init__(*args, **kwargs)

        # populate options (just until serve() runs)
        for opt in self.MODULE_OPTIONS:
            setattr(self, opt['name'], opt['default'])

        # other
        self.run = True
        self.event = Event()
        self._ruleset_cache: Optional[predictor.Ruleset] = None
        self._ruleset_raw: Optional[str] = None

        # for mypy which does not run the code
        if TYPE_CHECKING:
            self.enable_monitoring = True
            self.scrape_frequency = 0.0
            self.pool_name = ''
            self.device_health_metrics = ''
            self.retention_period = 0.0
            self.mark_out_threshold = 0.0
            self.warn_threshold = 0.0
            self.self_heal = True
            self.mark_out_max_concurrent = 0
            self.mark_out_min_interval = 0.0
            self.sleep_interval = 0.0
            self.prediction_window = 0.0

    def is_valid_daemon_name(self, who: str) -> bool:
        parts = who.split('.', 1)
        if len(parts) != 2:
            return False
        return parts[0] in ('osd', 'mon')

    @DevicehealthCLICommand.Read('device query-daemon-health-metrics')
    def do_query_daemon_health_metrics(self, who: str) -> Tuple[int, str, str]:
        '''
        Get device health metrics for a given daemon
        '''
        if not self.is_valid_daemon_name(who):
            return -errno.EINVAL, '', 'not a valid mon or osd daemon name'
        (daemon_type, daemon_id) = who.split('.')
        result = CommandResult('')
        self.send_command(result, daemon_type, daemon_id, json.dumps({
            'prefix': 'smart',
            'format': 'json',
        }), '')
        return result.wait()

    @CLIRequiresDB
    @DevicehealthCLICommand.Read('device scrape-daemon-health-metrics')
    @MgrModuleRecoverDB
    def do_scrape_daemon_health_metrics(self, who: str) -> Tuple[int, str, str]:
        '''
        Scrape and store device health metrics for a given daemon
        '''
        if not self.is_valid_daemon_name(who):
            return -errno.EINVAL, '', 'not a valid mon or osd daemon name'
        (daemon_type, daemon_id) = who.split('.')
        return self.scrape_daemon(daemon_type, daemon_id)

    @CLIRequiresDB
    @DevicehealthCLICommand.Read('device scrape-health-metrics')
    @MgrModuleRecoverDB
    def do_scrape_health_metrics(self, devid: Optional[str] = None) -> Tuple[int, str, str]:
        '''
        Scrape and store device health metrics
        '''
        if devid is None:
            return self.scrape_all()
        else:
            return self.scrape_device(devid)

    @CLIRequiresDB
    @DevicehealthCLICommand.Read('device get-health-metrics')
    @MgrModuleRecoverDB
    def do_get_health_metrics(self, devid: str, sample: Optional[str] = None) -> Tuple[int, str, str]:
        '''
        Show stored device metrics for the device
        '''
        return self.show_device_metrics(devid, sample)

    @CLIRequiresDB
    @DevicehealthCLICommand('device check-health')
    @MgrModuleRecoverDB
    def do_check_health(self) -> Tuple[int, str, str]:
        '''
        Check life expectancy of devices
        '''
        return self.check_health()

    @DevicehealthCLICommand('device monitoring on')
    def do_monitoring_on(self) -> Tuple[int, str, str]:
        '''
        Enable device health monitoring
        '''
        self.set_module_option('enable_monitoring', True)
        self.event.set()
        return 0, '', ''

    @DevicehealthCLICommand('device monitoring off')
    def do_monitoring_off(self) -> Tuple[int, str, str]:
        '''
        Disable device health monitoring
        '''
        self.set_module_option('enable_monitoring', False)
        self.set_health_checks({})  # avoid stuck health alerts
        return 0, '', ''

    @CLIRequiresDB
    @DevicehealthCLICommand.Read('device predict-life-expectancy')
    @MgrModuleRecoverDB
    def do_predict_life_expectancy(self, devid: str) -> Tuple[int, str, str]:
        '''
        Predict life expectancy of a device
        '''
        return self.predict_life_expectancy(devid)

    @CLIRequiresDB
    @DevicehealthCLICommand.Read('device explain-health')
    @MgrModuleRecoverDB
    def do_explain_health(self, devid: str) -> Tuple[int, str, str]:
        '''
        Explain why the smart predictor reached its verdict for a device
        '''
        return self.explain_health(devid)

    @DevicehealthCLICommand.Write('device set-predictor-ruleset')
    def do_set_predictor_ruleset(self, inbuf: str) -> Tuple[int, str, str]:
        '''
        Load a SMART predictor ruleset from a file (-i <file>)
        '''
        if not inbuf:
            return -errno.EINVAL, '', \
                'no ruleset given; pass one with -i <file>'
        try:
            doc = json.loads(inbuf)
        except ValueError as e:
            return -errno.EINVAL, '', f'not valid JSON: {e}'
        try:
            ruleset = predictor.load_ruleset(doc)
        except predictor.RulesetError as e:
            return -errno.EINVAL, '', f'not a usable ruleset: {e}'
        self.set_store('ruleset', json.dumps(doc))
        self._ruleset_cache = None
        self._ruleset_raw = None
        return 0, 'loaded ruleset %s with %d profile(s)' % (
            ruleset.name, len(ruleset.profiles)), ''

    @DevicehealthCLICommand.Read('device get-predictor-ruleset')
    def do_get_predictor_ruleset(self) -> Tuple[int, str, str]:
        '''
        Show the SMART predictor ruleset currently in effect
        '''
        doc = predictor.dump_ruleset(self._ruleset())
        return 0, json.dumps(doc, indent=2, sort_keys=True), ''

    @DevicehealthCLICommand.Write('device rm-predictor-ruleset')
    def do_rm_predictor_ruleset(self) -> Tuple[int, str, str]:
        '''
        Discard an imported ruleset and return to the built-in rules
        '''
        if not self.get_store('ruleset'):
            return 0, 'already using the built-in ruleset', ''
        self.set_store('ruleset', None)
        self._ruleset_cache = None
        self._ruleset_raw = None
        return 0, 'now using the built-in ruleset', ''

    def self_test(self) -> None:
        assert self.db_ready()
        self.config_notify()
        osdmap = self.get('osd_map')
        osd_id = osdmap['osds'][0]['osd']
        osdmeta = self.get('osd_metadata')
        devs = osdmeta.get(str(osd_id), {}).get('device_ids')
        if devs:
            devid = devs.split()[0].split('=')[1]
            self.log.debug(f"getting devid {devid}")
            (r, before, err) = self.show_device_metrics(devid, None)
            assert r == 0
            self.log.debug(f"before: {before}")
            (r, out, err) = self.scrape_device(devid)
            assert r == 0
            (r, after, err) = self.show_device_metrics(devid, None)
            assert r == 0
            self.log.debug(f"after: {after}")
            assert before != after
            # explain_health is read-only
            (r, verdict, err) = self.explain_health(devid)
            assert r == 0
            self.log.debug(f"verdict: {verdict}")

    def config_notify(self) -> None:
        for opt in self.MODULE_OPTIONS:
            setattr(self,
                    opt['name'],
                    self.get_module_option(opt['name']))
            self.log.debug(' %s = %s', opt['name'], getattr(self, opt['name']))

    def _legacy_put_device_metrics(self, t: str, devid: str, data: str) -> None:
        SQL = """
        INSERT OR IGNORE INTO DeviceHealthMetrics (time, devid, raw_smart)
            VALUES (?, ?, ?);
        """

        self._create_device(devid)
        epoch = self._t2epoch(t)
        json.loads(data)  # valid?
        self.db.execute(SQL, (epoch, devid, data))

    devre = r"[a-zA-Z0-9-]+[_-][a-zA-Z0-9-]+[_-][a-zA-Z0-9-]+"

    def _load_legacy_object(self, ioctx: rados.Ioctx, oid: str) -> bool:
        MAX_OMAP = 10000
        self.log.debug(f"loading object {oid}")
        if re.search(self.devre, oid) is None:
            return False
        with rados.ReadOpCtx() as op:
            it, rc = ioctx.get_omap_vals(op, None, None, MAX_OMAP)
            if rc == 0:
                ioctx.operate_read_op(op, oid)
                count = 0
                for t, raw_smart in it:
                    self.log.debug(f"putting {oid} {t}")
                    self._legacy_put_device_metrics(t, oid, raw_smart)
                    count += 1
                assert count < MAX_OMAP
        self.log.debug(f"removing object {oid}")
        ioctx.remove_object(oid)
        return True

    def check_legacy_pool(self) -> bool:
        try:
            # 'device_health_metrics' is automatically renamed '.mgr' in
            # create_mgr_pool
            ioctx = self.rados.open_ioctx(self.MGR_POOL_NAME)
        except rados.ObjectNotFound:
            return True
        if not ioctx:
            return True

        done = False
        with ioctx, self._db_lock, self.db:
            self.db.execute('BEGIN;')
            count = 0
            for obj in ioctx.list_objects():
                try:
                    if self._load_legacy_object(ioctx, obj.key):
                        count += 1
                except json.decoder.JSONDecodeError:
                    pass
                except rados.ObjectNotFound:
                    # https://tracker.ceph.com/issues/63882
                    # Sometimes an object appears in the pool listing but cannot be interacted with?
                    self.log.debug(f"object {obj} does not exist because it is deleted in HEAD")
                    pass
                if count >= 10:
                    break
            done = count < 10
        self.log.debug(f"finished reading legacy pool, complete = {done}")
        return done

    @MgrModuleRecoverDB
    def _do_serve(self) -> None:
        last_scrape = None
        finished_loading_legacy = False

        while self.run:
            # sleep first, in case of exceptions causing retry:
            sleep_interval = self.sleep_interval or 60
            if not finished_loading_legacy:
                sleep_interval = 2
            self.log.debug('Sleeping for %d seconds', sleep_interval)
            self.event.wait(sleep_interval)
            self.event.clear()

            if self.db_ready() and self.enable_monitoring:
                self.log.debug('Running')

                if not finished_loading_legacy:
                    finished_loading_legacy = self.check_legacy_pool()

                if last_scrape is None:
                    ls = self.get_kv('last_scrape')
                    if ls:
                        try:
                            last_scrape = datetime.strptime(ls, TIME_FORMAT)
                        except ValueError:
                            pass
                    self.log.debug('Last scrape %s', last_scrape)

                self.check_health()

                now = datetime.utcnow()
                if not last_scrape:
                    next_scrape = now
                else:
                    # align to scrape interval
                    scrape_frequency = self.scrape_frequency or 86400
                    seconds = (last_scrape - datetime.utcfromtimestamp(0)).total_seconds()
                    seconds -= seconds % scrape_frequency
                    seconds += scrape_frequency
                    next_scrape = datetime.utcfromtimestamp(seconds)
                if last_scrape:
                    self.log.debug('Last scrape %s, next scrape due %s',
                                   last_scrape.strftime(TIME_FORMAT),
                                   next_scrape.strftime(TIME_FORMAT))
                else:
                    self.log.debug('Last scrape never, next scrape due %s',
                                   next_scrape.strftime(TIME_FORMAT))
                if now >= next_scrape:
                    self.scrape_all()
                    self.predict_all_devices()
                    last_scrape = now
                    self.set_kv('last_scrape', last_scrape.strftime(TIME_FORMAT))

    def serve(self) -> None:
        self.log.info("Starting")
        self.config_notify()

        self._do_serve()

    def shutdown(self) -> None:
        self.log.info('Stopping')
        self.run = False
        self.event.set()

    def scrape_daemon(self, daemon_type: str, daemon_id: str) -> Tuple[int, str, str]:
        if not self.db_ready():
            return -errno.EAGAIN, "", "mgr db not yet available"
        raw_smart_data = self.do_scrape_daemon(daemon_type, daemon_id)
        if raw_smart_data:
            for device, raw_data in raw_smart_data.items():
                data = self.extract_smart_features(raw_data)
                if device and data:
                    self.put_device_metrics(device, data)
        return 0, "", ""

    def scrape_all(self) -> Tuple[int, str, str]:
        if not self.db_ready():
            return -errno.EAGAIN, "", "mgr db not yet available"
        osdmap = self.get("osd_map")
        assert osdmap is not None
        did_device = {}
        ids = []
        for osd in osdmap['osds']:
            ids.append(('osd', str(osd['osd'])))
        monmap = self.get("mon_map")
        for mon in monmap['mons']:
            ids.append(('mon', mon['name']))
        for daemon_type, daemon_id in ids:
            raw_smart_data = self.do_scrape_daemon(daemon_type, daemon_id)
            if not raw_smart_data:
                continue
            for device, raw_data in raw_smart_data.items():
                if device in did_device:
                    self.log.debug('skipping duplicate %s' % device)
                    continue
                did_device[device] = 1
                data = self.extract_smart_features(raw_data)
                if device and data:
                    self.put_device_metrics(device, data)
        return 0, "", ""

    def scrape_device(self, devid: str) -> Tuple[int, str, str]:
        if not self.db_ready():
            return -errno.EAGAIN, "", "mgr db not yet available"
        r = self.get("device " + devid)
        if not r or 'device' not in r.keys():
            return -errno.ENOENT, '', 'device ' + devid + ' not found'
        daemons = r['device'].get('daemons', [])
        if not daemons:
            return (-errno.EAGAIN, '',
                    'device ' + devid + ' not claimed by any active daemons')
        (daemon_type, daemon_id) = daemons[0].split('.')
        raw_smart_data = self.do_scrape_daemon(daemon_type, daemon_id,
                                               devid=devid)
        if raw_smart_data:
            for device, raw_data in raw_smart_data.items():
                data = self.extract_smart_features(raw_data)
                if device and data:
                    self.put_device_metrics(device, data)
        return 0, "", ""

    def do_scrape_daemon(self,
                         daemon_type: str,
                         daemon_id: str,
                         devid: str = '') -> Optional[Dict[str, Any]]:
        """
        :return: a dict, or None if the scrape failed.
        """
        self.log.debug('do_scrape_daemon %s.%s' % (daemon_type, daemon_id))
        result = CommandResult('')
        self.send_command(result, daemon_type, daemon_id, json.dumps({
            'prefix': 'smart',
            'format': 'json',
            'devid': devid,
        }), '')
        r, outb, outs = result.wait()

        try:
            return json.loads(outb)
        except (IndexError, ValueError):
            self.log.error(
                "Fail to parse JSON result from daemon {0}.{1} ({2})".format(
                    daemon_type, daemon_id, outb))
            return None

    def _prune_device_metrics(self) -> None:
        SQL = """
        DELETE FROM DeviceHealthMetrics
            WHERE time < (strftime('%s', 'now') - ?);
        """

        cursor = self.db.execute(SQL, (self.retention_period,))
        if cursor.rowcount >= 1:
            self.log.info(f"pruned {cursor.rowcount} metrics")

    def _create_device(self, devid: str) -> None:
        SQL = """
        INSERT OR IGNORE INTO Device VALUES (?);
        """

        cursor = self.db.execute(SQL, (devid,))
        if cursor.rowcount >= 1:
            self.log.info(f"created device {devid}")
        else:
            self.log.debug(f"device {devid} already exists")

    def put_device_metrics(self, devid: str, data: Any) -> None:
        SQL = """
        INSERT OR REPLACE INTO DeviceHealthMetrics (devid, raw_smart, time)
            VALUES (?, ?, strftime('%s', 'now'));
        """

        with self._db_lock, self.db:
            self.db.execute('BEGIN;')
            self._create_device(devid)
            self.db.execute(SQL, (devid, json.dumps(data)))
            self._prune_device_metrics()

        # extract wear level?
        wear_level = get_ata_wear_level(data)
        if wear_level is None:
            wear_level = get_nvme_wear_level(data)
        dev_data = self.get(f"device {devid}") or {}
        if wear_level is not None:
            if dev_data.get(wear_level) != str(wear_level):
                dev_data["wear_level"] = str(wear_level)
                self.log.debug(f"updating {devid} wear level to {wear_level}")
                self.set_device_wear_level(devid, wear_level)
        else:
            if "wear_level" in dev_data:
                del dev_data["wear_level"]
                self.log.debug(f"removing {devid} wear level")
                self.set_device_wear_level(devid, -1.0)

    def _t2epoch(self, t: Optional[str]) -> int:
        if not t:
            return 0
        else:
            # timestamps are written with utcfromtimestamp(), so parse as UTC
            return calendar.timegm(datetime.strptime(t, TIME_FORMAT).timetuple())

    def _get_device_metrics(self, devid: str,
                            sample: Optional[str] = None,
                            min_sample: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        res = {}

        SQL_EXACT = """
        SELECT time, raw_smart
            FROM DeviceHealthMetrics
            WHERE devid = ? AND time = ?
            ORDER BY time DESC;
        """
        SQL_MIN = """
        SELECT time, raw_smart
            FROM DeviceHealthMetrics
            WHERE devid = ? AND ? <= time
            ORDER BY time DESC;
        """

        isample = None
        imin_sample = None
        if sample:
            isample = self._t2epoch(sample)
        else:
            imin_sample = self._t2epoch(min_sample)

        self.log.debug(f"_get_device_metrics: {devid} {sample} {min_sample}")

        with self._db_lock, self.db:
            self.db.execute('BEGIN;')
            if isample:
                cursor = self.db.execute(SQL_EXACT, (devid, isample))
            else:
                cursor = self.db.execute(SQL_MIN, (devid, imin_sample))
            for row in cursor:
                t = row['time']
                dt = datetime.utcfromtimestamp(t).strftime(TIME_FORMAT)
                try:
                    res[dt] = json.loads(row['raw_smart'])
                except (ValueError, IndexError):
                    self.log.debug(f"unable to parse value for {devid}:{t}")
                    pass
        return res

    def show_device_metrics(self, devid: str, sample: Optional[str]) -> Tuple[int, str, str]:
        # verify device exists
        r = self.get("device " + devid)
        if not r or 'device' not in r.keys():
            return -errno.ENOENT, '', 'device ' + devid + ' not found'
        # fetch metrics
        res = self._get_device_metrics(devid, sample=sample)
        return 0, json.dumps(res, indent=4, sort_keys=True), ''

    def check_health(self) -> Tuple[int, str, str]:
        self.log.info('Check health')
        config = self.get('config')
        min_in_ratio = float(config.get('mon_osd_min_in_ratio'))
        mark_out_threshold_td = timedelta(seconds=self.mark_out_threshold)
        warn_threshold_td = timedelta(seconds=self.warn_threshold)
        checks: Dict[str, Dict[str, Union[int, str, Sequence[str]]]] = {}
        health_warnings: Dict[str, List[str]] = {
            DEVICE_HEALTH: [],
            DEVICE_HEALTH_IN_USE: [],
            DEVICE_HEALTH_REPLACE: [],
        }
        devs = self.get("devices")
        osds_in = {}
        osds_out = {}
        # devid -> (life expectancy, host, the OSDs on it that are still in)
        devices_in: Dict[str, Tuple[datetime, Optional[str], List[str]]] = {}
        now = datetime.now(timezone.utc)  # e.g. '2021-09-22 13:18:45.021712+00:00'
        osdmap = self.get("osd_map")
        assert osdmap is not None
        for dev in devs['devices']:
            if 'life_expectancy_max' not in dev:
                continue
            # ignore devices that are not consumed by any daemons
            if not dev['daemons']:
                continue
            if not dev['life_expectancy_max'] or \
               dev['life_expectancy_max'] == '0.000000':
                continue
            # life_expectancy_(min/max) is in the format of:
            # '%Y-%m-%dT%H:%M:%S.%f%z', e.g.:
            # '2019-01-20 21:12:12.000000+00:00'
            life_expectancy_max = datetime.strptime(
                dev['life_expectancy_max'],
                '%Y-%m-%dT%H:%M:%S.%f%z')
            self.log.debug('device %s expectancy max %s', dev,
                           life_expectancy_max)

            # dev['daemons'] == ["osd.0","osd.1","osd.2"]
            osds = [x for x in dev['daemons'] if x.startswith('osd.')]
            osd_ids = [x[4:] for x in osds]

            if life_expectancy_max - now <= mark_out_threshold_td:
                if self.self_heal:
                    for _id in osd_ids:
                        if self.is_osd_in(osdmap, _id):
                            osds_in[_id] = life_expectancy_max
                        else:
                            osds_out[_id] = 1
                    # OSDs sharing a device are marked out together
                    still_in = [x for x in osd_ids
                                if self.is_osd_in(osdmap, x)]
                    if still_in:
                        devices_in[dev['devid']] = (
                            life_expectancy_max,
                            dev['location'][0]['host'] if dev['location']
                            else None,
                            still_in)

            if life_expectancy_max - now <= warn_threshold_td:
                # device can appear in more than one location in case
                # of SCSI multipath
                device_locations = ','.join(x['host'] + ':' + x['dev']
                                            for x in dev['location'])
                if self._awaiting_replacement(osdmap, dev, osds, osd_ids):
                    health_warnings[DEVICE_HEALTH_REPLACE].append(
                        '%s (%s); %s marked out and drained'
                        % (dev['devid'], device_locations, ','.join(osds)))
                else:
                    health_warnings[DEVICE_HEALTH].append(
                        '%s (%s); daemons %s; life expectancy between %s and %s'
                        % (dev['devid'],
                           device_locations,
                           ','.join(dev.get('daemons', ['none'])),
                           dev.get('life_expectancy_min', 'unknown'),
                           dev['life_expectancy_max']))

        # OSD might be marked 'out' (which means it has no
        # data), however PGs are still attached to it.
        for _id in osds_out:
            num_pgs = self.get_osd_num_pgs(_id)
            if num_pgs > 0:
                health_warnings[DEVICE_HEALTH_IN_USE].append(
                    'osd.%s is marked out '
                    'but still has %s PG(s)' %
                    (_id, num_pgs))
        if devices_in:
            self.log.debug('osds_in %s' % osds_in)
            # calculate target in ratio
            num_osds = len(osdmap['osds'])
            num_in = len([x for x in osdmap['osds'] if x['in']])
            num_bad = len(osds_in)
            # sort with next-to-fail first
            bad_devices = sorted(devices_in.items(),
                                 key=lambda kv: kv[1][0])
            did = 0
            eligible: List[Tuple[str, Optional[str], List[str]]] = []
            for devid, (when, host, group) in bad_devices:
                ratio = float(num_in - did - len(group)) / float(num_osds)
                if ratio < min_in_ratio:
                    final_ratio = float(num_in - num_bad) / float(num_osds)
                    checks[DEVICE_HEALTH_TOOMANY] = {
                        'severity': 'warning',
                        'summary': HEALTH_MESSAGES[DEVICE_HEALTH_TOOMANY],
                        'detail': [
                            '%d OSDs with failing device(s) would bring "in" ratio to %f < mon_osd_min_in_ratio %f' % (
                                num_bad - did, final_ratio, min_in_ratio)
                        ]
                    }
                    # skip it; smaller devices behind it may still fit
                    continue
                eligible.append((devid, host, group))
                did += len(group)
            if eligible:
                history = self._mark_out_history(osdmap)
                to_mark_out = self._limit_mark_out(eligible, history, now)
                if to_mark_out:
                    self.mark_out_etc(to_mark_out)
                    self._record_mark_out(eligible, to_mark_out, history, now)
        for warning, ls in health_warnings.items():
            n = len(ls)
            if n:
                checks[warning] = {
                    'severity': 'warning',
                    'summary': HEALTH_MESSAGES[warning] % n,
                    'count': len(ls),
                    'detail': ls,
                }
        self.set_health_checks(checks)
        return 0, "", ""

    def _mark_out_history(self,
                          osdmap: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        """OSDs that self-heal has marked out and that are still out.

        Kept in the module store so a mgr failover does not reset the limits.
        """
        raw = self.get_store('mark_out_history')
        try:
            history = json.loads(raw) if raw else {}
        except ValueError:
            self.log.warning('ignoring unparseable mark_out_history')
            return {}
        known = {str(osd['osd']): osd for osd in osdmap['osds']}
        return {osd_id: rec for osd_id, rec in history.items()
                if osd_id in known and not known[osd_id]['in']}

    def _record_mark_out(self,
                         eligible: List[Tuple[str, Optional[str], List[str]]],
                         marked_out: List[str],
                         history: Dict[str, Dict[str, Any]],
                         now: datetime) -> None:
        marked = set(marked_out)
        for devid, host, group in eligible:
            for osd_id in group:
                if osd_id in marked:
                    history[osd_id] = {'at': now.timestamp(),
                                       'host': host,
                                       'devid': devid}
        self.set_store('mark_out_history', json.dumps(history))

    def _limit_mark_out(self,
                        eligible: List[Tuple[str, Optional[str], List[str]]],
                        history: Dict[str, Dict[str, Any]],
                        now: datetime) -> List[str]:
        """Return the OSDs self-heal may mark out now, within the rate limits.

        ``eligible`` is (devid, host, osds), sorted next-to-fail first.
        """
        max_concurrent = int(self.mark_out_max_concurrent)
        if max_concurrent <= 0:
            self.log.warning('self_heal would evacuate %d device(s) but '
                             'mark_out_max_concurrent is 0: %s',
                             len(eligible), [d for d, _, _ in eligible])
            return []

        interval = float(self.mark_out_min_interval)
        if interval and history:
            waited = now.timestamp() - max(rec.get('at', 0.0)
                                           for rec in history.values())
            if waited < interval:
                self.log.info('deferring evacuation of %d device(s): last '
                              'mark out was %ds ago, mark_out_min_interval '
                              'is %ds', len(eligible), int(waited),
                              int(interval))
                return []

        # an unknown PG count (-1) counts as still draining
        draining: Dict[str, Optional[str]] = {}
        for osd_id, rec in history.items():
            if self.get_osd_num_pgs(osd_id) != 0:
                draining[rec.get('devid') or osd_id] = rec.get('host')
        slots = max_concurrent - len(draining)
        if slots <= 0:
            self.log.info('deferring evacuation of %d device(s): %d already '
                          'draining, mark_out_max_concurrent is %d',
                          len(eligible), len(draining), max_concurrent)
            return []

        # one device per host at a time, to spread backfill load and protect
        # CRUSH rules with an OSD failure domain
        busy = set(draining.values())
        busy.discard(None)
        allowed: List[str] = []
        taken = 0
        for devid, host, group in eligible:
            if taken >= slots:
                break
            if host is not None and host in busy:
                continue
            allowed.extend(group)
            busy.add(host)
            taken += 1

        deferred = len(eligible) - taken
        if deferred:
            self.log.info('evacuating %d device(s) (%d OSDs), deferring %d '
                          'device(s) to a later pass',
                          taken, len(allowed), deferred)
        return allowed

    def _awaiting_replacement(self,
                              osdmap: Dict[str, Any],
                              dev: Dict[str, Any],
                              osds: List[str],
                              osd_ids: List[str]) -> bool:
        """True if every daemon on the device is an OSD that is out and drained."""
        if not osd_ids or len(osds) != len(dev['daemons']):
            return False
        if any(self.is_osd_in(osdmap, _id) for _id in osd_ids):
            return False
        # get_osd_num_pgs() is -1 for an OSD missing from osd_stats
        return all(self.get_osd_num_pgs(_id) == 0 for _id in osd_ids)

    def is_osd_in(self, osdmap: Dict[str, Any], osd_id: str) -> bool:
        for osd in osdmap['osds']:
            if osd_id == str(osd['osd']):
                return bool(osd['in'])
        return False

    def get_osd_num_pgs(self, osd_id: str) -> int:
        stats = self.get('osd_stats')
        assert stats is not None
        for stat in stats['osd_stats']:
            if osd_id == str(stat['osd']):
                return stat['num_pgs']
        return -1

    def mark_out_etc(self, osd_ids: List[str]) -> None:
        self.log.info('Marking out OSDs: %s' % osd_ids)
        result = CommandResult('')
        self.send_command(result, 'mon', '', json.dumps({
            'prefix': 'osd out',
            'format': 'json',
            'ids': osd_ids,
        }), '')
        r, outb, outs = result.wait()
        if r != 0:
            self.log.warning('Could not mark OSD %s out. r: [%s], outb: [%s], outs: [%s]',
                             osd_ids, r, outb, outs)
        for osd_id in osd_ids:
            result = CommandResult('')
            self.send_command(result, 'mon', '', json.dumps({
                'prefix': 'osd primary-affinity',
                'format': 'json',
                'id': int(osd_id),
                'weight': 0.0,
            }), '')
            r, outb, outs = result.wait()
            if r != 0:
                self.log.warning('Could not set osd.%s primary-affinity, '
                                 'r: [%s], outb: [%s], outs: [%s]',
                                 osd_id, r, outb, outs)

    def extract_smart_features(self, raw: Any) -> Any:
        # FIXME: extract and normalize raw smartctl --json output and
        # generate a dict of the fields we care about.
        return raw

    def prediction_mode(self) -> str:
        return cast(str, self.get_ceph_option('device_failure_prediction_mode')).lower()

    def _remote_predict(self, method: str, **kwargs: Any) -> Tuple[int, str, str]:
        plugin_name = 'diskprediction_local'
        try:
            can_run, reason = self.remote(plugin_name, 'can_run')
            if not can_run:
                return -errno.EAGAIN, '', f'{plugin_name} is not available: {reason}'
            return cast(Tuple[int, str, str],
                        self.remote(plugin_name, method, **kwargs))
        except Exception as e:
            return -errno.EIO, '', f'unable to invoke {plugin_name}: {e}'

    def _ruleset(self) -> predictor.Ruleset:
        """The imported ruleset, or the built-in one if none is stored or it
        fails to load."""
        raw = self.get_store('ruleset')
        if not raw:
            return predictor.BUILTIN_RULESET
        if self._ruleset_cache is not None and self._ruleset_raw == raw:
            return self._ruleset_cache
        try:
            ruleset = predictor.load_ruleset(json.loads(raw))
        except (ValueError, predictor.RulesetError) as e:
            self.log.error('stored ruleset is unusable, falling back to the '
                           'built-in rules: %s', e)
            ruleset = predictor.BUILTIN_RULESET
        self._ruleset_raw = raw
        self._ruleset_cache = ruleset
        return ruleset

    def _predict_device(self, devid: str) -> predictor.Prediction:
        """
        Run the rule-based predictor over the samples in the prediction window.
        """
        window = self.prediction_window or (86400 * 30)
        since = datetime.now(timezone.utc) - timedelta(seconds=window)
        metrics = self.get_recent_device_metrics(devid, since.strftime(TIME_FORMAT))
        # TIME_FORMAT keys sort chronologically; the predictor wants newest first
        samples = [metrics[t] for t in sorted(metrics.keys(), reverse=True)]
        prediction = predictor.predict(samples, self._ruleset())
        self.log.debug('device %s: %s (%s sample(s)): %s', devid,
                       prediction.status, len(samples),
                       '; '.join(prediction.reasons) or 'no findings')
        return prediction

    def predict_life_expectancy(self, devid: str) -> Tuple[int, str, str]:
        mode = self.prediction_mode()
        if mode == 'local':
            return self._remote_predict('predict_life_expectancy', devid=devid)
        elif mode != 'smart':
            return -errno.EINVAL, '', \
                'device_failure_prediction_mode is not set to local or smart'

        status = self._predict_device(devid).status
        if status == predictor.GOOD:
            return 0, '>6w', ''
        elif status == predictor.WARNING:
            return 0, '>=2w and <=6w', ''
        elif status == predictor.BAD:
            return 0, '<2w', ''
        else:
            return 0, 'unknown', ''

    def explain_health(self, devid: str) -> Tuple[int, str, str]:
        # works in any mode, so the rules can be evaluated before enabling them
        prediction = self._predict_device(devid)
        lines = [f'{devid}: {prediction.status}']
        if prediction.reasons:
            lines += [f'  - {reason}' for reason in prediction.reasons]
        else:
            lines.append('  - no rule matched')
        provenance = f'ruleset: {prediction.ruleset}'
        if prediction.profiles:
            provenance += ' (profile: %s)' % ', '.join(prediction.profiles)
        lines.append(provenance)
        if self.prediction_mode() != 'smart':
            lines.append('(device_failure_prediction_mode is not "smart", so '
                         'this verdict is not being acted on)')
        return 0, '\n'.join(lines), ''

    def _reset_device_life_expectancy(self, devid: str) -> int:
        result = CommandResult('')
        self.send_command(result, 'mon', '', json.dumps({
            'prefix': 'device rm-life-expectancy',
            'devid': devid,
        }), '')
        r, _, outs = result.wait()
        if r != 0:
            self.log.error('failed to reset %s life expectancy: %s', devid, outs)
        return r

    def _set_device_life_expectancy(self, devid: str, from_date: str,
                                    to_date: Optional[str] = None) -> int:
        cmd: Dict[str, Any] = {
            'prefix': 'device set-life-expectancy',
            'devid': devid,
            'from': from_date,
        }
        if to_date is not None:
            cmd['to'] = to_date
        result = CommandResult('')
        self.send_command(result, 'mon', '', json.dumps(cmd), '')
        r, _, outs = result.wait()
        if r != 0:
            self.log.error('failed to set %s life expectancy: %s', devid, outs)
        return r

    @staticmethod
    def _is_still_good(dev: Dict[str, Any]) -> bool:
        """
        Whether the device already has an unexpired Good record (open upper
        bound, lower bound in the future).  Skipping the rewrite saves a mon
        config-key write per healthy device per pass.
        """
        # the mgr dumps an unset bound as '0.000000'
        if dev.get('life_expectancy_max') not in (None, '', '0.000000'):
            return False
        recorded = dev.get('life_expectancy_min')
        if not recorded:
            return False
        try:
            expires = datetime.strptime(recorded, '%Y-%m-%dT%H:%M:%S.%f%z')
        except ValueError:
            return False
        return expires > datetime.now(timezone.utc)

    def _apply_prediction(self, dev: Dict[str, Any], status: str) -> None:
        """Translate a verdict into a life expectancy on the device."""
        devid = dev['devid']
        if status not in LIFE_EXPECTANCY:
            # Unknown: retract any earlier expectancy
            if dev.get('life_expectancy_min') or dev.get('life_expectancy_max'):
                self._reset_device_life_expectancy(devid)
            return
        if status == predictor.GOOD and self._is_still_good(dev):
            return
        # utime_t::parse() reads these as UTC; keep the time of day
        now = datetime.now(timezone.utc)
        low, high = LIFE_EXPECTANCY[status]
        from_date = (now + timedelta(seconds=low)).strftime(LIFE_EXPECTANCY_FORMAT)
        to_date = None
        if high is not None:
            to_date = (now + timedelta(seconds=high)).strftime(LIFE_EXPECTANCY_FORMAT)
        if self._set_device_life_expectancy(devid, from_date, to_date) == 0:
            self.log.info('set %s life expectancy from %s to %s (%s)',
                          devid, from_date, to_date or 'unset', status)

    def predict_all_devices(self) -> Tuple[int, str, str]:
        mode = self.prediction_mode()
        if mode == 'local':
            return self._remote_predict('predict_all_devices')
        elif mode != 'smart':
            self.log.debug('device failure prediction is disabled')
            return 0, '', ''

        self.log.debug('predict_all_devices')
        for dev in self.get('devices').get('devices', []):
            devid = dev.get('devid')
            if not devid or not dev.get('daemons'):
                continue
            self._apply_prediction(dev, self._predict_device(devid).status)
        return 0, '', ''

    def get_recent_device_metrics(self, devid: str, min_sample: str) -> Dict[str, Dict[str, Any]]:
        try:
            return self._get_device_metrics(devid, min_sample=min_sample)
        except MgrDBNotReady:
            return dict()

    def get_time_format(self) -> str:
        return TIME_FORMAT
