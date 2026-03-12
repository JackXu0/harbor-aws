"""CDK stack for harbor-aws EKS/Fargate infrastructure.

This is the single source of truth for all AWS resources.
"""

from constructs import Construct

import aws_cdk as cdk
from aws_cdk import (
    aws_cloudwatch as cloudwatch,
    aws_ec2 as ec2,
    aws_eks as eks,
    aws_iam as iam,
    aws_logs as logs,
)
from aws_cdk.lambda_layer_kubectl_v31 import KubectlV31Layer


class HarborAWSStack(cdk.Stack):
    """EKS/Fargate infrastructure for Harbor benchmarks.

    All resources are reused across benchmark environments.
    Pay only for EKS control plane ($0.10/hr) + Fargate pod runtime.

    Resources created:
    - VPC with 2 public + 2 private subnets (1 NAT gateway)
    - EKS Cluster with Fargate profile (pods run in private subnets)
    - IAM Roles (Fargate pod execution, pod service account)
    - CloudWatch Log Group (7-day retention)
    - CloudWatch Dashboard (EKS + Bedrock metrics)
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

        self.node.set_context("aws:cdk:disable-metadata", True)

        namespace = "harbor"

        # ============================================================
        # Networking — VPC with public + private subnets
        # Fargate pods must run in private subnets (AWS requirement).
        # 1 NAT gateway (~$32/mo) for outbound internet from pods.
        # ============================================================

        vpc = ec2.Vpc(
            self,
            "VPC",
            vpc_name=f"{stack_prefix}-vpc",
            max_azs=2,
            nat_gateways=1,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    map_public_ip_on_launch=True,
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                ),
            ],
        )

        # ============================================================
        # EKS Cluster with Fargate
        # ============================================================

        cluster = eks.FargateCluster(
            self,
            "Cluster",
            cluster_name=stack_prefix,
            vpc=vpc,
            version=eks.KubernetesVersion.V1_31,
            kubectl_layer=KubectlV31Layer(self, "KubectlLayer"),
            vpc_subnets=[ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)],
            endpoint_access=eks.EndpointAccess.PUBLIC,
            default_profile=eks.FargateProfileOptions(
                selectors=[
                    eks.Selector(namespace=namespace),
                    eks.Selector(namespace="kube-system"),
                ],
            ),
        )

        # Patch CoreDNS to run on Fargate (remove ec2 compute-type annotation)
        coredns_patch = cluster.add_manifest(
            "CoreDnsFargatePatch",
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {
                    "name": "coredns",
                    "namespace": "kube-system",
                },
                "spec": {
                    "template": {
                        "metadata": {
                            "annotations": {
                                "eks.amazonaws.com/compute-type": "fargate",
                            },
                        },
                    },
                },
            },
        )

        # ============================================================
        # Namespace + Service Account for Bedrock access
        # ============================================================

        harbor_ns = cluster.add_manifest(
            "HarborNamespace",
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": namespace},
            },
        )

        # Pod service account with Bedrock permissions (via IRSA)
        pod_sa = cluster.add_service_account(
            "PodServiceAccount",
            name="harbor-pod",
            namespace=namespace,
            annotations={"eks.amazonaws.com/token-expiration": "43200"},
        )
        pod_sa.node.add_dependency(harbor_ns)

        pod_sa.add_to_principal_policy(
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

        pod_sa.add_to_principal_policy(
            iam.PolicyStatement(
                sid="CloudWatchLogs",
                actions=["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogGroups", "logs:DescribeLogStreams"],
                resources=[f"arn:aws:logs:{cdk.Aws.REGION}:{cdk.Aws.ACCOUNT_ID}:log-group:/harbor-aws/*"],
            )
        )

        # ============================================================
        # CloudWatch Log Group
        # ============================================================

        logs.LogGroup(
            self,
            "EKSLogs",
            log_group_name=f"/harbor-aws/{stack_prefix}",
            retention=logs.RetentionDays.ONE_WEEK,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )

        # ============================================================
        # CloudWatch Dashboard
        # ============================================================

        dashboard = cloudwatch.Dashboard(
            self,
            "Dashboard",
            dashboard_name=f"{stack_prefix}-monitor",
            default_interval=cdk.Duration.hours(3),
        )

        # --- EKS/Fargate Pod Metrics ---
        dashboard.add_widgets(cloudwatch.TextWidget(markdown="# EKS Fargate", width=24, height=1))

        pod_count = cloudwatch.Metric(
            namespace="ContainerInsights",
            metric_name="pod_number_of_running_pods",
            dimensions_map={"ClusterName": cluster.cluster_name},
            statistic="Maximum",
            period=cdk.Duration.minutes(1),
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(title="Running Pods", left=[pod_count], width=24, height=6),
        )

        cpu_metric = cloudwatch.Metric(
            namespace="ContainerInsights",
            metric_name="pod_cpu_utilization",
            dimensions_map={"ClusterName": cluster.cluster_name},
            statistic="Average",
            period=cdk.Duration.minutes(1),
        )
        memory_metric = cloudwatch.Metric(
            namespace="ContainerInsights",
            metric_name="pod_memory_utilization",
            dimensions_map={"ClusterName": cluster.cluster_name},
            statistic="Average",
            period=cdk.Duration.minutes(1),
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(title="Pod CPU Utilization", left=[cpu_metric], width=12, height=6),
            cloudwatch.GraphWidget(title="Pod Memory Utilization", left=[memory_metric], width=12, height=6),
        )

        # --- Bedrock ---
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
        # Outputs
        # ============================================================

        cdk.CfnOutput(self, "EksClusterName", value=cluster.cluster_name)
        cdk.CfnOutput(self, "Namespace", value=namespace)
        cdk.CfnOutput(self, "Region", value=cdk.Aws.REGION)
        cdk.CfnOutput(
            self,
            "DashboardURL",
            value=f"https://{cdk.Aws.REGION}.console.aws.amazon.com/cloudwatch/home?region={cdk.Aws.REGION}#dashboards:name={stack_prefix}-monitor",
        )
