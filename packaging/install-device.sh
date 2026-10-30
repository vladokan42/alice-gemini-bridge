#!/bin/sh
# Fresh-device transaction only; never overwrite firmware or an existing hook.
set -eu
umask 077
state=${QUASAR_DATA_DIR:-/data/opt/quasar-bridge}
target=$(readlink -f "${QUASAR_DEVICE_CONFIG:-/system/vendor/quasar/quasar.cfg}")
hook=${QUASAR_INIT_HOOK:-/etc/init.d/S98quasarbridge}
stock=${QUASAR_STOCK_INIT:-/etc/init.d/S99quasar}
mountinfo=${QUASAR_MOUNTINFO:-/proc/self/mountinfo}
mounts=${QUASAR_MOUNTS:-/proc/mounts}
bootfile=${QUASAR_BOOT_ID_FILE:-/proc/sys/kernel/random/boot_id}
export QUASAR_DATA_DIR="$state" QUASAR_DEVICE_CONFIG="$target" QUASAR_MOUNTINFO="$mountinfo"
refuse() { printf '%s\n' 'Device precondition failed; no unsafe fallback. Inspect locally.' >&2; exit 1; }
prerequisites() {
    test "$(id -u)" = 0 || refuse
    test "$(uname -m)" = aarch64 || refuse
    test -f "$target" || refuse
    test -x "$stock" || refuse
    test -r "$bootfile" || refuse
    for command in readlink cmp mount umount wget start-stop-daemon logger tar sleep sync reboot sha256sum mktemp ln cat chmod mv rm touch; do
        command -v "$command" >/dev/null || refuse
    done
}
no_file_mount() {
    while IFS= read -r row; do
        set -f; set -- $row
        if test "$#" -ge 6 && test "$5" = "$target"; then refuse; fi
    done < "$mountinfo"
}
original_root_ro=0
root_flags() {
    found=0
    while read -r device point fs options rest; do
        if test "$point" = /; then
            found=1
            case ",$options," in *,ro,*) original_root_ro=1;; esac
        fi
    done < "$mounts"
    test "$found" = 1 || refuse
}
restore_root() {
    if test "$original_root_ro" = 1; then mount -o remount,ro / || { printf '%s\n' 'Rootfs read-only restore failed; operator required' >&2; exit 1; }; fi
}
case "${1:-}" in
    preflight)
        prerequisites
        test ! -e "$state" && test ! -L "$state" || refuse
        test ! -e "$hook" && test ! -L "$hook" || refuse
        no_file_mount
        printf '%s\n' 'Compatible fresh ARM64 firmware preflight passed; no changes.'
        ;;
    install)
        prerequisites
        test ! -e "$hook" && test ! -L "$hook" || refuse
        test ! -e "$state/acceptance.pending" || refuse
        no_file_mount
        for name in original.cfg candidate.cfg runtime.env config.json S98quasarbridge recovery.sh start.sh supervise.sh rollback-bind.sh; do
            test -f "$state/$name" && test ! -L "$state/$name" || refuse
        done
        cmp -s "$target" "$state/original.cfg" || refuse
        test -x "$state/bin/quasar-bridge" || refuse
        sh "$state/start.sh" -check-config >/dev/null 2>&1 || refuse
        sh "$state/start.sh" -check-backends >/dev/null 2>&1 || refuse
        root_flags
        trap restore_root EXIT
        trap 'exit 1' HUP INT TERM
        if test "$original_root_ro" = 1; then mount -o remount,rw /; fi
        # Pending state exists before the boot hook can become visible.
        cat "$bootfile" > "$state/install.boot-id"
        : > "$state/acceptance.pending"
        chmod 0600 "$state/install.boot-id" "$state/acceptance.pending"
        temporary_hook=$(mktemp "$hook.alice-XXXXXX")
        trap 'rm -f "$temporary_hook"; restore_root' EXIT
        cat "$state/S98quasarbridge" > "$temporary_hook"
        chmod 0755 "$temporary_hook"
        # Hard-link creation is atomic and refuses a raced-in foreign hook.
        ln "$temporary_hook" "$hook"
        cmp -s "$hook" "$state/S98quasarbridge" || refuse
        printf '%s\n' 'Installed own boot hook. Stock unchanged; reboot starts pending bridge with timed recovery.'
        ;;
    confirm)
        prerequisites
        test -f "$state/acceptance.pending" || refuse
        test ! -f "$state/disabled" || refuse
        test -f "$state/install.boot-id" || refuse
        if cmp -s "$bootfile" "$state/install.boot-id"; then refuse; fi
        cmp -s "$hook" "$state/S98quasarbridge" || refuse
        cmp -s "$target" "$state/candidate.cfg" || refuse
        wget -T 2 -q -O /dev/null http://127.0.0.1:18861/healthz || refuse
        # A foreign/stacked mount is rejected by the same exact-record guard.
        observed=
        count=0
        while IFS= read -r row; do
            set -f; set -- $row
            if test "$#" -ge 6 && test "$5" = "$target"; then count=$((count + 1)); observed=$row; fi
        done < "$mountinfo"
        test "$count" = 1 || refuse
        IFS= read -r expected < "$state/mount.record"
        test "$observed" = "$expected" || refuse
        test -f "$state/acceptance.pending" && test ! -f "$state/disabled" || refuse
        # Rename the one pending marker atomically. Whichever actor moves it
        # first owns the outcome; no persistent lock can survive lost SSH/boot.
        mv "$state/acceptance.pending" "$state/acceptance.confirmed"
        printf '%s\n' 'Operator accepted voice cycle; automatic first-install recovery disarmed.'
        ;;
    rollback)
        prerequisites
        cmp -s "$hook" "$state/S98quasarbridge" || refuse
        sh "$state/S98quasarbridge" stop
        cmp -s "$target" "$state/original.cfg" || refuse
        if test -f "$state/acceptance.pending"; then mv "$state/acceptance.pending" "$state/acceptance.failed"; fi
        printf '%s\n' 'Own bridge disabled; original configuration restored. Reboot to clear cached endpoint.'
        ;;
    *) printf '%s\n' 'usage: install-device.sh preflight|install|confirm|rollback' >&2; exit 2;;
esac
