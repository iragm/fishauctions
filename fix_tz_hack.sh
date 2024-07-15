#!/bin/bash

set -e

FILE="/usr/local/lib/python3.11/site-packages/django/templatetags/tz.py"
SEARCH="        with timezone.override(self.tz.resolve(context)):"
REPLACE="\        try:\n            with timezone.override(self.tz.resolve(context)):\n                output = self.nodelist.render(context)\n            return output\n        except:\n            return self.nodelist.render(context)"

if grep -qF "$SEARCH" "$FILE"; then
    sed -i "/$SEARCH/,+2 c $REPLACE" "$FILE"
else
    echo "Unable to find the text $SEARCH in $FILE, update or remove fix_tz_hack.sh"
    exit 1
fi