#!/usr/bin/env bash
# Start ContainerLab GUI
set -e
cd "$(dirname "$0")"

if [ ! -d venv ]; then
  echo "Creating virtual environment..."
  python3 -m venv venv
  venv/bin/pip install -q -r requirements.txt
fi

echo "Starting ContainerLab GUI on http://0.0.0.0:8080"
exec venv/bin/python3 run.py
