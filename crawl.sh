#!/bin/bash
# crawl.sh - run the crawler. Example: ./crawl.sh --mode crawl --site kubernetes_docs
cd "$(dirname "$0")" && exec ./.venv/bin/python crawl.py "$@"
