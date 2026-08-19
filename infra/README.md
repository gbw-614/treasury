# AWS deployment with Terraform

This configuration provisions a deliberately small production environment:

- one Amazon Linux 2023 ARM64 EC2 instance;
- one immutable ECR repository;
- one encrypted persistent EBS data volume;
- a dedicated VPC, public subnet, Elastic IP, and HTTP/HTTPS security group;
- an EC2 role for Session Manager, ECR pulls, and narrowly scoped SSM parameters;
- optional Route 53 DNS; and
- Caddy plus the application as Docker containers.

There is no inbound SSH rule. Use AWS Systems Manager Session Manager for host
access. Terraform provisions infrastructure; it does not build or release the
application image.

## Prerequisites

- Terraform 1.10 or newer.
- AWS CLI authenticated to the target account.
- A public domain name if HTTPS is required. Caddy obtains and renews its own
  certificate; an ACM certificate is not consumed by this single-instance
  topology because TLS terminates on EC2 rather than on an ALB or CloudFront.
- Docker Buildx for building the ARM64 image.

## 1. Bootstrap remote state

The state bucket is created separately because Terraform cannot use a bucket
that does not exist yet:

```bash
cd infra/bootstrap
terraform init
terraform apply
terraform output -raw backend_hcl > ../production/backend.hcl
```

The bootstrap state is local and sensitive. Preserve it securely. The state
bucket is encrypted, versioned, private, and protected against Terraform
destruction.

## 2. Configure and provision production

```bash
cd ../production
cp terraform.tfvars.example terraform.tfvars
# Edit domain_name, caddy_email, route53_zone_id, bootstrap_username,
# catalog_url, and the two SSM parameter names as needed.
terraform init -backend-config=backend.hcl
terraform plan -out=production.tfplan
terraform apply production.tfplan
```

If `route53_zone_id` is empty, Terraform does not create a DNS record. Point
the chosen hostname at the `public_ip` output through the existing DNS
provider. If `domain_name` is empty, Caddy serves plain HTTP on the Elastic IP.

The data EBS volume has `prevent_destroy` enabled. This intentionally causes a
normal `terraform destroy` to stop rather than erase queue records and uploaded
artwork. Take a snapshot and remove that lifecycle protection deliberately when
the deployment is retired.

## 3. Store runtime secrets outside Terraform

Create SSM Parameter Store `SecureString` values named:

```text
/treasury/production/openrouter-api-key
/treasury/production/bootstrap-password
```

The first is optional when the deployment will run OCR-only. The bootstrap
password is required for a fresh database; it creates the configured
`bootstrap_username` account exactly once. Neither value is declared as a
Terraform resource because Terraform would otherwise retain it in state. The
instance role can read only these two named parameters. The deploy script
mounts each retrieved value as a root-managed, read-only file rather than
placing it in Terraform, cloud-init, or the container image.

The deployment remains usable in OCR-only mode when the OpenRouter parameter
is absent.

The current planning hostname is `treasury.roninflow.xyz`. Its DNS zone is not
managed by this Terraform root, so the production Elastic IP output must be
added as an external A record before Caddy can obtain the certificate.

## 4. Build and push the first image

Run from the repository root after Terraform has created ECR:

```bash
REPOSITORY=$(terraform -chdir=infra/production output -raw ecr_repository_url)
REGISTRY=${REPOSITORY%%/*}
TAG=bootstrap

aws ecr get-login-password --region us-east-2 \
  | docker login --username AWS --password-stdin "$REGISTRY"

docker buildx build \
  --platform linux/arm64 \
  --tag "$REPOSITORY:$TAG" \
  --push .
```

The repository uses immutable tags. After the first image, use the Git commit
SHA for every release rather than overwriting `bootstrap` or `latest`.

## 5. Deploy through Session Manager

```bash
INSTANCE_ID=$(terraform -chdir=infra/production output -raw instance_id)
aws ssm start-session --target "$INSTANCE_ID"
```

Then, on the instance:

```bash
sudo /usr/local/bin/deploy-treasury ACCOUNT.dkr.ecr.REGION.amazonaws.com/treasury-label-verification:GIT_SHA
```

The deploy script:

1. finds and mounts the EBS volume by its stable volume ID;
2. authenticates to ECR and pulls the exact image;
3. retrieves the OpenRouter key and bootstrap password into root-only files;
4. configures secure sessions and the public catalog URL;
5. starts the application and waits for its container health check;
6. starts Caddy; and
7. records the image reference for the next reboot.

The eventual GitHub Actions workflow should perform the same image build and
invoke this script with SSM Run Command. AWS credentials for CI should use
GitHub OIDC, not long-lived access keys.

## Important operational notes

- `/srv/treasury/app` contains SQLite state and uploaded artwork.
- Caddy certificate state lives on the same persistent EBS volume.
- Docker logs rotate at 10 MB with three files per container.
- Port 22 is not open. Ports 80 and 443 are the only public ingress.
- The initial systemd application start may fail before the first ECR image is
  pushed; this is expected. Running `deploy-treasury` completes the launch.
- This is a single-instance deployment. There is no automatic failover or
  zero-downtime release mechanism.
