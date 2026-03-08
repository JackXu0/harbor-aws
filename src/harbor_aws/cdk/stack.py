"""CDK stack for harbor-aws infrastructure.

This is the single source of truth for all AWS resources.
Produces the same resources whether deployed via `cdk deploy` or auto-provisioned.
"""

from constructs import Construct

import aws_cdk as cdk
from aws_cdk import (
    aws_codebuild as codebuild,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_ecs as ecs,
    aws_iam as iam,
    aws_logs as logs,
    aws_s3 as s3,
)


class HarborAWSStack(cdk.Stack):
    """Shared ECS/Fargate infrastructure for Harbor benchmarks.

    All resources are reused across Docker-based environments.
    Zero cost when idle — pay only for Fargate task runtime and CodeBuild minutes.

    Resources created:
    - VPC with 2 public subnets (no NAT gateway = $0 idle)
    - ECS Cluster (Fargate only = $0 idle)
    - ECR Repository (~$0.10/GB stored images)
    - S3 Bucket (build context, auto-expires after 7 days)
    - CodeBuild Project ($0 idle, LARGE compute for fast builds)
    - IAM Roles (task execution, task, codebuild)
    - CloudWatch Log Group (7-day retention)
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        stack_prefix: str = "harbor-aws",
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Disable CDK metadata to keep the template clean for boto3 deployment
        self.node.set_context("aws:cdk:disable-metadata", True)

        # ============================================================
        # Networking — VPC with public subnets only (no NAT = $0 idle)
        # ============================================================

        vpc = ec2.Vpc(
            self,
            "VPC",
            vpc_name=f"{stack_prefix}-vpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    map_public_ip_on_launch=True,
                ),
            ],
        )

        sg = ec2.SecurityGroup(
            self,
            "SG",
            vpc=vpc,
            security_group_name=f"{stack_prefix}-sg",
            description=f"{stack_prefix} - Egress only for benchmark containers",
            allow_all_outbound=True,
        )

        # ============================================================
        # ECS Cluster — Fargate only, $0 when no tasks running
        # ============================================================

        cluster = ecs.Cluster(
            self,
            "Cluster",
            cluster_name=stack_prefix,
            vpc=vpc,
            container_insights_v2=ecs.ContainerInsights.ENABLED,
            execute_command_configuration=ecs.ExecuteCommandConfiguration(
                logging=ecs.ExecuteCommandLogging.DEFAULT,
            ),
        )

        # ============================================================
        # ECR Repository
        # ============================================================

        ecr_repo = ecr.Repository(
            self,
            "ECR",
            repository_name=stack_prefix,
            image_scan_on_push=False,
            lifecycle_rules=[
                ecr.LifecycleRule(
                    description="Keep last 50 images",
                    max_image_count=50,
                ),
            ],
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )

        # ============================================================
        # S3 Bucket — build context staging, auto-cleanup after 7 days
        # ============================================================

        bucket = s3.Bucket(
            self,
            "Bucket",
            bucket_name=f"{stack_prefix}-{cdk.Aws.ACCOUNT_ID}-{cdk.Aws.REGION}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="CleanupOldArtifacts",
                    enabled=True,
                    expiration=cdk.Duration.days(7),
                ),
            ],
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # ============================================================
        # IAM Roles
        # ============================================================

        task_execution_role = iam.Role(
            self,
            "TaskExecRole",
            role_name=f"{stack_prefix}-task-execution",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                ),
            ],
        )
        task_execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"arn:aws:logs:{cdk.Aws.REGION}:{cdk.Aws.ACCOUNT_ID}:log-group:/harbor-aws/*"],
            )
        )

        task_role = iam.Role(
            self,
            "TaskRole",
            role_name=f"{stack_prefix}-task",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="SSMForECSExec",
                actions=[
                    "ssmmessages:CreateControlChannel",
                    "ssmmessages:CreateDataChannel",
                    "ssmmessages:OpenControlChannel",
                    "ssmmessages:OpenDataChannel",
                ],
                resources=["*"],
            )
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="CloudWatchLogs",
                actions=["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogGroups", "logs:DescribeLogStreams"],
                resources=[f"arn:aws:logs:{cdk.Aws.REGION}:{cdk.Aws.ACCOUNT_ID}:log-group:/harbor-aws/*"],
            )
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                sid="S3FileTransfer",
                actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
                resources=[bucket.bucket_arn, f"{bucket.bucket_arn}/*"],
            )
        )

        codebuild_role = iam.Role(
            self,
            "CodeBuildRole",
            role_name=f"{stack_prefix}-codebuild",
            assumed_by=iam.ServicePrincipal("codebuild.amazonaws.com"),
        )
        codebuild_role.add_to_policy(
            iam.PolicyStatement(actions=["ecr:GetAuthorizationToken"], resources=["*"])
        )
        codebuild_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage",
                    "ecr:PutImage", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload",
                ],
                resources=[ecr_repo.repository_arn],
            )
        )
        codebuild_role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:GetBucketLocation", "s3:ListBucket"],
                resources=[bucket.bucket_arn, f"{bucket.bucket_arn}/*"],
            )
        )
        codebuild_role.add_to_policy(
            iam.PolicyStatement(
                actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                resources=[f"arn:aws:logs:{cdk.Aws.REGION}:{cdk.Aws.ACCOUNT_ID}:log-group:/aws/codebuild/*"],
            )
        )

        # ============================================================
        # CodeBuild — LARGE compute for max build speed, $0 when idle
        # ============================================================

        cb_log_group = logs.LogGroup(
            self,
            "CBLogs",
            log_group_name=f"/aws/codebuild/{stack_prefix}-builder",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        cb_project = codebuild.Project(
            self,
            "Builder",
            project_name=f"{stack_prefix}-builder",
            description="Builds Docker images for Harbor benchmark environments",
            role=codebuild_role,
            source=codebuild.Source.s3(
                bucket=bucket,
                path=f"{stack_prefix}/build-context/",
            ),
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
                compute_type=codebuild.ComputeType.LARGE,
                privileged=True,
            ),
            build_spec=codebuild.BuildSpec.from_object({
                "version": 0.2,
                "phases": {"build": {"commands": ["echo 'Default buildspec - overridden at build time'"]}},
            }),
            timeout=cdk.Duration.minutes(30),
            logging=codebuild.LoggingOptions(
                cloud_watch=codebuild.CloudWatchLoggingOptions(enabled=True, log_group=cb_log_group),
            ),
        )

        # ============================================================
        # CloudWatch Log Group for ECS tasks
        # ============================================================

        logs.LogGroup(
            self,
            "ECSLogs",
            log_group_name=f"/harbor-aws/{stack_prefix}",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # ============================================================
        # Outputs — consumed by harbor-aws Python client
        # ============================================================

        public_subnets = vpc.select_subnets(subnet_type=ec2.SubnetType.PUBLIC)

        cdk.CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        cdk.CfnOutput(self, "SubnetIds", value=",".join([s.subnet_id for s in public_subnets.subnets]))
        cdk.CfnOutput(self, "SecurityGroupId", value=sg.security_group_id)
        cdk.CfnOutput(self, "TaskExecutionRoleArn", value=task_execution_role.role_arn)
        cdk.CfnOutput(self, "TaskRoleArn", value=task_role.role_arn)
        cdk.CfnOutput(self, "ECRRepositoryUri", value=ecr_repo.repository_uri)
        cdk.CfnOutput(self, "CodeBuildProjectName", value=cb_project.project_name)
        cdk.CfnOutput(self, "S3BucketName", value=bucket.bucket_name)
        cdk.CfnOutput(self, "Region", value=cdk.Aws.REGION)
