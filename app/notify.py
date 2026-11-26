"""Notifications : ntfy, Gotify, e-mail."""
import smtplib
import ssl
from email.message import EmailMessage

import requests


def _ntfy(c, title, msg, prio):
    # publication JSON : gere correctement les accents dans le titre
    h = {"Authorization": f"Bearer {c['token']}"} if c.get("token") else {}
    r = requests.post(c["url"].rstrip("/"), headers=h, timeout=15,
                      json={"topic": c["topic"].strip("/"), "title": title, "message": msg,
                            "priority": {"low": 2, "normal": 3, "high": 4}[prio], "tags": ["musical_note"]})
    r.raise_for_status()


def _gotify(c, title, msg, prio):
    r = requests.post(c["url"].rstrip("/") + "/message", params={"token": c["token"]},
                      json={"title": title, "message": msg,
                            "priority": {"low": 2, "normal": 5, "high": 8}[prio]}, timeout=15)
    r.raise_for_status()


def _email(c, title, msg, prio):
    m = EmailMessage()
    m["Subject"], m["From"] = title, c["from"] or c["user"]
    m["To"] = c["to"]
    m.set_content(msg)
    port = int(c.get("port") or 587)
    ctx = ssl.create_default_context()
    if c.get("security") == "ssl":
        srv = smtplib.SMTP_SSL(c["host"], port, context=ctx, timeout=20)
    else:
        srv = smtplib.SMTP(c["host"], port, timeout=20)
        if c.get("security") == "starttls":
            srv.starttls(context=ctx)
    with srv:
        if c.get("user"):
            srv.login(c["user"], c["password"])
        srv.send_message(m)


CHANNELS = {"ntfy": _ntfy, "gotify": _gotify, "email": _email}


def send(cfg, title, msg, prio="normal", only=None, log=None):
    """Envoie sur tous les canaux actifs (ou seulement `only`). Retourne {canal: erreur|None}."""
    res = {}
    for name, fn in CHANNELS.items():
        c = cfg["notify"].get(name, {})
        if only and name != only:
            continue
        if not only and not c.get("enabled"):
            continue
        try:
            fn(c, title, msg, prio)
            res[name] = None
        except Exception as ex:
            res[name] = str(ex)
            if log:
                log(f"!! Notification {name} : {ex}")
    return res
