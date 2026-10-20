"""Checks for the local migration archive and DynamoDB item round trip."""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tarfile
from argparse import Namespace
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "aws_migration.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("aws_migration", SCRIPT)
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


def test_scan_and_restore_round_trip_including_binary_and_pagination(tmp_path):
    original = {"Pk": {"S": "a"}, "Nested": {"M": {"Bytes": {"B": b"\x00\xff"}}}}

    class Source:
        calls = []

        def scan(self, **kwargs):
            self.calls.append(kwargs)
            if "ExclusiveStartKey" not in kwargs:
                return {"Items": [original], "LastEvaluatedKey": {"Pk": {"S": "a"}}}
            return {"Items": [{"Pk": {"S": "b"}}]}

    class Destination:
        writes = []

        def batch_write_item(self, **kwargs):
            self.writes.extend(kwargs["RequestItems"]["table"])
            return {"UnprocessedItems": {}}

    source, destination = Source(), Destination()
    path = tmp_path / "items.jsonl.gz"
    assert migration.scan_to_file(source, "table", path) == 2
    assert all(call["ConsistentRead"] is True for call in source.calls)
    assert migration.restore_items(destination, "table", path) == 2
    assert destination.writes[0]["PutRequest"]["Item"] == original


def test_batch_write_retries_unprocessed_items(monkeypatch):
    batch = [{"PutRequest": {"Item": {"Pk": {"S": "a"}}}}]

    class Destination:
        calls = 0

        def batch_write_item(self, RequestItems):
            self.calls += 1
            return {"UnprocessedItems": RequestItems if self.calls == 1 else {}}

    monkeypatch.setattr(migration.time, "sleep", lambda _: None)
    target = Destination()
    migration.write_batch(target, "table", batch)
    assert target.calls == 2


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl no está instalado")
def test_password_encryption_round_trip(tmp_path):
    original = tmp_path / "plain.txt"
    encrypted = tmp_path / "backup.enc"
    restored = tmp_path / "restored.txt"
    original.write_bytes(b"documentos confidenciales\n")
    migration.encrypt_archive(original, encrypted, "contrasena-fuerte-123")
    assert encrypted.read_bytes() != original.read_bytes()
    migration.decrypt_archive(encrypted, restored, "contrasena-fuerte-123")
    assert restored.read_bytes() == original.read_bytes()
    with pytest.raises(RuntimeError, match="Contraseña incorrecta"):
        migration.decrypt_archive(encrypted, tmp_path / "wrong.txt", "otra-contrasena-123")


def test_archive_rejects_tampered_file_before_restore(tmp_path, monkeypatch):
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / "data.json").write_text("changed")
    (backup / "manifest.json").write_text(json.dumps({"sha256": {"data.json": "0" * 64}}))
    archive_path = tmp_path / "payload.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(backup, arcname="backup")

    def fake_decrypt(encrypted, output, password):
        output.write_bytes(archive_path.read_bytes())

    monkeypatch.setattr(migration, "decrypt_archive", fake_decrypt)
    with pytest.raises(RuntimeError, match="Checksum inválido"):
        migration.extract_archive(tmp_path / "encrypted.enc", tmp_path / "unpack", "password12345")


def test_archive_rejects_path_traversal(tmp_path, monkeypatch):
    content = tmp_path / "file"
    content.write_text("bad")
    archive_path = tmp_path / "payload.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(content, arcname="../outside")

    def fake_decrypt(encrypted, output, password):
        output.write_bytes(archive_path.read_bytes())

    monkeypatch.setattr(migration, "decrypt_archive", fake_decrypt)
    with pytest.raises(RuntimeError, match="ruta o tipo inseguro"):
        migration.extract_archive(tmp_path / "encrypted.enc", tmp_path / "unpack", "password12345")


def test_plan_requires_tty_before_any_aws_call(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["aws_migration.py", "plan"])
    monkeypatch.setattr(migration.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(migration, "session", lambda *args: pytest.fail("AWS consultado sin TTY"))
    assert migration.main() == 2


def test_confirmation_rejects_any_other_answer(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "si")
    with pytest.raises(RuntimeError, match="cancelada"):
        migration.confirm("RESPALDAR 123456789012")


def test_profile_selector_offers_configured_names_and_explains_account_id(monkeypatch):
    class FakeSession:
        available_profiles = ["default", "alsuper"]

    monkeypatch.setattr(migration.boto3, "Session", lambda: FakeSession())
    notes = []
    monkeypatch.setattr(migration.ui, "note", notes.append)
    def select(label, options, **kwargs):
        assert label == "Perfil AWS de origen"
        assert [value for value, _ in options] == ["actual", "default", "alsuper"]
        return "alsuper"
    monkeypatch.setattr(migration.ui, "select", select)
    assert migration.choose_profile("Perfil AWS de origen") == "alsuper"
    assert "ID numérico de la cuenta AWS" in notes[0]


def test_restore_collects_bucket_names_interactively_without_json(monkeypatch):
    manifest = {"buckets": [{"environment": "prod"}, {"environment": "dev"}]}
    answers = iter(["bucket-prod-nuevo", "bucket-dev-nuevo"])
    monkeypatch.setattr(migration.ui, "text", lambda label: next(answers))
    monkeypatch.setattr(migration.ui, "note", lambda message: None)
    assert migration.bucket_map_for_restore(manifest) == {
        "prod": "bucket-prod-nuevo", "dev": "bucket-dev-nuevo"
    }


def test_restore_maps_to_stack_owned_physical_resources(tmp_path, monkeypatch):
    folder = tmp_path / "cloudformation" / "prod"
    folder.mkdir(parents=True)
    folder.joinpath("resources.json").write_text(json.dumps([
        {"LogicalResourceId": "DocumentTable", "PhysicalResourceId": "old-table", "ResourceType": "AWS::DynamoDB::Table"},
        {"LogicalResourceId": "DocumentBucket", "PhysicalResourceId": "old-bucket", "ResourceType": "AWS::S3::Bucket"},
    ]))
    monkeypatch.setattr(migration, "stack_resources", lambda cfn, name: [
        {"LogicalResourceId": "DocumentTable", "PhysicalResourceId": "new-table", "ResourceType": "AWS::DynamoDB::Table"},
        {"LogicalResourceId": "DocumentBucket", "PhysicalResourceId": "new-bucket", "ResourceType": "AWS::S3::Bucket"},
    ])
    class AWS:
        def client(self, name):
            assert name == "cloudformation"
            return object()
    manifest = {"stacks": {"prod": "DocumentValidationStack"},
                "tables": [{"environment": "prod", "name": "old-table"}],
                "buckets": [{"environment": "prod", "name": "old-bucket"}]}
    tables, buckets, managed = migration.stack_restore_targets(AWS(), tmp_path, manifest)
    assert tables == {("prod", "old-table"): "new-table"}
    assert buckets == {"prod": "new-bucket"}
    assert managed == {("prod", "old-table")}


def test_migration_deploy_uses_selected_profile_and_pauses_ingress(monkeypatch):
    calls = []
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "source-key")
    monkeypatch.setenv("CDK_DEFAULT_ACCOUNT", "source-account")
    monkeypatch.setattr(migration, "session", lambda profile, region: object())
    monkeypatch.setattr(migration, "account", lambda aws: "222222222222")
    monkeypatch.setattr(migration.ui, "spinner", lambda message: nullcontext())
    monkeypatch.setattr(migration, "confirm", lambda phrase: None)
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout="222222222222\n")
    monkeypatch.setattr(migration.subprocess, "run", run)
    migration.deploy_stacks(Namespace(destination_profile="destino", region="us-east-1",
                                      env="dev", phase="initial"))
    command, kwargs = calls[1]
    assert command == ["pnpm", "run", "deploy:dev"]
    assert kwargs["env"]["AWS_PROFILE"] == "destino"
    assert kwargs["env"]["MIGRATION_PAUSE_INGRESS"] == "1"
    assert "AWS_ACCESS_KEY_ID" not in kwargs["env"]
    assert "CDK_DEFAULT_ACCOUNT" not in kwargs["env"]
    migration.deploy_stacks(Namespace(destination_profile="destino", region="us-east-1",
                                      env="prod", phase="activate"))
    assert calls[3][0] == ["pnpm", "run", "deploy:prod"]
    assert calls[3][1]["env"]["MIGRATION_PAUSE_INGRESS"] == "0"


def test_stack_restore_rejects_nonempty_destination_table():
    class DynamoDB:
        def scan(self, **kwargs):
            assert kwargs["ConsistentRead"] is True
            return {"Count": 1}
    schema = {"KeySchema": [{"AttributeName": "DocumentId", "KeyType": "HASH"}],
              "AttributeDefinitions": [{"AttributeName": "DocumentId", "AttributeType": "S"}]}
    with pytest.raises(RuntimeError, match="no está vacía"):
        migration.check_existing_table_empty(DynamoDB(), "target", schema, schema)


def test_stack_restore_rejects_different_key_type_before_scanning():
    class DynamoDB:
        def scan(self, **kwargs):
            pytest.fail("No debe consultar filas si el esquema es incompatible")

    schema = {"KeySchema": [{"AttributeName": "DocumentId", "KeyType": "HASH"}],
              "AttributeDefinitions": [{"AttributeName": "DocumentId", "AttributeType": "S"}]}
    target = {**schema, "AttributeDefinitions": [{"AttributeName": "DocumentId", "AttributeType": "N"}]}
    with pytest.raises(RuntimeError, match="Tipos de atributos clave"):
        migration.check_existing_table_empty(DynamoDB(), "target", schema, target)
