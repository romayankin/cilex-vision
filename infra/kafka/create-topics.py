#!/usr/bin/env python3
"""Create Kafka topics from topics.yaml — idempotent.

Usage:
    python create-topics.py --bootstrap-servers localhost:9092
    python create-topics.py --bootstrap-servers broker1:9092,broker2:9092 --dry-run

Reads infra/kafka/topics.yaml and creates any topics that do not already exist.
Existing topics are left untouched (no partition/config changes applied
automatically — use --diff to see discrepancies).

Requirements:
    pip install confluent-kafka pyyaml
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time
from typing import Any

import yaml
from confluent_kafka.admin import AdminClient, NewTopic

TOPICS_YAML = pathlib.Path(__file__).with_name("topics.yaml")

# Mapping from topics.yaml field names to Kafka topic-level config keys.
_CONFIG_MAP: dict[str, str] = {
    "cleanup_policy": "cleanup.policy",
    "retention_ms": "retention.ms",
    "min_insync_replicas": "min.insync.replicas",
}


def load_topics(path: pathlib.Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return (defaults, topic_list) from the YAML file."""
    with open(path) as fh:
        data = yaml.safe_load(fh)
    defaults = data.get("defaults", {})
    topics = data.get("topics", [])
    if not topics:
        print("ERROR: no topics defined in", path, file=sys.stderr)
        sys.exit(1)
    return defaults, topics


def build_new_topic(
    entry: dict[str, Any],
    defaults: dict[str, Any],
) -> NewTopic:
    """Translate one YAML topic entry into a confluent_kafka NewTopic."""
    name = entry["name"]
    num_partitions = entry.get("partitions", 1)
    replication_factor = entry.get(
        "replication_factor", defaults.get("replication_factor", 3)
    )

    config: dict[str, str] = {}
    if "cleanup_policy" in entry:
        config["cleanup.policy"] = entry["cleanup_policy"]
    if "retention_ms" in entry:
        config["retention.ms"] = str(entry["retention_ms"])
    min_isr = defaults.get("min_insync_replicas")
    if min_isr is not None:
        config["min.insync.replicas"] = str(min_isr)

    return NewTopic(
        topic=name,
        num_partitions=num_partitions,
        replication_factor=replication_factor,
        config=config,
    )


def existing_topics(admin: AdminClient) -> set[str]:
    """Return the set of topic names that already exist on the cluster."""
    metadata = admin.list_topics(timeout=10)
    return set(metadata.topics.keys())


def create_topics(
    admin: AdminClient,
    new_topics: list[NewTopic],
    dry_run: bool = False,
) -> bool:
    """Create topics that don't yet exist. Returns True if all succeeded."""
    current = existing_topics(admin)
    to_create = [t for t in new_topics if t.topic not in current]
    skipped = [t for t in new_topics if t.topic in current]

    for t in skipped:
        print(f"  SKIP  {t.topic} (already exists)")

    if not to_create:
        print("Nothing to create — all topics already exist.")
        return True

    if dry_run:
        for t in to_create:
            print(f"  DRY   {t.topic}  partitions={t.num_partitions}  rf={t.replication_factor}  config={t.config}")
        return True

    futures = admin.create_topics(to_create, request_timeout=30)
    success = True
    for topic_name, future in futures.items():
        try:
            future.result()  # blocks until topic is created or fails
            print(f"  OK    {topic_name}")
        except Exception as exc:
            print(f"  FAIL  {topic_name}: {exc}", file=sys.stderr)
            success = False

    return success


def diff_topics(
    admin: AdminClient,
    new_topics: list[NewTopic],
) -> None:
    """Print configuration differences for existing topics."""
    current = existing_topics(admin)
    metadata = admin.list_topics(timeout=10)

    for nt in new_topics:
        if nt.topic not in current:
            print(f"  NEW   {nt.topic} (does not exist yet)")
            continue

        topic_meta = metadata.topics[nt.topic]
        actual_partitions = len(topic_meta.partitions)
        if actual_partitions != nt.num_partitions:
            print(
                f"  DIFF  {nt.topic}: partitions  "
                f"yaml={nt.num_partitions}  actual={actual_partitions}"
            )
        else:
            print(f"  OK    {nt.topic}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create Kafka topics from topics.yaml (idempotent)."
    )
    parser.add_argument(
        "--bootstrap-servers",
        required=True,
        help="Comma-separated list of Kafka broker addresses.",
    )
    parser.add_argument(
        "--topics-file",
        type=pathlib.Path,
        default=TOPICS_YAML,
        help="Path to topics.yaml (default: alongside this script).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be created without making changes.",
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Compare YAML definitions against existing cluster topics.",
    )
    args = parser.parse_args()

    defaults, topic_entries = load_topics(args.topics_file)
    new_topics = [build_new_topic(e, defaults) for e in topic_entries]

    admin = AdminClient({"bootstrap.servers": args.bootstrap_servers})

    if args.diff:
        print(f"Comparing {len(new_topics)} topics against cluster...\n")
        diff_topics(admin, new_topics)
        return

    action = "DRY RUN" if args.dry_run else "Creating"
    print(f"{action} {len(new_topics)} topics on {args.bootstrap_servers}...\n")

    ok = create_topics(admin, new_topics, dry_run=args.dry_run)
    if not ok:
        sys.exit(1)

    print("\nDone.")


if __name__ == "__main__":
    main()
