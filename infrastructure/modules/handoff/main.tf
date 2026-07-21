locals {
  values = {
    data_table_name    = var.data_table_name
    control_table_name = var.control_table_name
    ingest_queue_arn   = var.ingest_queue_arn
    ingest_queue_url   = var.ingest_queue_url
    dlq_arn            = var.dlq_arn
    region             = var.aws_region
  }
}

resource "aws_ssm_parameter" "infra" {
  for_each = local.values

  name        = "/fauxnance/${var.environment}/infra/${each.key}"
  description = "Terraform to Serverless Framework handoff: ${each.key}"
  type        = "String"
  value       = each.value

  tags = merge(var.tags, { component = "infra-handoff" })
}

