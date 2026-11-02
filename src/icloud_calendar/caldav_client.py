"""A small async CalDAV client tailored to iCloud.

Only the handful of WebDAV/CalDAV requests this project needs are implemented, which keeps
iCloud's quirks explicit:

* Discovery: ``caldav.icloud.com`` -> current-user-principal -> calendar-home-set, which lives
  on a per-account partition host (``pNN-caldav.icloud.com``). All later requests go there.
* iCloud answers any UID-filtered calendar-query with 412, so objects are addressed by href.
* ``If-Match: *`` is broken on iCloud; we always send real ETags.
* Events must carry DTEND (iCloud refuses events without one) - handled in ``ical.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit
from xml.sax.saxutils import escape

import httpx
from lxml import etree

from .errors import Conflict, Forbidden, InvalidInput, NotFound, UpstreamError

NS = {
    "d": "DAV:",
    "c": "urn:ietf:params:xml:ns:caldav",
    "a": "http://apple.com/ns/ical/",
    "cs": "http://calendarserver.org/ns/",
}
_XML_HEADERS = {"Content-Type": "application/xml; charset=utf-8"}
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)


@dataclass
class DavResource:
    href: str  # server path, e.g. /123/calendars/home/ABC.ics
    props: dict[str, etree._Element | None] = field(default_factory=dict)
    status: int = 200

    def text(self, tag: str) -> str | None:
        el = self.props.get(tag)
        if el is None:
            return None
        return (el.text or "").strip() or None


@dataclass
class CalendarObject:
    href: str
    etag: str | None
    data: str


def _clark(prefixed: str) -> str:
    prefix, local = prefixed.split(":")
    return f"{{{NS[prefix]}}}{local}"


def _fmt_utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


class CalDAVClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not username or not password:
            raise InvalidInput(
                "iCloud credentials missing: set ICLOUD_USERNAME and ICLOUD_APP_PASSWORD "
                "(an app-specific password from account.apple.com)."
            )
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self._http = httpx.AsyncClient(
            auth=httpx.BasicAuth(username, password),
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "icloud-calendar-mcp/0.1"},
            transport=transport,
        )
        self.origin: str = self._origin(self.base_url)

    @staticmethod
    def _origin(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}"

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ core

    def url_for(self, href: str) -> str:
        return href if href.startswith("http") else urljoin(self.origin + "/", href)

    @staticmethod
    def path_of(href: str) -> str:
        return urlsplit(href).path if href.startswith("http") else href

    async def _request(
        self,
        method: str,
        href: str,
        *,
        body: str | bytes | None = None,
        headers: dict[str, str] | None = None,
        ok: tuple[int, ...] = (200, 201, 204, 207),
    ) -> httpx.Response:
        try:
            resp = await self._http.request(method, self.url_for(href), content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise UpstreamError(f"CalDAV request failed: {exc}") from exc
        if resp.status_code in ok:
            return resp
        detail = resp.text[:300].strip()
        if resp.status_code == 401:
            raise UpstreamError(
                "iCloud rejected the credentials. Check ICLOUD_USERNAME and that ICLOUD_APP_PASSWORD "
                "is a valid app-specific password."
            )
        if resp.status_code == 403:
            raise Forbidden(f"Not allowed ({method} {href}). The calendar may be read-only. {detail}")
        if resp.status_code == 404:
            raise NotFound(f"Not found: {href}")
        if resp.status_code == 412:
            raise Conflict("The item changed on the server in the meantime (or already exists). Retry.")
        raise UpstreamError(f"CalDAV {method} {href} -> HTTP {resp.status_code}: {detail}")

    def _parse_multistatus(self, content: bytes) -> list[DavResource]:
        try:
            root = etree.fromstring(content, parser=_PARSER)
        except etree.XMLSyntaxError as exc:
            raise UpstreamError(f"Invalid XML from CalDAV server: {exc}") from exc
        out: list[DavResource] = []
        for response in root.iterfind("d:response", NS):
            href_el = response.find("d:href", NS)
            if href_el is None or not href_el.text:
                continue
            res = DavResource(href=self.path_of(href_el.text.strip()))
            status_el = response.find("d:status", NS)
            if status_el is not None and status_el.text:
                res.status = int(status_el.text.split()[1])
            for propstat in response.iterfind("d:propstat", NS):
                st = propstat.find("d:status", NS)
                code = int(st.text.split()[1]) if st is not None and st.text else 200
                if code != 200:
                    continue
                prop = propstat.find("d:prop", NS)
                if prop is None:
                    continue
                for child in prop:
                    res.props[child.tag] = child
            out.append(res)
        return out

    async def propfind(self, href: str, props: list[str], depth: int = 0) -> list[DavResource]:
        prop_xml = "".join(f"<{p}/>" for p in props)
        ns_decl = " ".join(f'xmlns:{k}="{v}"' for k, v in NS.items())
        body = f'<?xml version="1.0" encoding="utf-8"?><d:propfind {ns_decl}><d:prop>{prop_xml}</d:prop></d:propfind>'
        resp = await self._request("PROPFIND", href, body=body, headers={**_XML_HEADERS, "Depth": str(depth)})
        return self._parse_multistatus(resp.content)

    # ------------------------------------------------------------- discovery

    async def discover(self) -> tuple[str, str, list[str]]:
        """Return (principal_path, calendar_home_url, calendar_user_addresses)."""
        res = await self.propfind(self.base_url, ["d:current-user-principal"])
        principal = None
        for r in res:
            el = r.props.get(_clark("d:current-user-principal"))
            if el is not None:
                href = el.find("d:href", NS)
                if href is not None and href.text:
                    principal = href.text.strip()
        if not principal:
            raise UpstreamError("CalDAV server did not report a current-user-principal.")
        principal_url = principal if principal.startswith("http") else urljoin(self.base_url, principal)

        res = await self.propfind(principal_url, ["c:calendar-home-set", "c:calendar-user-address-set"])
        home = None
        addresses: list[str] = []
        for r in res:
            el = r.props.get(_clark("c:calendar-home-set"))
            if el is not None:
                href = el.find("d:href", NS)
                if href is not None and href.text:
                    home = href.text.strip()
            el = r.props.get(_clark("c:calendar-user-address-set"))
            if el is not None:
                addresses.extend(h.text.strip() for h in el.iterfind("d:href", NS) if h.text)
        if not home:
            raise UpstreamError("CalDAV server did not report a calendar-home-set.")
        home_url = home if home.startswith("http") else urljoin(principal_url, home)
        if not home_url.endswith("/"):
            home_url += "/"
        # iCloud moves the account to a partition host; talk to it directly from now on.
        self.origin = self._origin(home_url)
        return self.path_of(principal_url), home_url, addresses

    # ------------------------------------------------------------- calendars

    async def list_collections(self, home_href: str) -> list[DavResource]:
        return await self.propfind(
            home_href,
            [
                "d:resourcetype",
                "d:displayname",
                "d:current-user-privilege-set",
                "a:calendar-color",
                "a:calendar-order",
                "c:calendar-description",
                "c:supported-calendar-component-set",
                "cs:getctag",
            ],
            depth=1,
        )

    async def mkcalendar(
        self,
        href: str,
        name: str,
        *,
        color: str | None = None,
        description: str | None = None,
        components: tuple[str, ...] = ("VEVENT",),
    ) -> None:
        extra = ""
        if color:
            extra += f"<a:calendar-color>{escape(color)}</a:calendar-color>"
        if description:
            extra += f"<c:calendar-description>{escape(description)}</c:calendar-description>"
        comps = "".join(f'<c:comp name="{c}"/>' for c in components)
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<c:mkcalendar xmlns:d="DAV:" xmlns:c="{NS["c"]}" xmlns:a="{NS["a"]}">'
            "<d:set><d:prop>"
            f"<d:displayname>{escape(name)}</d:displayname>{extra}"
            f"<c:supported-calendar-component-set>{comps}</c:supported-calendar-component-set>"
            "</d:prop></d:set></c:mkcalendar>"
        )
        await self._request("MKCALENDAR", href, body=body, headers=_XML_HEADERS, ok=(200, 201))

    async def proppatch(self, href: str, set_props: dict[str, str], remove_props: list[str] | None = None) -> None:
        sets = "".join(f"<{k}>{escape(v)}</{k}>" for k, v in set_props.items())
        removes = "".join(f"<{k}/>" for k in remove_props or [])
        ns_decl = " ".join(f'xmlns:{k}="{v}"' for k, v in NS.items())
        body = f'<?xml version="1.0" encoding="utf-8"?><d:propertyupdate {ns_decl}>'
        if sets:
            body += f"<d:set><d:prop>{sets}</d:prop></d:set>"
        if removes:
            body += f"<d:remove><d:prop>{removes}</d:prop></d:remove>"
        body += "</d:propertyupdate>"
        resp = await self._request("PROPPATCH", href, body=body, headers=_XML_HEADERS)
        # A 207 can still hide per-property failures.
        if resp.status_code == 207:
            root = etree.fromstring(resp.content, parser=_PARSER)
            for st in root.iterfind(".//d:propstat/d:status", NS):
                code = int((st.text or "HTTP/1.1 200").split()[1])
                if code >= 400:
                    raise UpstreamError(f"Server refused property update (HTTP {code}).")

    # ---------------------------------------------------------------- events

    async def query_events(self, calendar_href: str, start: datetime, end: datetime) -> list[CalendarObject]:
        body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<c:calendar-query xmlns:d="DAV:" xmlns:c="{NS["c"]}">'
            "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
            '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
            f'<c:time-range start="{_fmt_utc(start)}" end="{_fmt_utc(end)}"/>'
            "</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>"
        )
        resp = await self._request("REPORT", calendar_href, body=body, headers={**_XML_HEADERS, "Depth": "1"})
        out: list[CalendarObject] = []
        for r in self._parse_multistatus(resp.content):
            data = r.text(_clark("c:calendar-data"))
            if not data:
                continue
            out.append(CalendarObject(href=r.href, etag=r.text(_clark("d:getetag")), data=data))
        return out

    async def get(self, href: str) -> CalendarObject:
        resp = await self._request("GET", href, ok=(200,))
        return CalendarObject(href=self.path_of(href), etag=resp.headers.get("ETag"), data=resp.text)

    async def put(self, href: str, data: str, *, etag: str | None = None, create: bool = False) -> str | None:
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if create:
            headers["If-None-Match"] = "*"
        elif etag:
            headers["If-Match"] = etag
        resp = await self._request("PUT", href, body=data.encode("utf-8"), headers=headers, ok=(200, 201, 204))
        return resp.headers.get("ETag")

    async def delete(self, href: str, *, etag: str | None = None) -> None:
        headers = {"If-Match": etag} if etag else None
        await self._request("DELETE", href, headers=headers, ok=(200, 204))
