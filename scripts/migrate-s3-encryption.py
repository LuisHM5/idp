#!/usr/bin/env python3
"""Re-encrypt existing S3 objects with the production pipeline CMK.

Safe by default: without --apply this only inventories what would change. It
never deletes or schedules deletion of a KMS key.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

import boto3


MAX_COPY_OBJECT_SIZE = 5 * 1024**3


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migra objetos S3 existentes a la única CMK compartida de prod."
    )
    parser.add_argument("--bucket", help="Bucket; si se omite se resuelve desde CloudFormation.")
    parser.add_argument("--stack", default="DocumentValidationStack")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--key", default="alias/document-validation/pipeline")
    parser.add_argument("--apply", action="store_true", help="Ejecuta las copias; default=dry-run.")
    parser.add_argument("--yes", action="store_true", help="No solicita confirmación con --apply.")
    return parser.parse_args()


def resolve_bucket(cloudformation, stack_name: str) -> str:
    resources = cloudformation.describe_stack_resources(StackName=stack_name)[
        "StackResources"
    ]
    buckets = [
        r["PhysicalResourceId"]
        for r in resources
        if r["ResourceType"] == "AWS::S3::Bucket"
        and "DocumentProcessingBucket" in r["LogicalResourceId"]
    ]
    if len(buckets) != 1:
        raise RuntimeError(
            f"Esperaba un DocumentProcessingBucket en {stack_name}; encontré {len(buckets)}. "
            "Usa --bucket explícitamente."
        )
    return buckets[0]


def same_key(current: str | None, target_arn: str, target_id: str) -> bool:
    return current in {target_arn, target_id}


def main() -> int:
    args = arguments()
    session = boto3.Session(region_name=args.region)
    s3 = session.client("s3")
    kms = session.client("kms")
    cloudformation = session.client("cloudformation")

    bucket = args.bucket or resolve_bucket(cloudformation, args.stack)
    key = kms.describe_key(KeyId=args.key)["KeyMetadata"]
    target_arn, target_id = key["Arn"], key["KeyId"]

    objects: list[dict] = []
    raw_objects: list[str] = []
    current_keys: Counter[str] = Counter()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for item in page.get("Contents", []):
            object_key = item["Key"]
            if object_key.startswith("raw/"):
                raw_objects.append(object_key)
            head = s3.head_object(Bucket=bucket, Key=object_key)
            current = head.get("SSEKMSKeyId")
            current_keys[current or head.get("ServerSideEncryption", "sin-header")] += 1
            if head.get("ServerSideEncryption") != "aws:kms" or not same_key(
                current, target_arn, target_id
            ):
                objects.append({"Key": object_key, "Size": item["Size"]})

    print(f"Bucket: {bucket}")
    print(f"CMK destino: {target_arn}")
    print(f"Objetos revisados: {sum(current_keys.values())}")
    print(f"Objetos por cifrado/llave: {dict(current_keys)}")
    print(f"Objetos por migrar: {len(objects)}")

    if raw_objects:
        print(
            f"ERROR: hay {len(raw_objects)} objetos bajo raw/. Espera a que terminen las "
            "ejecuciones antes de migrar para no redisparar el pipeline.",
            file=sys.stderr,
        )
        return 2
    oversized = [o for o in objects if o["Size"] > MAX_COPY_OBJECT_SIZE]
    if oversized:
        print(
            "ERROR: CopyObject no admite objetos mayores a 5 GiB: "
            + ", ".join(o["Key"] for o in oversized),
            file=sys.stderr,
        )
        return 2
    if not args.apply:
        print("Dry-run: vuelve a ejecutar con --apply para migrar.")
        return 0
    if objects and not args.yes:
        answer = input(f"Reescribir {len(objects)} objetos en el mismo bucket (S/n)? ").strip().lower()
        if answer not in {"", "s", "si", "sí", "y", "yes"}:
            print("Cancelado.")
            return 0

    failures: list[tuple[str, str]] = []
    for index, item in enumerate(objects, 1):
        object_key = item["Key"]
        try:
            s3.copy_object(
                Bucket=bucket,
                Key=object_key,
                CopySource={"Bucket": bucket, "Key": object_key},
                MetadataDirective="COPY",
                TaggingDirective="COPY",
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId=target_arn,
                BucketKeyEnabled=True,
            )
            head = s3.head_object(Bucket=bucket, Key=object_key)
            if not same_key(head.get("SSEKMSKeyId"), target_arn, target_id):
                raise RuntimeError("HeadObject no reportó la CMK destino")
            print(f"[{index}/{len(objects)}] OK {object_key}")
        except Exception as exc:  # keep migrating; report every failed object
            failures.append((object_key, str(exc)))
            print(f"[{index}/{len(objects)}] ERROR {object_key}: {exc}", file=sys.stderr)

    if failures:
        print(f"Fallaron {len(failures)} objetos; NO retires la llave anterior.", file=sys.stderr)
        return 1
    print("Migración completa. Ejecuta otra vez sin --apply y confirma 0 objetos por migrar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
