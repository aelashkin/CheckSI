#!/usr/bin/env bash
set -euo pipefail

# run-repomix.sh
# OUTPUT: AI-friendly single file pack of the project codebase
# DESCRIPTION: Runs repomix with predefined packing modes. Extra arguments are
# forwarded to repomix unchanged.

# Directory containing this script
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
# Repo root = parent of scripts/
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

SCRIPT_NAME="$(basename "$0")"
MODE="full"
FORWARD_ARGS=()

# Patterns to ignore in every mode (comma-separated when passed to repomix).
COMMON_IGNORE_PATTERNS=(
    '**/*.png'
    '**/*.svg'
    "**/${SCRIPT_NAME}"
    'LICENSE'
    '__pycache__/**'
    '.venv/**'
    '.env'
    'poetry.lock'
    'data/**'
    'tests/**'
    'docs/**'
    'assets/**'
    'scripts/**'
    'Check_nmr_*.py'
    'mac_4.py'
    'wind22.py'
)

MODE2_IGNORE_PATTERNS=(
    'src/checksi/gui/**'
    'src/checksi/gui_main.py'
)

# Repomix supports combining comma-separated --include and --ignore glob lists.
# Mode 3 keeps the pack focused on the GUI and the shared modules it relies on,
# while intentionally leaving out the dedicated analysis implementation package.
MODE3_INCLUDE_PATTERNS=(
    'AGENTS.md'
    'README.md'
    'pyproject.toml'
    'settings.toml'
    'src/checksi/__init__.py'
    'src/checksi/application_runner.py'
    'src/checksi/config.py'
    'src/checksi/config_schema.py'
    'src/checksi/controller.py'
    'src/checksi/document_parser.py'
    'src/checksi/errors/**'
    'src/checksi/events.py'
    'src/checksi/file_io.py'
    'src/checksi/gui/**'
    'src/checksi/gui_main.py'
    'src/checksi/report_service.py'
    'src/checksi/reporting.py'
    'src/checksi/utils/**'
)

MODE3_IGNORE_PATTERNS=(
    'src/checksi/analysis/**'
    'src/checksi/main.py'
)

usage() {
    cat <<EOF
Usage: ${SCRIPT_NAME} [--mode full|no-gui|gui-only] [--] [repomix args...]

Modes:
  full      Pack everything. Default mode if --mode is not specified.
  no-gui    Pack everything except the GUI.
  gui-only  Pack only the GUI and shared files relevant to the GUI,
            excluding analysis implementation modules under
            src/checksi/analysis/.

Notes:
  - Repomix uses comma-separated glob lists for both --include and --ignore.
  - Additional arguments are forwarded to repomix unchanged.
  - Use -- to stop this wrapper's argument parsing if needed.
EOF
}

join_by_comma() {
    local IFS=,
    echo "$*"
}

while (($# > 0)); do
    case "$1" in
        -m|--mode)
            if (($# < 2)); then
                echo "Error: missing value for $1" >&2
                usage >&2
                exit 1
            fi
            MODE="$2"
            shift 2
            ;;
        --mode=*)
            MODE="${1#*=}"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            FORWARD_ARGS+=("$@")
            break
            ;;
        *)
            FORWARD_ARGS+=("$1")
            shift
            ;;
    esac
done

IGNORE_PATTERNS=("${COMMON_IGNORE_PATTERNS[@]}")
INCLUDE_PATTERNS=()

case "${MODE}" in
    full)
        ;;
    no-gui)
        IGNORE_PATTERNS+=("${MODE2_IGNORE_PATTERNS[@]}")
        ;;
    gui-only)
        INCLUDE_PATTERNS=("${MODE3_INCLUDE_PATTERNS[@]}")
        IGNORE_PATTERNS+=("${MODE3_IGNORE_PATTERNS[@]}")
        ;;
    *)
        echo "Error: unsupported mode '${MODE}'. Expected full, no-gui, or gui-only." >&2
        usage >&2
        exit 1
        ;;
esac

COMMAND=(repomix)

if ((${#INCLUDE_PATTERNS[@]} > 0)); then
    INCLUDE_ARG="$(join_by_comma "${INCLUDE_PATTERNS[@]}")"
    COMMAND+=(--include "${INCLUDE_ARG}")
fi

IGNORE_ARG="$(join_by_comma "${IGNORE_PATTERNS[@]}")"
COMMAND+=(--ignore "${IGNORE_ARG}")

if ((${#FORWARD_ARGS[@]} > 0)); then
    COMMAND+=("${FORWARD_ARGS[@]}")
fi

printf 'Running:'
printf ' %q' "${COMMAND[@]}"
printf '\n'

exec "${COMMAND[@]}"
