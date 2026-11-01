"""CLI for refund reconciliation."""
import argparse
import json
from pathlib import Path
import sys

# Support direct execution from the repository checkout.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pilot.refunds.reconcile import reconcile_items


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--token', required=True)
    parser.add_argument('--items', required=True, type=Path)
    args = parser.parse_args()
    items = json.loads(args.items.read_text(encoding='utf-8'))
    print(json.dumps(reconcile_items(items, args.base_url, args.token), ensure_ascii=False))


if __name__ == '__main__':
    main()
