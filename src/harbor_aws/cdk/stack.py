"""CDK stack for harbor-aws infrastructure.

This is the single source of truth for all AWS resources.
Produces the same resources whether deployed via `cdk deploy` or `python -m harbor_aws deploy`.
"""

from constructs import Construct

import aws_cdk as cdk
from aws_cdk import (
    aws_cloudwatch as cloudwatch,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_iam as iam,
    aws_logs as logs,
)


class HarborAWSStack(cdk.Stack):
    """Shared ECS/Fargate infrastructure for Harbor benchmarks.

    All resources are reused across Docker-based environments.
    Zero cost when idle — pay only for Fargate task runtime.

    Resources created:
    - VPC with 2 public subnets (no NAT gateway = $0 idle)
    - ECS Cluster (Fargate only = $0 idle)
    - IAM Roles (task execution, task)
    - CloudWatch Log Group (7-day retention)
    - CloudWatch Dashboard (ECS + Bedrock metrics)
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
                sid="BedrockInvoke",
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                resources=[
                    f"arn:aws:bedrock:*:{cdk.Aws.ACCOUNT_ID}:inference-profile/*",
                    f"arn:aws:bedrock:*:{cdk.Aws.ACCOUNT_ID}:application-inference-profile/*",
                    "arn:aws:bedrock:*::foundation-model/*",
                ],
            )
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
        # CloudWatch Dashboard — monitor running tasks & resources
        # ============================================================

        dashboard = cloudwatch.Dashboard(
            self,
            "Dashboard",
            dashboard_name=f"{stack_prefix}-monitor",
            default_interval=cdk.Duration.hours(3),
        )

        # --- ECS Fargate ---
        dashboard.add_widgets(cloudwatch.TextWidget(markdown="# ECS Fargate", width=24, height=1))

        task_count = cloudwatch.Metric(
            namespace="ECS/ContainerInsights", metric_name="TaskCount",
            dimensions_map={"ClusterName": cluster.cluster_name}, statistic="Maximum", period=cdk.Duration.minutes(1),
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(title="Task Count", left=[task_count], width=24, height=6),
        )

        # --- Bedrock (per model via SEARCH expressions) ---
        dashboard.add_widgets(cloudwatch.TextWidget(markdown="# Bedrock", width=24, height=1))

        def _bedrock_search(metric_name: str, stat: str = "Sum") -> cloudwatch.MathExpression:
            return cloudwatch.MathExpression(
                expression=f"SEARCH('{{AWS/Bedrock,ModelId}} MetricName=\"{metric_name}\"', '{stat}', 60)",
                label=metric_name,
                period=cdk.Duration.minutes(1),
            )

        dashboard.add_widgets(
            cloudwatch.GraphWidget(title="Input Tokens by Model", left=[_bedrock_search("InputTokenCount")], width=12, height=6),
            cloudwatch.GraphWidget(title="Output Tokens by Model", left=[_bedrock_search("OutputTokenCount")], width=12, height=6),
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(title="Invocations by Model", left=[_bedrock_search("Invocations")], width=8, height=6),
            cloudwatch.GraphWidget(title="Errors by Model", left=[_bedrock_search("InvocationClientErrors")], width=8, height=6),
            cloudwatch.GraphWidget(title="Throttles by Model", left=[_bedrock_search("InvocationThrottles")], width=8, height=6),
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(title="Latency by Model (ms)", left=[_bedrock_search("InvocationLatency", "Average")], width=24, height=6),
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
        cdk.CfnOutput(self, "Region", value=cdk.Aws.REGION)
        cdk.CfnOutput(
            self,
            "DashboardURL",
            value=f"https://{cdk.Aws.REGION}.console.aws.amazon.com/cloudwatch/home?region={cdk.Aws.REGION}#dashboards:name={stack_prefix}-monitor",
        )
