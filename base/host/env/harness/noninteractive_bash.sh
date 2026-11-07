#!/bin/sh
for b in /opt/homebrew/bin/bash /usr/local/bin/bash /bin/bash; do
    if [ -x "$b" ]; then
        BASH_BIN="$b"
        break
    fi
done
: "${BASH_BIN:=/bin/sh}"
case "${1:-}" in
    -c|-lc|-ic)
        exec "$BASH_BIN" "$@" < /dev/null
        ;;
    *)
        exec "$BASH_BIN" "$@"
        ;;
esac
