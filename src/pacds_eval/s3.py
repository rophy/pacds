"""Log storage for evaluations: S3-compatible (the dev stack's MinIO, or MinIO/Ceph/S3 elsewhere).

Clients hand PACDS presigned URLs to logs stored here. Configured by environment, defaulting to the dev stack:
  PACDS_LOGS_S3_ENDPOINT         where PACDS fetches from; presigned URLs are signed for this host (default http://s3:9000)
  PACDS_LOGS_S3_UPLOAD_ENDPOINT  where this machine uploads to, if it reaches the store by another name (default: same)
  PACDS_LOGS_S3_BUCKET           bucket (default "logs")
  PACDS_LOGS_S3_REGION           region (default us-east-1)
  PACDS_LOGS_S3_ACCESS_KEY / PACDS_LOGS_S3_SECRET_KEY   credentials (default: the dev MinIO's)
TLS to the store trusts AWS_CA_BUNDLE when set (e.g. the corporate CA bundle).

Usage: python -m pacds_eval.s3 seed [--cases-dir DIR ...]   upload every case's logs as replay/<case>/<log>
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import boto3
from botocore.config import Config

S3_HOST = "s3"
BUCKET = "logs"
DEV_ENDPOINT = f"http://{S3_HOST}:9000"
DEV_CREDENTIALS = ("pacds-dev", "pacds-dev-secret")


@dataclass(frozen=True)
class LogStore:
    endpoint: str = DEV_ENDPOINT
    bucket: str = BUCKET
    region: str = "us-east-1"
    access_key: str = DEV_CREDENTIALS[0]
    secret_key: str = DEV_CREDENTIALS[1]
    upload_endpoint: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> LogStore:
        access_key, secret_key = dev_credentials(env)
        return cls(endpoint=env.get("PACDS_LOGS_S3_ENDPOINT") or DEV_ENDPOINT, bucket=env.get("PACDS_LOGS_S3_BUCKET") or BUCKET,
                   region=env.get("PACDS_LOGS_S3_REGION") or "us-east-1", access_key=access_key, secret_key=secret_key,
                   upload_endpoint=env.get("PACDS_LOGS_S3_UPLOAD_ENDPOINT") or None)

    @property
    def host(self) -> str:
        """The host PACDS fetches from: what its logs.allowed_hosts must list."""
        return urlsplit(self.endpoint).hostname or ""

    def _client(self, endpoint: str) -> Any:
        # Path-style keeps the bucket out of the hostname, which must match logs.allowed_hosts.
        return boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=self.access_key, aws_secret_access_key=self.secret_key,
                            region_name=self.region, config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))

    def presign(self, key: str, expires: int = 3600) -> str:
        # Signed for the host PACDS fetches from; signing is offline, so that host need not resolve here.
        return self._client(self.endpoint).generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires)

    def seed(self, cases_dirs: list[Path]) -> int:
        """Upload the log files each case lists (case.json "logs") as replay/<case id>/<file>; returns how many."""
        client = self._client(self.upload_endpoint or self.endpoint)
        uploaded = 0
        for cases_dir in cases_dirs:
            for case_file in sorted(Path(cases_dir).glob("*/case.json")):
                for name in json.loads(case_file.read_text()).get("logs", []):
                    log = case_file.parent / name
                    if not log.is_file():
                        raise FileNotFoundError(f"{case_file} lists {name}, which is not in {case_file.parent}")
                    client.upload_file(str(log), self.bucket, f"replay/{case_file.parent.name}/{name}")
                    uploaded += 1
        return uploaded


def presign(key: str, *, access_key: str, secret_key: str, expires: int = 3600) -> str:
    """The dev stack's MinIO, or the store configured in the environment when its credentials are given."""
    store = LogStore.from_env()
    return LogStore(endpoint=store.endpoint, bucket=store.bucket, region=store.region, access_key=access_key,
                    secret_key=secret_key).presign(key, expires)


def dev_credentials(env: Mapping[str, str] = os.environ) -> tuple[str, str]:
    # The dev S3's root credentials (compose.yaml) unless the environment names others.
    return (env.get("PACDS_LOGS_S3_ACCESS_KEY") or env.get("PACDS_S3_ACCESS_KEY") or DEV_CREDENTIALS[0],
            env.get("PACDS_LOGS_S3_SECRET_KEY") or env.get("PACDS_S3_SECRET_KEY") or DEV_CREDENTIALS[1])


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m pacds_eval.s3", description="Log storage for evaluations")
    commands = parser.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("seed", help="upload the cases' log files")
    seed.add_argument("--cases-dir", type=Path, action="append", help="case set directory (repeatable; default $PACDS_CASES_DIR)")
    args = parser.parse_args()
    from pacds_eval.harness import resolve_cases_dir

    store = LogStore.from_env()
    count = store.seed(args.cases_dir or [resolve_cases_dir()])
    print(f"=== seeded {count} log files to s3://{store.bucket}/replay/ at {store.upload_endpoint or store.endpoint}")


if __name__ == "__main__":
    main()
