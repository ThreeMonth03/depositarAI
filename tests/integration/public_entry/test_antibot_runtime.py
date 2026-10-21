"""Opt-in real Anubis/Nginx acceptance with a disposable CPU-only fake gateway.

Only this fixture's Compose project is created/stopped. It publishes no host
ports, never imports inference code, and uses immutable preselected images.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
import uuid

from search.operations.deployment.public_entry.antibot import (
    ANUBIS_IMAGE,
    NETWORK,
    render_antibot,
)
from search.operations.deployment.source_bundle import NGINX_BASE
from tests.integration.public_entry import test_runtime as runtime_fixture


GATEWAY = r"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
calls=[]
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def handle_request(self):
        body=self.rfile.read(int(self.headers.get('Content-Length','0'))).decode()
        if self.path != '/stats':
            calls.append({'method':self.command,'path':self.path,'body':body,
                'client_ip':self.headers.get('X-Real-IP')})
        raw=json.dumps({'calls':calls}).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    do_GET=do_POST=do_DELETE=handle_request
HTTPServer(('0.0.0.0',9080),Handler).serve_forever()
"""

PROBE = r"""
import hashlib,http.client,json,re,time,urllib.parse
from http.cookies import SimpleCookie
from html import unescape
jar={}
def request(method,path,body=None,headers=None,cookies=None):
    h={'Host':'public.test:9443','Origin':'https://public.test:9443',
       'Accept-Encoding':'gzip','User-Agent':'runtime-probe'}
    h.update(headers or {})
    selected=jar if cookies is None else cookies
    if selected: h['Cookie']='; '.join(k+'='+v for k,v in selected.items())
    c=http.client.HTTPConnection('public-edge',8080,timeout=10)
    c.request(method,path,body=body,headers=h)
    r=c.getresponse(); raw=r.read(); fields=r.getheaders()
    if r.getheader('Content-Encoding')=='gzip':
        import gzip
        raw=gzip.decompress(raw)
    for name,value in fields:
        if name.lower()=='set-cookie':
            parsed=SimpleCookie();parsed.load(value)
            if cookies is None:
                for key,m in parsed.items():
                    if m['max-age']=='-1': jar.pop(key,None)
                    else: jar[key]=m.value
    result=(r.status,dict((k.lower(),v) for k,v in fields),raw.decode())
    c.close(); return result
def stats(): return json.loads(request('GET','/stats')[2])['calls']
assert request('GET','/')[0]==200
baseline=len(stats())
for path in ['/api/session','/api/turn']:
    code,headers,body=request('POST',path,'{}',{'X-Anubis-Status':'PASS','X-Real-IP':'8.8.8.8'})
    assert code==403,(path,code,body)
    assert headers['content-type'].startswith('application/json'),headers
    assert json.loads(body)['error']=={'code':'challenge_required','verification_path':'/verify'},body
assert len(stats())==baseline
assert request('DELETE','/api/session')[0]==200
assert request('GET','/auth/callback?code=fixture&state=fixture')[0]==200
assert request('POST','/auth/callback','state=fixture&code=fixture')[0]==200
assert stats()[-1]['body']=='state=fixture&code=fixture'
assert request('GET','/auth/login')[0:2][0]==302
code,headers,body=request('GET','/verify?redir=https://untrusted.invalid/')
assert code==403 and '<html' in body.lower(),(code,body[:200])
match=re.search(r'<script[^>]*id="anubis_challenge"[^>]*>(.*?)</script>',body,re.S)
assert match,body[:1000]
challenge=json.loads(unescape(match.group(1)))
data=challenge['challenge']; difficulty=challenge['rules']['difficulty']
nonce=0
while True:
    response=hashlib.sha256((data['randomData']+str(nonce)).encode()).hexdigest()
    if response.startswith('0'*difficulty): break
    nonce+=1
assert nonce<1000000,nonce
query=urllib.parse.urlencode({'id':data['id'],'response':response,'nonce':nonce,
    'redir':'https://public.test:9443/verify','elapsedTime':1000})
code,headers,body=request('GET','/.within.website/x/cmd/anubis/api/pass-challenge?'+query)
assert code==302,(code,body)
assert 'Secure' in headers['set-cookie'] and 'HttpOnly' in headers['set-cookie'],headers
code,headers,body=request('GET','/verify?redir=https://untrusted.invalid/')
assert code==302 and headers['location']=='/',(code,headers,body)
time.sleep(1)
assert request('POST','/api/session','{"query":"fixture"}')[0]==200
assert stats()[-1]['method']=='POST' and stats()[-1]['body']=='{"query":"fixture"}'
assert stats()[-1]['client_ip']!='8.8.8.8'
assert request('POST','/api/turn','{}')[0]==200
valid=dict(jar)
bad={k:('invalid' if 'auth' in k else v) for k,v in valid.items()}
assert request('POST','/api/turn','{}',cookies=bad)[0]==403
assert request('GET','/.within.website/x/cmd/anubis/api/check')[0]==404
assert request('GET','/_antibot_check')[0]==404
print(json.dumps({'status':'passed','cookie':valid,'upstream_calls':len(stats()),
    'challenge_required_json':True,'post_preserved':True,'callback_preserved':True,
    'fixed_redirect':True,'spoofed_pass_rejected':True}))
"""


@unittest.skipUnless(
    os.environ.get("DEPOSITAR_TEST_ANTIBOT_RUNTIME") == "1",
    "real Anubis acceptance requires explicit isolated CPU test opt-in",
)
class AntibotRuntimeTests(unittest.TestCase):
    def command(self, *argv, timeout=45):
        result = subprocess.run(
            argv, text=True, capture_output=True, timeout=timeout, check=False
        )
        if result.returncode:
            self.fail("isolated antibot command failed: " + result.stderr[-2000:])
        return result.stdout

    def compose(self, *args, timeout=45):
        return self.command(
            "docker",
            "compose",
            "--project-name",
            self.project,
            "--file",
            str(self.compose_file),
            *args,
            timeout=timeout,
        )

    def test_real_challenge_gate_keeps_json_and_post_boundaries_across_restart(self):
        uid, gid = os.geteuid(), os.getegid()
        if uid == 0:
            self.skipTest("disposable runtime requires a non-root operator")
        with tempfile.TemporaryDirectory(prefix="depositar-antibot-runtime-") as tmp:
            root = Path(tmp)
            root.chmod(0o700)
            private = root / "public-entry"
            private.mkdir(mode=0o700)
            signing_key = root / "signing-key"
            signing_key.write_text("a" * 64 + "\n")
            signing_key.chmod(0o600)
            config = SimpleNamespace(
                antibot_enabled=True,
                antibot_signing_key_file=signing_key,
                state_root=root,
                public_origin="https://public.test:9443",
                trusted_proxy_cidrs=(),
                loopback_port=18084,
            )
            plan = render_antibot(config)
            for name, content in plan["assets"].items():
                path = private / name
                path.write_text(content)
                path.chmod(0o600)
            (private / "antibot-data").mkdir(mode=0o700)
            for name, content in (("gateway.py", GATEWAY), ("probe.py", PROBE)):
                path = root / name
                path.write_text(content)
                path.chmod(0o600)
            images = {
                "anubis": ANUBIS_IMAGE,
                "nginx": NGINX_BASE,
                "python": os.environ.get(
                    "DEPOSITAR_TEST_PUBLIC_FIXTURE_IMAGE", "depositar-search-api:ci"
                ),
            }
            for name, image in images.items():
                images[name] = self.command(
                    "docker", "image", "inspect", image, "--format", "{{.Id}}"
                ).strip()
                self.assertRegex(images[name], r"^sha256:[0-9a-f]{64}$")
            services = plan["services"]
            services["public-edge"].pop("ports")
            services["public-edge"]["image"] = images["nginx"]
            services["antibot"]["image"] = images["anubis"]
            common = {
                "image": images["python"],
                "user": f"{uid}:{gid}",
                "runtime": "runc",
                "read_only": True,
                "cpus": 0.25,
                "mem_limit": "64m",
                "pids_limit": 32,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "networks": [NETWORK],
                "environment": {
                    "NVIDIA_VISIBLE_DEVICES": "void",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(root),
                        "target": "/fixture",
                        "read_only": True,
                    }
                ],
            }
            services["public-entry"] = {
                **common,
                "entrypoint": ["python", "/fixture/gateway.py"],
                "depends_on": {"public-edge": {"condition": "service_healthy"}},
            }
            services["probe"] = {
                **common,
                "entrypoint": ["python", "-c", "import time; time.sleep(600)"],
            }
            services["second-probe"] = dict(services["probe"])
            network = runtime_fixture.PublicEntryRuntimeFixture.networks()["website"]
            self.project = "depositar-antibot-test-" + uuid.uuid4().hex[:12]
            self.compose_file = root / "compose.json"
            self.compose_file.write_text(
                json.dumps(
                    {
                        "services": services,
                        "networks": {NETWORK: network},
                    }
                )
            )
            self.compose_file.chmod(0o600)
            try:
                self.compose("up", "--detach", "--wait", timeout=90)
                outcome = json.loads(
                    self.compose("exec", "-T", "probe", "python", "/fixture/probe.py")
                )
                self.assertEqual(outcome.pop("status"), "passed")
                cookie = outcome.pop("cookie")
                cookie_header = "; ".join(k + "=" + v for k, v in cookie.items())
                request_script = (
                    "import http.client,json; c=http.client.HTTPConnection('public-edge',8080,timeout=10); "
                    "c.request('POST','/api/turn',body='{}',headers="
                    + repr({"Host": "public.test:9443", "Cookie": cookie_header})
                    + "); r=c.getresponse(); print(json.dumps({'status':r.status,'body':r.read().decode()}))"
                )
                # Another socket address cannot reuse the first browser's proof.
                second = json.loads(
                    self.compose(
                        "exec", "-T", "second-probe", "python", "-c", request_script
                    )
                )
                self.assertEqual(second["status"], 403)
                self.assertEqual(
                    json.loads(second["body"])["error"]["code"], "challenge_required"
                )
                time.sleep(1)
                self.compose("stop", "antibot")
                unavailable = json.loads(
                    self.compose("exec", "-T", "probe", "python", "-c", request_script)
                )
                self.assertEqual(unavailable["status"], 503)
                self.compose("up", "--detach", "--wait", "antibot", timeout=60)
                recovered = json.loads(
                    self.compose("exec", "-T", "probe", "python", "-c", request_script)
                )
                self.assertEqual(recovered["status"], 200)
                self.assertTrue((private / "antibot-data" / "anubis.bdb").is_file())
            except AssertionError as error:
                logs = self.compose(
                    "logs", "--no-color", "--tail", "30", "public-edge", "antibot"
                )
                self.fail(str(error) + "\n" + logs[-4000:])
            finally:
                self.compose("down", "--volumes", timeout=60)


if __name__ == "__main__":
    unittest.main()
