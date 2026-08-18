# Terraform state bootstrap

This small root creates the encrypted, versioned S3 bucket used by the real
production configuration. Its first apply necessarily uses local state because
the remote state bucket does not exist yet.

```bash
terraform init
terraform apply
terraform output -raw backend_hcl > ../production/backend.hcl
```

Keep the bootstrap `terraform.tfstate` secure until the bucket has been created
and backed up. The bucket has `prevent_destroy` enabled intentionally.
