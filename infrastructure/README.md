# Fauxnance infrastructure

Terraform owns the stateful resources shared by the application. The
Serverless Framework consumes their names and ARNs through SSM Parameter Store.

## Bootstrap remote state

Choose a globally unique bucket name, then run the bootstrap root once. Its
state remains local so it can create the backend used by the environment roots.

```sh
terraform -chdir=infrastructure/bootstrap init
terraform -chdir=infrastructure/bootstrap apply \
  -var='state_bucket_name=<globally-unique-bucket-name>'
```

## Initialize and apply dev

The environment backend is intentionally partial because an S3 bucket name is
account-specific and globally unique.

```sh
terraform -chdir=infrastructure/environments/dev init \
  -backend-config='bucket=<globally-unique-bucket-name>'

terraform -chdir=infrastructure/environments/dev plan
terraform -chdir=infrastructure/environments/dev apply
```

Terraform creates placeholder upstream credentials. Replace each placeholder
out of band with `aws ssm put-parameter --overwrite`; real secret values must
not be passed to Terraform or committed to the repository.

Both Terraform roots and the S3 backend use the `megh.io` shared AWS profile in
`eu-west-2`, reading `~/.aws/config` and `~/.aws/credentials`.
