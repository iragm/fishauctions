#!/bin/bash

set -e
# this is a hack to overwrite Django's broken TZ stuff that causes errors (500 page) to fail.  See https://code.djangoproject.com/ticket/33674

FILE="/usr/local/lib/python3.11/site-packages/django/templatetags/tz.py"
SEARCH="        with timezone.override(self.tz.resolve(context)):"
REPLACE="\        try:\n            with timezone.override(self.tz.resolve(context)):\n                output = self.nodelist.render(context)\n            return output\n        except:\n            return self.nodelist.render(context)"

if grep -qF "$SEARCH" "$FILE"; then
    sed -i "/$SEARCH/,+2 c $REPLACE" "$FILE"
else
    echo "Unable to find the text $SEARCH in $FILE, update or remove fix_tz_hack.sh"
    exit 1
fi

# Reportlab causes certain label settings to throw an error
# https://github.com/virantha/pypdfocr/issues/80
FILE="/usr/local/lib/python3.11/site-packages/reportlab/platypus/paragraph.py"
SEARCH="        if availWidth<_FUZZ:"
REPLACE="\        # removed\n"

if grep -qF "$SEARCH" "$FILE"; then
    sed -i "/$SEARCH/,+2 c $REPLACE" "$FILE"
else
    echo "Unable to find the text $SEARCH in $FILE, update or remove fix_tz_hack.sh"
    exit 1
fi

