SHELL := /bin/sh

ENV ?= dev
AWS_PROFILE ?= megh.io
AWS_REGION ?= eu-west-2
TF_ENV_DIR := infrastructure/environments/$(ENV)

.PHONY: bootstrap infra infra-plan deploy package remove destroy test format logs

bootstrap:
	AWS_PROFILE=$(AWS_PROFILE) AWS_REGION=$(AWS_REGION) terraform -chdir=infrastructure/bootstrap init
	AWS_PROFILE=$(AWS_PROFILE) AWS_REGION=$(AWS_REGION) terraform -chdir=infrastructure/bootstrap apply

infra:
	AWS_PROFILE=$(AWS_PROFILE) AWS_REGION=$(AWS_REGION) terraform -chdir=$(TF_ENV_DIR) init
	AWS_PROFILE=$(AWS_PROFILE) AWS_REGION=$(AWS_REGION) terraform -chdir=$(TF_ENV_DIR) apply

infra-plan:
	AWS_PROFILE=$(AWS_PROFILE) AWS_REGION=$(AWS_REGION) terraform -chdir=$(TF_ENV_DIR) init
	AWS_PROFILE=$(AWS_PROFILE) AWS_REGION=$(AWS_REGION) terraform -chdir=$(TF_ENV_DIR) plan

deploy: infra
	npx serverless deploy --stage $(ENV) --region $(AWS_REGION) --aws-profile $(AWS_PROFILE)

package:
	npx serverless package --stage $(ENV) --region $(AWS_REGION) --aws-profile $(AWS_PROFILE)

remove:
	npx serverless remove --stage $(ENV) --region $(AWS_REGION) --aws-profile $(AWS_PROFILE)

destroy: remove
	AWS_PROFILE=$(AWS_PROFILE) AWS_REGION=$(AWS_REGION) terraform -chdir=$(TF_ENV_DIR) destroy

test:
	python3 -m pytest

format:
	terraform fmt -recursive infrastructure

logs:
	@test -n "$(FN)" || (echo "FN is required, for example: make logs FN=api ENV=dev" && exit 1)
	npx serverless logs --function $(FN) --stage $(ENV) --region $(AWS_REGION) --aws-profile $(AWS_PROFILE) --tail
