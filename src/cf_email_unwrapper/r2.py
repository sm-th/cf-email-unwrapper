"""R2 access over the S3 API."""

from typing import Protocol

import boto3
from botocore.exceptions import ClientError


class ObjectStore(Protocol):
    def get(self, key: str) -> bytes | None: ...
    def delete(self, key: str) -> None: ...


class R2Store:
    def __init__(self, *, endpoint: str, access_key_id: str, secret_access_key: str, bucket: str):
        self._bucket = bucket
        self._s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
        )

    def get(self, key: str) -> bytes | None:
        """Object bytes, or None when the object no longer exists."""
        try:
            return self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise

    def delete(self, key: str) -> None:
        self._s3.delete_object(Bucket=self._bucket, Key=key)
