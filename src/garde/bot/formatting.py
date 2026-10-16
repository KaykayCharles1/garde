from garde.domain import CheckReport, Severity
from garde.sanitize import clean_text

TELEGRAM_LIMIT = 4096
DISCLAIMER = (
    "Garde reports risk signals only. This is not financial advice, "
    "and alerts can arrive after the damage is done."
)
ICONS = {Severity.INFO: "⚪", Severity.WARNING: "🟡", Severity.CRITICAL: "🔴"}
LABELS = {
    "source_verified": "Source code",
    "proxy": "Proxy",
    "owner": "Owner",
    "deployer": "Deployer",
    "holders": "Holders",
    "liquidity": "Liquidity",
    "scam_flag": "Explorer flag",
}


def format_report(report: CheckReport, explorer_url: str) -> str:
    """Plain text on purpose: no Markdown means nothing in token data can break the formatting."""
    token = report.token
    parts = [clean_text(p, 24) for p in (token.symbol, token.name) if p]
    lines = [
        f"Check: {' · '.join(parts) or 'Unnamed token'}",
        f"Chain: {token.chain.value}",
        f"Address: {token.address}",
        f"Explorer: {explorer_url}",
        "",
    ]
    for finding in report.findings:
        icon = ICONS[finding.severity] if finding.visible else "❔"
        level = finding.severity.value.upper() if finding.visible else "NOT VISIBLE"
        label = LABELS.get(finding.key, finding.key.replace("_", " ").title())
        lines += [f"{icon} {label} [{level}]", finding.summary, ""]
    if report.blind_spots:
        lines.append("What Garde cannot see:")
        lines += [f"- {spot}" for spot in report.blind_spots]

    # The disclaimer must survive truncation, so trim the body, never the disclaimer.
    room = TELEGRAM_LIMIT - len(DISCLAIMER) - 2
    body = "\n".join(lines).rstrip()
    if len(body) > room:
        body = body[: room - 1] + "…"
    return f"{body}\n\n{DISCLAIMER}"