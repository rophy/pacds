"""Presigned URLs for logs in the Kind dev cluster's S3 (see k8s/dev/s3.yaml, scripts/seed-logs.sh)."""

import base64
import os
import subprocess

import boto3
from botocore.config import Config

from tests.kube import kubectl

S3_HOST = "pacds-s3.pacds.svc.cluster.local"
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
    # The in-cluster test pod gets the keys as env vars; from the host, read the Secret.
    if os.environ.get("PACDS_S3_ACCESS_KEY"):
        return os.environ["PACDS_S3_ACCESS_KEY"], os.environ["PACDS_S3_SECRET_KEY"]

    def field(name: str) -> str:
        raw = subprocess.run(kubectl("get", "secret", "pacds-s3", "-o", f"jsonpath={{.data.{name}}}"), check=True, capture_output=True, text=True).stdout
        return base64.b64decode(raw).decode()

    return field("access-key"), field("secret-key")
