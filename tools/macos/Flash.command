#!/bin/bash
set -e

tool_dir="$(cd -- "$(dirname -- "$0")" && pwd -P)"
export PATH="$PATH:/opt/homebrew/bin:/usr/local/bin"
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
fi

if ! command -v python3 >/dev/null; then
    printf '%s\n' '请先安装 Python 3.11 或更高版本。' >&2
    exit 1
fi

exec python3 -I "$tool_dir/flash_macos.py" "$@"
