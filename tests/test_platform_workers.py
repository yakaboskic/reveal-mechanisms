"""Deployment boundaries for the derived dispatcher/worker pool and network prerequisite."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("render_workers", ROOT / "deploy/dig/render_workers.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
IMAGE = "005901288866.dkr.ecr.us-east-1.amazonaws.com/dig-reveal:" + "a" * 40
SECRET = "arn:aws:secretsmanager:us-east-1:005901288866:secret:reveal/backend-AbCdEf"


@pytest.fixture
def api():
    values = {"Env": "qa", "ServiceName": "reveal", "ImageUri": IMAGE, "Cpu": "1024", "Memory": "4096"}
    parameters = [{"ParameterKey": key, "ParameterValue": value} for key, value in values.items()]
    role = {"Type": "AWS::IAM::Role", "Properties": {
        "RoleName": {"Fn::Sub": "svc-${ServiceName}-${Env}-task"},
        "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": []},
        "Policies": [{"PolicyName": "ArtifactAccess", "PolicyDocument": {
            "Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::cyaka-reveal-data/*"}]}}],
    }}
    template = {"Parameters": {key: {"Type": "String"} for key in values}, "Resources": {
        "TaskRole": copy.deepcopy(role), "ExecutionRole": copy.deepcopy(role),
        "LogGroup": {"Type": "AWS::Logs::LogGroup", "Properties": {"LogGroupName": "/api/log", "RetentionInDays": 30}},
        "TaskSecurityGroup": {"Type": "AWS::EC2::SecurityGroup", "Properties": {
            "VpcId": {"Fn::ImportValue": {"Fn::Sub": "dig-platform-${Env}-VpcId"}},
            "SecurityGroupIngress": [{"IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8000}],
            "SecurityGroupEgress": [{"IpProtocol": -1, "CidrIp": "0.0.0.0/0"}],
        }},
        "TaskDefinition": {"Type": "AWS::ECS::TaskDefinition", "Properties": {
            "Cpu": {"Ref": "Cpu"}, "Memory": {"Ref": "Memory"},
            "NetworkMode": "awsvpc", "RequiresCompatibilities": ["FARGATE"],
            "RuntimePlatform": {"OperatingSystemFamily": "LINUX", "CpuArchitecture": "ARM64"},
            "ExecutionRoleArn": {"Fn::GetAtt": ["ExecutionRole", "Arn"]},
            "TaskRoleArn": {"Fn::GetAtt": "TaskRole.Arn"},
            "ContainerDefinitions": [{
                "Name": "app", "Image": {"Ref": "ImageUri"}, "Essential": True,
                "ReadonlyRootFilesystem": True, "StopTimeout": 120,
                "LinuxParameters": {"InitProcessEnabled": True, "Tmpfs": [
                    {"ContainerPath": "/work", "Size": 256, "MountOptions": ["rw", "nosuid", "nodev", "mode=1777"]},
                    {"ContainerPath": "/tmp", "Size": 64, "MountOptions": ["rw", "nosuid", "nodev", "mode=1777"]},
                ]},
                "Environment": [
                    {"Name": "SERVICE_PATH_PREFIX", "Value": "/api/reveal"},
                    {"Name": "REVEAL_JOB_NAMESPACE", "Value": "reveal-platform"},
                    {"Name": "REVEAL_S3_READ_PREFIXES", "Value": "local/,prod/"},
                ],
                "Secrets": [{"Name": "REVEAL_REDIS_URL", "ValueFrom": SECRET + ":REVEAL_REDIS_URL::"}],
                "PortMappings": [{"ContainerPort": 8000}],
                "LogConfiguration": {"LogDriver": "awslogs", "Options": {
                    "awslogs-group": {"Ref": "LogGroup"}, "awslogs-region": {"Ref": "AWS::Region"},
                }},
            }],
        }},
        "Service": {"Type": "AWS::ECS::Service", "Properties": {
            "Cluster": {"Fn::ImportValue": {"Fn::Sub": "dig-platform-${Env}-ClusterName"}},
            "NetworkConfiguration": {"AwsvpcConfiguration": {
                "SecurityGroups": [{"Ref": "TaskSecurityGroup"}], "AssignPublicIp": "ENABLED",
                "Subnets": {"Fn::Split": [",", {"Fn::ImportValue": {"Fn::Sub": "dig-platform-${Env}-PublicSubnets"}}]},
            }},
        }},
    }}
    return template, parameters


def test_background_runtime_is_identical_except_process_and_capacity(api):
    source, parameters = api
    before = copy.deepcopy(source)
    rendered = MODULE.render(source, parameters)
    resources = rendered["Resources"]
    api_container = source["Resources"]["TaskDefinition"]["Properties"]["ContainerDefinitions"][0]
    for label, module, count, cpu, memory in (("Worker", "worker", 2, "1024", "3072"), ("Dispatcher", "dispatcher", 1, "256", "1024")):
        task = resources[label + "TaskDefinition"]["Properties"]
        container = task["ContainerDefinitions"][0]
        assert container["Image"] == IMAGE
        for field in ("Environment", "Secrets", "LinuxParameters", "ReadonlyRootFilesystem"):
            assert container[field] == api_container[field]
        for role in ("TaskRole", "ExecutionRole"):
            assert resources[label + role]["Properties"]["Policies"] == source["Resources"][role]["Properties"]["Policies"]
        assert task["Cpu"] == cpu and task["Memory"] == memory
        assert container["Command"] == ["python", "-m", "reveal_backend." + module]
        assert container["StopTimeout"] == 120
        assert container["HealthCheck"]["Command"][-2:] == ["health", module]
        assert "PortMappings" not in container
        service = resources[label + "Service"]["Properties"]
        assert service["DesiredCount"] == count
        assert service["CapacityProviderStrategy"] == [{"CapacityProvider": "FARGATE", "Weight": 1}]
        assert service["EnableExecuteCommand"] is False
        assert service["DeploymentConfiguration"]["MaximumPercent"] == 200
    assert source == before
    assert "Parameters" not in rendered
    assert resources["BackgroundSecurityGroup"]["Properties"]["SecurityGroupIngress"] == []
    assert not any(resource["Type"].startswith(("AWS::ElasticLoadBalancing", "AWS::ApplicationAutoScaling")) for resource in resources.values())


def test_background_references_and_names_never_point_to_api_resources(api):
    resources = MODULE.render(*api)["Resources"]
    for label, suffix in (("Worker", "workers"), ("Dispatcher", "dispatcher")):
        task = resources[label + "TaskDefinition"]["Properties"]
        assert task["Family"] == f"svc-reveal-qa-{suffix}"
        assert task["TaskRoleArn"] == {"Fn::GetAtt": [label + "TaskRole", "Arn"]}
        assert task["ExecutionRoleArn"] == {"Fn::GetAtt": [label + "ExecutionRole", "Arn"]}
        assert task["ContainerDefinitions"][0]["LogConfiguration"]["Options"]["awslogs-group"] == {"Ref": label + "LogGroup"}
        service = resources[label + "Service"]["Properties"]
        assert service["Cluster"] == {"Fn::ImportValue": "dig-platform-qa-ClusterName"}
        assert service["NetworkConfiguration"]["AwsvpcConfiguration"]["SecurityGroups"] == [{"Ref": "BackgroundSecurityGroup"}]


@pytest.mark.parametrize("failure", ["missing_image", "wrong_image", "mutable_root", "disk_scratch", "plaintext_redis", "no_redis_secret", "other_service", "missing_parameter"])
def test_unsafe_or_incomplete_runtime_is_rejected(api, failure):
    source, parameters = api
    container = source["Resources"]["TaskDefinition"]["Properties"]["ContainerDefinitions"][0]
    if failure == "missing_image": parameters[2]["ParameterValue"] = ""
    elif failure == "wrong_image": container["Image"] = "different:latest"
    elif failure == "mutable_root": container["ReadonlyRootFilesystem"] = False
    elif failure == "disk_scratch": container["LinuxParameters"]["Tmpfs"] = []
    elif failure == "plaintext_redis": container["Environment"].append({"Name": "REVEAL_REDIS_URL", "Value": "redis://localhost"})
    elif failure == "no_redis_secret": container["Secrets"] = []
    elif failure == "other_service": parameters[1]["ParameterValue"] = "kg"
    elif failure == "missing_parameter": parameters.pop()
    with pytest.raises(ValueError): MODULE.render(source, parameters)


def test_cli_writes_a_deployable_json_template(api, tmp_path):
    source, parameters = api
    template, params, output = [tmp_path / name for name in ("api.json", "params.json", "workers.json")]
    template.write_text(json.dumps(source))
    params.write_text(json.dumps(parameters))
    subprocess.run([sys.executable, str(ROOT / "deploy/dig/render_workers.py"), "--api-template", str(template), "--api-params", str(params), "--out-template", str(output)], check=True)
    assert json.loads(output.read_text()) == MODULE.render(source, parameters)


def test_network_prerequisite_only_authorizes_application_and_database_connectivity():
    class CloudFormationLoader(yaml.SafeLoader):
        pass
    CloudFormationLoader.add_multi_constructor("!", lambda loader, tag, node: {
        tag: loader.construct_scalar(node) if isinstance(node, yaml.ScalarNode)
        else loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode)
        else loader.construct_mapping(node)
    })
    template = yaml.load((ROOT / "deploy/dig/network-stack.yaml").read_text(), Loader=CloudFormationLoader)
    resources = template["Resources"]
    assert set(resources) == {"ClientSecurityGroup", "DatabaseIngress"}
    assert set(template["Parameters"]) == {"Env", "DatabaseSecurityGroupId"}
    ingress = resources["ClientSecurityGroup"]["Properties"]["SecurityGroupIngress"]
    assert len(ingress) == 1
    assert ingress[0]["FromPort"] == 8000 and ingress[0]["ToPort"] == 8000
    assert ingress[0]["SourceSecurityGroupId"] == {"ImportValue": {"Fn::Sub": "dig-platform-${Env}-AlbSecurityGroupId"}}
    assert "CidrIp" not in ingress[0] and "CidrIpv6" not in ingress[0]
    database_rule = resources["DatabaseIngress"]["Properties"]
    assert database_rule["SourceSecurityGroupId"] == {"Ref": "ClientSecurityGroup"}
    assert database_rule["GroupId"] == {"Ref": "DatabaseSecurityGroupId"}
    assert database_rule["FromPort"] == 3306 and database_rule["ToPort"] == 3306
    assert set(template["Outputs"]) == {"ClientSecurityGroupId"}
