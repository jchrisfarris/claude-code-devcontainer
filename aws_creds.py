#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3"]
# ///
"""AWS credential management for Claude Code devcontainer.

Commands:
  aws-creds       Assume ClaudeDevContainer role and inject credentials into container
  aws-setup-role  Create the ClaudeDevContainer IAM role in the target account

Workflow:
  1. Run 'devc aws-creds --profile myprofile' to inject scoped credentials
  2. Claude operates read-only by default (session-policy.json)
  3. When Claude needs more permissions, it edits /workspace/session-policy.json
  4. Re-run 'devc aws-creds --profile myprofile' to apply the updated policy
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, NoCredentialsError, ProfileNotFound

# Edit these to customize default behavior across all containers
DEFAULT_ROLE_NAME = "ClaudeDevContainer"
DEFAULT_MANAGED_POLICY_ARN = "arn:aws:iam::aws:policy/AdministratorAccess"
DEFAULT_SESSION_DURATION_SECONDS = 21600  # 6 hours

# AWS session policy size limit (characters, not URL-encoded)
SESSION_POLICY_MAX_CHARS = 2048


def err(msg: str) -> None:
    print(f"[devc] {msg}", file=sys.stderr)


def die(msg: str) -> None:
    err(msg)
    sys.exit(1)


def get_session(profile: str) -> boto3.Session:
    try:
        return boto3.Session(profile_name=profile)
    except ProfileNotFound:
        die(f"AWS profile '{profile}' not found. Check ~/.aws/config")


def get_caller_identity(session: boto3.Session) -> dict:
    try:
        return session.client("sts").get_caller_identity()
    except NoCredentialsError:
        die(
            f"No valid credentials found.\n"
            "For SSO profiles, run: aws sso login --profile <profile>"
        )
    except ClientError as e:
        die(f"Failed to get caller identity: {e}")


def extract_session_name(caller_arn: str, project: str) -> str:
    """Build a session name from the caller ARN and project name.

    For SSO, caller_arn looks like:
      arn:aws:sts::123:assumed-role/AWSReservedSSO_Admin_xxxx/chris@example.com
    The last segment (after the final /) is the SSO username.

    Session names must match [\\w+=,.@-] and be <= 64 chars.
    """
    username = caller_arn.rsplit("/", 1)[-1]
    raw = f"devc-{project}-{username}"
    sanitized = "".join(c for c in raw if c.isalnum() or c in "+=,.@-_")
    return sanitized[:64]


def ensure_role(iam, account_id: str, role_name: str) -> str:
    """Return the role ARN, creating the role if it doesn't exist."""
    try:
        arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        err(f"Found existing role: {arn}")
        return arn
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise

    err(f"Role '{role_name}' not found — creating it now...")
    return create_role(iam, account_id, role_name)


def create_role(iam, account_id: str, role_name: str) -> str:
    """Create the devcontainer IAM role and attach the managed policy."""
    trust_policy = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"AWS": f"arn:aws:iam::{account_id}:root"},
            "Action": "sts:AssumeRole",
        }],
    })

    try:
        response = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=trust_policy,
            MaxSessionDuration=43200,  # 12 hours (role ceiling; session uses DEFAULT_SESSION_DURATION_SECONDS)
            Description=(
                "Claude Code devcontainer role - managed by devc. "
                "Actual permissions are constrained by the session policy in "
                "/workspace/session-policy.json inside the container."
            ),
        )
        arn = response["Role"]["Arn"]
    except ClientError as e:
        die(f"Failed to create role '{role_name}': {e}")

    try:
        iam.attach_role_policy(RoleName=role_name, PolicyArn=DEFAULT_MANAGED_POLICY_ARN)
    except ClientError as e:
        die(f"Failed to attach policy to '{role_name}': {e}")

    err(f"Created role: {arn}")
    err(f"Attached managed policy: {DEFAULT_MANAGED_POLICY_ARN}")
    return arn


def load_session_policy(workspace: Path, script_dir: Path) -> str:
    """Load session policy from the workspace directory, falling back to the repo default.

    /workspace in the container is a bind mount of the host workspace folder, so
    reading workspace/session-policy.json here picks up any edits Claude made inside
    the container without needing docker exec.
    """
    workspace_policy = workspace / "session-policy.json"
    if workspace_policy.exists():
        err(f"Using session policy from {workspace_policy}")
        return workspace_policy.read_text()

    default = script_dir / "aws-config" / "session-policy.json"
    if not default.exists():
        die(f"Default session policy not found at {default}")
    err(f"Using default session policy from {default}")
    return default.read_text()


def get_container_id(workspace: str) -> str:
    result = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"label=devcontainer.local_folder={workspace}"],
        capture_output=True,
        text=True,
        check=True,
    )
    container_id = result.stdout.strip()
    if not container_id:
        die(
            f"No running devcontainer found for {workspace}\n"
            "Start the container first with: devc up"
        )
    return container_id


def inject_credentials(container_id: str, profile: str, creds: dict, region: str) -> None:
    """Write temporary credentials into the container via aws configure set.

    aws_session_token must be set explicitly — aws configure's interactive
    flow does not prompt for it.
    """
    credential_keys = {
        "aws_access_key_id": creds["AccessKeyId"],
        "aws_secret_access_key": creds["SecretAccessKey"],
        "aws_session_token": creds["SessionToken"],
    }
    for key, value in credential_keys.items():
        try:
            subprocess.run(
                ["docker", "exec", container_id,
                 "aws", "configure", "set", key, value, "--profile", profile],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            die(f"Failed to set {key} in container: {e.stderr.decode().strip()}")

    try:
        subprocess.run(
            ["docker", "exec", container_id,
             "aws", "configure", "set", "region", region, "--profile", profile],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        die(f"Failed to set region in container: {e.stderr.decode().strip()}")


def write_session_policy(workspace: Path, policy_text: str) -> None:
    """Write the active session policy to session-policy.json in the workspace.

    /workspace in the container is a bind mount of the host workspace folder, so
    writing here makes the file immediately visible inside the container.
    """
    (workspace / "session-policy.json").write_text(policy_text)


def cmd_aws_setup_role(args: argparse.Namespace) -> None:
    session = get_session(args.profile)
    identity = get_caller_identity(session)
    account_id = identity["Account"]

    iam = session.client("iam")
    try:
        arn = iam.get_role(RoleName=args.role_name)["Role"]["Arn"]
        err(f"Role already exists: {arn}")
        return
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            die(f"Failed to check for existing role: {e}")

    arn = create_role(iam, account_id, args.role_name)
    print(arn)


def cmd_aws_creds(args: argparse.Namespace, script_dir: Path) -> None:
    session = get_session(args.profile)
    identity = get_caller_identity(session)
    account_id = identity["Account"]
    caller_arn = identity["Arn"]

    region = session.region_name or "us-east-1"
    workspace = Path(args.workspace)
    project = workspace.name
    container_id = get_container_id(args.workspace)

    iam = session.client("iam")
    role_arn = ensure_role(iam, account_id, args.role_name)

    policy_text = load_session_policy(workspace, script_dir)

    if len(policy_text) > SESSION_POLICY_MAX_CHARS:
        die(
            f"Session policy is {len(policy_text)} characters — "
            f"AWS limit is {SESSION_POLICY_MAX_CHARS}. "
            "Remove unused actions from /workspace/session-policy.json."
        )

    session_name = extract_session_name(caller_arn, project)
    err(f"Assuming {role_arn} as session '{session_name}'...")

    try:
        response = session.client("sts").assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
            DurationSeconds=DEFAULT_SESSION_DURATION_SECONDS,
            Policy=policy_text,
        )
    except ClientError as e:
        die(f"Failed to assume role: {e}")

    creds = response["Credentials"]

    err(f"Injecting credentials into container as profile '{args.profile}'...")
    inject_credentials(container_id, args.profile, creds, region)

    err("Writing session policy to session-policy.json...")
    write_session_policy(workspace, policy_text)

    expiry = creds["Expiration"].strftime("%Y-%m-%d %H:%M:%S UTC")
    err(f"Done. Credentials expire at {expiry}.")
    err(f"Profile '{args.profile}' is ready inside the container.")
    err("")
    err("To expand Claude's permissions:")
    err("  1. Edit /workspace/session-policy.json in the container")
    err(f"  2. Re-run: devc aws-creds --profile {args.profile}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devc aws-*",
        description="AWS credential management for Claude Code devcontainer",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    creds = sub.add_parser(
        "aws-creds",
        help="Assume ClaudeDevContainer role and inject credentials into the container",
    )
    creds.add_argument("--profile", required=True, help="Host AWS profile to use")
    creds.add_argument(
        "--role-name",
        default=DEFAULT_ROLE_NAME,
        help=f"IAM role name to assume (default: {DEFAULT_ROLE_NAME})",
    )
    creds.add_argument(
        "--workspace",
        required=True,
        help="Host workspace folder path (used to locate the running container)",
    )

    setup = sub.add_parser(
        "aws-setup-role",
        help="Create the ClaudeDevContainer IAM role in the target account",
    )
    setup.add_argument("--profile", required=True, help="Host AWS profile to use")
    setup.add_argument(
        "--role-name",
        default=DEFAULT_ROLE_NAME,
        help=f"IAM role name to create (default: {DEFAULT_ROLE_NAME})",
    )

    return parser


def main() -> None:
    args = build_parser().parse_args()
    script_dir = Path(__file__).parent

    if args.command == "aws-creds":
        cmd_aws_creds(args, script_dir)
    elif args.command == "aws-setup-role":
        cmd_aws_setup_role(args)


if __name__ == "__main__":
    main()
