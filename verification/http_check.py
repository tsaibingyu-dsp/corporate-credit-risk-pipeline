"""Optional bounded public-source probe. A successful probe does not authenticate content."""
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
from urllib.error import HTTPError, URLError

HOSTS = {'judicial': {'judgment.judicial.gov.tw'}, 'sfb': {'www.sfb.gov.tw'},
         'environment': {'prtr.moenv.gov.tw'}, 'news': {'news.google.com','www.cna.com.tw','cna.com.tw'},
         'mops': {'mops.twse.com.tw','mopsov.twse.com.tw'}}


def allowed_url(url, module):
    try:
        p=urlsplit(url)
        return (p.scheme=='https' and p.hostname in HOSTS.get(module,set()) and not p.username
                and not p.password and p.port in (None,443))
    except (ValueError,TypeError):
        return False


def probe(url, module):
    class SafeRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not allowed_url(newurl,module):
                raise ValueError('Redirect outside source whitelist')
            return super().redirect_request(req,fp,code,msg,headers,newurl)
    if not allowed_url(url,module):
        return {'status':'fail','detail':'URL outside HTTPS source whitelist'}
    try:
        with build_opener(SafeRedirect()).open(Request(url,method='GET',headers={
                'User-Agent':'CreditRiskVerifier/1.0','Range':'bytes=0-4095'}),timeout=15) as response:
            sample=response.read(4096)
            # Login/challenge pages can return 200; never equate status with content verification.
            blocked=any(t in sample.lower() for t in (b'captcha',b'access denied',b'cloudflare',b'checking your browser'))
            return dict(status='unverifiable' if blocked else 'pass',http_status=response.status,
                        final_url=response.geturl(),detail='Reachability only; not a full-content comparison')
    except (HTTPError,URLError,OSError,ValueError) as exc:
        return dict(status='unverifiable',detail=str(exc))
