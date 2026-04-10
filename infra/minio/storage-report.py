#!/usr/bin/env python3
"""Generate a storage utilization report from MinIO.

Usage:
    python storage-report.py --minio-url http://localhost:9000 \
        --output artifacts/storage-report.md
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

DEFAULT_MC_IMAGE = "minio/mc:RELEASE.2024-06-11T21-32-12Z"
DEFAULT_CAPACITY_THRESHOLD_BYTES = 100_000_000_000


@dataclass(frozen=True)
class BucketPolicy:
    bucket: str
    tier: str
    expiration_days: int


@dataclass(frozen=True)
class BucketUsage:
    bucket: str
    tier: str
    object_count: int
    size_bytes: int
    expiration_days: int
    projected_cost_usd: float
    capacity_flag: str
    present: bool


@dataclass(frozen=True)
class McRunner:
    alias: str
    alias_url: str
    use_docker: bool
    mc_binary: str
    mc_image: str

    def run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment[f"MC_HOST_{self.alias}"] = self.alias_url

        if self.use_docker:
            command = [
                "docker",
                "run",
                "--rm",
                "--network",
                "host",
                "-e",
                f"MC_HOST_{self.alias}={self.alias_url}",
                self.mc_image,
                *args,
            ]
            run_env: dict[str, str] | None = None
        else:
            command = [self.mc_binary, *args]
            run_env = environment

        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=run_env,
        )
        if check and result.returncode != 0:
            details = "\n".join(
                part.strip()
                for part in (result.stdout, result.stderr)
                if part and part.strip()
            )
            raise RuntimeError(
                f"`{' '.join(command)}` failed with exit code {result.returncode}: "
                f"{details or 'no output'}",
            )
        return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--minio-url",
        default="http://localhost:9000",
        help="Base MinIO URL.",
    )
    parser.add_argument(
        "--access-key",
        default="minioadmin",
        help="MinIO access key.",
    )
    parser.add_argument(
        "--secret-key",
        default="minioadmin123",
        help="MinIO secret key.",
    )
    parser.add_argument(
        "--policies",
        type=Path,
        default=Path("infra/minio/lifecycle-policies.json"),
        help="Path to lifecycle policy source file.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/storage-report.md"),
        help="Markdown output path.",
    )
    parser.add_argument(
        "--hot-rate",
        type=float,
        default=0.023,
        help="Monthly hot-tier object storage cost in USD per GB.",
    )
    parser.add_argument(
        "--warm-rate",
        type=float,
        default=0.0125,
        help="Monthly warm-tier object storage cost in USD per GB.",
    )
    parser.add_argument(
        "--capacity-threshold-bytes",
        type=int,
        default=DEFAULT_CAPACITY_THRESHOLD_BYTES,
        help="Per-bucket nominal capacity threshold used for 80%% warning flags.",
    )
    parser.add_argument(
        "--alias",
        default="cilex",
        help="Logical alias used for mc commands.",
    )
    parser.add_argument(
        "--mc-binary",
        default="mc",
        help="Local mc binary to use before falling back to Docker.",
    )
    parser.add_argument(
        "--mc-image",
        default=DEFAULT_MC_IMAGE,
        help="Docker image used when local mc is unavailable.",
    )
    return parser.parse_args()


def build_alias_url(minio_url: str, access_key: str, secret_key: str) -> str:
    parts = urlsplit(minio_url)
    if not parts.scheme or not parts.netloc:
        raise SystemExit(f"invalid --minio-url: {minio_url}")

    credentials = f"{quote(access_key, safe='')}:{quote(secret_key, safe='')}"
    return urlunsplit(
        (parts.scheme, f"{credentials}@{parts.netloc}", parts.path, parts.query, parts.fragment),
    )


def load_policies(path: Path) -> list[BucketPolicy]:
    if not path.is_file():
        raise SystemExit(f"policy file not found: {path}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_policies = payload.get("policies")
    if not isinstance(raw_policies, list) or not raw_policies:
        raise SystemExit(f"policy file {path} does not contain a non-empty 'policies' list")

    policies: list[BucketPolicy] = []
    for item in raw_policies:
        if not isinstance(item, dict):
            raise SystemExit("policy entries must be objects")

        bucket = item.get("bucket")
        tier = item.get("tier")
        rules = item.get("rules")
        if not isinstance(bucket, str) or not isinstance(tier, str) or not isinstance(rules, list):
            raise SystemExit("invalid lifecycle policy entry")
        if not rules or not isinstance(rules[0], dict):
            raise SystemExit(f"bucket {bucket} has no usable lifecycle rule")
        expiration = rules[0].get("expiration")
        if not isinstance(expiration, dict) or not isinstance(expiration.get("days"), int):
            raise SystemExit(f"bucket {bucket} lifecycle rule is missing expiration.days")

        policies.append(
            BucketPolicy(
                bucket=bucket,
                tier=tier,
                expiration_days=int(expiration["days"]),
            ),
        )
    return policies


def resolve_runner(args: argparse.Namespace) -> McRunner:
    alias_url = build_alias_url(args.minio_url, args.access_key, args.secret_key)
    if shutil.which(args.mc_binary):
        return McRunner(
            alias=args.alias,
            alias_url=alias_url,
            use_docker=False,
            mc_binary=args.mc_binary,
            mc_image=args.mc_image,
        )

    if shutil.which("docker") is None:
        raise SystemExit(
            f"neither {args.mc_binary!r} nor docker is available; cannot run mc",
        )

    return McRunner(
        alias=args.alias,
        alias_url=alias_url,
        use_docker=True,
        mc_binary=args.mc_binary,
        mc_image=args.mc_image,
    )


def parse_json_lines(stdout: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        decoded = json.loads(stripped)
        if isinstance(decoded, dict):
            payloads.append(decoded)
    return payloads


def extract_int(payloads: list[dict[str, Any]], candidates: tuple[str, ...]) -> int | None:
    for payload in payloads:
        for key in candidates:
            value = payload.get(key)
            if isinstance(value, (int, float)):
                return int(value)
        for nested_key in ("usage", "status", "metadata"):
            nested_value = payload.get(nested_key)
            if isinstance(nested_value, dict):
                for key in candidates:
                    nested_item = nested_value.get(key)
                    if isinstance(nested_item, (int, float)):
                        return int(nested_item)
    return None


def human_bytes(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    units = ["KiB", "MiB", "GiB", "TiB", "PiB"]
    size = float(value)
    for unit in units:
        size /= 1024.0
        if size < 1024 or unit == units[-1]:
            return f"{size:.2f} {unit}"
    return f"{value} B"


def tier_rate(tier: str, *, hot_rate: float, warm_rate: float) -> float:
    if tier == "hot":
        return hot_rate
    return warm_rate


def collect_bucket_usage(
    runner: McRunner,
    policy: BucketPolicy,
    *,
    hot_rate: float,
    warm_rate: float,
    capacity_threshold_bytes: int,
) -> BucketUsage:
    target = f"{runner.alias}/{policy.bucket}"
    du_result = runner.run(["du", "--json", target], check=False)
    if du_result.returncode != 0:
        combined = "\n".join(
            part.strip()
            for part in (du_result.stdout, du_result.stderr)
            if part and part.strip()
        ).lower()
        if "not found" in combined or "does not exist" in combined or "no such bucket" in combined:
            return BucketUsage(
                bucket=policy.bucket,
                tier=policy.tier,
                object_count=0,
                size_bytes=0,
                expiration_days=policy.expiration_days,
                projected_cost_usd=0.0,
                capacity_flag="MISSING",
                present=False,
            )
        raise RuntimeError(
            f"failed to collect usage for {policy.bucket}: {combined or 'no output'}",
        )

    du_payloads = parse_json_lines(du_result.stdout)
    stat_result = runner.run(["stat", "--json", target], check=False)
    stat_payloads = parse_json_lines(stat_result.stdout) if stat_result.returncode == 0 else []

    size_bytes = extract_int(du_payloads + stat_payloads, ("size", "totalSize", "usage"))
    object_count = extract_int(
        du_payloads + stat_payloads,
        ("objectsCount", "objects", "count", "num_objects", "numObjects"),
    )
    if size_bytes is None:
        raise RuntimeError(f"mc output for {policy.bucket} did not include bucket size")
    if object_count is None:
        object_count = 0

    rate = tier_rate(policy.tier, hot_rate=hot_rate, warm_rate=warm_rate)
    projected_cost_usd = (size_bytes / (1024**3)) * rate
    warn_threshold = int(capacity_threshold_bytes * 0.8)
    capacity_flag = "WARN" if size_bytes >= warn_threshold else "OK"

    return BucketUsage(
        bucket=policy.bucket,
        tier=policy.tier,
        object_count=object_count,
        size_bytes=size_bytes,
        expiration_days=policy.expiration_days,
        projected_cost_usd=projected_cost_usd,
        capacity_flag=capacity_flag,
        present=True,
    )


def generate_report(
    buckets: list[BucketUsage],
    *,
    minio_url: str,
    policies_path: Path,
    hot_rate: float,
    warm_rate: float,
    capacity_threshold_bytes: int,
) -> str:
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    lines = [
        "# Storage Tiering Report",
        "",
        f"- Generated at: `{now}`",
        f"- MinIO endpoint: `{minio_url}`",
        f"- Policies source: `{policies_path}`",
        f"- Hot-tier rate: `${hot_rate:.4f}/GB-month`",
        f"- Warm-tier rate: `${warm_rate:.4f}/GB-month`",
        f"- Capacity warning threshold: 80% of `{capacity_threshold_bytes}` bytes",
        "",
        "## Assumptions",
        "",
        "- Cold-tier projections reuse the warm-tier rate because `scripts/cost-model/params.yaml` defines `hot_object` and `warm_object`, but no separate cold-object rate.",
        "- Missing buckets are reported explicitly instead of failing the entire report.",
        "",
        "## Per-Bucket Utilization",
        "",
        "| Bucket | Tier | Objects | Total Size | Expiration | Projected Cost / Month | Capacity |",
        "|--------|------|---------|------------|------------|------------------------|----------|",
    ]

    for bucket in buckets:
        lines.append(
            "| "
            + " | ".join(
                [
                    bucket.bucket,
                    bucket.tier,
                    str(bucket.object_count),
                    human_bytes(bucket.size_bytes),
                    f"{bucket.expiration_days}d",
                    f"${bucket.projected_cost_usd:.2f}",
                    bucket.capacity_flag,
                ],
            )
            + " |",
        )

    tier_totals: dict[str, dict[str, float]] = {}
    for bucket in buckets:
        tier_bucket = tier_totals.setdefault(
            bucket.tier,
            {"size_bytes": 0.0, "cost_usd": 0.0, "bucket_count": 0.0},
        )
        tier_bucket["size_bytes"] += bucket.size_bytes
        tier_bucket["cost_usd"] += bucket.projected_cost_usd
        tier_bucket["bucket_count"] += 1

    lines.extend(
        [
            "",
            "## Tier Summary",
            "",
            "| Tier | Buckets | Total Size | Projected Cost / Month |",
            "|------|---------|------------|------------------------|",
        ],
    )
    for tier in ("hot", "warm", "cold"):
        summary = tier_totals.get(tier, {"size_bytes": 0.0, "cost_usd": 0.0, "bucket_count": 0.0})
        lines.append(
            f"| {tier} | {int(summary['bucket_count'])} | "
            f"{human_bytes(int(summary['size_bytes']))} | ${summary['cost_usd']:.2f} |",
        )

    flagged = [bucket for bucket in buckets if bucket.capacity_flag in {"WARN", "MISSING"}]
    lines.extend(["", "## Capacity Alerts", ""])
    if not flagged:
        lines.append("- No buckets exceeded the 80% warning threshold.")
    else:
        for bucket in flagged:
            if bucket.capacity_flag == "MISSING":
                lines.append(f"- `{bucket.bucket}`: bucket is missing from MinIO.")
                continue
            lines.append(
                f"- `{bucket.bucket}`: `{human_bytes(bucket.size_bytes)}` exceeds the 80% threshold "
                f"for `{capacity_threshold_bytes}` bytes.",
            )

    return "\n".join(lines) + "\n"


def main() -> None:
    args = parse_args()
    policies = load_policies(args.policies)
    runner = resolve_runner(args)

    usages = [
        collect_bucket_usage(
            runner,
            policy,
            hot_rate=args.hot_rate,
            warm_rate=args.warm_rate,
            capacity_threshold_bytes=args.capacity_threshold_bytes,
        )
        for policy in policies
    ]

    report = generate_report(
        usages,
        minio_url=args.minio_url,
        policies_path=args.policies,
        hot_rate=args.hot_rate,
        warm_rate=args.warm_rate,
        capacity_threshold_bytes=args.capacity_threshold_bytes,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"Wrote storage report to {args.output}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
