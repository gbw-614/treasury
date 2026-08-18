data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

locals {
  caddy_site_address = var.domain_name != "" ? var.domain_name : ":80"
  session_secure     = var.domain_name != "" ? "true" : "false"
  caddy_global_options = var.caddy_email != "" ? join("\n", [
    "{",
    "  email ${var.caddy_email}",
    "}",
  ]) : ""
}

resource "aws_ebs_volume" "data" {
  availability_zone = aws_subnet.public.availability_zone
  size              = var.data_volume_size_gib
  type              = "gp3"
  encrypted         = true

  tags = {
    Name   = "${var.name}-data"
    Backup = "true"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_instance" "app" {
  ami                         = data.aws_ssm_parameter.al2023_arm64.value
  instance_type               = var.instance_type
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.web.id]
  iam_instance_profile        = aws_iam_instance_profile.instance.name
  associate_public_ip_address = true
  monitoring                  = false

  user_data_replace_on_change = false
  user_data = templatefile("${path.module}/cloud-init.yaml.tftpl", {
    aws_region                        = var.aws_region
    bootstrap_password_parameter_name = var.bootstrap_password_parameter_name
    bootstrap_username                = var.bootstrap_username
    catalog_url                       = var.catalog_url
    caddy_global_options              = local.caddy_global_options
    caddy_site_address                = local.caddy_site_address
    data_volume_id                    = aws_ebs_volume.data.id
    ecr_registry                      = split("/", aws_ecr_repository.app.repository_url)[0]
    initial_image_ref                 = "${aws_ecr_repository.app.repository_url}:${var.initial_image_tag}"
    openrouter_model                  = var.openrouter_model
    openrouter_parameter_name         = var.openrouter_parameter_name
    session_hours                     = var.session_hours
    session_secure                    = local.session_secure
  })

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  root_block_device {
    encrypted             = true
    delete_on_termination = true
    volume_size           = var.root_volume_size_gib
    volume_type           = "gp3"
  }

  tags = {
    Name = var.name
  }

  depends_on = [
    aws_iam_role_policy.instance,
    aws_iam_role_policy_attachment.ssm_core,
    aws_route_table_association.public,
  ]
}

resource "aws_volume_attachment" "data" {
  device_name                    = "/dev/sdf"
  volume_id                      = aws_ebs_volume.data.id
  instance_id                    = aws_instance.app.id
  stop_instance_before_detaching = true
}

resource "aws_eip" "app" {
  domain = "vpc"

  tags = {
    Name = var.name
  }
}

resource "aws_eip_association" "app" {
  allocation_id = aws_eip.app.id
  instance_id   = aws_instance.app.id
}

resource "aws_route53_record" "app" {
  count = var.domain_name != "" && var.route53_zone_id != "" ? 1 : 0

  zone_id = var.route53_zone_id
  name    = var.domain_name
  type    = "A"
  ttl     = 60
  records = [aws_eip.app.public_ip]
}
