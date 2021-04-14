#!/usr/bin/env bash

source $CEPH_ROOT/qa/standalone/ceph-helpers.sh

mon_port=$(get_unused_port)

function run() {
    local dir=$1
    shift

    export CEPH_MON="127.0.0.1:$mon_port"
    export CEPH_ARGS
    CEPH_ARGS+="--fsid=$(uuidgen) --auth-supported=none "
    CEPH_ARGS+="--mon-host=$CEPH_MON "
    CEPH_ARGS+="--osd_min_pg_log_entries=1 --osd_max_pg_log_entries=2 "
    set -e

    local funcs=${@:-$(set | sed -n -e 's/^\(TEST_[0-9a-z_]*\) .*/\1/p')}
    for func in $funcs ; do
        setup $dir || return 1
	$func $dir || return 1
        teardown $dir || return 1
    done
}

function TEST_flap() {
    local dir=$1

    run_mon $dir a
    run_mgr $dir x
    run_osd $dir 0
    run_osd $dir 1
    run_osd $dir 2
    run_osd $dir 3

    ceph osd pool create foo 64
    wait_for_clean

    # write lots of objects
    rados -p foo bench 30 write -b 4096 --no-cleanup
    wait_for_clean

    # set norebalance so that we don't backfill
    ceph osd set norebalance
    wait_for_clean

    # flap an osd, it should repeer and come back clean
    ceph osd down 0
    wait_for_clean

    # drain osd.0, then wait for peering
    ceph osd crush reweight osd.0 0
    wait_for_peered

    # flap osd.0 while draining, this has been known to incorrectly degrade pgs
    ceph osd down 0
    wait_for_osd up 0
    wait_for_peered

    # now there should be zero undersized or degraded pgs
    ceph pg debug degraded_pgs_exist | grep -q FALSE
}

main flap "$@"
