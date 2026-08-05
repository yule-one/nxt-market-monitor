from __future__ import annotations

import logging
from typing import Any

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import truststore

    truststore.inject_into_ssl()
except (ImportError, RuntimeError):
    pass


LOGGER = logging.getLogger(__name__)


class ResilientSession:
    """재시도와 Windows 인증서 저장소 문제에 대한 제한적 폴백을 제공합니다."""

    def __init__(self, referer: str | None = None) -> None:
        self.session = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "POST"}),
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/139.0 Safari/537.36"
                ),
                "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
            }
        )
        if referer:
            self.session.headers["Referer"] = referer
        self.ssl_fallback_used = False

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", 30)
        try:
            response = self.session.request(method, url, **kwargs)
        except requests.exceptions.SSLError:
            # 사내 프록시가 자체 서명 인증서를 삽입하는 Windows 환경에서만 사용됩니다.
            self.ssl_fallback_used = True
            LOGGER.warning("SSL verification failed; retrying %s without verification", url)
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            self.session.verify = False
            kwargs["verify"] = False
            response = self.session.request(method, url, **kwargs)
        response.raise_for_status()
        return response

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)
