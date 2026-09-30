"""极简 HTTP 客户端（只用标准库，零依赖）。"""
from __future__ import annotations

import gzip
import json
import time
import urllib.error
import urllib.request
import zlib

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class HttpError(RuntimeError):
    def __init__(self, status, url, body=""):
        super().__init__("HTTP %s %s %s" % (status, url, body[:200]))
        self.status = status
        self.url = url
        self.body = body


def _decode(raw, encoding):
    if encoding:
        try:
            return raw.decode(encoding, errors="replace")
        except LookupError:
            pass
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def request(url, headers=None, data=None, method=None, timeout=25, retries=2,
            encoding=None, referer=None):
    h = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }
    if referer:
        h["Referer"] = referer
    if headers:
        h.update(headers)

    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=h, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                enc = resp.headers.get("Content-Encoding", "")
                if "gzip" in enc:
                    raw = gzip.decompress(raw)
                elif "deflate" in enc:
                    try:
                        raw = zlib.decompress(raw)
                    except zlib.error:
                        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                return _decode(raw, encoding or resp.headers.get_content_charset())
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            if e.code != 429 and 400 <= e.code < 500:
                raise HttpError(e.code, url, body)
            last = HttpError(e.code, url, body)
        except Exception as e:
            last = e
        if attempt < retries:
            time.sleep(1.2 * (attempt + 1))
    raise last if last else RuntimeError("request failed: " + url)


def get_json(url, headers=None, timeout=25, retries=2):
    return json.loads(request(url, headers=headers, timeout=timeout, retries=retries))


def post_json(url, payload, headers=None, timeout=25):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    text = request(url, headers=h, data=body, method="POST", timeout=timeout, retries=1)
    try:
        return json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        return {"_raw": text}
