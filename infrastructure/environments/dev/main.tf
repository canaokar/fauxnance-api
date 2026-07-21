locals {
  environment = "dev"
  name_prefix = "fauxnance-${local.environment}"
  common_tags = merge({
    project    = "fauxnance"
    env        = local.environment
    managed-by = "terraform"
  }, var.tags)
}

provider "aws" {
  region                   = "eu-west-2"
  shared_config_files      = ["~/.aws/config"]
  shared_credentials_files = ["~/.aws/credentials"]
  profile                  = "megh.io"

  default_tags {
    tags = local.common_tags
  }
}

module "data" {
  source = "../../modules/data"

  name_prefix                    = local.name_prefix
  billing_mode                   = var.billing_mode
  data_read_capacity             = var.data_read_capacity
  data_write_capacity            = var.data_write_capacity
  control_read_capacity          = var.control_read_capacity
  control_write_capacity         = var.control_write_capacity
  point_in_time_recovery_enabled = var.point_in_time_recovery_enabled
  deletion_protection_enabled    = var.deletion_protection_enabled
  tags                           = local.common_tags
}

module "ingestion" {
  source = "../../modules/ingestion"

  name_prefix = local.name_prefix
  tags        = local.common_tags
}

module "secrets" {
  source = "../../modules/secrets"

  environment = local.environment
  tags        = local.common_tags
}

module "handoff" {
  source = "../../modules/handoff"

  environment        = local.environment
  data_table_name    = module.data.data_table_name
  control_table_name = module.data.control_table_name
  ingest_queue_arn   = module.ingestion.ingest_queue_arn
  ingest_queue_url   = module.ingestion.ingest_queue_url
  dlq_arn            = module.ingestion.dlq_arn
  aws_region         = "eu-west-2"
  tags               = local.common_tags
}
