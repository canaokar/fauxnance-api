output "data_table_name" {
  description = "Dev market-data DynamoDB table name."
  value       = module.data.data_table_name
}

output "control_table_name" {
  description = "Dev control DynamoDB table name."
  value       = module.data.control_table_name
}

output "ingest_queue_arn" {
  description = "Dev ingest queue ARN."
  value       = module.ingestion.ingest_queue_arn
}

output "ingest_queue_url" {
  description = "Dev ingest queue URL."
  value       = module.ingestion.ingest_queue_url
}

output "dlq_arn" {
  description = "Dev ingest dead-letter queue ARN."
  value       = module.ingestion.dlq_arn
}

output "handoff_parameter_names" {
  description = "SSM parameter names consumed by Serverless Framework."
  value       = module.handoff.parameter_names
}

output "source_api_key_parameter_names" {
  description = "SecureString placeholders that must be replaced out of band."
  value       = module.secrets.source_api_key_parameter_names
}

output "admin_key_ids_parameter_name" {
  description = "Admin key ID allowlist parameter name."
  value       = module.secrets.admin_key_ids_parameter_name
}

