"""A local Request Hub for the browser tests: the real Lambda handlers behind a small HTTP server.

moto plays DynamoDB, Cognito, EventBridge, SSM, S3 and Secrets Manager. The state machine
interpreter from tests/local_e2e.py plays Step Functions. The server plays API Gateway: it
serves the web app and config.json, routes /api/... to the handlers with the same resources
and path parameters as template.yaml, and acts as the Cognito authorizer.

Tokens are unsigned JWTs minted by token_for(); the authorizer only checks their expiry.
That is fine for a local test double and never reaches AWS.

  backend = LocalHub().start()
  backend.base_url            # http://127.0.0.1:<port>/
  backend.token_for("requester")
  backend.stop()
"""
import base64
import json
import mimetypes
import pathlib
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
import local_e2e as e2e  # noqa: E402  (sets the handler environment and imports moto)

import os  # noqa: E402

import boto3  # noqa: E402

WEB = ROOT / "web"

# key, role, name, department, reports to
PEOPLE = [
    ("admin", "admins", "Asha Admin", "IT", None),
    ("admin2", "admins", "Omar Admin", "IT", None),
    ("manager", "approvers", "Mia Manager", "Engineering", "admin"),
    ("finance", "approvers", "Farah Finance", "Finance", "admin"),
    ("requester", "requesters", "Rae Requester", "Engineering", "manager"),
]

ROUTES = [  # method, path pattern, API Gateway resource, handler name
    ("GET", r"/requests", "/requests", "query"),
    ("POST", r"/requests", "/requests", "intake"),
    ("GET", r"/requests/(?P<requestId>[^/]+)", "/requests/{requestId}", "query"),
    ("POST", r"/requests/(?P<requestId>[^/]+)/decision", "/requests/{requestId}/decision", "decision"),
    ("POST", r"/requests/(?P<requestId>[^/]+)/attachments", "/requests/{requestId}/attachments", "presigner"),
    ("GET", r"/approvals", "/approvals", "query"),
    ("GET", r"/me", "/me", "query"),
    ("GET", r"/forms", "/forms", "query"),
    ("ANY", r"/admin/(?P<proxy>.+)", "/admin/{proxy+}", "admin"),
]


def _b64(data):
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def _claims(token):
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (AttributeError, IndexError, ValueError):
        return None


class LocalHub:
    def __init__(self, port=0, sla_seconds=1800):
        self.port, self.sla = port, sla_seconds
        self.lock = threading.RLock()
        self.ids, self.emails, self.roles = {}, {}, {}
        self.calls = 0

    # ------------------------------------------------------------ setup
    def start(self):
        self.mock = e2e.mock_aws()
        self.mock.start()
        e2e.create_table()
        boto3.client("ssm").put_parameter(Name=os.environ["POLICY_PARAMETER"], Type="String",
                                          Value=json.dumps({"slaSeconds": self.sla, "escalationSeconds": self.sla}))
        os.environ["DOWNSTREAM_SECRET_ARN"] = boto3.client("secretsmanager").create_secret(
            Name="downstream", SecretString=json.dumps({"apiKey": "local-test"}))["ARN"]
        boto3.client("s3").create_bucket(Bucket=os.environ["ATTACHMENTS_BUCKET"])
        cognito = boto3.client("cognito-idp")
        os.environ["USER_POOL_ID"] = cognito.create_user_pool(PoolName="hub")["UserPool"]["Id"]
        for group in ("requesters", "approvers", "admins"):
            cognito.create_group(UserPoolId=os.environ["USER_POOL_ID"], GroupName=group)

        load = e2e.load
        self.h = {name: load(folder) for name, folder in (
            ("policy", "policy_engine"), ("intake", "request_intake"), ("decision", "decision_handler"),
            ("query", "query_api"), ("publisher", "stream_publisher"), ("admin", "user_admin"),
            ("presigner", "attachment_presigner"), ("worker", "fulfilment_worker"))}
        self.flows = e2e.Workflows({"policy": self.h["policy"].handler})
        self.h["decision"].sfn = lambda: self.flows
        self._seed()

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), self._handler_class())
        self.server.daemon_threads = True
        self.server.handle_error = lambda request, address: None  # a browser closing a tab mid-response is normal
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}/"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.mock.stop()

    def _seed(self):
        cognito, table = boto3.client("cognito-idp"), boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
        modules = {"PURCHASE", "ACCESS", "LEAVE", "CHANGE", "GENERAL"}
        for key, role, name, department, manager in PEOPLE:
            email = f"{key}@example.com"
            user = cognito.admin_create_user(UserPoolId=os.environ["USER_POOL_ID"], Username=email, MessageAction="SUPPRESS",
                                             UserAttributes=[{"Name": "email", "Value": email}, {"Name": "name", "Value": name},
                                                             {"Name": "email_verified", "Value": "true"}])["User"]
            cognito.admin_add_user_to_group(UserPoolId=os.environ["USER_POOL_ID"], Username=email, GroupName=role)
            uid = {a["Name"]: a["Value"] for a in user["Attributes"]}["sub"]
            self.ids[key], self.emails[key], self.roles[key] = uid, email, role
            item = {"PK": f"USER#{uid}", "SK": "PROFILE", "entity": "USER_PROFILE", "userId": uid, "username": email,
                    "email": email, "name": name, "role": role, "status": "ACTIVE", "version": 1, "department": department,
                    "createdAt": int(time.time() * 1000), "GSI2PK": "DIRECTORY#USERS", "GSI2SK": int(time.time() * 1000)}
            if role != "admins":
                item["modules"] = modules
            if manager:
                item["managerId"] = self.ids[manager]
            table.put_item(Item=item)

        sys.path.insert(0, str(ROOT / "src" / "user_admin"))
        import defaults  # noqa: E402
        for group in defaults.DEFAULT_GROUPS:
            self.admin_api("POST", "groups", {**group, "members": [self.ids["finance"]]})
        for module, form in defaults.DEFAULT_FORMS.items():
            self.admin_api("PUT", f"forms/{module}", {"fields": form["fields"], "approvalField": form.get("approvalField")})
        for module, levels in defaults.DEFAULT_WORKFLOWS.items():
            self.admin_api("PUT", f"workflows/{module}", {"levels": levels, "slaSeconds": self.sla, "escalationSeconds": self.sla})

    def admin_api(self, method, path, body=None, who="admin"):
        status, data = self.invoke("admin", method, "/admin/{proxy+}", {"proxy": path}, body, who=who)
        if status >= 300:
            raise RuntimeError(f"seeding {method} {path} failed: {status} {data}")
        return data

    # ------------------------------------------------------------ tokens
    def token_for(self, key, ttl=3600):
        now = int(time.time())
        payload = {"sub": self.ids[key], "email": self.emails[key], "cognito:groups": [self.roles[key]],
                   "token_use": "id", "iat": now, "exp": now + ttl}
        return f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(payload)}.local"

    def token_for_unknown(self, email, ttl=3600):
        """A valid token for someone who exists in the identity provider but has no access profile."""
        now = int(time.time())
        payload = {"sub": "99999999-0000-0000-0000-000000000000", "email": email, "token_use": "id", "iat": now, "exp": now + ttl}
        return f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(payload)}.local"

    # ------------------------------------------------------------ the "AWS" side
    def invoke(self, name, method, resource, path_params, body, headers=None, query=None, who=None, claims=None):
        claims = claims or {"sub": self.ids[who], "email": self.emails[who]}
        event = {"resource": resource, "httpMethod": method, "headers": headers or {}, "pathParameters": path_params or None,
                 "queryStringParameters": query or None, "body": json.dumps(body) if isinstance(body, (dict, list)) else body,
                 "requestContext": {"authorizer": {"claims": claims}}}
        with self.lock:
            self.calls += 1
            res = self.h[name].handler(event, None)
            self._after(name, method, path_params or {}, res)
        try:
            data = json.loads(res.get("body") or "null")
        except ValueError:
            data = res.get("body")
        return res["statusCode"], data

    def _after(self, name, method, params, res):
        """Plays DynamoDB Streams, the Lambda publisher, EventBridge and the fulfilment queue."""
        request_id = params.get("requestId")
        if name == "intake" and res["statusCode"] == 201:
            request_id = json.loads(res["body"])["requestId"]
            meta = boto3.client("dynamodb").get_item(TableName=os.environ["TABLE_NAME"],
                                                     Key={"PK": {"S": f"REQ#{request_id}"}, "SK": {"S": "META"}})["Item"]
            entry = self.h["publisher"].to_entry({"eventName": "INSERT", "dynamodb": {"NewImage": meta}})
            self.flows.start(json.loads(entry["Detail"]))
        if request_id and name in ("intake", "decision"):
            self.fulfil_if_approved(request_id)

    def fulfil_if_approved(self, request_id):
        item = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"]).get_item(
            Key={"PK": f"REQ#{request_id}", "SK": "META"}, ConsistentRead=True).get("Item") or {}
        if item.get("status") in ("APPROVED", "AUTO_APPROVED"):
            try:
                self.h["worker"].process(json.dumps({"detail": {"requestId": request_id}}))
            except Exception:  # noqa: BLE001  a "[fail]" title: the message would go to the DLQ
                pass

    def time_out(self, request_id):
        """Fires the current level's SLA timer, as Step Functions would after slaSeconds."""
        with self.lock:
            self.flows.time_out(request_id)
            self.fulfil_if_approved(request_id)

    def put_attachment(self, key, content):
        boto3.client("s3").put_object(Bucket=os.environ["ATTACHMENTS_BUCKET"], Key=key, Body=content)

    # ------------------------------------------------------------ the HTTP side
    def _handler_class(self):
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _send(self, status, body, content_type="application/json", extra=None):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _static(self, path):
                if path == "/config.json":
                    base = hub.base_url
                    return self._send(200, {"apiBase": base + "api", "region": "us-east-1", "userPoolId": os.environ["USER_POOL_ID"],
                                            "clientId": "local-client", "cognitoDomain": base + "cognito", "redirectUri": base})
                if path.startswith("/cognito/logout"):
                    query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    return self._send(302, b"", "text/plain", {"Location": query.get("logout_uri", [hub.base_url])[0]})
                if path.startswith("/cognito/"):
                    return self._send(200, b"<!doctype html><title>Local hosted UI</title><h1>Local hosted UI</h1>", "text/html")
                file = WEB / ("index.html" if path in ("/", "/index.html") else path.lstrip("/"))
                if WEB not in file.resolve().parents or not file.is_file():
                    return self._send(404, {"message": "Not found"})
                return self._send(200, file.read_bytes(), mimetypes.guess_type(str(file))[0] or "application/octet-stream")

            def _api(self, method):
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path[len("/api"):] or "/"
                for verb, pattern, resource, name in ROUTES:
                    match = re.fullmatch(pattern, path)
                    if match and verb in (method, "ANY"):
                        break
                else:
                    return self._send(404 if not any(re.fullmatch(p, path) for _, p, _, _ in ROUTES) else 405,
                                      {"message": "Missing Authentication Token"})
                claims = _claims(self.headers.get("Authorization") or "")
                if not claims or int(claims.get("exp", 0)) < time.time():
                    return self._send(401, {"message": "Unauthorized"})
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode() if length else None
                query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
                params = {k: urllib.parse.unquote(v) for k, v in match.groupdict().items()}
                status, data = hub.invoke(name, method, resource, params, body, headers=dict(self.headers.items()),
                                          query=query, claims={"sub": claims["sub"], "email": claims.get("email", "")})
                return self._send(status, data)

            def _route(self, method):
                path = urllib.parse.urlparse(self.path).path
                if path.startswith("/api/"):
                    return self._api(method)
                if method != "GET":
                    return self._send(405, {"message": "Method not allowed"})
                return self._static(path)

            def do_GET(self):  # noqa: N802
                self._route("GET")

            def do_POST(self):  # noqa: N802
                self._route("POST")

            def do_PUT(self):  # noqa: N802
                self._route("PUT")

            def do_PATCH(self):  # noqa: N802
                self._route("PATCH")

            def do_DELETE(self):  # noqa: N802
                self._route("DELETE")

        return Handler


if __name__ == "__main__":  # run it by hand: python3 tests/ui/local_backend.py, then open the URL
    hub = LocalHub(port=int(sys.argv[1]) if len(sys.argv) > 1 else 8765).start()
    print(f"Local Request Hub at {hub.base_url}")
    for person in hub.ids:
        print(f"  sessionStorage.setItem('id_token', '{hub.token_for(person, ttl=8 * 3600)}')  // {person}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        hub.stop()
