#!/bin/sh
case "$1" in
  *Username*) printf '%s\n' git ;;
  *Password*) cat "$LOOM_OVERLEAF_TOKEN_FILE" ;;
  *) exit 1 ;;
esac
