#!/bin/bash
# Replace vendor/fakebox.py with a newer commit of openhamshack-remote, the
# only side of vendor/ that still moves.
#
#     tools/refresh-vendor.sh                     # openhamshack-remote's current HEAD
#     tools/refresh-vendor.sh <openhamshack-remote-rev>
#
# Classic (vendor/openhamshack/appliance) has no live repo any more - it was
# archived, not renamed, on 21/10/2026. Left alone, this script keeps
# whatever is already vendored and copies its PINNED line forward unchanged,
# which is correct: there is nothing to refresh it against. If that frozen
# commit is ever found to be wrong, extract Classic's git history from the
# pre-rename backup kept on shack (~/archive/pre-rename-20261021/ - see
# GUIDE.md for what is in there) into a working checkout and pass it as
# CLASSIC_REPO, with the commit to take as the second argument:
#
#     CLASSIC_REPO=/path/to/classic/checkout tools/refresh-vendor.sh <remote-rev> <classic-rev>
#
# Why vendored rather than mounted: the image is built and run by the dock360
# service account, which cannot read /home/user (mode 0700), and a
# container that reached into a working tree would also change behaviour every
# time somebody edited that tree. Taking files out of git by commit - never
# from the working tree - means vendor/PINNED says exactly what is running, and
# an uncommitted edit in a checkout cannot leak into the image unrecorded.
#
# After running it: rebuild (dock360ctl dev sync + update), run the tests, and
# commit vendor/ in one commit whose message names the commit(s) that changed.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
remote="${OPENHAMSHACK_REMOTE_REPO:-$HOME/openhamshack-remote}"
rm_rev="$(git -C "$remote" rev-parse "${1:-HEAD}")"

classic_repo="${CLASSIC_REPO:-}"
if [ -n "$classic_repo" ]; then
    classic_rev="$(git -C "$classic_repo" rev-parse "${2:-HEAD}")"
    rm -rf "$here/vendor/openhamshack"
    mkdir -p "$here/vendor/openhamshack"
    # The whole appliance directory except its tests and what only builds a
    # disk image: openhamshack.py reads www/, VERSION and release-keys/
    # relative to itself, and which of openhamshacklib's modules it imports
    # changes from release to release.
    git -C "$classic_repo" archive "$classic_rev" appliance \
        | tar -x -C "$here/vendor/openhamshack" \
            --exclude='appliance/test_*.py' --exclude='appliance/testdata' \
            --exclude='appliance/installer' --exclude='appliance/mkimage.sh' --exclude='appliance/mkiso.sh'
    classic_subject="$(git -C "$classic_repo" log -1 --format=%s "$classic_rev")"
else
    # Nothing to re-vendor Classic from: keep today's copy and PINNED line.
    classic_rev="$(awk '$1 == "classic" {print $2}' "$here/vendor/PINNED")"
    classic_subject="$(sed -n 's/^classic *[0-9a-f]* *(\(.*\))$/\1/p' "$here/vendor/PINNED")"
fi

rm -f "$here/vendor/fakebox.py"
git -C "$remote" show "$rm_rev:tools/fakebox.py" > "$here/vendor/fakebox.py"

cat > "$here/vendor/PINNED" <<EOF
# Written by tools/refresh-vendor.sh - do not edit by hand.
# Classic has no live repo any more (archived 21/10/2026): the line below
# names a frozen copy of the archived appliance at its last commit, not
# something this tool can fetch a newer one of. See the tool's own comment
# if that commit ever needs to change.
classic               $classic_rev  ($classic_subject)
openhamshack-remote   $rm_rev  ($(git -C "$remote" log -1 --format=%s "$rm_rev"))
EOF
cat "$here/vendor/PINNED"
