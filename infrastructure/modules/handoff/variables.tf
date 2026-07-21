variable "environment" {
  description = "Deployment environment used in SSM paths."
  type        = string
}

variable "data_table_name" {
  description = "Market-data DynamoDB table name."
  type        = string
}

variable "control_table_name" {
  description = "Control DynamoDB table name."
  type        = string
}

variable "ingest_queue_arn" {
  description = "Ingest queue ARN."
  type        = string
}

variable "ingest_queue_url" {
  description = "Ingest queue URL."
  type        = string
}

variable "dlq_arn" {
  description = "Dead-letter queue ARN."
  type        = string
}

variable "aws_region" {
  description = "AWS region consumed by Serverless Framework."
  type        = string
}

variable "tags" {
  description = "Tags applied to SSM handoff parameters."
  type        = map(string)
}

