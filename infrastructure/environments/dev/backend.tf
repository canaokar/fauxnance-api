terraform {
  backend "s3" {
    key          = "fauxnance/dev/terraform.tfstate"
    encrypt      = true
    use_lockfile = true
    region       = "eu-west-2"
    profile      = "megh.io"
  }
}
