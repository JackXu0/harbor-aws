"""CDK stack for harbor-aws EKS/Fargate infrastructure.

This is the single source of truth for all AWS resources.
Deploys a fully functional harbor-aws cluster including the L3 control
plane (harbor-control Deployment, NLB, ConfigMap) — no manual kubectl
or docker commands required after ``python -m harbor_aws deploy``.
"""

# ruff: noqa: I001 — ruff isort wants to split the multi-alias aws_cdk
# block into one import per submodule, which is uglier than this form.
import os
import secrets

import aws_cdk as cdk
from aws_cdk import (
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_ecr_assets as ecr_assets,
    aws_eks as eks,
    aws_iam as iam,
    aws_logs as logs,
)
from aws_cdk.lambda_layer_kubectl_v31 import KubectlV31Layer
from constructs import Construct


class HarborAWSStack(cdk.Stack):
    """EKS/Fargate infrastructure for Harbor benchmarks.

    All resources are reused across benchmark environments.
    Pay only for EKS control plane ($0.10/hr) + Fargate pod runtime.

    Resources created:
    - VPC with 2 public + 2 private subnets (1 NAT gateway)
    - EKS Cluster with Fargate profile (pods run in private subnets)
    - IAM Roles (Fargate pod execution, pod service account)
    - CloudWatch Log Group (7-day retention)
    - harbor-control Deployment + ClusterIP + NLB Services
    - harbor-runner ConfigMap (runner.sh)
    - AWS Load Balancer Controller (Helm chart + IRSA)
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        stack_prefix: str = "harbor-aws",
        project_root: str | None = None,
        runner_account_ids: list[str] | None = None,
        docker_hub_secret_arn: str | None = None,
        cluster_admin_role_arn: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Resolve project root for Docker build context + runner.sh
        if project_root is None:
            project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

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

        # Grant the deployer's IAM role cluster admin access.
        # CDK creates the cluster via its own deploy role, so the caller's
        # role needs to be explicitly added to aws-auth for kubectl access.
        if cluster_admin_role_arn:
            cluster.aws_auth.add_masters_role(
                iam.Role.from_role_arn(self, "ClusterAdminRole", cluster_admin_role_arn)
            )

        # Grant ECR pull-through cache permissions to Fargate pod execution role.
        cluster.default_profile.pod_execution_role.add_to_principal_policy(
            iam.PolicyStatement(
                sid="ECRPullThroughCache",
                actions=["ecr:CreateRepository", "ecr:BatchImportUpstreamImage"],
                resources=[f"arn:aws:ecr:{cdk.Aws.REGION}:{cdk.Aws.ACCOUNT_ID}:repository/docker-hub/*"],
            )
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
        # ECR Pull-Through Cache for Docker Hub (optional)
        # Requires a Secrets Manager secret with Docker Hub credentials.
        # Without it, pods pull directly from Docker Hub (may hit rate limits).
        # ============================================================

        if docker_hub_secret_arn:
            ecr.CfnPullThroughCacheRule(
                self,
                "DockerHubCache",
                ecr_repository_prefix="docker-hub",
                upstream_registry_url="registry-1.docker.io",
                credential_arn=docker_hub_secret_arn,
            )

        # VPC endpoint for ECR — pods pull images without going through NAT
        vpc.add_interface_endpoint(
            "EcrApiEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.ECR,
        )
        vpc.add_interface_endpoint(
            "EcrDkrEndpoint",
            service=ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
        )
        vpc.add_gateway_endpoint(
            "S3Endpoint",
            service=ec2.GatewayVpcEndpointAwsService.S3,
        )

        # ============================================================
        # AWS Load Balancer Controller (provisions NLBs from K8s Services)
        # ============================================================

        lb_sa = cluster.add_service_account(
            "LBControllerSA",
            name="aws-load-balancer-controller",
            namespace="kube-system",
        )
        lb_sa.add_to_principal_policy(
            iam.PolicyStatement(
                actions=[
                    "ec2:Describe*",
                    "ec2:AuthorizeSecurityGroupIngress",
                    "ec2:RevokeSecurityGroupIngress",
                    "ec2:CreateSecurityGroup",
                    "ec2:DeleteSecurityGroup",
                    "ec2:CreateTags",
                    "ec2:DeleteTags",
                    "elasticloadbalancing:*",
                    "iam:CreateServiceLinkedRole",
                    "tag:GetResources",
                    "tag:TagResources",
                ],
                resources=["*"],
            )
        )
        lb_chart = cluster.add_helm_chart(
            "LBController",
            chart="aws-load-balancer-controller",
            repository="https://aws.github.io/eks-charts",
            namespace="kube-system",
            release="aws-load-balancer-controller",
            values={
                "clusterName": stack_prefix,
                "serviceAccount": {"create": False, "name": "aws-load-balancer-controller"},
                "region": cdk.Aws.REGION,
                "vpcId": vpc.vpc_id,
                "enableShield": False,
                "enableWaf": False,
                "enableWafv2": False,
            },
            wait=True,
        )
        lb_chart.node.add_dependency(lb_sa)

        # ============================================================
        # harbor-control: Docker image, Deployment, Services, ConfigMap
        # ============================================================

        admin_token = secrets.token_urlsafe(32)

        # Build & push the harbor-control image to a CDK-managed ECR repo.
        harbor_control_image = ecr_assets.DockerImageAsset(
            self,
            "HarborControlImage",
            directory=project_root,
            file="docker/harbor-control/Dockerfile",
            platform=ecr_assets.Platform.LINUX_AMD64,
        )
        # Grant Fargate execution role permission to pull the image.
        harbor_control_image.repository.grant_pull(cluster.default_profile.pod_execution_role)

        # ConfigMap with runner.sh (mounted into every trial pod as PID 1).
        runner_sh_path = os.path.join(project_root, "src", "harbor_aws", "runner.sh")
        with open(runner_sh_path) as f:
            runner_sh_content = f.read()

        runner_configmap = cluster.add_manifest(
            "RunnerConfigMap",
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "harbor-runner", "namespace": namespace},
                "data": {"runner.sh": runner_sh_content},
            },
        )
        runner_configmap.node.add_dependency(harbor_ns)

        # Kubernetes Secret for the admin token (so kubectl can read it).
        import base64 as _b64
        admin_token_b64 = _b64.b64encode(admin_token.encode()).decode()

        admin_secret = cluster.add_manifest(
            "HarborControlAdminSecret",
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "harbor-control-admin", "namespace": namespace},
                "data": {"token": admin_token_b64},
            },
        )
        admin_secret.node.add_dependency(harbor_ns)

        # harbor-control Deployment (the in-VPC gateway).
        harbor_control_deploy = cluster.add_manifest(
            "HarborControlDeployment",
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "harbor-control", "namespace": namespace},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": "harbor-control"}},
                    "template": {
                        "metadata": {"labels": {"app": "harbor-control"}},
                        "spec": {
                            "containers": [{
                                "name": "harbor-control",
                                "image": harbor_control_image.image_uri,
                                "ports": [
                                    {"containerPort": 8443, "name": "http-api"},
                                    {"containerPort": 8444, "name": "runner-accept"},
                                ],
                                "env": [
                                    {"name": "HARBOR_ADMIN_TOKEN", "value": admin_token},
                                    {"name": "HARBOR_CONTROL_PORT", "value": "8443"},
                                    {"name": "HARBOR_RUNNER_PORT", "value": "8444"},
                                ],
                                "resources": {
                                    "requests": {"cpu": "1", "memory": "2Gi"},
                                    "limits": {"cpu": "1", "memory": "2Gi"},
                                },
                                "readinessProbe": {
                                    "httpGet": {"path": "/healthz", "port": 8443},
                                    "initialDelaySeconds": 5,
                                    "periodSeconds": 10,
                                },
                            }],
                        },
                    },
                },
            },
        )
        harbor_control_deploy.node.add_dependency(harbor_ns)
        harbor_control_deploy.node.add_dependency(runner_configmap)

        # ClusterIP Service — runner pods dial port 8444 via in-cluster DNS.
        runner_svc = cluster.add_manifest(
            "HarborControlClusterIP",
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "harbor-control", "namespace": namespace},
                "spec": {
                    "selector": {"app": "harbor-control"},
                    "ports": [
                        {"port": 8443, "targetPort": 8443, "name": "http-api"},
                        {"port": 8444, "targetPort": 8444, "name": "runner-accept"},
                    ],
                    "type": "ClusterIP",
                },
            },
        )
        runner_svc.node.add_dependency(harbor_control_deploy)

        # NLB Service — orchestrator talks to harbor-control from outside the VPC.
        nlb_svc = cluster.add_manifest(
            "HarborControlNLB",
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {
                    "name": "harbor-control-nlb",
                    "namespace": namespace,
                    "annotations": {
                        "service.beta.kubernetes.io/aws-load-balancer-type": "external",
                        "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type": "ip",
                        "service.beta.kubernetes.io/aws-load-balancer-scheme": "internet-facing",
                    },
                },
                "spec": {
                    "selector": {"app": "harbor-control"},
                    "ports": [{"port": 8443, "targetPort": 8443, "protocol": "TCP"}],
                    "type": "LoadBalancer",
                },
            },
        )
        nlb_svc.node.add_dependency(lb_chart)
        nlb_svc.node.add_dependency(harbor_control_deploy)

        # ============================================================
        # Cross-Account Runner Role (optional)
        # Allows specified AWS accounts to assume a scoped role for
        # running benchmarks without deployer-level permissions.
        # ============================================================

        if runner_account_ids:
            runner_role = iam.Role(
                self,
                "RunnerRole",
                role_name=f"{stack_prefix}-runner",
                assumed_by=iam.CompositePrincipal(
                    *[iam.AccountPrincipal(acc) for acc in runner_account_ids]
                ),
                max_session_duration=cdk.Duration.hours(12),
            )

            runner_role.add_to_policy(
                iam.PolicyStatement(
                    sid="EKSAccess",
                    actions=["eks:DescribeCluster", "eks:AccessKubernetesApi"],
                    resources=[cluster.cluster_arn],
                )
            )

            runner_role.add_to_policy(
                iam.PolicyStatement(
                    sid="StackConfig",
                    actions=["cloudformation:DescribeStacks"],
                    resources=[cdk.Aws.STACK_ID],
                )
            )

            runner_role.add_to_policy(
                iam.PolicyStatement(
                    sid="Identity",
                    actions=["sts:GetCallerIdentity"],
                    resources=["*"],
                )
            )

            runner_role.add_to_policy(
                iam.PolicyStatement(
                    sid="ECRPull",
                    actions=[
                        "ecr:GetAuthorizationToken",
                        "ecr:BatchGetImage",
                        "ecr:GetDownloadUrlForLayer",
                    ],
                    resources=["*"],
                )
            )

            # Grant the runner role Kubernetes RBAC access to the harbor namespace
            cluster.aws_auth.add_role_mapping(
                runner_role,
                groups=["system:masters"],
            )

            cdk.CfnOutput(self, "RunnerRoleArn", value=runner_role.role_arn)

        # ============================================================
        # Outputs
        # ============================================================

        cdk.CfnOutput(self, "EksClusterName", value=cluster.cluster_name)
        cdk.CfnOutput(self, "Namespace", value=namespace)
        cdk.CfnOutput(self, "Region", value=cdk.Aws.REGION)
        cdk.CfnOutput(self, "PodServiceAccount", value=pod_sa.service_account_name)
        cdk.CfnOutput(self, "HarborAdminToken", value=admin_token)
