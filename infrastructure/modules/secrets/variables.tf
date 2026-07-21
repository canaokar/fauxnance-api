variable "environment" {
  description = "Deployment environment used in SSM paths."
  type        = string
}

variable "source_names" {
  description = "Upstream sources requiring an API key placeholder."
  type        = set(string)
  default     = ["finnhub", "alpha_vantage", "coingecko"]
}

variable "placeholder_value" {
  description = "Non-secret initial value replaced out of band after apply."
  type        = string
  default     = "REPLACE_ME"
}

variable "tags" {
  description = "Tags applied to SSM parameters."
  type        = map(string)
}

