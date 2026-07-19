#!/bin/sh
# SPDX-License-Identifier: BSL-1.0
# README-contract smoke test: every documented CLI tool must print its
# usage text (not a traceback) when invoked without arguments, and the
# shared Python modules must import cleanly.  Guards against the
# README and the actual CLIs drifting apart.

cd "$(dirname "$0")/.." || exit 1
fail=0

for t in gmbank_build.py gmrender.py bank_qa.py bank_calibrate.py \
         bank_manifest.py corpus_scan.py; do
    if python3 "$t" 2>&1 | grep -qi usage; then
        echo "smoke ok:   $t"
    else
        echo "smoke FAIL: $t prints no usage"
        fail=1
    fi
done

if python3 -c 'import gmbank_format, sf2parse, smf, gmrender, \
    bank_qa, bank_manifest, bank_calibrate, corpus_scan' 2>&1; then
    echo "smoke ok:   module imports"
else
    echo "smoke FAIL: module imports"
    fail=1
fi

exit $fail
