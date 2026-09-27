"""Presigned URLs for logs in the Compose dev stack's S3 (see compose.yaml, scripts/seed-logs.sh)."""

import os

import boto3
from botocore.config import Config

S3_HOST = "s3"
BUCKET = "logs"


def presign(key: str, *, access_key: str, secret_key: str, expires: int = 3600) -> str:
    # Signed for the host PACDS fetches from; signing is offline, so the host need not resolve here.
    # Path-style keeps the bucket out of the hostname, which must match logs.allowed_hosts.
    s3 = boto3.client(
        "s3",
        endpoint_url=f"http://{S3_HOST}:9000",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    return s3.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=expires)


def dev_credentials() -> tuple[str, str]:
    # The dev S3's root credentials (compose.yaml); override to point at another S3.
    return os.environ.get("PACDS_S3_ACCESS_KEY", "pacds-dev"), os.environ.get("PACDS_S3_SECRET_KEY", "pacds-dev-secret")
