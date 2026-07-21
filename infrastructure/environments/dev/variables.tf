variable "billing_mode" {
  description = "DynamoDB billing mode; PAY_PER_REQUEST is the documented escape hatch."
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
  description = "Whether DynamoDB point-in-time recovery is enabled in dev."
  type        = bool
  default     = false
}

variable "deletion_protection_enabled" {
  description = "Whether DynamoDB deletion protection is enabled in dev."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Additional tags applied to all dev resources."
  type        = map(string)
  default     = {}
}
