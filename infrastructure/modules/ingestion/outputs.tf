output "ingest_queue_arn" {
  description = "Ingest queue ARN consumed by Serverless Framework."
  value       = aws_sqs_queue.ingest.arn
}

output "ingest_queue_url" {
  description = "Ingest queue URL used by the dispatcher."
  value       = aws_sqs_queue.ingest.url
}

output "dlq_arn" {
  description = "Ingest dead-letter queue ARN."
  value       = aws_sqs_queue.dlq.arn
}

