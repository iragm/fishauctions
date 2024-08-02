#!/bin/bash

set -e

update_file() {
    local file="$1"
    local search="$2"
    local replace="$3"
    local additional="$4"

    if grep -qF "$search" "$file"; then
        sed -ri "/$search/,+$additional c $replace" "$file"
    else
        echo "Unable to find the text $search in $file, update python_file_hack.sh"
        exit 1
    fi
}

# this is a hack to overwrite Django's broken TZ stuff that causes errors (500 page) to fail.  See https://code.djangoproject.com/ticket/33674
FILE="/usr/local/lib/python3.11/site-packages/django/templatetags/tz.py"
SEARCH="        with timezone.override(self.tz.resolve(context)):"
REPLACE="\        try:\n            with timezone.override(self.tz.resolve(context)):\n                output = self.nodelist.render(context)\n            return output\n        except:\n            return self.nodelist.render(context)"
update_file "$FILE" "$SEARCH" "$REPLACE" "2"


# Reportlab causes certain label settings to throw an error
# https://github.com/virantha/pypdfocr/issues/80
FILE="/usr/local/lib/python3.11/site-packages/reportlab/platypus/paragraph.py"
SEARCH="        if availWidth<_FUZZ:"
REPLACE="\        # removed\n"
update_file "$FILE" "$SEARCH" "$REPLACE" "2"

