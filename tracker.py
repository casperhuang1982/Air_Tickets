"""機票價格追蹤：抓 Travelpayouts 價格 → 存歷史 → 低於目標價寄 Email。

環境變數：
  TP_TOKEN            Travelpayouts API token（必填）
  GMAIL_USER          寄件 Gmail 帳號（選填，沒設就不寄信）
  GMAIL_APP_PASSWORD  Gmail 應用程式密碼
  NOTIFY_TO           收件人，預設同 GMAIL_USER
  SERPAPI_KEY         SerpApi key（選填，有設就用 Google Flights 即時價格查指定行程）
"""

import json
import os
import smtplib
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
HISTORY_FILE = DATA / "history.json"
LATEST_FILE = DATA / "latest.json"
NOTIFIED_FILE = DATA / "notified.json"

API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
SERP_URL = "https://serpapi.com/search.json"
SERP_MIN_INTERVAL = timedelta(hours=20)  # 免費額度有限，指定行程每天只查一次
TW = timezone(timedelta(hours=8))
HISTORY_MAX_DAYS = 365
OFFERS_KEPT = 10
PREFERRED_KEPT = 5


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path, obj):
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def target_months(cfg):
    if cfg.get("months"):
        return cfg["months"]
    today = date.today()
    months = []
    y, m = today.year, today.month
    for _ in range(cfg.get("months_ahead", 3)):
        m += 1
        if m > 12:
            y, m = y + 1, 1
        months.append(f"{y}-{m:02d}")
    return months


def fetch_offers(cfg, dest, departure_at, token, return_at=None):
    params = {
        "origin": cfg["origin"],
        "destination": dest,
        "departure_at": departure_at,
        "one_way": str(cfg.get("one_way", False)).lower(),
        "direct": str(cfg.get("direct_only", False)).lower(),
        "currency": cfg.get("currency", "twd"),
        "sorting": "price",
        "unique": "false",
        "limit": 100,
        "token": token,
    }
    if return_at:
        params["return_at"] = return_at
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not body.get("success"):
        raise RuntimeError(f"API 回傳失敗：{body}")
    return body.get("data", [])


def fetch_serp_trip(cfg, dest, trip, key):
    """用 SerpApi 查 Google Flights 指定日期的即時來回票價。"""
    airports = {d["code"]: d.get("airports", d["code"]) for d in cfg["destinations"]}
    params = {
        "engine": "google_flights",
        "departure_id": cfg.get("origin_airports", cfg["origin"]),
        "arrival_id": airports[dest],
        "outbound_date": trip["depart"],
        "return_date": trip["return"],
        "type": 1,
        "currency": cfg.get("currency", "twd").upper(),
        "hl": "zh-TW",
        "gl": "tw",
        "api_key": key,
    }
    url = f"{SERP_URL}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if body.get("error"):
        raise RuntimeError(body["error"])
    gf_url = body.get("search_metadata", {}).get("google_flights_url")
    offers = []
    for f in body.get("best_flights", []) + body.get("other_flights", []):
        if not f.get("price"):
            continue
        segs = f["flights"]
        offers.append({
            "price": f["price"],
            "airline": " / ".join(dict.fromkeys(s["airline"] for s in segs)),
            "flight_number": ", ".join(s["flight_number"] for s in segs),
            "destination_airport": segs[-1]["arrival_airport"]["id"],
            "departure_at": segs[0]["departure_airport"]["time"].replace(" ", "T"),
            "return_at": trip["return"],
            "transfers": len(f.get("layovers", [])),
            "return_transfers": None,
            "duration_min": f.get("total_duration"),
            "link": gf_url,
            "source": "google",
        })
    pi = body.get("price_insights") or {}
    insights = {
        "lowest": pi.get("lowest_price"),
        "level": pi.get("price_level"),
        "typical_range": pi.get("typical_price_range"),
    } if pi else None
    return sorted(offers, key=lambda o: o["price"]), insights


def serp_account(key):
    """查詢 SerpApi 帳號額度（此查詢本身不計入額度）。"""
    try:
        with urllib.request.urlopen(f"https://serpapi.com/account.json?api_key={key}", timeout=30) as resp:
            a = json.loads(resp.read().decode("utf-8"))
        print(f"SerpApi 方案：{a.get('plan_name')}，本月剩餘 {a.get('plan_searches_left')} / {a.get('searches_per_month')} 次")
        return {k: a.get(k) for k in ("plan_name", "searches_per_month", "plan_searches_left",
                                      "this_month_usage", "extra_credits", "account_rate_limit_per_hour")}
    except Exception as e:
        print(f"SerpApi 帳號查詢失敗：{type(e).__name__}")
        return None


WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def flex_dates(ft):
    """彈性行程：列出指定月份中每個指定星期幾出發的去回日期（只取未來日期）。"""
    y, m = map(int, ft["month"].split("-"))
    wd = WEEKDAYS[ft.get("weekday", "sat")]
    d = date(y, m, 1)
    out = []
    while d.month == m:
        if d.weekday() == wd and d > date.today():
            out.append((d.isoformat(), (d + timedelta(days=ft["nights"])).isoformat()))
        d += timedelta(days=1)
    return out


def matches_airline(offer, airlines):
    """航班是否屬於清單中任一家航空；轉機行程只要有一段符合就算。
    同時比對航空代碼、航班號前綴與中文名稱。"""
    airline = offer.get("airline") or ""
    flights = [f.strip() for f in (offer.get("flight_number") or "").split(",")]
    for a in airlines:
        if airline == a["code"] or (a.get("name") and a["name"] in airline):
            return True
        # Google 航班號為「BR 186」；Travelpayouts 的 flight_number 只有數字，由 airline 判斷
        if any(f.startswith(a["code"] + " ") for f in flights):
            return True
    return False


def is_preferred(cfg, offer):
    return matches_airline(offer, cfg.get("preferred_airlines", []))


def pick_offers(cfg, offers):
    """排除不要的航空，保留最便宜的 N 班，另外把偏好航空的航班也留下，避免因為較貴而被刷掉。"""
    offers = [o for o in offers if not matches_airline(o, cfg.get("excluded_airlines", []))]
    for o in offers:
        o["preferred"] = is_preferred(cfg, o)
    top = offers[:OFFERS_KEPT]
    extra = [o for o in offers[OFFERS_KEPT:] if o["preferred"]][:PREFERRED_KEPT]
    return top + extra


def trip_days(offer):
    if not offer.get("return_at"):
        return None
    dep = datetime.fromisoformat(offer["departure_at"]).date()
    ret = datetime.fromisoformat(offer["return_at"]).date()
    return (ret - dep).days


def keep_offer(cfg, offer):
    if cfg.get("one_way", False):
        return True
    days = trip_days(offer)
    if days is None:
        return False
    return cfg.get("trip_days_min", 0) <= days <= cfg.get("trip_days_max", 99)


def simplify(offer):
    return {
        "price": offer["price"],
        "airline": offer.get("airline"),
        "flight_number": offer.get("flight_number"),
        "destination_airport": offer.get("destination_airport"),
        "departure_at": offer.get("departure_at"),
        "return_at": offer.get("return_at"),
        "transfers": offer.get("transfers", 0),
        "return_transfers": offer.get("return_transfers", 0),
        "link": "https://www.aviasales.com" + offer["link"] if offer.get("link") else None,
    }


def send_email(subject, html):
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not password:
        print("未設定 GMAIL_USER / GMAIL_APP_PASSWORD，略過寄信")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = os.environ.get("NOTIFY_TO") or user
    msg.set_content("請用支援 HTML 的信箱檢視此通知。")
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)
    print(f"已寄出通知：{subject}")
    return True


def build_email(alerts, currency):
    rows = []
    for a in alerts:
        o = a["offer"]
        dep = o["departure_at"][:10]
        ret = o["return_at"][:10] if o.get("return_at") else "單程"
        stops = "直飛" if o["transfers"] == 0 else f"轉機 {o['transfers']} 次"
        if o.get("preferred"):
            stops += " ⭐"
        link = f'<a href="{o["link"]}">查看</a>' if o.get("link") else ""
        rows.append(
            f"<tr><td>{a['name']}</td><td><b>{o['price']:,}</b></td>"
            f"<td>{a['target']:,}</td><td>{dep} → {ret}</td>"
            f"<td>{o.get('airline') or ''} {stops}</td><td>{link}</td></tr>"
        )
    return (
        f"<p>以下航線低於目標價（{currency.upper()}）：</p>"
        '<table border="1" cellpadding="6" style="border-collapse:collapse">'
        "<tr><th>目的地</th><th>價格</th><th>目標</th><th>日期</th><th>航空</th><th></th></tr>"
        + "".join(rows)
        + "</table><p>價格為快取資料，下訂前請再確認即時價格。</p>"
    )


def main():
    token = os.environ.get("TP_TOKEN")
    if not token:
        sys.exit("缺少 TP_TOKEN 環境變數")

    cfg = load_json(ROOT / "config.json", None)
    months = target_months(cfg)
    now = datetime.now(TW).isoformat(timespec="minutes")

    history = load_json(HISTORY_FILE, [])
    prev_routes = {r["key"]: r for r in load_json(LATEST_FILE, {}).get("routes", [])}
    serp_key = os.environ.get("SERPAPI_KEY")
    calls = {"serpapi": 0, "travelpayouts": 0}

    def serp_due(key, interval=SERP_MIN_INTERVAL):
        """每條航線各自計時：從沒查過、或距上次查詢已滿間隔才查。"""
        last = prev_routes.get(key, {}).get("checked_at")
        return not last or datetime.now(TW) - datetime.fromisoformat(last) >= interval
    notified = load_json(NOTIFIED_FILE, {})
    latest = {"updated_at": now, "currency": cfg.get("currency", "twd"), "routes": []}
    alerts = []
    errors = 0

    # 要查的期間：指定行程（固定去回日期）＋ 未來每個月份
    periods = [
        {"id": t["id"], "label": t["name"], "kind": "trip", "depart": t["depart"],
         "return": t.get("return"), "target": t.get("target_price")}
        for t in cfg.get("trips", []) if t["depart"] >= date.today().isoformat()
    ] + [
        {"id": ft["id"], "label": ft["name"], "kind": "flex", "dest": ft["dest"], "flex": ft,
         "target": ft.get("target_price")}
        for ft in cfg.get("flex_trips", []) if serp_key and flex_dates(ft)
    ] + [{"id": m, "label": m, "kind": "month", "depart": m, "return": None} for m in months]

    for d in cfg["destinations"]:
        for p in periods:
            if p.get("dest") and p["dest"] != d["code"]:
                continue  # 彈性行程只查指定的城市
            key = f"{cfg['origin']}-{d['code']}-{p['id']}"
            target = d["target_price"] if p["kind"] == "month" else p["target"]
            route = {
                "key": key, "code": d["code"], "name": d["name"], "month": p["id"],
                "label": p["label"], "kind": p["kind"], "target_price": target,
            }
            if p["kind"] == "trip":
                route.update(depart=p["depart"], **{"return": p["return"]})
            fresh = True
            try:
                if p["kind"] == "flex":
                    ft = p["flex"]
                    interval = timedelta(days=ft.get("refresh_days", 4)) - timedelta(hours=2)
                    if serp_due(key, interval):
                        # 每組日期各查一次，每天保留前幾名與偏好航空，再合併
                        offers, dates = [], []
                        for dep, ret in flex_dates(ft):
                            got, ins = fetch_serp_trip(cfg, d["code"], {"depart": dep, "return": ret}, serp_key)
                            calls["serpapi"] += 1
                            got = pick_offers(cfg, got)
                            pref = next((o for o in got if o["preferred"]), None)
                            dates.append({"depart": dep, "return": ret, "best": got[0]["price"] if got else None,
                                          "pref": pref["price"] if pref else None, "level": (ins or {}).get("level")})
                            offers += got[:3] + [o for o in got[3:] if o["preferred"]][:2]
                        offers.sort(key=lambda o: o["price"])
                        route["dates"] = dates
                        route["checked_at"] = now
                    else:
                        prev = prev_routes.get(key, {})
                        offers = prev.get("offers", [])
                        route["dates"] = prev.get("dates", [])
                        route["checked_at"] = prev.get("checked_at")
                        fresh = False
                    route["source"] = "google"
                    route["refresh_days"] = ft.get("refresh_days", 4)
                    print(f"[{key}] Google Flights {len(route['dates'])} 組日期，最低 {offers[0]['price'] if offers else '-'}"
                          + ("" if fresh else "（沿用上次結果）"))
                elif p["kind"] == "trip" and serp_key:
                    if serp_due(key):
                        offers, route["insights"] = fetch_serp_trip(cfg, d["code"], p, serp_key)
                        calls["serpapi"] += 1
                        offers = pick_offers(cfg, offers)
                        route["checked_at"] = now
                    else:  # 沿用上次 SerpApi 結果，不寫入歷史
                        prev = prev_routes.get(key, {})
                        offers = pick_offers(cfg, prev.get("offers", []))
                        route["insights"] = prev.get("insights")
                        route["checked_at"] = prev.get("checked_at")
                        fresh = False
                    route["source"] = "google"
                    print(f"[{key}] Google Flights {len(offers)} 筆，最低 {offers[0]['price'] if offers else '-'}")
                else:
                    raw = fetch_offers(cfg, d["code"], p["depart"], token, p["return"])
                    calls["travelpayouts"] += 1
                    time.sleep(0.2)
                    # 指定行程已固定日期，不再套用天數篩選
                    kept = raw if p["kind"] == "trip" else [o for o in raw if keep_offer(cfg, o)]
                    offers = pick_offers(cfg, sorted((simplify(o) for o in kept), key=lambda o: o["price"]))
                    print(f"[{key}] {len(raw)} 筆，符合條件 {len(offers)} 筆，最低 {offers[0]['price'] if offers else '-'}")
            except Exception as e:  # 單一航線失敗不影響其他航線
                print(f"[{key}] 抓取失敗：{e}")
                errors += 1
                continue

            route["offers"] = offers
            latest["routes"].append(route)
            best = offers[0] if offers else None
            if not fresh:
                continue
            if best:
                entry = {"t": now, "key": key, "code": d["code"], "month": p["id"], "price": best["price"]}
                pref = next((o for o in offers if o.get("preferred")), None)
                if pref:
                    entry["pref"] = pref["price"]
                history.append(entry)

            if target is None:  # 沒設目標價只記錄、不通知
                continue
            # 低於目標價，且比上次通知的價格更低才寄信，避免每次重複通知
            if best and best["price"] <= target:
                last = notified.get(key)
                if last is None or best["price"] < last:
                    alerts.append({"key": key, "name": f"{d['name']} {p['label']}", "target": target, "offer": best})
            elif key in notified:
                del notified[key]  # 價格回升，下次再跌破目標時重新通知

    cutoff = (datetime.now(TW) - timedelta(days=HISTORY_MAX_DAYS)).isoformat()
    history = [h for h in history if h["t"] >= cutoff]

    # 信真的寄出才記錄已通知，未設定 Gmail 時下次仍會通知
    if alerts:
        names = "、".join(a["name"] for a in alerts)
        if send_email(f"✈️ 機票降價：{names}", build_email(alerts, latest["currency"])):
            for a in alerts:
                notified[a["key"]] = a["offer"]["price"]

    # API 額度：放在最後查，數字才包含本次用量
    today = date.today().isoformat()
    planned = sum(30 for t in cfg.get("trips", []) if t["depart"] >= today) * len(cfg["destinations"])
    planned += sum(len(flex_dates(ft)) * 30 / ft.get("refresh_days", 4) for ft in cfg.get("flex_trips", []))
    latest["api_usage"] = {
        "checked_at": now,
        "serpapi": serp_account(serp_key) if serp_key else None,
        "serpapi_calls_this_run": calls["serpapi"],
        "serpapi_planned_per_month": round(planned),
        "travelpayouts_calls_this_run": calls["travelpayouts"],
    }

    save_json(LATEST_FILE, latest)
    save_json(HISTORY_FILE, history)
    save_json(NOTIFIED_FILE, notified)

    if errors and not latest["routes"]:
        sys.exit("所有航線都抓取失敗")


if __name__ == "__main__":
    main()
