output "source_api_key_parameter_names" {
  description = "Upstream API key SSM parameter names by source."
  value       = { for name, parameter in aws_ssm_parameter.source_api_key : name => parameter.name }
}

output "admin_key_ids_parameter_name" {
  description = "Admin key ID allowlist SSM parameter name."
  value       = aws_ssm_parameter.admin_key_ids.name
}

