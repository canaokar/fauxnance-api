variable "name_prefix" {
  description = "Prefix used for DynamoDB table names."
  type        = string
}

variable "billing_mode" {
  description = "DynamoDB billing mode for both tables."
  type        = string
  default     = "PROVISIONED"

  validation {
    condition     = contains(["PROVISIONED", "PAY_PER_REQUEST"], var.billing_mode)
    error_message = "billing_mode must be PROVISIONED or PAY_PER_REQUEST."
  }
}

variable "data_read_capacity" {
  description = "Provisioned read capacity for the market-data table."
  type        = number
  default     = 15
}

variable "data_write_capacity" {
  description = "Provisioned write capacity for the market-data table."
  type        = number
  default     = 10
}

variable "control_read_capacity" {
  description = "Provisioned read capacity for the control table."
  type        = number
  default     = 10
}

variable "control_write_capacity" {
  description = "Provisioned write capacity for the control table."
  type        = number
  default     = 15
}

variable "point_in_time_recovery_enabled" {
  description = "Whether to enable DynamoDB point-in-time recovery."
  type        = bool
  default     = false
}

variable "deletion_protection_enabled" {
  description = "Whether to protect the tables from deletion."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags applied to DynamoDB resources."
  type        = map(string)
}

