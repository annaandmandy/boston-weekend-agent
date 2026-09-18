#!/usr/bin/env python3
"""Exchange a Threads OAuth code and store the long-lived token securely."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError


DEFAULT_APP_ID = "2160903374822787"
DEFAULT_REDIRECT_URI = "https://www.hsiangyuhuang.com/threads-oauth/callback"
DEFAULT_EXPECTED_STATE = "f098bcc6e9ee5fb79055311ca13453d0"
DEFAULT_EXPECTED_USERNAME = "bostonweekendagent"
DEFAULT_SECRET_ID = "boston-weekend-agent/threads"


def request_json(
    url: str,
    *,
    method: str = "GET",
    data: dict[str, str] | None = None,
) -> dict[str, Any]:
    encoded = urllib.parse.urlencode(data).encode("utf-8") if data else None
    request = urllib.request.Request(url, data=encoded, method=method)
    request.add_header("Accept", "application/json")
    if encoded:
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read().decode("utf-8"))
            message = payload.get("error", {}).get("message", "Threads API error")
        except (json.JSONDecodeError, AttributeError):
            message = "Threads API error"
        raise RuntimeError(f"{message} (HTTP {error.code})") from None


def parse_callback(callback_url: str, expected_state: str) -> str:
    parsed = urllib.parse.urlparse(callback_url.strip())
    params = urllib.parse.parse_qs(parsed.query)
    if params.get("error"):
        raise RuntimeError(f"OAuth authorization failed: {params['error'][0]}")
    code = params.get("code", [""])[0]
    state = params.get("state", [""])[0]
    if not code:
        raise ValueError("The callback URL does not contain an authorization code")
    if state != expected_state:
        raise ValueError("OAuth state did not match; start the authorization flow again")
    return code


def ensure_secret(secrets: Any, secret_id: str) -> None:
    try:
        secrets.describe_secret(SecretId=secret_id)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise
        secrets.create_secret(
            Name=secret_id,
            Description="Long-lived Threads token for Boston Weekend Agent publishing",
            SecretString=json.dumps({"setup_status": "pending"}),
            Tags=[{"Key": "Project", "Value": "boston-weekend-agent"}],
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="boston-deployer")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--app-id", default=DEFAULT_APP_ID)
    parser.add_argument("--redirect-uri", default=DEFAULT_REDIRECT_URI)
    parser.add_argument("--expected-state", default=DEFAULT_EXPECTED_STATE)
    parser.add_argument("--expected-username", default=DEFAULT_EXPECTED_USERNAME)
    parser.add_argument("--secret-id", default=DEFAULT_SECRET_ID)
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    sts = session.client("sts")
    identity = sts.get_caller_identity()
    print(f"AWS account: {identity['Account']} ({identity['Arn']})")

    secrets = session.client("secretsmanager")
    ensure_secret(secrets, args.secret_id)
    print(f"Secrets Manager target is ready: {args.secret_id}")

    callback_url = getpass.getpass("Paste the full callback URL (hidden): ")
    code = parse_callback(callback_url, args.expected_state)
    app_secret = getpass.getpass("Paste the Threads App Secret (hidden): ").strip()
    if not app_secret:
        raise ValueError("Threads App Secret is required")

    short_lived = request_json(
        "https://graph.threads.net/oauth/access_token",
        method="POST",
        data={
            "client_id": args.app_id,
            "client_secret": app_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": args.redirect_uri,
        },
    )
    short_token = str(short_lived.get("access_token", ""))
    if not short_token:
        raise RuntimeError("Meta did not return a short-lived access token")

    long_query = urllib.parse.urlencode(
        {
            "grant_type": "th_exchange_token",
            "client_secret": app_secret,
            "access_token": short_token,
        }
    )
    long_lived = request_json(f"https://graph.threads.net/access_token?{long_query}")
    long_token = str(long_lived.get("access_token", ""))
    expires_in = int(long_lived.get("expires_in", 0))
    if not long_token or not expires_in:
        raise RuntimeError("Meta did not return a long-lived access token")

    profile_query = urllib.parse.urlencode(
        {
            "fields": "id,username,threads_profile_picture_url",
            "access_token": long_token,
        }
    )
    profile = request_json(f"https://graph.threads.net/v1.0/me?{profile_query}")
    username = str(profile.get("username", ""))
    if username.lower() != args.expected_username.lower():
        raise RuntimeError(
            f"Authorized @{username or 'unknown'}, expected @{args.expected_username}"
        )

    now = datetime.now(timezone.utc)
    secret_value = {
        "THREADS_ACCESS_TOKEN": long_token,
        "THREADS_USER_ID": str(profile.get("id", "")),
        "THREADS_USERNAME": username,
        "TOKEN_TYPE": str(long_lived.get("token_type", "bearer")),
        "TOKEN_ISSUED_AT": now.isoformat(),
        "TOKEN_EXPIRES_AT": (now + timedelta(seconds=expires_in)).isoformat(),
    }
    secrets.put_secret_value(
        SecretId=args.secret_id,
        SecretString=json.dumps(secret_value),
    )
    print(f"Authorized Threads account: @{username}")
    print(f"Long-lived token expires in {expires_in // 86400} days")
    print(f"Token stored securely in Secrets Manager: {args.secret_id}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ClientError, RuntimeError, ValueError) as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        raise SystemExit(1)
