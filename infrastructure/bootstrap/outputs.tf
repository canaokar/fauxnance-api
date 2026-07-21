output "state_bucket_name" {
  description = "Bucket name to pass to environment backend initialization."
  value       = aws_s3_bucket.terraform_state.id
}

output "state_bucket_region" {
  description = "Bucket region to pass to environment backend initialization."
  value       = "eu-west-2"
}
