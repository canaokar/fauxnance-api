resource "aws_dynamodb_table" "data" {
  name         = "${var.name_prefix}-data"
  billing_mode = var.billing_mode

  read_capacity  = var.billing_mode == "PROVISIONED" ? var.data_read_capacity : null
  write_capacity = var.billing_mode == "PROVISIONED" ? var.data_write_capacity : null

  hash_key  = "PK"
  range_key = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  ttl {
    attribute_name = "expiresAt"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = var.point_in_time_recovery_enabled
  }

  deletion_protection_enabled = var.deletion_protection_enabled
  server_side_encryption {
    enabled = true
  }

  tags = merge(var.tags, { component = "market-data" })
}

resource "aws_dynamodb_table" "control" {
  name         = "${var.name_prefix}-control"
  billing_mode = var.billing_mode

  read_capacity  = var.billing_mode == "PROVISIONED" ? var.control_read_capacity : null
  write_capacity = var.billing_mode == "PROVISIONED" ? var.control_write_capacity : null

  hash_key  = "PK"
  range_key = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  ttl {
    attribute_name = "expiresAt"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = var.point_in_time_recovery_enabled
  }

  deletion_protection_enabled = var.deletion_protection_enabled
  server_side_encryption {
    enabled = true
  }

  tags = merge(var.tags, { component = "control" })
}

