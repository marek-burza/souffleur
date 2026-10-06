import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .content_rules import TextOrigin
from .context import ScanContext
from .findings import Severity
from .textutil import LineIndex, make_snippet, refang

URL_PATTERN = re.compile(r"(?i)\b(?:https?|wss?|ftp)://[^\s'\"`<>()\[\]{},\\|^]+")
QUOTED_IP_PATTERN = re.compile(r"""['"`/@](\d{1,3}(?:\.\d{1,3}){3})(?::(\d{2,5}))?(?=['"`/:])""")
SAME_QUOTE_CONCATENATION = re.compile(r"""(['"`])[ \t]*\+[ \t]*\1""")
PYTHON_ADJACENT_LITERALS = re.compile(r"""(['"])[ \t]+\1""")


@dataclass
class UrlVerdict:
    rule: str
    severity: Severity
    title: str


def scan_all_files_for_network_indicators(ctx: ScanContext) -> None:
    for record in ctx.text_files():
        origin = TextOrigin(record.rel, [])
        scan_text_for_network_indicators(ctx, record.text, origin)
        detect_urls_assembled_from_fragments(ctx, record.text, origin)
        detect_wallet_extension_ids(ctx, record.text, origin)


def scan_text_for_network_indicators(ctx: ScanContext, text: str, origin: TextOrigin) -> Severity | None:
    plain = refang(text)
    index = LineIndex(plain)
    worst = None
    for match in URL_PATTERN.finditer(plain):
        verdict = worst_verdict_for_url(ctx, match.group(0), origin)
        record_host(ctx, match.group(0), origin.rel)
        if verdict:
            report_url(ctx, verdict, plain, index, match, origin)
            worst = max(worst or verdict.severity, verdict.severity)
    for match in QUOTED_IP_PATTERN.finditer(plain):
        verdict = verdict_for_bare_ip(ctx, match.group(1), match.group(2))
        if verdict:
            report_url(ctx, verdict, plain, index, match, origin)
            worst = max(worst or verdict.severity, verdict.severity)
    worst = max(worst or Severity.INFO, report_ioc_domains_without_scheme(ctx, plain, index, origin))
    return worst if worst > Severity.INFO else None


def record_host(ctx: ScanContext, url: str, rel: str) -> None:
    host = hostname_of(url)
    if host:
        ctx.hosts_seen[host].add(rel)


def hostname_of(url: str) -> str | None:
    try:
        return (urlsplit(url).hostname or "").lower() or None
    except ValueError:
        return None


def worst_verdict_for_url(ctx: ScanContext, url: str, origin: TextOrigin) -> UrlVerdict | None:
    verdicts = ioc_verdicts_for_url(ctx, url) + class_verdicts_for_url(ctx, url) + raw_ip_verdicts_for_url(ctx, url)
    if not verdicts:
        return None
    worst = max(verdicts, key=lambda v: v.severity)
    return cap_severity_outside_code(ctx, worst, origin)


def cap_severity_outside_code(ctx: ScanContext, verdict: UrlVerdict, origin: TextOrigin) -> UrlVerdict | None:
    if verdict.rule.startswith("IOC") or origin.is_decoded:
        return verdict
    if ctx.is_lockfile(origin.rel):
        return None
    if ctx.is_doc(origin.rel):
        cap = Severity.parse(ctx.config.section("hosts")["docs_max_severity"])
        return UrlVerdict(verdict.rule, min(verdict.severity, cap), verdict.title + " (in documentation)")
    return verdict


def ioc_verdicts_for_url(ctx: ScanContext, url: str) -> list[UrlVerdict]:
    iocs = ctx.config.iocs
    host = hostname_of(url) or ""
    lowered = url.lower()
    verdicts = []
    if host in iocs.domains or any(host.endswith("." + d) for d in iocs.domains):
        verdicts.append(UrlVerdict("IOC-DOMAIN", Severity.CRITICAL, "Known malicious domain"))
    if any(p.search(host) for p in iocs.domain_patterns):
        verdicts.append(UrlVerdict("IOC-DOMAIN", Severity.CRITICAL, "Domain matches a known malicious naming pattern"))
    if any(u in lowered for u in iocs.urls) or any(p.search(url) for p in iocs.url_patterns):
        verdicts.append(UrlVerdict("IOC-URL", Severity.CRITICAL, "URL matches a known malicious URL pattern"))
    if host in iocs.ips:
        verdicts.append(UrlVerdict("IOC-IP", Severity.CRITICAL, "Known command-and-control IP address"))
    return verdicts


def class_verdicts_for_url(ctx: ScanContext, url: str) -> list[UrlVerdict]:
    return [UrlVerdict(c.id, c.severity, c.title) for c in ctx.config.host_classes if c.regex.search(url)]


def raw_ip_verdicts_for_url(ctx: ScanContext, url: str) -> list[UrlVerdict]:
    host = hostname_of(url)
    if not host or not is_public_ip(host):
        return []
    try:
        parts = urlsplit(url)
        port, path = parts.port, parts.path
    except ValueError:
        port, path = None, ""
    iocs = ctx.config.iocs
    if port in iocs.c2_ports or any(p.search(path or "") for p in iocs.c2_paths):
        return [UrlVerdict("IOC-C2-SHAPE", Severity.CRITICAL, "Raw-IP URL with a known C2 port or path shape")]
    return [UrlVerdict("NET-RAW-IP", Severity.HIGH, "URL points at a raw public IP address")]


def verdict_for_bare_ip(ctx: ScanContext, ip: str, port: str | None) -> UrlVerdict | None:
    iocs = ctx.config.iocs
    if ip in iocs.ips:
        return UrlVerdict("IOC-IP", Severity.CRITICAL, "Known command-and-control IP address")
    if not is_public_ip(ip) or port is None:
        return None
    if int(port) in iocs.c2_ports:
        return UrlVerdict("IOC-C2-SHAPE", Severity.CRITICAL, "Public IP with a known C2 port")
    return UrlVerdict("NET-RAW-IP", Severity.HIGH, "Hardcoded public IP address and port")


def is_public_ip(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_global


def report_ioc_domains_without_scheme(ctx: ScanContext, plain: str, index: LineIndex, origin: TextOrigin) -> Severity:
    lowered = plain.lower()
    found = Severity.INFO
    for domain in [*ctx.config.iocs.domains, *ctx.config.iocs.urls, *ctx.config.iocs.ips]:
        offset = lowered.find(domain)
        if offset >= 0:
            verdict = UrlVerdict("IOC-INDICATOR", Severity.CRITICAL, "Known malicious domain, URL or IP mentioned")
            report_span(ctx, verdict, plain, index, offset, offset + len(domain), origin)
            found = Severity.CRITICAL
    return found


def report_url(ctx: ScanContext, verdict: UrlVerdict, text: str, index: LineIndex, match: re.Match, origin: TextOrigin) -> None:
    report_span(ctx, verdict, text, index, match.start(), match.end(), origin)


def report_span(ctx, verdict: UrlVerdict, text: str, index: LineIndex, start: int, end: int, origin: TextOrigin) -> None:
    ctx.report(
        verdict.rule,
        verdict.severity,
        verdict.title if not origin.is_decoded else f"{verdict.title} (inside decoded data)",
        path=origin.rel,
        line=origin.line_override or index.line_number(start),
        snippet=make_snippet(text, index, start, end),
        explain="Never fetch this URL to check it; that contacts attacker infrastructure.",
        category="network",
        decode_chain=origin.decode_chain,
    )


def detect_urls_assembled_from_fragments(ctx: ScanContext, text: str, origin: TextOrigin) -> None:
    joined = PYTHON_ADJACENT_LITERALS.sub("", SAME_QUOTE_CONCATENATION.sub("", text))
    if joined == text:
        return
    original_hosts = {hostname_of(url) for url in URL_PATTERN.findall(refang(text))}
    index = LineIndex(joined)
    for match in URL_PATTERN.finditer(refang(joined)):
        if hostname_of(match.group(0)) not in original_hosts:
            report_assembled_url(ctx, joined, index, match, origin)


def report_assembled_url(ctx: ScanContext, joined: str, index: LineIndex, match: re.Match, origin: TextOrigin) -> None:
    verdict = worst_verdict_for_url(ctx, match.group(0), TextOrigin(origin.rel, ["string-concatenation"]))
    severity = max(Severity.HIGH, verdict.severity) if verdict else Severity.MEDIUM
    ctx.report(
        "OB-SPLIT-URL",
        severity,
        "URL assembled from concatenated string fragments",
        path=origin.rel,
        line=index.line_number(match.start()),
        snippet=make_snippet(joined, index, match.start(), match.end()),
        explain="Splitting a URL across string pieces hides it from simple searches.",
        category="obfuscation",
    )


def detect_wallet_extension_ids(ctx: ScanContext, text: str, origin: TextOrigin) -> None:
    index = None
    for extension_id, wallet in ctx.config.iocs.wallet_extension_ids.items():
        offset = text.find(extension_id)
        if offset >= 0:
            index = index or LineIndex(text)
            ctx.report(
                "IOC-WALLET-EXT",
                Severity.CRITICAL,
                f"Chrome extension ID of the {wallet} wallet",
                path=origin.rel,
                line=origin.line_override or index.line_number(offset),
                snippet=make_snippet(text, index, offset, offset + len(extension_id)),
                explain="Infostealers (BeaverTail) list wallet extension IDs to locate and steal their data.",
                category="crypto-theft",
                decode_chain=origin.decode_chain,
            )
