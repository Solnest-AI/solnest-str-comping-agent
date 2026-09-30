"""Gmail SMTP email delivery for the branded report."""

import html
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

import config


def send_report_email(
    to_address: str,
    property_address: str,
    html_path: Path,
) -> None:
    """Send the report as an email attachment via Gmail SMTP.

    Uses GMAIL_ADDRESS and GMAIL_APP_PASSWORD from config.
    """
    if not config.GMAIL_ADDRESS or not config.GMAIL_APP_PASSWORD:
        raise ValueError(
            "Gmail credentials not configured. "
            "Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env"
        )

    brand = config.BRANDING

    def esc(value) -> str:
        return html.escape(str(value), quote=True)

    company = esc(brand["company_name"])
    address = esc(property_address)
    primary = esc(brand["primary_color"])
    accent = esc(brand["accent_color"])
    logo_url = brand.get("logo_url") or ""
    website_url = brand.get("website_url") or ""

    msg = MIMEMultipart()
    msg["From"] = config.GMAIL_ADDRESS
    msg["To"] = to_address
    # A scraped brand name or a pasted address must not be able to add headers.
    msg["Subject"] = " ".join(
        f"{brand['company_name']} | STR Income Analysis \u2013 {property_address}".split()
    )

    logo_html = ""
    if logo_url:
        logo_html = f"""
        <div style="text-align: center; margin-bottom: 30px;">
            <img src="{esc(logo_url)}"
                 alt="{company}" style="height: 80px; width: auto;" />
        </div>
"""
    reply_html = "Reply to this email."
    if website_url:
        reply_html = f"""Reply to this email or visit
                <a href="{esc(website_url)}" style="color: {accent}; text-decoration: none; font-weight: 600;">
                    {esc(website_url)}
                </a>"""

    # Branded HTML email body
    body = f"""
    <div style="font-family: 'Inter', Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 40px 20px;">{logo_html}
        <h2 style="color: {primary}; font-size: 24px; margin-bottom: 10px;">
            STR Income Analysis
        </h2>
        <p style="color: #57534e; font-size: 16px; line-height: 1.6;">
            Your property income analysis for <strong>{address}</strong> is ready.
        </p>
        <p style="color: #57534e; font-size: 14px; line-height: 1.6;">
            Please find the full interactive report attached. Open the HTML file in any
            web browser to view the interactive revenue calculator, seasonal charts,
            and comparable property analysis.
        </p>

        <div style="margin-top: 30px; padding: 20px; background: #f7f3ee; border-radius: 12px; border-left: 4px solid {accent};">
            <p style="color: {primary}; font-size: 14px; font-weight: 600; margin: 0;">
                Questions about this report?
            </p>
            <p style="color: #57534e; font-size: 13px; margin-top: 8px;">
                {reply_html}
            </p>
        </div>

        <div style="margin-top: 40px; padding-top: 20px; border-top: 1px solid #e5e7eb; text-align: center;">
            <p style="color: #9ca3af; font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em;">
                {company} &middot; {esc(brand["tagline"])}
            </p>
        </div>
    </div>
    """

    msg.attach(MIMEText(body, "html"))

    # Attach the HTML report
    with open(html_path, "rb") as f:
        attachment = MIMEBase("text", "html")
        attachment.set_payload(f.read())
        encoders.encode_base64(attachment)
        attachment.add_header("Content-Disposition", "attachment", filename=html_path.name)
        msg.attach(attachment)

    # Send via Gmail SMTP
    print(f"[Email] Sending report to {to_address}...")
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30, context=context) as server:
        server.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
        server.send_message(msg)

    print(f"[Email] Report sent successfully to {to_address}")
