#!/bin/sh
set -eu
if [ "$#" -eq 0 ]; then
  set -- sleep infinity
fi
exec "$@"
