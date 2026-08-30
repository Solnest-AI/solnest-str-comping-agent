"""Gmail SMTP email delivery for the branded report."""

import smtplib
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

    msg = MIMEMultipart()
    msg["From"] = config.GMAIL_ADDRESS
    msg["To"] = to_address
    msg["Subject"] = f"{config.BRANDING['company_name']} | STR Income Analysis \u2013 {property_address}"

    # Branded HTML email body
    body = f"""
    <div style="font-family: 'Inter', Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 40px 20px;">
        <div style="text-align: center; margin-bottom: 30px;">
            <img src="{config.BRANDING['logo_url']}"
                 alt="{config.BRANDING['company_name']}" style="height: 80px; width: auto;" />
        </div>

        <h2 style="color: #1f3c34; font-size: 24px; margin-bottom: 10px;">
            STR Income Analysis
        </h2>
        <p style="color: #57534e; font-size: 16px; line-height: 1.6;">
            Your property income analysis for <strong>{property_address}</strong> is ready.
        </p>
        <p style="color: #57534e; font-size: 14px; line-height: 1.6;">
            Please find the full interactive report attached. Open the HTML file in any
            web browser to view the interactive revenue calculator, seasonal charts,
            and comparable property analysis.
        </p>

        <div style="margin-top: 30px; padding: 20px; background: #f7f3ee; border-radius: 12px; border-left: 4px solid #4b7c6b;">
            <p style="color: #1f3c34; font-size: 14px; font-weight: 600; margin: 0;">
                Questions about this report?
            </p>
            <p style="color: #57534e; font-size: 13px; margin-top: 8px;">
                Reply to this email or visit
                <a href="{config.BRANDING['website_url']}" style="color: #4b7c6b; text-decoration: none; font-weight: 600;">
                    {config.BRANDING['website_url']}
                </a>
            </p>
        </div>

        <div style="margin-top: 40px; padding-top: 20px; border-top: 1px solid #e5e7eb; text-align: center;">
            <p style="color: #9ca3af; font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em;">
                {config.BRANDING['company_name']} &middot; {config.BRANDING['tagline']}
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
        attachment.add_header(
            "Content-Disposition",
            f"attachment; filename={html_path.name}",
        )
        msg.attach(attachment)

    # Send via Gmail SMTP
    print(f"[Email] Sending report to {to_address}...")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
        server.send_message(msg)

    print(f"[Email] Report sent successfully to {to_address}")
