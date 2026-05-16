#!/bin/bash
set -e

# Require Python 3.12+
PYTHON=""
for cmd in python3.12 python3.13 python3; do
    if command -v "$cmd" &> /dev/null; then
        version=$("$cmd" -c "import sys; print(sys.version_info[:2])")
        major=$("$cmd" -c "import sys; print(sys.version_info[0])")
        minor=$("$cmd" -c "import sys; print(sys.version_info[1])")
        if [ "$major" -ge 3 ] && [ "$minor" -ge 12 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "Error: Python 3.12+ is required but not found."
    echo "Install it from https://www.python.org/downloads/"
    exit 1
fi

echo "Using $PYTHON ($($PYTHON --version))"
$PYTHON -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

echo ""
echo "Done! Activate with: source .venv/bin/activate"
echo "Then run: python gpx_viewer.py your_file.gpx"
