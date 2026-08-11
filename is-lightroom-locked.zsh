#!/bin/sh
#
# Fails if the Lightroom catalog is locked.
# Succeeds otherwise.
#

if find "$HOME/Pictures/Lightroom" -name "*.lrcat.lock" -print -quit | grep -q .; then
    echo "Lightroom catalog is locked."
    exit 1
fi

exit 0
