import argparse

from bench.corpus import check

parser = argparse.ArgumentParser()
parser.add_argument("--check", action="store_true", required=True)
parser.parse_args()
raise SystemExit(check())
