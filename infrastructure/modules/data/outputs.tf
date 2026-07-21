output "data_table_name" {
  description = "Market-data DynamoDB table name."
  value       = aws_dynamodb_table.data.name
}

output "data_table_arn" {
  description = "Market-data DynamoDB table ARN."
  value       = aws_dynamodb_table.data.arn
}

output "control_table_name" {
  description = "Control DynamoDB table name."
  value       = aws_dynamodb_table.control.name
}

output "control_table_arn" {
  description = "Control DynamoDB table ARN."
  value       = aws_dynamodb_table.control.arn
}

