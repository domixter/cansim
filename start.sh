#!/bin/sh
# cansim: uruchomienie (Linux/macOS)
cd "$(dirname "$0")"
exec python3 -m cansim "$@"
