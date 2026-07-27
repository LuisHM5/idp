"""Focused tests for dev/prod workflow resolution in ver-resultado.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import Mock


SCRIPT = Path(__file__).parents[2] / "scripts" / "ver-resultado.py"
SPEC = importlib.util.spec_from_file_location("ver_resultado", SCRIPT)
assert SPEC and SPEC.loader
ver_resultado = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ver_resultado)


def _stack_response(state_machine_arn: str) -> dict:
    return {
        "StackResources": [
            {"ResourceType": "AWS::S3::Bucket", "PhysicalResourceId": "bucket"},
            {
                "ResourceType": "AWS::StepFunctions::StateMachine",
                "PhysicalResourceId": state_machine_arn,
            },
        ]
    }


def test_find_state_machine_defaults_to_dev(monkeypatch):
    aws = Mock(return_value=_stack_response("arn:dev"))
    monkeypatch.setattr(ver_resultado, "aws", aws)

    assert ver_resultado.find_state_machine("us-east-1") == "arn:dev"
    assert ver_resultado.DEV_STACK in aws.call_args.args[0]


def test_find_state_machine_resolves_prod(monkeypatch):
    aws = Mock(return_value=_stack_response("arn:prod"))
    monkeypatch.setattr(ver_resultado, "aws", aws)

    assert ver_resultado.find_state_machine("us-east-1", "prod") == "arn:prod"
    assert ver_resultado.PROD_STACK in aws.call_args.args[0]


def test_explicit_stack_wins_over_environment(monkeypatch):
    aws = Mock(return_value=_stack_response("arn:custom"))
    monkeypatch.setattr(ver_resultado, "aws", aws)

    result = ver_resultado.find_state_machine(
        "us-east-1", "prod", "DocumentValidationStack-Custom"
    )

    assert result == "arn:custom"
    assert "DocumentValidationStack-Custom" in aws.call_args.args[0]
