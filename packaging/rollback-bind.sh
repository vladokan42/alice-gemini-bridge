#!/bin/sh
# Only remove the mount recorded by this pilot, never a foreign/stacked bind.
# "armed" is intentionally kept until voice recovery is independently proven.
set -eu
stage=${1:?stage required}
target=$(readlink -f "${2:?target required}")
mountinfo=${3:-/proc/self/mountinfo}
test -f "$stage/original.cfg"
test -f "$stage/candidate.cfg"
test -f "$target"
inspect_mounts() {
    count=0
    observed=
    while IFS= read -r row; do
        set -f
        set -- $row
        test "$#" -ge 6 || continue
        if test "$5" = "$target"; then
            count=$((count + 1))
            observed=$row
        fi
    done < "$mountinfo"
}
refuse() {
    printf '%s\n' 'Refusing rollback: unexpected mount or configuration' >&2
    exit 1
}
inspect_mounts
if test "$count" = 0; then
    cmp -s "$target" "$stage/original.cfg" || refuse
    touch "$stage/restored"
    exit 0
fi
test "$count" = 1 || refuse
test -f "$stage/mount.record" || refuse
IFS= read -r expected < "$stage/mount.record"
test "$observed" = "$expected" || refuse
cmp -s "$target" "$stage/candidate.cfg" || refuse
umount "$target"
inspect_mounts
test "$count" = 0 || refuse
cmp -s "$target" "$stage/original.cfg" || refuse
touch "$stage/restored"
printf '%s\n' 'Original configuration restored exactly; recovery still armed'
