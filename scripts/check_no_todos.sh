#!/usr/bin/env bash
# Fail on TODO/FIXME in src/ and tests/ (this script lives in scripts/, so it is excluded).
if grep -rnE 'TODO|FIXME' src tests; then
  echo "TODO/FIXME found" >&2
  exit 1
fi
