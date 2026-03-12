"""CDK stack for harbor-aws EKS/Fargate infrastructure.

This is the single source of truth for all AWS resources.
"""

from constructs import Construct

import aws_cdk as cdk
from aws_cdk import (
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
        # Outputs
        # ============================================================

        cdk.CfnOutput(self, "EksClusterName", value=cluster.cluster_name)
        cdk.CfnOutput(self, "Namespace", value=namespace)
        cdk.CfnOutput(self, "Region", value=cdk.Aws.REGION)
