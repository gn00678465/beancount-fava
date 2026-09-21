#!/bin/sh
# fava starts even when a ledger file is missing and then serves a blank page:
# it reports `filename: null`, which its own frontend rejects. Fail here instead.
set -eu

if [ "${1:-}" = "fava" ] && [ -n "${BEANCOUNT_FILE:-}" ]; then
  # fava splits BEANCOUNT_FILE on os.pathsep, which is ":" in this image.
  old_ifs=$IFS
  IFS=:
  for ledger in $BEANCOUNT_FILE; do
    if [ ! -r "$ledger" ]; then
      echo "beancount-fava: BEANCOUNT_FILE names '$ledger', which does not exist or is not readable inside the container. Mount the ledger under /ledger and give its absolute path." >&2
      exit 1
    fi
  done
  IFS=$old_ifs
fi

exec "$@"
