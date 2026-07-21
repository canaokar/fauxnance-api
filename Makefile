SHELL := /bin/sh

ENV ?= dev
AWS_PROFILE ?= megh.io
AWS_REGION ?= eu-west-2

.PHONY: infra infra-plan deploy package remove destroy test format logs

infra:
	terraform -chdir=infrastructure init
	terraform -chdir=infrastructure apply

infra-plan:
	terraform -chdir=infrastructure init
	terraform -chdir=infrastructure plan

deploy:
	npx serverless deploy --stage $(ENV) --region $(AWS_REGION) --aws-profile $(AWS_PROFILE)

package:
	npx serverless package --stage $(ENV) --region $(AWS_REGION) --aws-profile $(AWS_PROFILE)

remove:
	npx serverless remove --stage $(ENV) --region $(AWS_REGION) --aws-profile $(AWS_PROFILE)

destroy: remove
	terraform -chdir=infrastructure destroy

test:
	python3 -m pytest

format:
	terraform fmt -recursive infrastructure

logs:
	@test -n "$(FN)" || (echo "FN is required, for example: make logs FN=api ENV=dev" && exit 1)
	npx serverless logs --function $(FN) --stage $(ENV) --region $(AWS_REGION) --aws-profile $(AWS_PROFILE) --tail
