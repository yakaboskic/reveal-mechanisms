# AWS runtime identity

**On hold:** the administrator has requested use of `broadinstitute/dig-service-platform` instead. See [the platform integration status](../../docs/platform-deployment.md). The following policies and commands describe the earlier dedicated-EC2 proposal; they have not been applied.

Account `005901288866`, region `us-east-1`, existing RDS VPC `vpc-a53ba7c2`.

The approved role and instance profile are both named `cyaka-reveal-ec2`.
`ec2-trust-policy.json` allows only the EC2 service to assume the role.
`ec2-runtime-policy.json` grants this application's runtime access:

- Read versioned artifacts in the existing bucket's `local/artifacts/` and `prod/artifacts/` paths; download release bundles from `prod/releases/`.
- Write new artifacts only to `prod/artifacts/`, with no object deletion permission.
- Read only the REVEAL backend secret and pull only the REVEAL backend ECR repository.
- Write logs only to the REVEAL backend log group.

Attach the AWS-managed `AmazonSSMManagedInstanceCore` policy for Systems Manager administration, avoiding public SSH ingress. Attach this role to the dedicated REVEAL instance profile. This does not grant the deployment operator broader IAM privileges.

Existing `reveal_*` RDS tables and stored S3 object versions must be preserved. The cloud runtime will write to `prod/` and explicitly retain read access to legacy `local/` references. Do not replace the original tables or rewrite scientific identities during deployment.

## Current blocker (September 29, 2026)

The user explicitly approved this runtime role. The subsequent AWS call was denied:

- `iam:CreateRole` on `cyaka-reveal-ec2`.
- `ecr:CreateRepository` on `cyaka-reveal-backend`.

The deployment operator is `arn:aws:iam::005901288866:user/cyakabos`, using the `broad` CLI profile. Approval in Codex does not grant permissions in AWS. No role, profile, ECR repository, or EC2 host was created. Nonmutating EC2 dry runs for instance launch, a dedicated security group, and an Elastic IP passed; `iam:PassRole` is still needed for a launch with the runtime profile.

## Administrator handoff

Run the following from the repository root (or the extracted administrator ZIP root) using an administrator's authorized AWS session **in account `005901288866`**. These commands create only dedicated REVEAL resources; they do not change the shared database or its security groups. If a resource already exists, inspect it before continuing instead of creating a duplicate.

```bash
aws sts get-caller-identity
aws iam create-role --role-name cyaka-reveal-ec2 \
  --assume-role-policy-document file://deploy/aws/ec2-trust-policy.json \
  --tags Key=Project,Value=reveal-mechanisms
aws iam put-role-policy --role-name cyaka-reveal-ec2 \
  --policy-name RevealRuntime --policy-document file://deploy/aws/ec2-runtime-policy.json
aws iam attach-role-policy --role-name cyaka-reveal-ec2 \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam create-instance-profile --instance-profile-name cyaka-reveal-ec2
aws iam add-role-to-instance-profile --instance-profile-name cyaka-reveal-ec2 --role-name cyaka-reveal-ec2
aws ecr create-repository --region us-east-1 --repository-name cyaka-reveal-backend \
  --image-tag-mutability IMMUTABLE --image-scanning-configuration scanOnPush=true \
  --tags Key=Project,Value=reveal-mechanisms
aws logs create-log-group --region us-east-1 --log-group-name /cyaka/reveal/backend
aws logs put-retention-policy --region us-east-1 --log-group-name /cyaka/reveal/backend --retention-in-days 30
```

The administrator should also review [deployment-operator-policy.json](deployment-operator-policy.json), which supplies the missing deployment actions for the existing `cyakabos` operator: pass only this role to EC2, publish only this repository, configure only the backend secret, and send SSM commands only to REVEAL-tagged instances. This is a separate proposed operator policy, **not an already-applied policy or part of the approved runtime policy**. It grants no role creation or role policy editing. The existing operator's EC2/S3 permissions remain unchanged.

If approved by the administrator:

```bash
aws iam put-user-policy --user-name cyakabos --policy-name RevealDeployment \
  --policy-document file://deploy/aws/deployment-operator-policy.json
```

See [the cloud rollout runbook](../../docs/cloud-deployment.md) for image publication, secret delivery, the host setup, Vercel, and the controlled handoff from localhost.
