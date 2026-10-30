#!/bin/sh
# Independent deadline for an unattended first-install boot, not a speech timer.
set -eu
umask 077
state=${QUASAR_DATA_DIR:-/data/opt/quasar-bridge}
target=$(readlink -f "${QUASAR_DEVICE_CONFIG:-/system/vendor/quasar/quasar.cfg}")
mountinfo=${QUASAR_MOUNTINFO:-/proc/self/mountinfo}
deadline=${QUASAR_RECOVERY_SECONDS:-600}
case "$deadline" in *[!0-9]*|'') exit 2;; esac
test "$deadline" -ge 1 && test "$deadline" -le 3600 || exit 2
test -f "$state/acceptance.pending" || exit 0
sleep "$deadline"
test -f "$state/acceptance.pending" || exit 0
# Atomic pending-marker transition races safely with explicit confirmation.
# No directory lock is left behind if confirmation loses SSH or receives HUP.
if ! mv "$state/acceptance.pending" "$state/acceptance.failed"; then
    test -f "$state/acceptance.confirmed" && test ! -f "$state/acceptance.pending" && exit 0
    exit 1
fi
# Disable first so even a failed restore cannot turn into a reboot loop.
touch "$state/disabled"
if sh "$state/S98quasarbridge" stop && cmp -s "$target" "$state/original.cfg"; then
    logger -t quasar-bridge 'first install not accepted; restored own stock route and rebooting'
    sync
    reboot
else
    logger -t quasar-bridge 'timed recovery refused foreign state; local operator required'
    exit 1
fi
