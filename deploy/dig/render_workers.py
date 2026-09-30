#!/usr/bin/env python3
"""Derive REVEAL's background ECS stack from its rendered DIG API stack.

The API, dispatcher and workers must use the same image, environment, secrets
and data permissions. This renderer copies those from the API instead of
maintaining another runtime configuration. It does not contact or deploy AWS.
The output has concrete configuration values and takes no stack parameters.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
from pathlib import Path
from typing import Any


def parameter_values(rows: list[dict[str, str]]) -> dict[str, str]:
    values: dict[str, str] = {}
    for row in rows:
        key, value = row.get("ParameterKey"), row.get("ParameterValue")
        if not isinstance(key, str) or not isinstance(value, str) or key in values:
            raise ValueError("API parameters must have unique string keys and explicit string values")
        values[key] = value
    return values


def _resolve(value: Any, parameters: dict[str, str], resources: dict[str, str]) -> Any:
    """Resolve input parameters and remap CFN references into the derived stack."""
    if isinstance(value, list):
        return [_resolve(item, parameters, resources) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"Ref"}:
        reference = value["Ref"]
        if reference in parameters:
            return parameters[reference]
        return {"Ref": resources.get(reference, reference)}
    if set(value) == {"Fn::GetAtt"}:
        attribute = value["Fn::GetAtt"]
        name, suffix = attribute.split(".", 1) if isinstance(attribute, str) else attribute
        return {"Fn::GetAtt": [resources.get(name, name), suffix]}
    if set(value) == {"Fn::Sub"}:
        expression = value["Fn::Sub"]
        template, variables = (expression, {}) if isinstance(expression, str) else expression

        def replace(match: re.Match[str]) -> str:
            token = match.group(1)
            if token in variables or token.startswith("!"):
                return match.group(0)
            if token in parameters:
                return parameters[token]
            head, separator, tail = token.partition(".")
            return "${" + resources.get(head, head) + separator + tail + "}"

        template = re.sub(r"\$\{([^}]+)\}", replace, template)
        if variables:
            return {"Fn::Sub": [template, _resolve(variables, parameters, resources)]}
        return {"Fn::Sub": template} if "${" in template else template
    return {key: _resolve(item, parameters, resources) for key, item in value.items()}


def render(api_template: dict[str, Any], api_parameters: list[dict[str, str]]) -> dict[str, Any]:
    parameters = parameter_values(api_parameters)
    environment = parameters.get("Env")
    if parameters.get("ServiceName") != "reveal" or environment not in {"qa", "prod"}:
        raise ValueError("Only the reveal service in qa or prod can produce a background stack")
    image = parameters.get("ImageUri", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9._-]+|@sha256:[a-f0-9]{64})", image):
        raise ValueError("ImageUri must be an explicit image tag or sha256 digest")
    definitions = api_template.get("Parameters", {})
    missing = set(definitions) - parameters.keys()
    for name in sorted(missing):
        if "Default" not in definitions[name]:
            raise ValueError(f"Missing API parameter: {name}")
        parameters[name] = str(definitions[name]["Default"])
    source = api_template.get("Resources", {})
    required = {"TaskDefinition", "TaskRole", "ExecutionRole", "TaskSecurityGroup", "LogGroup", "Service"}
    if not required <= source.keys():
        raise ValueError("Rendered API template is missing required ECS resources")
    task = source["TaskDefinition"]["Properties"]
    containers = task.get("ContainerDefinitions", [])
    if len(containers) != 1:
        raise ValueError("Expected exactly one API container")
    container = containers[0]
    if _resolve(container.get("Image"), parameters, {}) != image:
        raise ValueError("API container image differs from ImageUri")
    if container.get("ReadonlyRootFilesystem") is not True:
        raise ValueError("API container must use a read-only root filesystem")
    mounts = container.get("LinuxParameters", {}).get("Tmpfs", [])
    for path in ("/work", "/tmp"):
        if not any(mount.get("ContainerPath") == path and isinstance(mount.get("Size"), int)
                   and mount["Size"] > 0 for mount in mounts):
            raise ValueError(f"API container requires RAM-backed tmpfs at {path}")
    if any(not mount.get("ReadOnly") for mount in container.get("MountPoints", [])):
        raise ValueError("Writable disk mount points are not permitted")
    secrets = container.get("Secrets", [])
    if not any(secret.get("Name") == "REVEAL_REDIS_URL" and isinstance(secret.get("ValueFrom"), str)
               and ":secretsmanager:" in secret["ValueFrom"] for secret in secrets):
        raise ValueError("REVEAL_REDIS_URL must come from Secrets Manager")
    if any(entry.get("Name") == "REVEAL_REDIS_URL" for entry in container.get("Environment", [])):
        raise ValueError("REVEAL_REDIS_URL cannot be a plaintext environment variable")

    result: dict[str, Any] = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "REVEAL dispatcher and worker pool; derived from the DIG API image and runtime configuration.",
        "Resources": {},
        "Outputs": {},
    }
    for section in ("Mappings", "Conditions"):
        if section in api_template:
            result[section] = _resolve(copy.deepcopy(api_template[section]), parameters, {})
    output_resources = result["Resources"]
    security_group = _resolve(copy.deepcopy(source["TaskSecurityGroup"]), parameters, {})
    security_group["Properties"].update({
        "GroupName": f"svc-reveal-{environment}-background-sg",
        "GroupDescription": "REVEAL workers and dispatcher; no inbound connections",
        "SecurityGroupIngress": [],
    })
    output_resources["BackgroundSecurityGroup"] = security_group
    result["Outputs"]["BackgroundSecurityGroupId"] = {
        "Description": "Authorize this SG separately on the Redis and RDS security groups.",
        "Value": {"Ref": "BackgroundSecurityGroup"},
    }
    for label, service, module, count, cpu, memory in (
        ("Worker", "workers", "worker", 2, "1024", "3072"),
        ("Dispatcher", "dispatcher", "dispatcher", 1, "256", "1024"),
    ):
        names = {key: label + key for key in ("TaskDefinition", "TaskRole", "ExecutionRole", "LogGroup", "Service")}
        names["TaskSecurityGroup"] = "BackgroundSecurityGroup"
        for key in ("TaskRole", "ExecutionRole", "LogGroup", "TaskDefinition"):
            output_resources[names[key]] = _resolve(copy.deepcopy(source[key]), parameters, names)
        stem = f"svc-reveal-{environment}-{service}"
        for key, suffix in (("TaskRole", "task"), ("ExecutionRole", "exec")):
            output_resources[names[key]]["Properties"]["RoleName"] = f"{stem}-{suffix}"
        output_resources[names["LogGroup"]]["Properties"]["LogGroupName"] = f"/dig/svc-reveal/{environment}/{service}"
        background_task = output_resources[names["TaskDefinition"]]["Properties"]
        background_task.update({"Family": stem, "Cpu": cpu, "Memory": memory})
        background_container = background_task["ContainerDefinitions"][0]
        background_container.pop("PortMappings", None)
        background_container.update({
            "Command": ["python", "-m", f"reveal_backend.{module}"],
            "StopTimeout": 120,
            "HealthCheck": {
                "Command": ["CMD", "python", "-m", "reveal_backend.deployment", "health", module],
                "Interval": 30, "Timeout": 20, "Retries": 3, "StartPeriod": 120,
            },
        })
        network = _resolve(copy.deepcopy(source["Service"]["Properties"]["NetworkConfiguration"]), parameters, names)
        output_resources[names["Service"]] = {
            "Type": "AWS::ECS::Service",
            "Properties": {
                "ServiceName": stem,
                "Cluster": _resolve(source["Service"]["Properties"]["Cluster"], parameters, names),
                "TaskDefinition": {"Ref": names["TaskDefinition"]},
                "CapacityProviderStrategy": [{"CapacityProvider": "FARGATE", "Weight": 1}],
                "PlatformVersion": "LATEST", "DesiredCount": count,
                "EnableExecuteCommand": False,
                "DeploymentConfiguration": {
                    "MinimumHealthyPercent": 100, "MaximumPercent": 200,
                    "DeploymentCircuitBreaker": {"Enable": True, "Rollback": True},
                },
                "NetworkConfiguration": network,
                "PropagateTags": "SERVICE",
            },
        }
        for key, value in (
            ("TaskDefinitionArn", {"Ref": names["TaskDefinition"]}),
            ("TaskRoleArn", {"Fn::GetAtt": [names["TaskRole"], "Arn"]}),
            ("ExecutionRoleArn", {"Fn::GetAtt": [names["ExecutionRole"], "Arn"]}),
            ("ServiceName", {"Fn::GetAtt": [names["Service"], "Name"]}),
        ):
            result["Outputs"][label + key] = {"Value": value}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-template", type=Path, required=True)
    parser.add_argument("--api-params", type=Path, required=True)
    parser.add_argument("--out-template", type=Path, required=True)
    args = parser.parse_args()
    try:
        template = render(json.loads(args.api_template.read_text()), json.loads(args.api_params.read_text()))
    except (ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    args.out_template.parent.mkdir(parents=True, exist_ok=True)
    args.out_template.write_text(json.dumps(template, indent=2) + "\n")


if __name__ == "__main__":
    main()
