terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0, < 7.0"
    }
  }
}

provider "aws" {
  region                   = "eu-west-2"
  shared_config_files      = ["~/.aws/config"]
  shared_credentials_files = ["~/.aws/credentials"]
  profile                  = "megh.io"
}

resource "aws_dynamodb_table" "data" {
  name           = "fauxnance-dev-data"
  billing_mode   = "PROVISIONED"
  read_capacity  = 15
  write_capacity = 10
  hash_key       = "PK"
  range_key      = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  ttl {
    attribute_name = "expiresAt"
    enabled        = true
  }
}

resource "aws_dynamodb_table" "control" {
  name           = "fauxnance-dev-control"
  billing_mode   = "PROVISIONED"
  read_capacity  = 10
  write_capacity = 15
  hash_key       = "PK"
  range_key      = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  ttl {
    attribute_name = "expiresAt"
    enabled        = true
  }
}

resource "aws_ssm_parameter" "admin_key_ids" {
  name        = "/fauxnance/dev/admin/key_ids"
  description = "Comma-separated allowlist of admin key IDs"
  type        = "String"
  value       = "bootstrap-required"

  lifecycle {
    ignore_changes = [value]
  }
}
