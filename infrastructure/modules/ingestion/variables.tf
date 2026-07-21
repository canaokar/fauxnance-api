variable "name_prefix" {
  description = "Prefix used for queue names."
  type        = string
}

variable "visibility_timeout_seconds" {
  description = "Time an in-flight ingest message remains invisible."
  type        = number
  default     = 180
}

variable "max_receive_count" {
  description = "Failed receives before a message moves to the DLQ."
  type        = number
  default     = 5
}

variable "tags" {
  description = "Tags applied to SQS resources."
  type        = map(string)
}

