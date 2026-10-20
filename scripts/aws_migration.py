#!/usr/bin/env python3
"""Interactive local backup and restore for the project's DynamoDB and S3 data.

Every command requires a terminal. Writes require an explicit typed confirmation.
The archive is encrypted with a password; plaintext is temporary.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from terminal_ui import TerminalUI

ui = TerminalUI()

STACKS = {"prod": "DocumentValidationStack", "dev": "DocumentValidationStack-Dev"}
FALLBACK_TABLE = "document-validation-fallback-models"


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def json_file(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=iso, sort_keys=True) + "\n")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_backup_path():
    """Keep the encrypted result in a durable user directory, outside the repo."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return str(Path.home() / f"aws-migration-backup-{stamp}.tar.gz.enc")


def _valid_bucket_map(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return False
    return isinstance(value, dict) and bool(value) and all(
        key in ("prod", "dev") and isinstance(bucket, str) and bool(bucket.strip())
        for key, bucket in value.items())


def ask(label, current=None, *, required=True, choices=None):
    if choices:
        return ui.select(label, [(choice, choice) for choice in choices], default=current)
    return ui.text(label, default=current, required=required)


def confirm(phrase):
    ui.confirm_phrase(phrase)


def choose_profile(label, current=None):
    profiles = boto3.Session().available_profiles
    ui.note("Elige un perfil configurado, no el ID numérico de la cuenta AWS.")
    options = [("actual", "Credenciales activas (entorno, rol o default)")]
    options.extend((profile, profile) for profile in profiles if profile != "actual")
    return ui.select(label, options, default=current or "actual")


def ask_password(*, confirm_password=False):
    return ui.password("Contraseña para cifrar/abrir el respaldo", min_length=12,
                       repeat=confirm_password)


def session(profile, region):
    return boto3.Session(profile_name=None if profile == "actual" else profile, region_name=region)


def account(aws):
    return aws.client("sts").get_caller_identity()["Account"]


def stack_resources(cfn, stack):
    return [r for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack)
            for r in page["StackResourceSummaries"]]


def table_inventory(aws, environments):
    cfn, ddb = aws.client("cloudformation"), aws.client("dynamodb")
    result = []
    buckets = []
    for env in environments:
        resources = stack_resources(cfn, STACKS[env])
        for resource in resources:
            logical = resource["LogicalResourceId"]
            if resource["ResourceType"] == "AWS::DynamoDB::Table":
                result.append((env, resource["PhysicalResourceId"]))
            if resource["ResourceType"] == "AWS::S3::Bucket" and "DocumentProcessingBucket" in logical:
                buckets.append((env, resource["PhysicalResourceId"]))
        suffix = "" if env == "prod" else "-dev"
        result.append((env, "document-validation-catalog" + suffix))
    if len(environments) == 2:
        try:
            ddb.describe_table(TableName=FALLBACK_TABLE)
            result.append(("shared", FALLBACK_TABLE))
        except ddb.exceptions.ResourceNotFoundException:
            pass
    result = list(dict.fromkeys(result))
    if len(buckets) != len(environments):
        raise RuntimeError("No se encontró exactamente un bucket documental por ambiente")
    return result, buckets


def describe(aws, tables):
    ddb = aws.client("dynamodb")
    details = []
    for env, name in tables:
        table = ddb.describe_table(TableName=name)["Table"]
        pitr = ddb.describe_continuous_backups(TableName=name)["ContinuousBackupsDescription"]["PointInTimeRecoveryDescription"]
        ttl = ddb.describe_time_to_live(TableName=name)["TimeToLiveDescription"]
        details.append({"environment": env, "name": name, "schema": table, "pitr": pitr, "ttl": ttl})
    return details


def print_plan(aws, environments):
    tables, buckets = table_inventory(aws, environments)
    details = describe(aws, tables)
    print(f"Cuenta: {account(aws)}; región: {aws.region_name}")
    for item in details:
        print(f"  DynamoDB {item['environment']}: {item['name']} PITR={item['pitr']['PointInTimeRecoveryStatus']}")
    for env, bucket in buckets:
        print(f"  S3 {env}: {bucket}")
    print("Método local: Scan consistente por tabla; detener escrituras durante toda la copia.")
    return tables, buckets, details


def encode_attribute(value):
    kind, data = next(iter(value.items()))
    if kind == "B":
        data = base64.b64encode(data).decode("ascii")
    elif kind == "BS":
        data = [base64.b64encode(part).decode("ascii") for part in data]
    elif kind == "M":
        data = {key: encode_attribute(part) for key, part in data.items()}
    elif kind == "L":
        data = [encode_attribute(part) for part in data]
    return {kind: data}


def decode_attribute(value):
    kind, data = next(iter(value.items()))
    if kind == "B":
        data = base64.b64decode(data, validate=True)
    elif kind == "BS":
        data = [base64.b64decode(part, validate=True) for part in data]
    elif kind == "M":
        data = {key: decode_attribute(part) for key, part in data.items()}
    elif kind == "L":
        data = [decode_attribute(part) for part in data]
    return {kind: data}


def scan_to_file(ddb, table_name, path):
    """Stream all attributes to local DynamoDB JSON without holding a table in RAM."""
    count = 0
    cursor = None
    with gzip.open(path, "wt", encoding="utf-8") as output:
        while True:
            request = {"TableName": table_name, "ConsistentRead": True}
            if cursor:
                request["ExclusiveStartKey"] = cursor
            page = ddb.scan(**request)
            for item in page.get("Items", []):
                encoded = {key: encode_attribute(value) for key, value in item.items()}
                output.write(json.dumps({"Item": encoded}, separators=(",", ":")) + "\n")
                count += 1
            cursor = page.get("LastEvaluatedKey")
            if not cursor:
                return count


def restore_items(ddb, table_name, path):
    count = 0
    batch = []
    with gzip.open(path, "rt", encoding="utf-8") as source:
        for line in source:
            encoded = json.loads(line)["Item"]
            item = {key: decode_attribute(value) for key, value in encoded.items()}
            batch.append({"PutRequest": {"Item": item}})
            if len(batch) == 25:
                write_batch(ddb, table_name, batch)
                count += len(batch)
                batch = []
    if batch:
        write_batch(ddb, table_name, batch)
        count += len(batch)
    return count


def write_batch(ddb, table_name, batch):
    pending = {table_name: batch}
    delay = 0.25
    for _ in range(20):
        response = ddb.batch_write_item(RequestItems=pending)
        pending = {name: items for name, items in response.get("UnprocessedItems", {}).items() if items}
        if not pending:
            return
        time.sleep(delay)
        delay = min(delay * 2, 5)
    raise RuntimeError(f"BatchWriteItem sigue sin procesar items de {table_name} después de 20 intentos")


def encrypt_archive(source, output, password):
    with tempfile.TemporaryDirectory(prefix="idp-crypto-", dir=source.parent) as temp:
        ciphertext = Path(temp) / "cipher.bin"
        subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "600000",
                        "-salt", "-in", str(source), "-out", str(ciphertext), "-pass", "stdin"],
                       input=(password + "\n").encode(), check=True)
        salt = os.urandom(16)
        key = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
        mac = hmac.new(key, digestmod=hashlib.sha256)
        with ciphertext.open("rb") as data:
            for chunk in iter(lambda: data.read(1024 * 1024), b""):
                mac.update(chunk)
        with output.open("wb") as result, ciphertext.open("rb") as data:
            result.write(b"IDPMIGR1" + salt + mac.digest())
            shutil.copyfileobj(data, result)


def decrypt_archive(encrypted, output, password):
    with tempfile.TemporaryDirectory(prefix="idp-crypto-", dir=output.parent) as temp:
        ciphertext = Path(temp) / "cipher.bin"
        with encrypted.open("rb") as source:
            if source.read(8) != b"IDPMIGR1":
                raise RuntimeError("Formato de cifrado no compatible")
            salt = source.read(16)
            expected_mac = source.read(32)
            if len(salt) != 16 or len(expected_mac) != 32:
                raise RuntimeError("Respaldo cifrado incompleto")
            with ciphertext.open("wb") as data:
                shutil.copyfileobj(source, data)
        key = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
        mac = hmac.new(key, digestmod=hashlib.sha256)
        with ciphertext.open("rb") as data:
            for chunk in iter(lambda: data.read(1024 * 1024), b""):
                mac.update(chunk)
        if not hmac.compare_digest(mac.digest(), expected_mac):
            raise RuntimeError("Contraseña incorrecta o respaldo cifrado alterado")
        subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "600000",
                        "-in", str(ciphertext), "-out", str(output), "-pass", "stdin"],
                       input=(password + "\n").encode(), check=True)


def download_prefix(s3, bucket, prefix, root):
    count = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            relative = key[len(prefix):]
            if not relative or relative.endswith("/"):
                continue
            relative_path = Path(relative)
            if relative_path.is_absolute() or ".." in relative_path.parts:
                raise RuntimeError(f"Clave S3 insegura: {key}")
            target = root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, key, str(target))
            count += 1
    return count


def backup(args):
    aws = session(args.source_profile, args.region)
    tables, buckets, details = print_plan(aws, args.environments)
    if shutil.which("openssl") is None:
        raise RuntimeError("Se requiere openssl para cifrar con contraseña")
    s3, ddb, cfn = aws.client("s3"), aws.client("dynamodb"), aws.client("cloudformation")
    output = Path(args.output).expanduser().resolve()
    if output.exists():
        raise RuntimeError(f"El archivo ya existe: {output}")
    second = Path(args.second_copy).expanduser().resolve() if args.second_copy else None
    if second and (second == output or second.exists()):
        raise RuntimeError("La ruta de la segunda copia debe ser distinta y no existir")
    output.parent.mkdir(parents=True, exist_ok=True)
    print(f"Se leerán {len(details)} tablas y {len(buckets)} buckets desde la cuenta {account(aws)}.")
    print(f"Archivo cifrado local: {output}")
    print("Es necesario mantener detenidas todas las escrituras de estos datos hasta terminar.")
    if args.second_copy:
        print(f"Segunda copia cifrada local: {args.second_copy}")
    if args.transfer_bucket:
        print(f"Copia cifrada adicional: {args.transfer_bucket} (perfil {args.destination_profile})")
    confirm(f"RESPALDAR {account(aws)}")
    with tempfile.TemporaryDirectory(prefix="idp-backup-", dir=args.work_dir) as temp:
        root = Path(temp) / "backup"
        root.mkdir(mode=0o700)
        manifest = {"version": 2, "method": "LOCAL_SCAN", "sourceAccount": account(aws),
                    "region": args.region, "backupStartedAt": iso(datetime.now(timezone.utc)),
                    "tables": [], "buckets": [], "stacks": {}}
        for env in args.environments:
            stack = STACKS[env]
            folder = root / "cloudformation" / env
            json_file(folder / "template.json", cfn.get_template(StackName=stack)["TemplateBody"])
            json_file(folder / "resources.json", stack_resources(cfn, stack))
            manifest["stacks"][env] = stack
        for detail in details:
            env, name = detail["environment"], detail["name"]
            folder = root / "dynamodb" / env / name
            json_file(folder / "schema.json", detail["schema"])
            json_file(folder / "pitr.json", detail["pitr"])
            json_file(folder / "ttl.json", detail["ttl"])
            folder.mkdir(parents=True, exist_ok=True)
            with ui.spinner(f"Respaldando tabla {name}"):
                count = scan_to_file(ddb, name, folder / "items.jsonl.gz")
            manifest["tables"].append({"environment": env, "name": name,
                "itemCount": count})
            print("Respaldada:", name, count, "filas")
        for env, bucket in buckets:
            folder = root / "s3" / env
            with ui.spinner(f"Descargando bucket {bucket}"):
                count = download_prefix(s3, bucket, "", folder)
            metadata = {}
            for path in folder.rglob("*"):
                if path.is_file():
                    key = str(path.relative_to(folder))
                    head = s3.head_object(Bucket=bucket, Key=key)
                    metadata[key] = {field: head[field] for field in (
                        "ContentType", "ContentDisposition", "ContentEncoding", "CacheControl", "Metadata")
                        if field in head}
            if len(metadata) != count:
                raise RuntimeError(f"Claves S3 colisionaron en el respaldo local: {bucket}")
            manifest["buckets"].append({"environment": env, "name": bucket,
                                        "objects": count, "objectMetadata": metadata})
            print("Descargado:", bucket, count, "objetos")
        manifest["backupFinishedAt"] = iso(datetime.now(timezone.utc))
        hashes = {str(p.relative_to(root)): sha256(p) for p in sorted(root.rglob("*")) if p.is_file()}
        manifest["sha256"] = hashes
        json_file(root / "manifest.json", manifest)
        tar_path = Path(temp) / "backup.tar.gz"
        with tarfile.open(tar_path, "w:gz") as archive:
            archive.add(root, arcname="backup")
        with ui.spinner("Cifrando el respaldo local"):
            encrypt_archive(tar_path, output, args.password)
    print(f"Respaldo cifrado: {output}  SHA256={sha256(output)}")
    if second:
        second.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(output, second)
        if sha256(second) != sha256(output):
            raise RuntimeError("La segunda copia local no coincide con el respaldo")
        print(f"Segunda copia local: {second}")
    if args.transfer_bucket:
        if not args.destination_profile:
            raise RuntimeError("--destination-profile es obligatorio con --transfer-bucket")
        target_aws = session(args.destination_profile, args.region)
        if account(target_aws) == account(aws):
            raise RuntimeError("El perfil de transferencia apunta a la cuenta origen")
        key = "idp-migration-archives/" + output.name
        target_s3 = target_aws.client("s3")
        if target_s3.list_objects_v2(Bucket=args.transfer_bucket, Prefix=key, MaxKeys=1).get("KeyCount"):
            raise RuntimeError(f"Ya existe s3://{args.transfer_bucket}/{key}")
        target_s3.upload_file(str(output), args.transfer_bucket, key)
        if target_s3.head_object(Bucket=args.transfer_bucket, Key=key)["ContentLength"] != output.stat().st_size:
            raise RuntimeError("La copia de transferencia tiene tamaño distinto")
        print(f"Copia cifrada de transferencia: s3://{args.transfer_bucket}/{key}")


def extract_archive(encrypted, root, password):
    root.mkdir(parents=True, exist_ok=True)
    tar_path = root / "backup.tar.gz"
    decrypt_archive(encrypted, tar_path, password)
    with tarfile.open(tar_path, "r:gz") as archive:
        for member in archive.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or (not member.isfile() and not member.isdir()):
                raise RuntimeError("Archivo contiene ruta o tipo inseguro")
        archive.extractall(root, filter="data")
    tar_path.unlink()
    backup_root = root / "backup"
    manifest = json.loads((backup_root / "manifest.json").read_text())
    found = {str(path.relative_to(backup_root)) for path in backup_root.rglob("*") if path.is_file()}
    expected = set(manifest["sha256"]) | {"manifest.json"}
    if found != expected:
        raise RuntimeError("El archivo no coincide con el inventario de checksums")
    for relative, expected in manifest["sha256"].items():
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise RuntimeError("Ruta inválida en manifiesto")
        target = backup_root / relative_path
        if not target.is_file() or sha256(target) != expected:
            raise RuntimeError(f"Checksum inválido: {relative}")
    return backup_root, manifest


def bucket_map_for_restore(manifest, configured_path=None):
    """Collect destination bucket names interactively when no map file was supplied."""
    environments = [item["environment"] for item in manifest["buckets"]]
    if configured_path:
        bucket_map = json.loads(Path(configured_path).read_text())
    else:
        ui.note("Indica los buckets documentales VACÍOS de la cuenta destino, uno por ambiente.")
        bucket_map = {}
        for env in environments:
            bucket_map[env] = ui.text(f"Bucket documental destino para {env}")
    if not isinstance(bucket_map, dict) or set(bucket_map) != set(environments):
        raise RuntimeError(f"El mapa de buckets debe incluir exactamente: {sorted(environments)}")
    if any(not isinstance(value, str) or not value.strip() for value in bucket_map.values()):
        raise RuntimeError("Todos los buckets destino deben tener un nombre")
    if len(set(bucket_map.values())) != len(bucket_map):
        raise RuntimeError("Cada ambiente necesita un bucket destino distinto")
    return bucket_map


def stack_restore_targets(aws, root, manifest):
    """Map source physical resources to the same logical IDs in target stacks."""
    cfn = aws.client("cloudformation")
    table_map = {}
    bucket_map = {}
    managed_tables = set()
    for env in manifest["stacks"]:
        source_resources = json.loads((root / "cloudformation" / env / "resources.json").read_text())
        source_ids = {item["PhysicalResourceId"]: item
                      for item in source_resources if "PhysicalResourceId" in item}
        target_resources = stack_resources(cfn, STACKS[env])
        target_ids = {item["LogicalResourceId"]: item
                      for item in target_resources if "PhysicalResourceId" in item}
        for item in (entry for entry in manifest["tables"] if entry["environment"] == env):
            name = item["name"]
            source_resource = source_ids.get(name)
            if source_resource:
                logical_id = source_resource["LogicalResourceId"]
                target_resource = target_ids.get(logical_id)
                if (source_resource["ResourceType"] != "AWS::DynamoDB::Table" or
                        not target_resource or target_resource["ResourceType"] != "AWS::DynamoDB::Table"):
                    raise RuntimeError(f"Falta {logical_id} en el stack destino {STACKS[env]}")
                table_map[(env, name)] = target_resource["PhysicalResourceId"]
                managed_tables.add((env, name))
            else:
                # Catalog/fallback tables are intentionally imported by name.
                table_map[(env, name)] = name
        for item in (entry for entry in manifest["buckets"] if entry["environment"] == env):
            source_resource = source_ids.get(item["name"])
            logical_id = source_resource["LogicalResourceId"] if source_resource else None
            target_resource = target_ids.get(logical_id)
            if (not source_resource or source_resource["ResourceType"] != "AWS::S3::Bucket" or
                    not target_resource or target_resource["ResourceType"] != "AWS::S3::Bucket"):
                raise RuntimeError(f"No se encontró el bucket documental de {env} en el stack destino")
            bucket_map[env] = target_resource["PhysicalResourceId"]
    for item in manifest["tables"]:
        if item["environment"] == "shared":
            table_map[("shared", item["name"])] = item["name"]
    return table_map, bucket_map, managed_tables


def check_existing_table_empty(ddb, name, source_schema, target_schema):
    if target_schema["KeySchema"] != source_schema["KeySchema"]:
        raise RuntimeError(f"Clave primaria distinta en tabla destino {name}")
    source_attributes = {entry["AttributeName"]: entry["AttributeType"]
                         for entry in source_schema["AttributeDefinitions"]}
    target_attributes = {entry["AttributeName"]: entry["AttributeType"]
                         for entry in target_schema["AttributeDefinitions"]}
    if any(target_attributes.get(key) != value for key, value in source_attributes.items()):
        raise RuntimeError(f"Tipos de atributos clave distintos en tabla destino {name}")
    source_indexes = {index["IndexName"]: index for index in source_schema.get("GlobalSecondaryIndexes", [])}
    target_indexes = {index["IndexName"]: index for index in target_schema.get("GlobalSecondaryIndexes", [])}
    for index_name, source in source_indexes.items():
        target = target_indexes.get(index_name)
        if not target or target["KeySchema"] != source["KeySchema"] or target["Projection"] != source["Projection"]:
            raise RuntimeError(f"GSI distinto o ausente en tabla destino {name}: {index_name}")
    source_local = {index["IndexName"]: index for index in source_schema.get("LocalSecondaryIndexes", [])}
    target_local = {index["IndexName"]: index for index in target_schema.get("LocalSecondaryIndexes", [])}
    for index_name, source in source_local.items():
        target = target_local.get(index_name)
        if not target or target["KeySchema"] != source["KeySchema"] or target["Projection"] != source["Projection"]:
            raise RuntimeError(f"LSI distinto o ausente en tabla destino {name}: {index_name}")
    if ddb.scan(TableName=name, Limit=1, ConsistentRead=True, Select="COUNT").get("Count", 0):
        raise RuntimeError(f"La tabla destino no está vacía: {name}")


def restore(args):
    encrypted = Path(args.archive).expanduser().resolve()
    if not encrypted.is_file():
        raise RuntimeError(f"No existe el respaldo: {encrypted}")
    with tempfile.TemporaryDirectory(prefix="idp-restore-", dir=args.work_dir) as temp:
        with ui.spinner("Verificando y abriendo el respaldo"):
            root, manifest = extract_archive(encrypted, Path(temp), args.password)
        if manifest.get("version") != 2 or manifest.get("method") != "LOCAL_SCAN":
            raise RuntimeError("Formato de respaldo no compatible con la restauración local")
        print("Checksum verificado; cuenta origen:", manifest["sourceAccount"],
              "inicio:", manifest["backupStartedAt"])
        aws = session(args.destination_profile, manifest["region"])
        if account(aws) == manifest["sourceAccount"]:
            raise RuntimeError("El perfil destino apunta a la cuenta origen")
        s3, ddb = aws.client("s3"), aws.client("dynamodb")
        if args.into_stacks:
            table_map, bucket_map, managed_tables = stack_restore_targets(aws, root, manifest)
            ui.note("Se usarán las tablas y buckets de los stacks CDK ya desplegados.")
        else:
            table_map = {(item["environment"], item["name"]): item["name"]
                         for item in manifest["tables"]}
            bucket_map = bucket_map_for_restore(manifest, args.bucket_map)
            managed_tables = set()
        for env, bucket in bucket_map.items():
            s3.head_bucket(Bucket=bucket)
            notifications = s3.get_bucket_notification_configuration(Bucket=bucket)
            if any(key in notifications for key in ("LambdaFunctionConfigurations", "QueueConfigurations", "TopicConfigurations", "EventBridgeConfiguration")):
                raise RuntimeError(f"Bucket {bucket} tiene notificaciones; desactívalas antes de restaurar")
            if s3.list_objects_v2(Bucket=bucket, MaxKeys=1).get("KeyCount"):
                raise RuntimeError(f"Bucket destino no vacío: {bucket}")
        for item in manifest["tables"]:
            env, name = item["environment"], item["name"]
            target_name = table_map[(env, name)]
            schema = json.loads((root / "dynamodb" / env / name / "schema.json").read_text())
            try:
                target_schema = ddb.describe_table(TableName=target_name)["Table"]
            except ddb.exceptions.ResourceNotFoundException:
                if (env, name) in managed_tables:
                    raise RuntimeError(f"Falta la tabla CDK destino: {target_name}")
                target_schema = None
            if target_schema:
                if not args.into_stacks:
                    raise RuntimeError(f"La tabla destino ya existe: {target_name}")
                check_existing_table_empty(ddb, target_name, schema, target_schema)
                if (env == "prod" and schema.get("SSEDescription", {}).get("SSEType") == "KMS"
                        and target_schema.get("SSEDescription", {}).get("SSEType") != "KMS"):
                    raise RuntimeError(f"La tabla prod destino no usa KMS: {target_name}")
            data = root / "dynamodb" / env / name / "items.jsonl.gz"
            if not data.is_file():
                raise RuntimeError(f"Datos locales ausentes: {name}")
            if (not args.into_stacks and env == "prod"
                    and schema.get("SSEDescription", {}).get("SSEType") == "KMS"
                    and not args.table_kms_key):
                raise RuntimeError("--table-kms-key es obligatorio para tablas prod cifradas con KMS")
        print(f"Destino: cuenta {account(aws)}; región {manifest['region']}.")
        print(f"Se cargarán {len(manifest['tables'])} tablas y {len(manifest['buckets'])} buckets.")
        for env, bucket in bucket_map.items():
            print(f"  {env} → {bucket}")
        for item in manifest["tables"]:
            print(f"  {item['environment']} {item['name']} → {table_map[(item['environment'], item['name'])]}")
        confirm(f"RESTAURAR {account(aws)}")
        for item in manifest["tables"]:
            env, name = item["environment"], item["name"]
            target_name = table_map[(env, name)]
            folder = root / "dynamodb" / env / name
            schema = json.loads((folder / "schema.json").read_text())
            data = folder / "items.jsonl.gz"
            attributes = {part["AttributeName"] for part in schema["KeySchema"]}
            for index in schema.get("GlobalSecondaryIndexes", []) + schema.get("LocalSecondaryIndexes", []):
                attributes.update(part["AttributeName"] for part in index["KeySchema"])
            billing_mode = schema.get("BillingModeSummary", {}).get("BillingMode") or (
                "PROVISIONED" if schema.get("ProvisionedThroughput", {}).get("ReadCapacityUnits", 0) else "PAY_PER_REQUEST")
            params = {"TableName": target_name, "AttributeDefinitions": [a for a in schema["AttributeDefinitions"] if a["AttributeName"] in attributes],
                      "KeySchema": schema["KeySchema"], "BillingMode": billing_mode}
            if params["BillingMode"] == "PROVISIONED":
                throughput = schema["ProvisionedThroughput"]
                params["ProvisionedThroughput"] = {key: throughput[key] for key in
                    ("ReadCapacityUnits", "WriteCapacityUnits")}
            if schema.get("GlobalSecondaryIndexes"):
                params["GlobalSecondaryIndexes"] = [
                    {"IndexName": i["IndexName"], "KeySchema": i["KeySchema"],
                     "Projection": i["Projection"],
                     **({"ProvisionedThroughput": {key: i["ProvisionedThroughput"][key] for key in
                         ("ReadCapacityUnits", "WriteCapacityUnits")}}
                        if params["BillingMode"] == "PROVISIONED" else {})}
                    for i in schema["GlobalSecondaryIndexes"]]
            if schema.get("LocalSecondaryIndexes"):
                params["LocalSecondaryIndexes"] = [
                    {"IndexName": i["IndexName"], "KeySchema": i["KeySchema"],
                     "Projection": i["Projection"]} for i in schema["LocalSecondaryIndexes"]]
            if args.table_kms_key and env == "prod" and not args.into_stacks:
                params["SSESpecification"] = {"Enabled": True, "SSEType": "KMS",
                                               "KMSMasterKeyId": args.table_kms_key}
            if not args.into_stacks or (env, name) not in managed_tables:
                try:
                    ddb.describe_table(TableName=target_name)
                except ddb.exceptions.ResourceNotFoundException:
                    ddb.create_table(**params)
                    ddb.get_waiter("table_exists").wait(TableName=target_name)
            with ui.spinner(f"Restaurando filas de {target_name}"):
                count = restore_items(ddb, target_name, data)
            if count != item["itemCount"]:
                raise RuntimeError(f"Conteo de filas no coincide al restaurar {name}: {count}")
            print("Restaurada:", name, count, "filas")
            original_pitr = json.loads((folder / "pitr.json").read_text())
            if original_pitr["PointInTimeRecoveryStatus"] == "ENABLED":
                current_pitr = ddb.describe_continuous_backups(TableName=target_name)["ContinuousBackupsDescription"]["PointInTimeRecoveryDescription"]
                if current_pitr["PointInTimeRecoveryStatus"] != "ENABLED":
                    ddb.update_continuous_backups(TableName=target_name,
                        PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True})
            original_ttl = json.loads((folder / "ttl.json").read_text())
            if original_ttl.get("TimeToLiveStatus") in ("ENABLED", "ENABLING"):
                current_ttl = ddb.describe_time_to_live(TableName=target_name)["TimeToLiveDescription"]
                if current_ttl.get("TimeToLiveStatus") not in ("ENABLED", "ENABLING"):
                    ddb.update_time_to_live(TableName=target_name, TimeToLiveSpecification={
                        "Enabled": True, "AttributeName": original_ttl["AttributeName"]})
            ddb.get_waiter("table_exists").wait(TableName=target_name)
            ddb.update_table(TableName=target_name, DeletionProtectionEnabled=True)
        for item in manifest["buckets"]:
            env, source = item["environment"], item["name"]
            target = bucket_map[env]
            files = sorted((root / "s3" / env).rglob("*"))
            for path in files:
                if path.is_file():
                    key = str(path.relative_to(root / "s3" / env))
                    extra = item.get("objectMetadata", {}).get(key, {})
                    kwargs = {"ExtraArgs": extra} if extra else {}
                    s3.upload_file(str(path), target, key, **kwargs)
                    if s3.head_object(Bucket=target, Key=key)["ContentLength"] != path.stat().st_size:
                        raise RuntimeError(f"Tamaño S3 distinto tras restaurar: {target}/{key}")
            print("Restaurado S3:", source, "→", target)
        if args.into_stacks:
            print("Datos cargados en los stacks CDK. Activa el ingreso S3 con el deploy final y valida la aplicación.")
        else:
            print("Datos cargados. Falta adoptar tablas en IaC y validar la aplicación.")


def deploy_stacks(args):
    target_aws = session(args.destination_profile, args.region)
    with ui.spinner("Comprobando identidad de la cuenta destino"):
        destination_account = account(target_aws)
    ui.success(f"Cuenta destino: {destination_account} · región {args.region}")
    selected = ["dev", "prod"] if args.env == "both" else [args.env]
    phase_text = "PAUSADO" if args.phase == "initial" else "ACTIVO"
    ui.warning(f"Se desplegarán {', '.join(selected)} en {destination_account} con ingreso S3 {phase_text}.")
    if args.phase == "initial":
        ui.note("Antes de desplegar, usa configuración y certificado ACM de la cuenta destino.")
    confirm(f"DESPLEGAR {destination_account} {phase_text}")
    environment = os.environ.copy()
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                "AWS_SECURITY_TOKEN", "AWS_DEFAULT_PROFILE", "AWS_ROLE_ARN",
                "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
                "AWS_CONTAINER_CREDENTIALS_FULL_URI", "CDK_DEFAULT_ACCOUNT",
                "CDK_DEFAULT_REGION"):
        environment.pop(key, None)
    environment["AWS_PROFILE"] = args.destination_profile
    environment["AWS_REGION"] = args.region
    environment["AWS_DEFAULT_REGION"] = args.region
    if args.phase == "initial":
        environment["MIGRATION_PAUSE_INGRESS"] = "1"
    else:
        environment["MIGRATION_PAUSE_INGRESS"] = "0"
    subprocess_account = subprocess.run(
        ["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text",
         "--region", args.region], check=True, env=environment, capture_output=True, text=True
    ).stdout.strip()
    if subprocess_account != destination_account:
        raise RuntimeError(f"CDK usaría la cuenta {subprocess_account}, pero el perfil eligió {destination_account}")
    for env in selected:
        ui.heading(f"CDK {env} · ingreso {phase_text}")
        subprocess.run(["pnpm", "run", f"deploy:{env}"], check=True, env=environment)
    ui.success("Despliegue terminado. " + ("Ahora restaura los datos en los stacks." if args.phase == "initial"
                                     else "Valida datos y API antes de cambiar el tráfico."))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="Inventario y preparación, solo lectura")
    plan.add_argument("--source-profile")
    plan.add_argument("--region", default="us-east-1")
    plan.add_argument("--env", choices=("prod", "dev", "both"), default="both")
    backup_parser = sub.add_parser("backup", help="Leer DynamoDB/S3 directamente y cifrar localmente")
    backup_parser.add_argument("--source-profile")
    backup_parser.add_argument("--region", default="us-east-1")
    backup_parser.add_argument("--env", choices=("prod", "dev", "both"), default="both")
    backup_parser.add_argument("--output", help="Archivo cifrado final; por defecto en tu carpeta personal")
    backup_parser.add_argument("--work-dir", help="Directorio para archivos temporales; por defecto /tmp")
    backup_parser.add_argument("--second-copy", help="Ruta opcional para una copia del mismo archivo cifrado")
    backup_parser.add_argument("--transfer-bucket", help="Bucket opcional en destino para copiar el archivo cifrado")
    backup_parser.add_argument("--destination-profile", help="Perfil de la cuenta destino para la transferencia")
    restore_parser = sub.add_parser("restore", help="Verificar y cargar datos directamente en DynamoDB/S3")
    restore_parser.add_argument("--archive")
    restore_parser.add_argument("--destination-profile")
    restore_parser.add_argument("--bucket-map", help="JSON opcional: ambiente a bucket documental vacío de destino")
    restore_parser.add_argument("--table-kms-key", help="ARN de la CMK destino para cifrar tablas importadas")
    restore_parser.add_argument("--work-dir", help="Directorio para archivos temporales; por defecto /tmp")
    restore_parser.add_argument("--into-stacks", action="store_true",
                                help="Cargar datos en los stacks CDK ya desplegados con ingreso S3 pausado")
    deploy_parser = sub.add_parser("deploy", help="Desplegar CDK interactivamente en la cuenta destino")
    deploy_parser.add_argument("--destination-profile")
    deploy_parser.add_argument("--region", default="us-east-1")
    deploy_parser.add_argument("--env", choices=("prod", "dev", "both"), default="both")
    deploy_parser.add_argument("--phase", choices=("initial", "activate"))
    args = parser.parse_args()
    try:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise RuntimeError("Este comando requiere una terminal interactiva (TTY)")
        ui.heading("Migración de cuenta AWS · " + args.command)
        if args.command in ("plan", "backup"):
            args.source_profile = choose_profile("Perfil AWS de origen", args.source_profile)
            args.region = ui.text("Región del stack", default=args.region,
                                  validate=lambda value: None if re.fullmatch(r'[a-z]{2}(?:-gov)?-[a-z]+-\d+', value) else "Usa una región AWS válida, por ejemplo us-east-1.")
            args.env = ui.select("Ambiente", [("both", "Ambos: producción y desarrollo"),
                                                  ("prod", "Sólo producción"), ("dev", "Sólo desarrollo")],
                                 default=args.env)
            with ui.spinner("Comprobando identidad de la cuenta origen"):
                source_account = account(session(args.source_profile, args.region))
            ui.success(f"Cuenta AWS verificada: {source_account}")
        if args.command == "plan":
            confirm(f"CONSULTAR {args.source_profile}")
            print_plan(session(args.source_profile, args.region), ["prod", "dev"] if args.env == "both" else [args.env])
        elif args.command == "backup":
            args.environments = ["prod", "dev"] if args.env == "both" else [args.env]
            args.output = args.output or default_backup_path()
            args.work_dir = args.work_dir or tempfile.gettempdir()
            if shutil.which("openssl") is None:
                raise RuntimeError("Instala openssl para cifrar el respaldo con contraseña")
            print("Elige una contraseña nueva para cifrar este respaldo; la necesitarás al restaurar.")
            args.password = ask_password(confirm_password=True)
            ui.note("El respaldo cifrado se guardará en tu carpeta personal; puedes cambiar la ruta.")
            args.output = ui.path("Ruta del archivo cifrado de salida", default=args.output,
                                  kind="file", must_not_exist=True)
            ui.warning("Durante la copia habrá datos sin cifrar en la carpeta temporal. /tmp puede estar en un disco sin cifrar; asegúrate de tener espacio suficiente o elige un volumen cifrado.")
            args.work_dir = ui.path("Carpeta para archivos temporales", default=args.work_dir,
                                    kind="directory", must_exist=True)
            ui.note("La segunda copia es idéntica al archivo cifrado. Es opcional ahora; para el corte final, guárdala en otro disco o cuenta.")
            args.second_copy = ui.path("Segunda copia cifrada (opcional)", default=args.second_copy,
                                       required=False, kind="file", must_not_exist=True)
            if args.second_copy and os.stat(Path(args.output).parent).st_dev == os.stat(Path(args.second_copy).parent).st_dev:
                ui.warning("Ambas rutas están en el mismo sistema de archivos; esta copia no protege frente a su pérdida.")
            args.transfer_bucket = ui.text("Bucket de transferencia en destino (opcional)",
                                           default=args.transfer_bucket, required=False)
            if args.transfer_bucket:
                args.destination_profile = choose_profile("Perfil AWS de destino", args.destination_profile)
            backup(args)
        elif args.command == "deploy":
            args.destination_profile = choose_profile("Perfil AWS de la cuenta destino", args.destination_profile)
            while args.destination_profile == "actual":
                ui.error("Para desplegar elige un perfil AWS con nombre, no credenciales implícitas.")
                args.destination_profile = choose_profile("Perfil AWS de la cuenta destino")
            args.region = ui.text("Región del stack", default=args.region,
                                  validate=lambda value: None if re.fullmatch(r'[a-z]{2}(?:-gov)?-[a-z]+-\d+', value) else "Usa una región AWS válida, por ejemplo us-east-1.")
            args.env = ui.select("Ambiente", [("both", "Ambos: desarrollo y producción"),
                                                 ("dev", "Sólo desarrollo"), ("prod", "Sólo producción")],
                                 default=args.env)
            args.phase = ui.select("Fase del despliegue", [
                ("initial", "Inicial: crear infraestructura sin notificaciones S3"),
                ("activate", "Activar: conectar notificaciones S3 tras restaurar"),
            ], default=args.phase or "initial")
            deploy_stacks(args)
        else:
            args.work_dir = args.work_dir or tempfile.gettempdir()
            if shutil.which("openssl") is None:
                raise RuntimeError("Instala openssl para abrir el respaldo cifrado")
            args.archive = ui.path("Archivo de respaldo cifrado", default=args.archive,
                                   kind="file", must_exist=True)
            ui.warning("El archivo se abrirá temporalmente sin cifrar. /tmp puede estar en un disco sin cifrar; asegúrate de tener espacio suficiente o elige un volumen cifrado.")
            args.work_dir = ui.path("Carpeta para archivos temporales", default=args.work_dir,
                                    kind="directory", must_exist=True)
            args.password = ask_password()
            args.destination_profile = choose_profile("Perfil AWS de destino", args.destination_profile)
            args.into_stacks = ui.select("Tipo de destino", [
                ("stack", "Stacks CDK ya desplegados, con ingreso S3 pausado"),
                ("new", "Tablas nuevas fuera de CDK y buckets preparados manualmente"),
            ], default="stack") == "stack"
            with ui.spinner("Comprobando identidad de la cuenta destino"):
                destination_account = account(session(args.destination_profile, "us-east-1"))
            ui.success(f"Cuenta AWS de destino verificada: {destination_account}")
            if args.bucket_map and not args.into_stacks:
                args.bucket_map = ui.path("JSON de buckets destino", default=args.bucket_map,
                                          kind="file", must_exist=True,
                                          validate=lambda value: None if _valid_bucket_map(value) else "El archivo debe contener un objeto JSON con buckets por ambiente.")
            if not args.into_stacks:
                args.table_kms_key = ask("ARN CMK para tablas prod (Enter si no aplica)", args.table_kms_key, required=False)
            restore(args)
    except (RuntimeError, BotoCoreError, ClientError, subprocess.CalledProcessError, EOFError,
            KeyboardInterrupt, OSError, ValueError) as error:
        print("ERROR:", error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
