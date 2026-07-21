resource "aws_ssm_parameter" "source_api_key" {
  for_each = var.source_names

  name        = "/fauxnance/${var.environment}/sources/${each.value}/api_key"
  description = "${each.value} API key; replace this placeholder out of band"
  type        = "SecureString"
  value       = var.placeholder_value

  lifecycle {
    ignore_changes = [value]
  }

  tags = merge(var.tags, { component = "source-secrets" })
}

resource "aws_ssm_parameter" "admin_key_ids" {
  name        = "/fauxnance/${var.environment}/admin/key_ids"
  description = "Comma-separated allowlist of admin key IDs"
  type        = "String"
  value       = var.placeholder_value

  lifecycle {
    ignore_changes = [value]
  }

  tags = merge(var.tags, { component = "authentication" })
}

