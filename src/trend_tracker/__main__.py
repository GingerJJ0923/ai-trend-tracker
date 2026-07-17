import argparse
import sys

from .config import Settings
from .pipeline import collect, digest, seed_tracks


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect and personalize AI product and technology signals")
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect_parser = subparsers.add_parser("collect", help="Collect configured sources into Supabase")
    collect_parser.add_argument("--dry-run", action="store_true", help="Fetch and print samples without writing to Supabase")
    subparsers.add_parser("digest", help="Match recent items to Tracks and generate a digest")
    subparsers.add_parser("seed-tracks", help="Upsert TRACKS_JSON into Supabase")
    args = parser.parse_args()
    settings = Settings.from_env()
    try:
        if args.command == "collect":
            collect(settings, dry_run=args.dry_run)
        elif args.command == "digest":
            digest(settings)
        elif args.command == "seed-tracks":
            seed_tracks(settings)
        return 0
    except Exception as exc:
        print("ERROR: {0}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

