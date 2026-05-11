"""CDK stack for harbor-aws EKS/Fargate infrastructure"""

import base64
import datetime
import os
import secrets

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_eks as eks
from aws_cdk import aws_iam as iam
from aws_cdk.lambda_layer_kubectl_v33 import KubectlV33Layer
from constructs import Construct
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

APP_LABEL = "harbor-control"
API_PORT = 8443
RUNNER_PORT = 8444


class HarborAWSStack(cdk.Stack):
    """EKS/Fargate infrastructure for Harbor benchmarks.

    All resources are reused across benchmark environments.
    Pay only for EKS control plane ($0.10/hr) + Fargate pod runtime.

    Resources created:
    - VPC with 2 public + 2 private subnets (1 NAT gateway)
    - VPC endpoints for ECR + S3 (faster pulls, less NAT traffic)
    - EKS Cluster with Fargate profile (pods run in private subnets)
    - IAM: Fargate pod execution role, harbor-pod service account, LB controller IRSA
    - harbor namespace
    - control pod: Docker image (built to CDK-managed ECR), Deployment,
      ClusterIP + NLB Services
    - harbor-runner ConfigMap (runner.sh)
    - AWS Load Balancer Controller (Helm chart + IRSA)

    Optional (gated by constructor args):
    - ECR Pull-Through Cache rule
    - Cross-account access IAM role
    - Cluster admin role arns
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        stack_prefix: str = "harbor-aws",
        cross_account_caller_ids: list[str] | None = None,
        docker_hub_secret_arn: str | None = None,
        cluster_admin_role_arn: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Package root (src/harbor_aws/) — both Dockerfile and runner.sh ship with the package.
        pkg_root = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
        self.node.set_context("aws:cdk:disable-metadata", True)

        namespace = "harbor"
        bearer_token = secrets.token_urlsafe(32)
        tls_cert_pem, tls_key_pem = _generate_self_signed_cert()

        # ============================================================
        # VPC — public + private subnets, 1 NAT for outbound (~$32/mo).
        # Fargate pods must run in private subnets (AWS requirement).
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
        # EKS Fargate cluster + admin role mapping + ECR pull permissions
        # ============================================================
        cluster = eks.FargateCluster(
            self,
            "Cluster",
            cluster_name=stack_prefix,
            vpc=vpc,
            version=eks.KubernetesVersion.V1_33,
            kubectl_layer=KubectlV33Layer(self, "KubectlLayer"),
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
        if cluster_admin_role_arn:
            cluster.aws_auth.add_masters_role(
                iam.Role.from_role_arn(self, "ClusterAdminRole", cluster_admin_role_arn),
            )

        # ============================================================
        # harbor namespace + IRSA service account for in-pod agents
        # ============================================================
        harbor_ns = cluster.add_manifest(
            "HarborNamespace",
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": namespace},
            },
        )

        # Grants AWS permissions to the agent running inside the pod (via IRSA).
        # Pods opt in by setting serviceAccountName=agent-inside-pod in their spec.
        pod_sa = cluster.add_service_account(
            "PodServiceAccount",
            name="agent-inside-pod",
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
            ),
        )

        # ============================================================
        # ECR pull-through cache (optional) + VPC endpoints
        # Pods pull images without going through NAT.
        # ============================================================
        if docker_hub_secret_arn:
            ecr.CfnPullThroughCacheRule(
                self,
                "DockerHubCache",
                ecr_repository_prefix="docker-hub",
                upstream_registry_url="registry-1.docker.io",
                credential_arn=docker_hub_secret_arn,
            )
            # Pod execution role needs these to actually pull through the cache.
            cluster.default_profile.pod_execution_role.add_to_principal_policy(
                iam.PolicyStatement(
                    sid="ECRPullThroughCache",
                    actions=["ecr:CreateRepository", "ecr:BatchImportUpstreamImage"],
                    resources=[f"arn:aws:ecr:{cdk.Aws.REGION}:{cdk.Aws.ACCOUNT_ID}:repository/docker-hub/*"],
                ),
            )
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
        # NLB provisioner (AWS Load Balancer Controller)
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
            ),
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
            },
            wait=True,
        )
        lb_chart.node.add_dependency(lb_sa)

        # ============================================================
        # control pod: Docker image, runner.sh ConfigMap, Deployment, Services
        # ============================================================
        # Build & push the control-pod image to a CDK-managed ECR repo.
        control_pod_image = ecr_assets.DockerImageAsset(
            self,
            "HarborControlImage",
            directory=pkg_root,
            file="Dockerfile",
            platform=ecr_assets.Platform.LINUX_AMD64,
        )
        control_pod_image.repository.grant_pull(cluster.default_profile.pod_execution_role)

        # ConfigMap with runner.sh (mounted into every trial pod as PID 1).
        runner_sh_path = os.path.join(pkg_root, "runner.sh")
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

        # TLS cert/key for the control pod's HTTPS API (port 8443).
        tls_secret = cluster.add_manifest(
            "HarborControlTLSSecret",
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "type": "kubernetes.io/tls",
                "metadata": {"name": "harbor-control-tls", "namespace": namespace},
                "data": {
                    "tls.crt": base64.b64encode(tls_cert_pem.encode()).decode(),
                    "tls.key": base64.b64encode(tls_key_pem.encode()).decode(),
                },
            },
        )
        tls_secret.node.add_dependency(harbor_ns)

        # control pod Deployment (the in-VPC gateway).
        control_pod_deploy = cluster.add_manifest(
            "HarborControlDeployment",
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "harbor-control", "namespace": namespace},
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": APP_LABEL}},
                    "template": {
                        "metadata": {"labels": {"app": APP_LABEL}},
                        "spec": {
                            "containers": [{
                                "name": APP_LABEL,
                                "image": control_pod_image.image_uri,
                                "ports": [
                                    {"containerPort": API_PORT, "name": "https-api"},
                                    {"containerPort": RUNNER_PORT, "name": "runner-accept"},
                                ],
                                "env": [
                                    {"name": "HARBOR_BEARER_TOKEN", "value": bearer_token},
                                    {"name": "HARBOR_CONTROL_PORT", "value": str(API_PORT)},
                                    {"name": "HARBOR_RUNNER_PORT", "value": str(RUNNER_PORT)},
                                    {"name": "HARBOR_TLS_CERT_FILE", "value": "/tls/tls.crt"},
                                    {"name": "HARBOR_TLS_KEY_FILE", "value": "/tls/tls.key"},
                                ],
                                "volumeMounts": [
                                    {"name": "tls", "mountPath": "/tls", "readOnly": True},
                                ],
                                "resources": {
                                    "requests": {"cpu": "16", "memory": "120Gi"},
                                    "limits": {"cpu": "16", "memory": "120Gi"},
                                },
                                "readinessProbe": {
                                    # scheme=HTTPS; kubelet skips cert verification for self-signed.
                                    "httpGet": {"path": "/healthz", "port": API_PORT, "scheme": "HTTPS"},
                                    "initialDelaySeconds": 5,
                                },
                            }],
                            "volumes": [
                                {"name": "tls", "secret": {"secretName": "harbor-control-tls"}},
                            ],
                        },
                    },
                },
            },
        )
        control_pod_deploy.node.add_dependency(harbor_ns)
        control_pod_deploy.node.add_dependency(runner_configmap)
        control_pod_deploy.node.add_dependency(tls_secret)

        # ClusterIP Service — runner pods dial port 8444 via in-cluster DNS.
        runner_svc = cluster.add_manifest(
            "HarborControlClusterIP",
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": "harbor-control", "namespace": namespace},
                "spec": {
                    "selector": {"app": APP_LABEL},
                    "ports": [
                        {"port": API_PORT, "targetPort": API_PORT, "name": "https-api"},
                        {"port": RUNNER_PORT, "targetPort": RUNNER_PORT, "name": "runner-accept"},
                    ],
                    "type": "ClusterIP",
                },
            },
        )
        runner_svc.node.add_dependency(control_pod_deploy)

        # NLB Service — orchestrator talks to the control pod from outside the VPC.
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
                    "selector": {"app": APP_LABEL},
                    "ports": [{"port": API_PORT, "targetPort": API_PORT, "protocol": "TCP"}],
                    "type": "LoadBalancer",
                },
            },
        )
        nlb_svc.node.add_dependency(lb_chart)
        nlb_svc.node.add_dependency(control_pod_deploy)

        # ============================================================
        # Cross-account access (optional)
        # Allows specified AWS accounts to run benchmarks without
        # deployer-level permissions.
        # ============================================================
        if cross_account_caller_ids:
            cross_account_access_role = iam.Role(
                self,
                "RunnerRole",
                role_name=f"{stack_prefix}-runner",
                assumed_by=iam.CompositePrincipal(
                    *[iam.AccountPrincipal(acc) for acc in cross_account_caller_ids],
                ),
                max_session_duration=cdk.Duration.hours(12),
            )
            cross_account_access_role.add_to_policy(
                iam.PolicyStatement(
                    sid="EKSAccess",
                    actions=["eks:DescribeCluster", "eks:AccessKubernetesApi"],
                    resources=[cluster.cluster_arn],
                ),
            )
            cross_account_access_role.add_to_policy(
                iam.PolicyStatement(
                    sid="StackConfig",
                    actions=["cloudformation:DescribeStacks"],
                    resources=[cdk.Aws.STACK_ID],
                ),
            )
            cross_account_access_role.add_to_policy(
                iam.PolicyStatement(
                    sid="Identity",
                    actions=["sts:GetCallerIdentity"],
                    resources=["*"],
                ),
            )
            cross_account_access_role.add_to_policy(
                iam.PolicyStatement(
                    sid="ECRPull",
                    actions=[
                        "ecr:GetAuthorizationToken",
                        "ecr:BatchGetImage",
                        "ecr:GetDownloadUrlForLayer",
                    ],
                    resources=["*"],
                ),
            )

            # Map the IAM role to a custom K8s group, then bind that group to a
            # namespace-scoped Role. Avoids system:masters (which would grant
            # cluster-wide admin, including kube-system).
            runner_k8s_group = f"{stack_prefix}-runners"
            cluster.aws_auth.add_role_mapping(
                cross_account_access_role,
                groups=[runner_k8s_group],
            )
            runner_rbac = cluster.add_manifest(
                "RunnerNamespaceRBAC",
                {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "Role",
                    "metadata": {"name": "harbor-runner", "namespace": namespace},
                    "rules": [
                        {
                            "apiGroups": [""],
                            "resources": ["pods", "pods/status", "pods/log"],
                            "verbs": ["get", "list", "watch", "create", "delete"],
                        },
                        {
                            "apiGroups": [""],
                            "resources": ["configmaps", "secrets"],
                            "verbs": ["get", "list", "create"],
                        },
                    ],
                },
            )
            runner_rbac.node.add_dependency(harbor_ns)

            runner_binding = cluster.add_manifest(
                "RunnerNamespaceRBACBinding",
                {
                    "apiVersion": "rbac.authorization.k8s.io/v1",
                    "kind": "RoleBinding",
                    "metadata": {"name": "harbor-runner", "namespace": namespace},
                    "subjects": [
                        {"kind": "Group", "name": runner_k8s_group, "apiGroup": "rbac.authorization.k8s.io"},
                    ],
                    "roleRef": {
                        "kind": "Role",
                        "name": "harbor-runner",
                        "apiGroup": "rbac.authorization.k8s.io",
                    },
                },
            )
            runner_binding.node.add_dependency(runner_rbac)

            cdk.CfnOutput(self, "CrossAccountAccessRoleArn", value=cross_account_access_role.role_arn)

        # ============================================================
        # CloudFormation outputs
        # ============================================================
        cdk.CfnOutput(self, "EksClusterName", value=cluster.cluster_name)
        cdk.CfnOutput(self, "Namespace", value=namespace)
        cdk.CfnOutput(self, "PodServiceAccount", value=pod_sa.service_account_name)
        cdk.CfnOutput(self, "HarborAdminToken", value=bearer_token)
        cdk.CfnOutput(self, "HarborNlbCert", value=tls_cert_pem)
        cdk.CfnOutput(self, "DockerHubCacheEnabled", value=str(docker_hub_secret_arn is not None).lower())


def _generate_self_signed_cert() -> tuple[str, str]:
    """Generate a self-signed RSA cert + key for the control pod's HTTPS API."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "harbor-aws-control"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    return cert_pem, key_pem
