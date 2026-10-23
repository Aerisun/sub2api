#!/bin/sh
# Keep the packaged Redis runtime relocatable with the project directory.
sub2api_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
sub2api_command=$(basename -- "$0")
case "$sub2api_command" in
    redis-cli|redis-server) ;;
    *) exit 64 ;;
esac
exec "$sub2api_root/runtime-redis/lib/ld-musl-x86_64.so.1" \
    --library-path "$sub2api_root/runtime-redis/lib:$sub2api_root/runtime-redis/usr/lib" \
    "$sub2api_root/runtime-redis/usr/local/bin/$sub2api_command" "$@"
