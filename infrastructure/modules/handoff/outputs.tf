output "parameter_names" {
  description = "SSM handoff parameter names by contract key."
  value       = { for name, parameter in aws_ssm_parameter.infra : name => parameter.name }
}

