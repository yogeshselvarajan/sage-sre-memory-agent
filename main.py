from __future__ import annotations

# main.py - Entry point for SAGE (System Architecture and Guidance Engine)
#
# Environment variables MUST be set before any strands or mem0 imports.
# mem0's AWSBedrockConfig reads AWS_REGION at class definition time and defaults
# to us-west-2. Setting it here ensures all Bedrock clients target us-east-1.
import os
import sys

os.environ["BYPASS_TOOL_CONSENT"] = "true"
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ["MEM0_LLM_PROVIDER"] = "aws_bedrock"
os.environ["MEM0_LLM_MODEL"] = "us.amazon.nova-lite-v1:0"
os.environ["MEM0_EMBEDDER_PROVIDER"] = "aws_bedrock"
os.environ["MEM0_EMBEDDER_MODEL"] = "amazon.titan-embed-text-v2:0"

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from pathlib import Path


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


def check_aws_credentials() -> tuple[bool, str]:
    try:
        identity = boto3.client("sts", region_name="us-east-1").get_caller_identity()
        return True, f"Account: {identity['Account']} | Identity: {identity['Arn']}"
    except NoCredentialsError:
        return False, (
            "No AWS credentials found.\n\n"
            "Configure with:\n"
            "  aws configure\n\n"
            "Or set: AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_DEFAULT_REGION"
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("InvalidClientTokenId", "AuthFailure", "AccessDenied"):
            return False, (
                f"Credentials invalid or insufficient ({code}).\n\n"
                "SAGE requires these IAM permissions:\n\n"
                "  Agent LLM:   bedrock:InvokeModelWithResponseStream (us.amazon.nova-pro-v1:0)\n"
                "  Memory LLM:  bedrock:InvokeModel (us.amazon.nova-lite-v1:0)\n"
                "  Embeddings:  bedrock:InvokeModel (amazon.titan-embed-text-v2:0)\n"
                "  Platform:    sts:GetCallerIdentity\n\n"
                "See README.md for the full IAM policy JSON."
            )
        return False, f"AWS error ({code}): {e.response['Error']['Message']}"
    except Exception as e:
        return False, f"Unexpected error: {e}"


def _validate_guardrail() -> None:
    # Guardrail IDs are account-scoped. If SAGE_GUARDRAIL_ID is set but the
    # guardrail does not exist in the current account, clear the env var and
    # continue without it rather than crashing on every invocation.
    guardrail_id = os.environ.get("SAGE_GUARDRAIL_ID")
    if not guardrail_id:
        return
    guardrail_version = os.environ.get("SAGE_GUARDRAIL_VERSION", "1")
    try:
        boto3.client("bedrock", region_name="us-east-1").get_guardrail(
            guardrailIdentifier=guardrail_id,
            guardrailVersion=guardrail_version,
        )
    except Exception:
        os.environ.pop("SAGE_GUARDRAIL_ID", None)
        os.environ.pop("SAGE_GUARDRAIL_VERSION", None)
        print("[WARN] Guardrail not found in this account - running without guardrail protection.\n")


def main() -> None:
    import sys

    _load_env_file(Path(__file__).parent / ".env.guardrail")
    _validate_guardrail()

    args = sys.argv[1:]
    resume_session_id: str | None = None
    resume = False

    if "--resume" in args:
        idx = args.index("--resume")
        if idx + 1 < len(args):
            resume_session_id = args[idx + 1]
            resume = True
        else:
            from sage.session import list_sessions
            print("SAGE - Available sessions to resume:")
            sessions = list_sessions()
            if sessions:
                for s in sessions:
                    print(f"  {s}")
                print("\nUsage: python main.py --resume <session-id>")
            else:
                print("  No sessions found in .sage-sessions/")
            return

    print("SAGE - System Architecture and Guidance Engine")
    if resume:
        print(f"Resuming session: {resume_session_id}\n")
    else:
        print("Verifying AWS credentials...\n")

    ok, msg = check_aws_credentials()
    if not ok:
        print(f"[ERROR] {msg}")
        sys.exit(1)

    if not resume:
        print(f"[OK] {msg}\n")

    from sage import SAGEAgent
    SAGEAgent(session_id=resume_session_id, resume=resume).run(resume=resume)


if __name__ == "__main__":
    main()
