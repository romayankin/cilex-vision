#!/usr/bin/env python3
"""Apply MinIO lifecycle (ILM) policies to all configured buckets.

Idempotent: safe to re-run. Uses `mc` either from the local PATH or from the
`minio/mc` container image.

Usage:
    python apply-lifecycle.py --minio-url http://localhost:9000 \
        --policies infra/minio/lifecycle-policies.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

DEFAULT_MC_IMAGE = "minio/mc:RELEASE.2024-06-11T21-32-12Z"


@dataclass(frozen=True)
class BucketPolicy:
    bucket: str
    tier: str
    rules: list[dict[str, Any]]


@dataclass(frozen=True)
class McRunner:
    alias: str
    alias_url: str
    use_docker: bool
    mc_binary: str
    mc_image: str

    def run(
        self,
        args: list[str],
        *,
        stdin_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment[f"MC_HOST_{self.alias}"] = self.alias_url

        if self.use_docker:
            command = [
                "docker",
                "run",
                "--rm",
                "-i",
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
            input=stdin_text,
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
        "--alias",
        default="cilex",
        help="Logical alias used for mc commands.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the actions that would be applied without changing MinIO.",
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
        if not isinstance(bucket, str) or not bucket:
            raise SystemExit("each policy entry requires a non-empty 'bucket'")
        if not isinstance(tier, str) or not tier:
            raise SystemExit(f"bucket {bucket} requires a non-empty 'tier'")
        if not isinstance(rules, list) or not rules:
            raise SystemExit(f"bucket {bucket} requires a non-empty 'rules' list")
        policies.append(BucketPolicy(bucket=bucket, tier=tier, rules=rules))
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


def build_mc_lifecycle_document(policy: BucketPolicy) -> str:
    rules: list[dict[str, Any]] = []
    for raw_rule in policy.rules:
        if not isinstance(raw_rule, dict):
            raise SystemExit(f"bucket {policy.bucket} contains a non-object rule")

        expiration = raw_rule.get("expiration")
        if not isinstance(expiration, dict) or not isinstance(expiration.get("days"), int):
            raise SystemExit(
                f"bucket {policy.bucket} rule {raw_rule.get('id', '<unknown>')} "
                "is missing expiration.days",
            )

        filter_payload = raw_rule.get("filter", {})
        prefix = ""
        if isinstance(filter_payload, dict):
            raw_prefix = filter_payload.get("prefix", "")
            if isinstance(raw_prefix, str):
                prefix = raw_prefix

        rules.append(
            {
                "ID": str(raw_rule.get("id", f"{policy.bucket}-expiry")),
                "Status": str(raw_rule.get("status", "Enabled")),
                "Filter": {"Prefix": prefix},
                "Expiration": {"Days": int(expiration["days"])},
            },
        )

    return json.dumps({"Rules": rules}, indent=2) + "\n"


def import_lifecycle_document(
    runner: McRunner,
    *,
    alias: str,
    bucket: str,
    document: str,
) -> None:
    target = f"{alias}/{bucket}"
    fallback_errors: list[str] = []
    for command in (
        ["ilm", "rule", "import", target],
        ["ilm", "import", target],
    ):
        result = runner.run(command, stdin_text=document, check=False)
        if result.returncode == 0:
            return

        combined = "\n".join(
            part.strip()
            for part in (result.stdout, result.stderr)
            if part and part.strip()
        )
        lowered = combined.lower()
        if "unknown command" in lowered or "is not a command" in lowered:
            fallback_errors.append(combined)
            continue
        raise RuntimeError(
            f"failed to import lifecycle rules for {bucket}: {combined or 'no output'}",
        )

    raise RuntimeError(
        "mc does not support lifecycle import commands in this environment: "
        + " | ".join(error for error in fallback_errors if error),
    )


def main() -> None:
    args = parse_args()
    policies = load_policies(args.policies)
    runner = resolve_runner(args)

    applied = 0
    skipped = 0
    for policy in policies:
        document = build_mc_lifecycle_document(policy)
        target = f"{args.alias}/{policy.bucket}"

        if args.dry_run:
            print(f"DRY-RUN {target}: create bucket if missing and import lifecycle policy")
            print(document.rstrip())
            skipped += 1
            continue

        runner.run(["mb", "--ignore-existing", target])
        import_lifecycle_document(
            runner,
            alias=args.alias,
            bucket=policy.bucket,
            document=document,
        )
        print(f"APPLIED {target} ({policy.tier} tier, {len(policy.rules)} rule(s))")
        applied += 1

    print(
        f"Lifecycle reconciliation complete: applied={applied} skipped={skipped} total={len(policies)}",
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
