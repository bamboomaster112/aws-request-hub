#!/usr/bin/env python3
"""Sign-in and token checks against the deployed stack (run from CloudShell).

Covers what the browser tests cannot see from inside the app:
  * Cognito settings: MFA required (authenticator app), password policy, no self sign-up,
    threat protection, and an app client that only allows the PKCE code flow back to the app.
  * The hosted UI and token endpoint: the app's authorize request is accepted, a foreign
    redirect URI is refused, and a made-up authorization code is rejected.
  * A new person's first sign-in, start to finish: temporary password, forced new password
    (weak ones refused), authenticator enrolment before any token is issued, then a TOTP code
    at every later sign-in. Wrong passwords, wrong codes, unknown users and disabled users are
    refused, and global sign-out revokes the refresh token.
  * API Gateway's Cognito authorizer: no token, garbage, a tampered token and an access token
    are all refused before the app sees them; a real ID token without an access profile reaches the app but
    gets no access.

It creates one throwaway user (no email is sent; passwords are generated in memory and never
printed) and deletes it at the end.

  python3 scripts/auth-check.py
"""
import base64
import json
import secrets
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import hubkit
from botocore.exceptions import ClientError

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not ok else ""))
    return ok


def section(title):
    print(f"\n{title}")


def password(length=20):
    symbols = "!#$%&*+-=?@^_"
    alphabet = string.ascii_letters + string.digits + symbols
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in symbols for c in pw)):
            return pw


def error_code(fn, **kwargs):
    try:
        fn(**kwargs)
        return "OK"
    except ClientError as exc:
        return exc.response["Error"]["Code"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def raw_get(url):
    """GET without following redirects. Returns (status, location, body)."""
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(url, timeout=20) as res:
            return res.status, res.headers.get("Location", ""), res.read(4000).decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, err.headers.get("Location", ""), err.read(4000).decode("utf-8", "replace")


def claims(token):
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def main():
    out = hubkit.outputs()
    cognito, pool, client_id = hubkit.cognito, out["UserPoolId"], out["WebClientId"]
    app_url, domain, api = out["AppUrl"], out["CognitoDomain"], out["ApiBaseUrl"]
    started = time.time()

    section("Cognito configuration")
    up = cognito.describe_user_pool(UserPoolId=pool)["UserPool"]
    mfa = cognito.get_user_pool_mfa_config(UserPoolId=pool)
    check("MFA is required for everyone", up.get("MfaConfiguration") == "ON", up.get("MfaConfiguration"))
    check("The second factor is an authenticator app (TOTP), not SMS",
          mfa.get("SoftwareTokenMfaConfiguration", {}).get("Enabled") and not mfa.get("SmsMfaConfiguration"), mfa)
    policy = up["Policies"]["PasswordPolicy"]
    check("Passwords: 12+ characters with upper, lower, number and symbol",
          policy["MinimumLength"] >= 12 and all(policy.get(k) for k in ("RequireUppercase", "RequireLowercase", "RequireNumbers", "RequireSymbols")), policy)
    check("No self sign-up: only admins create accounts", up["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"])
    check("Threat protection is enforced", (up.get("UserPoolAddOns") or {}).get("AdvancedSecurityMode") == "ENFORCED", up.get("UserPoolAddOns"))
    client = cognito.describe_user_pool_client(UserPoolId=pool, ClientId=client_id)["UserPoolClient"]
    check("The web client is public (no secret in the browser) and uses the code flow only",
          not client.get("ClientSecret") and client.get("AllowedOAuthFlows") == ["code"], client.get("AllowedOAuthFlows"))
    check("Sign-in and sign-out can only return to the app's own URL",
          client.get("CallbackURLs") == [app_url] and client.get("LogoutURLs") == [app_url], (client.get("CallbackURLs"), client.get("LogoutURLs")))
    check("Errors do not reveal whether an account exists", client.get("PreventUserExistenceErrors") == "ENABLED")
    check("Plain password auth from the browser is not allowed (SRP or the hosted UI only)",
          "ALLOW_USER_PASSWORD_AUTH" not in client.get("ExplicitAuthFlows", []), client.get("ExplicitAuthFlows"))
    check("Tokens can be revoked and ID tokens last an hour",
          client.get("EnableTokenRevocation") and client.get("IdTokenValidity") == 60, (client.get("EnableTokenRevocation"), client.get("IdTokenValidity")))

    section("Hosted UI and token endpoint")
    challenge = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": app_url, "scope": "openid email profile",
              "code_challenge_method": "S256", "code_challenge": challenge, "state": "auth-check"}
    status, location, body = raw_get(f"{domain}/oauth2/authorize?{urllib.parse.urlencode(params)}")
    check("The app's sign-in request is accepted and sent to the login page",
          status in (301, 302) and "/login" in location and "error" not in location, f"{status} {location[:120]}")
    status, location, body = raw_get(f"{domain}/oauth2/authorize?{urllib.parse.urlencode({**params, 'redirect_uri': 'https://attacker.example/'})}")
    check("A sign-in request that would return somewhere else is refused",
          "redirect_mismatch" in (location + body) or status >= 400, f"{status} {location[:120]}")
    req = urllib.request.Request(f"{domain}/oauth2/token", method="POST", data=urllib.parse.urlencode({
        "grant_type": "authorization_code", "client_id": client_id, "code": "not-a-real-code", "redirect_uri": app_url,
        "code_verifier": "x" * 43}).encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            token_status, token_body = res.status, res.read().decode()
    except urllib.error.HTTPError as err:
        token_status, token_body = err.code, err.read().decode()
    check("The token endpoint rejects a made-up authorization code", token_status == 400 and "invalid_grant" in token_body, f"{token_status} {token_body[:80]}")

    section("A new person's first sign-in")
    email = f"auth-check+{secrets.token_hex(4)}@example.com"
    temporary, chosen = password(), password()
    auth = dict(UserPoolId=pool, ClientId=client_id, AuthFlow="ADMIN_USER_PASSWORD_AUTH")
    try:
        cognito.admin_create_user(UserPoolId=pool, Username=email, TemporaryPassword=temporary, MessageAction="SUPPRESS",
                                  UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}])
        first = cognito.admin_initiate_auth(**auth, AuthParameters={"USERNAME": email, "PASSWORD": temporary})
        check("The temporary password only leads to 'choose a new password'", first.get("ChallengeName") == "NEW_PASSWORD_REQUIRED"
              and "AuthenticationResult" not in first, first.get("ChallengeName"))
        weak = error_code(cognito.admin_respond_to_auth_challenge, UserPoolId=pool, ClientId=client_id, ChallengeName="NEW_PASSWORD_REQUIRED",
                          Session=first["Session"], ChallengeResponses={"USERNAME": email, "NEW_PASSWORD": "short1!"})
        check("A weak new password is refused", weak == "InvalidPasswordException", weak)
        first = cognito.admin_initiate_auth(**auth, AuthParameters={"USERNAME": email, "PASSWORD": temporary})
        step = cognito.admin_respond_to_auth_challenge(UserPoolId=pool, ClientId=client_id, ChallengeName="NEW_PASSWORD_REQUIRED",
                                                       Session=first["Session"], ChallengeResponses={"USERNAME": email, "NEW_PASSWORD": chosen})
        check("After the new password, enrolling an authenticator is required before any token",
              step.get("ChallengeName") == "MFA_SETUP" and "AuthenticationResult" not in step, step.get("ChallengeName"))
        assoc = cognito.associate_software_token(Session=step["Session"])
        verify = cognito.verify_software_token(Session=assoc["Session"], UserCode=hubkit.totp(assoc["SecretCode"]), FriendlyDeviceName="auth-check")
        done = cognito.admin_respond_to_auth_challenge(UserPoolId=pool, ClientId=client_id, ChallengeName="MFA_SETUP",
                                                       Session=verify["Session"], ChallengeResponses={"USERNAME": email})
        tokens = done.get("AuthenticationResult") or {}
        check("Enrolment completes and tokens are issued", verify.get("Status") == "SUCCESS" and tokens.get("IdToken"), verify.get("Status"))
        id_claims = claims(tokens["IdToken"])
        check("The ID token is for this app and this person", id_claims.get("aud") == client_id and id_claims.get("email") == email
              and id_claims.get("token_use") == "id", {k: id_claims.get(k) for k in ("aud", "email", "token_use")})
        secret = assoc["SecretCode"]

        later = cognito.admin_initiate_auth(**auth, AuthParameters={"USERNAME": email, "PASSWORD": chosen})
        check("Every later sign-in asks for the authenticator code", later.get("ChallengeName") == "SOFTWARE_TOKEN_MFA", later.get("ChallengeName"))
        wrong_code = "000000" if hubkit.totp(secret) != "000000" else "111111"
        bad = error_code(cognito.admin_respond_to_auth_challenge, UserPoolId=pool, ClientId=client_id, ChallengeName="SOFTWARE_TOKEN_MFA",
                         Session=later["Session"], ChallengeResponses={"USERNAME": email, "SOFTWARE_TOKEN_MFA_CODE": wrong_code})
        check("A wrong authenticator code is refused", bad in ("CodeMismatchException", "NotAuthorizedException"), bad)
        hubkit.wait_for_fresh_window()
        later = cognito.admin_initiate_auth(**auth, AuthParameters={"USERNAME": email, "PASSWORD": chosen})
        good = cognito.admin_respond_to_auth_challenge(UserPoolId=pool, ClientId=client_id, ChallengeName="SOFTWARE_TOKEN_MFA",
                                                       Session=later["Session"], ChallengeResponses={"USERNAME": email, "SOFTWARE_TOKEN_MFA_CODE": hubkit.totp(secret)})
        tokens = good.get("AuthenticationResult") or {}
        check("The right code signs them in", bool(tokens.get("IdToken")))

        wrong_pw = error_code(cognito.admin_initiate_auth, **auth, AuthParameters={"USERNAME": email, "PASSWORD": password()})
        unknown = error_code(cognito.admin_initiate_auth, **auth, AuthParameters={"USERNAME": f"nobody+{secrets.token_hex(3)}@example.com", "PASSWORD": password()})
        check("A wrong password is refused", wrong_pw == "NotAuthorizedException", wrong_pw)
        check("An unknown email gets the same answer as a wrong password (no account probing)", unknown == wrong_pw, unknown)

        section("API Gateway's Cognito authorizer")
        status, body = hubkit.http("GET", f"{api}/me", tokens["IdToken"])
        check("A real ID token without an access profile reaches the app but gets no access",
              status == 200 and body.get("status") == "NO_PROFILE" and not body.get("role"), f"{status} {body}")
        status, _ = hubkit.http("GET", f"{api}/requests", tokens["IdToken"])
        check("...and cannot list requests (403)", status == 403, status)
        head, payload, sig = tokens["IdToken"].split(".")
        forged = dict(claims(tokens["IdToken"]), email="admin@example.com", sub="00000000-0000-0000-0000-000000000000")
        tampered = f"{head}.{base64.urlsafe_b64encode(json.dumps(forged).encode()).decode().rstrip('=')}.{sig}"
        for label, token in (("no token", None), ("a garbage token", "not.a.jwt"), ("a tampered ID token (claims changed)", tampered),
                             ("an access token instead of an ID token", tokens["AccessToken"])):
            status, body = hubkit.http("GET", f"{api}/me", token)
            reached_app = isinstance(body, dict) and "userId" in body
            # API Gateway answers 401 for a missing or malformed token and 403 for a token whose signature does not match.
            check(f"The API refuses {label} before the app sees it", status in (401, 403) and not reached_app, f"{status} {body}")
            print(f"         {status} {json.dumps(body)[:100]}")

        section("Disable and sign out everywhere")
        refresh = tokens["RefreshToken"]
        renewed = error_code(cognito.initiate_auth, ClientId=client_id, AuthFlow="REFRESH_TOKEN_AUTH", AuthParameters={"REFRESH_TOKEN": refresh})
        check("A refresh token renews the session while it is valid", renewed == "OK", renewed)
        cognito.admin_user_global_sign_out(UserPoolId=pool, Username=email)
        revoked = error_code(cognito.initiate_auth, ClientId=client_id, AuthFlow="REFRESH_TOKEN_AUTH", AuthParameters={"REFRESH_TOKEN": refresh})
        check("Sign out everywhere revokes the refresh token", revoked == "NotAuthorizedException", revoked)
        cognito.admin_disable_user(UserPoolId=pool, Username=email)
        disabled = error_code(cognito.admin_initiate_auth, **auth, AuthParameters={"USERNAME": email, "PASSWORD": chosen})
        check("A disabled account cannot sign in, even with the right password", disabled == "NotAuthorizedException", disabled)
    finally:
        try:
            cognito.admin_delete_user(UserPoolId=pool, Username=email)
            print(f"\n  (throwaway user {email} deleted)")
        except ClientError:
            pass

    passed = sum(results)
    print(f"\n{passed}/{len(results)} sign-in checks passed in {int(time.time() - started)}s")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
