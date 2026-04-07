"""Pod-side runner that ships into Fargate pods.

The runner script is intentionally stdlib-only so it can run inside any
benchmark base image without pip installs. harbor-aws ships it into the pod
as a ConfigMap-backed file at start time.
"""
