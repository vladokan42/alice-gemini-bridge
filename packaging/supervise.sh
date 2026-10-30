#!/bin/sh
# Restart isolated helper; repeated fast failure disables it before a stock reboot.
set -u
umask 077
state=${QUASAR_DATA_DIR:-/data/opt/quasar-bridge}
target=${QUASAR_DEVICE_CONFIG:-/system/vendor/quasar/quasar.cfg}
mountinfo=${QUASAR_MOUNTINFO:-/proc/self/mountinfo}
export QUASAR_DATA_DIR="$state"
failures=0
while test ! -f "$state/disabled"; do
    IFS=' ' read -r began rest < /proc/uptime
    began=${began%%.*}
    sh "$state/start.sh" 2>&1 | logger -t quasar-bridge
    if test -f "$state/disabled"; then exit 0; fi
    IFS=' ' read -r ended rest < /proc/uptime
    ended=${ended%%.*}
    if test "$((ended - began))" -ge 60; then
        failures=0
    else
        failures=$((failures + 1))
    fi
    if test "$failures" -ge 3; then
        touch "$state/disabled"
        if sh "$state/rollback-bind.sh" "$state" "$target" "$mountinfo"; then
            logger -t quasar-bridge 'disabled after repeated failure; rebooting to stock route'
            sync
            reboot
        else
            logger -t quasar-bridge 'recovery refused foreign state; operator needed'
        fi
        exit 1
    fi
    sleep 3
done
