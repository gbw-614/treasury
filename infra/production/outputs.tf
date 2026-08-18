output "public_ip" {
  description = "Stable public IPv4 address."
  value       = aws_eip.app.public_ip
}

output "application_url" {
  description = "Expected application URL after the first image is deployed."
  value       = var.domain_name != "" ? "https://${var.domain_name}" : "http://${aws_eip.app.public_ip}"
}

output "ecr_repository_url" {
  description = "Push release images to this ECR repository."
  value       = aws_ecr_repository.app.repository_url
}

output "instance_id" {
  description = "EC2 instance ID used by SSM deployment commands."
  value       = aws_instance.app.id
}

output "data_volume_id" {
  description = "Persistent application-data EBS volume."
  value       = aws_ebs_volume.data.id
}

output "initial_deployment_note" {
  description = "The instance becomes healthy after an application image is pushed and deployed."
  value       = "Push an ARM64 image to ${aws_ecr_repository.app.repository_url}:${var.initial_image_tag}, then invoke /usr/local/bin/deploy-treasury on ${aws_instance.app.id} through SSM."
}
