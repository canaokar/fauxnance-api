# Fauxnance infrastructure

Phase 1 uses one flat Terraform root. It creates only:

- `fauxnance-dev-data` DynamoDB table
- `fauxnance-dev-control` DynamoDB table
- `/fauxnance/dev/admin/key_ids` SSM parameter

The table names are deterministic, so `serverless.yml` does not need Terraform
outputs or SSM handoff parameters.

```sh
terraform -chdir=infrastructure init
terraform -chdir=infrastructure plan
terraform -chdir=infrastructure apply
```

Terraform state is local and ignored by Git. The root uses the `megh.io` shared
AWS profile in `eu-west-2`, reading `~/.aws/config` and
`~/.aws/credentials`.

Queues, scheduled ingestion, upstream credentials, remote state, reusable
modules, and additional environments are deferred until a phase actually needs
them.
