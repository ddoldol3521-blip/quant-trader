"""Upload private account settings; only status words are printed."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.quantmix_cloud import CloudError
from src.quantmix_cloud_sync import initialize, sync_profile

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        print(initialize() if args.initialize else sync_profile(force=args.force))
    except CloudError as error:
        print(f"CLOUD_SYNC_FAILED: {error}", file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print(f"CLOUD_SYNC_FAILED: {type(error).__name__}", file=sys.stderr)
        sys.exit(1)
