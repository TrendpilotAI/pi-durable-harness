#!/usr/bin/env bash
set -u
python3 -m unittest -q 2>&1 | tail -5
python3 -c "from roman import to_roman as t, from_roman as f; assert all(f(t(n))==n for n in range(1,4000)); assert t(1994)=='MCMXCIV'; print('hidden checks: PASS')"
