"""Classify a bounded batch of imported bird-feeder snapshots."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path

from camera.classification import BirdClassifier, OpenAIResponsesClient

MODEL = "gpt-5.4-mini"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Identify birds in unclassified snapshots. The default is a no-cost "
            "preview; --execute is required to contact OpenAI."
        )
    )
    parser.add_argument("--library", type=Path, required=True, help="Pi library root")
    parser.add_argument("--execute", action="store_true", help="make billable API calls")
    parser.add_argument("--max-images", type=int, default=5, help="hard cap per run")
    parser.add_argument(
        "--monthly-image-limit", type=int, default=100, help="hard local request cap"
    )
    parser.add_argument(
        "--monthly-budget-usd",
        type=float,
        default=10.00,
        help="local estimated spend cap",
    )
    parser.add_argument(
        "--request-cost-reserve-usd",
        type=float,
        default=0.01,
        help="reserve required before each request",
    )
    parser.add_argument(
        "--request-interval-seconds",
        type=float,
        default=6.1,
        help="minimum time between request starts (default: 6.1 seconds)",
    )
    parser.add_argument(
        "--max-image-bytes",
        type=int,
        default=4 * 1024 * 1024,
        help="ignore snapshots larger than this",
    )
    parser.add_argument("--max-output-tokens", type=int, default=512)
    parser.add_argument("--location", default="Toronto, Ontario, Canada")
    parser.add_argument(
        "--oldest-first", action="store_true", help="process oldest snapshots first"
    )
    parser.add_argument(
        "--paired-only",
        action="store_true",
        help="only classify snapshots with a matching video",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if args.execute and not api_key:
        parser.error("OPENAI_API_KEY is required with --execute")

    # Preview mode never calls the client, so a placeholder key is sufficient.
    client = OpenAIResponsesClient(
        api_key or "preview-only",
        model=MODEL,
        max_output_tokens=args.max_output_tokens,
    )
    classifier = BirdClassifier(args.library, client, model=MODEL)
    try:
        result = classifier.run(
            max_images=args.max_images,
            monthly_image_limit=args.monthly_image_limit,
            monthly_budget_usd=args.monthly_budget_usd,
            request_cost_reserve_usd=args.request_cost_reserve_usd,
            request_interval_seconds=args.request_interval_seconds,
            max_image_bytes=args.max_image_bytes,
            location=args.location,
            newest_first=not args.oldest_first,
            paired_only=args.paired_only,
            execute=args.execute,
        )
    except (FileNotFoundError, ValueError) as error:
        parser.error(str(error))

    payload = asdict(result)
    payload["mode"] = "execute" if args.execute else "preview"
    payload["limits"] = {
        "max_images": args.max_images,
        "monthly_image_limit": args.monthly_image_limit,
        "monthly_budget_usd": args.monthly_budget_usd,
        "request_cost_reserve_usd": args.request_cost_reserve_usd,
        "request_interval_seconds": args.request_interval_seconds,
        "max_image_bytes": args.max_image_bytes,
        "max_output_tokens": args.max_output_tokens,
        "paired_only": args.paired_only,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if result.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
