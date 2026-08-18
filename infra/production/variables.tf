variable "name" {
  description = "Prefix applied to named AWS resources."
  type        = string
  default     = "treasury-label-verification"

  validation {
    condition     = can(regex("^[a-z0-9-]+$", var.name))
    error_message = "name must contain only lowercase letters, digits, and hyphens."
  }
}

variable "aws_region" {
  description = "AWS region in which to deploy the application."
  type        = string
  default     = "us-east-2"
}

variable "vpc_cidr" {
  description = "CIDR block for the dedicated application VPC."
  type        = string
  default     = "10.42.0.0/16"
}

variable "public_subnet_cidr" {
  description = "CIDR block for the single public subnet."
  type        = string
  default     = "10.42.1.0/24"
}

variable "instance_type" {
  description = "ARM64 EC2 instance type. t4g.medium is the recommended starting point."
  type        = string
  default     = "t4g.medium"
}

variable "root_volume_size_gib" {
  description = "Size of the disposable instance root volume."
  type        = number
  default     = 20
}

variable "data_volume_size_gib" {
  description = "Size of the persistent SQLite, artwork, and Caddy EBS volume."
  type        = number
  default     = 20
}

variable "initial_image_tag" {
  description = "ECR image tag attempted on first boot. Releases subsequently use commit-SHA tags."
  type        = string
  default     = "bootstrap"
}

variable "openrouter_parameter_name" {
  description = "Existing SSM SecureString name containing the OpenRouter API key. Terraform never reads its value."
  type        = string
  default     = "/treasury/production/openrouter-api-key"

  validation {
    condition     = startswith(var.openrouter_parameter_name, "/")
    error_message = "openrouter_parameter_name must start with a slash."
  }
}

variable "openrouter_model" {
  description = "OpenRouter model slug passed to the application."
  type        = string
  default     = "google/gemini-3.5-flash"
}

variable "bootstrap_username" {
  description = "Initial reviewer username created only when the production database has no users."
  type        = string
  default     = "admin"

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]{1,64}$", var.bootstrap_username))
    error_message = "bootstrap_username must be 1-64 letters, digits, dots, underscores, or hyphens."
  }
}

variable "bootstrap_password_parameter_name" {
  description = "Existing SSM SecureString containing the initial reviewer password. Terraform never reads its value."
  type        = string
  default     = "/treasury/production/bootstrap-password"

  validation {
    condition     = startswith(var.bootstrap_password_parameter_name, "/")
    error_message = "bootstrap_password_parameter_name must start with a slash."
  }
}

variable "catalog_url" {
  description = "Optional HTTPS URL for the validated public reference catalog manifest."
  type        = string
  default     = ""

  validation {
    condition     = var.catalog_url == "" || startswith(var.catalog_url, "https://")
    error_message = "catalog_url must be empty or use HTTPS."
  }
}

variable "session_hours" {
  description = "Authenticated browser-session lifetime in hours."
  type        = number
  default     = 12

  validation {
    condition     = var.session_hours >= 1 && var.session_hours <= 720 && floor(var.session_hours) == var.session_hours
    error_message = "session_hours must be a whole number between 1 and 720."
  }
}

variable "domain_name" {
  description = "Public hostname. Leave empty to serve HTTP only on the Elastic IP."
  type        = string
  default     = ""
}

variable "caddy_email" {
  description = "Email used by Caddy for certificate notices. Recommended when domain_name is set."
  type        = string
  default     = ""
}

variable "route53_zone_id" {
  description = "Optional existing Route 53 hosted-zone ID. Leave empty to manage DNS elsewhere."
  type        = string
  default     = ""
}
