# Deploys the gateway container to AWS App Runner, with the Anthropic API key in
# Secrets Manager and an IAM instance role that can call Bedrock.
#
#   terraform init && terraform apply -var image_uri=<account>.dkr.ecr.<region>.amazonaws.com/llm-gateway:latest

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.60" }
  }
}

provider "aws" {
  region = var.region
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "image_uri" {
  type        = string
  description = "ECR image URI for the gateway"
}

resource "aws_secretsmanager_secret" "anthropic_key" {
  name = "llm-gateway/anthropic-api-key"
}

# Role App Runner uses to pull from ECR.
resource "aws_iam_role" "access" {
  name = "llm-gateway-apprunner-access"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "build.apprunner.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy_attachment" "ecr" {
  role       = aws_iam_role.access.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess"
}

# Role the running container assumes: Bedrock invoke + read its one secret.
resource "aws_iam_role" "instance" {
  name = "llm-gateway-apprunner-instance"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "tasks.apprunner.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "instance" {
  role = aws_iam_role.instance.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        # Check the Bedrock docs for any extra actions your account needs for the Mantle endpoint.
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.anthropic_key.arn
      },
    ]
  })
}

resource "aws_apprunner_auto_scaling_configuration_version" "gateway" {
  auto_scaling_configuration_name = "llm-gateway"
  min_size                        = 1
  max_size                        = 5
  max_concurrency                 = 80
}

resource "aws_apprunner_service" "gateway" {
  service_name                   = "llm-gateway"
  auto_scaling_configuration_arn = aws_apprunner_auto_scaling_configuration_version.gateway.arn

  source_configuration {
    auto_deployments_enabled = true
    authentication_configuration {
      access_role_arn = aws_iam_role.access.arn
    }
    image_repository {
      image_identifier      = var.image_uri
      image_repository_type = "ECR"
      image_configuration {
        port = "8080"
        runtime_environment_variables = {
          AWS_REGION     = var.region
          GATEWAY_CONFIG = "/app/config/gateway.yaml"
        }
        runtime_environment_secrets = {
          ANTHROPIC_API_KEY = aws_secretsmanager_secret.anthropic_key.arn
        }
      }
    }
  }

  instance_configuration {
    cpu               = "1024"
    memory            = "2048"
    instance_role_arn = aws_iam_role.instance.arn
  }

  health_check_configuration {
    protocol = "HTTP"
    path     = "/healthz"
  }
}

output "url" {
  value = "https://${aws_apprunner_service.gateway.service_url}"
}
