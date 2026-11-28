#!/usr/bin/env python3
"""天気予報フィード生成スクリプト。

気象庁の予報 JSON（府県予報区ごと）を取得し、一次細分区域（142地点）ごとに
短期予報＋週間予報をマージした7日分を iCal / RSS / JSON で出力する。
あわせて一覧ページ（index.html）と OPML を生成する。

使い方: python3 generate.py --out _site
"""
import argparse
import json
import re
import shutil
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
BASE = "https://nidoneru.github.io/jma-weather-feed"
AREA_JSON = "https://www.jma.go.jp/bosai/common/const/area.json"
FORECAST = "https://www.jma.go.jp/bosai/forecast/data/forecast/{}.json"
UA = {"User-Agent": "jma-weather-feed (https://github.com/nidoneru/jma-weather-feed)"}
WEEKDAYS = "月火水木金土日"
SOURCE_LINE = "出典：気象庁の天気予報データを加工して作成"

ROOT = Path(__file__).resolve().parent


def fetch_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def dstr(dt_iso):
    return dt_iso[:10]


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# 天気テキストの表示整形（2026-11-27 確定仕様）。天気コード自体は変えない。
_FMT_TOKENS = sorted(
    ["時々止む", "一時強く降る"]
    + ["朝のうち", "昼頃から", "夕方から", "夜半から", "朝夕", "朝晩", "日中",
       "午後は", "明け方", "夕方", "夜は"]
    + ["海上海岸は", "山沿い"]
    + ["風雨共に強い", "風雪強い", "暴風雪", "雷雨", "大雨", "大雪", "霧雨",
       "晴れ", "くもり", "雨", "雪", "霧", "みぞれ"]
    + ["のち", "時々", "一時"],
    key=len, reverse=True,
)
_FMT_KA = re.compile(r"(晴れ|くもり|雨|雪|霧|霧雨|みぞれ)か(晴れ|くもり|雨|雪|霧雨|雷雨|みぞれ)")


def format_weather_text(t):
    # 1. つなぎの「後」を「のち」に置換（直後が天気の語で始まる場合のみ。「午後」は対象外）
    t = re.sub(r"後(?=[く晴雨雪み時一])", "のち", t)
    # 2. 「で〜を伴う」の形は無区切りのまま
    if "を伴う" in t:
        return t
    # 3. 「か」を含む形は、「のち」の前後だけ区切る（「昼頃から」等の「から」は対象外）
    if _FMT_KA.search(t):
        return t.replace("のち", " のち ").strip() if "のち" in t else t
    # 4. それ以外は全要素を半角スペースで区切る（「時々止む」等の述語は1塊）
    toks, i = [], 0
    while i < len(t):
        for tok in _FMT_TOKENS:
            if t.startswith(tok, i):
                toks.append(tok)
                i += len(tok)
                break
        else:
            return t
    return " ".join(toks)


class Data:
    def __init__(self):
        self.codes = json.loads((ROOT / "data/weather_codes.json").read_text(encoding="utf-8"))
        self.points = json.loads((ROOT / "data/points.json").read_text(encoding="utf-8"))
        self.weekly_areas = json.loads((ROOT / "data/weekly_areas.json").read_text(encoding="utf-8"))
        self.area_points = {}
        for pc, p in self.points.items():
            self.area_points.setdefault(p["area"], []).append(pc)
        for a, pcs in self.area_points.items():
            pcs.sort(key=lambda pc: (not self.points[pc]["representative"], pc))
        self.area_weekly = {}
        for wcode, w in self.weekly_areas.items():
            for a in w["areas"]:
                self.area_weekly[a] = wcode
        self.unknown_codes = set()

    def weather_text(self, code):
        t = self.codes.get(code)
        if t is None:
            self.unknown_codes.add(code)
            return ""
        return format_weather_text(t)


def parse_forecasts(area):
    """全予報区 JSON を取得し、短期（区域ごと）と週間（週間区域ごと）に整理する。"""
    short = {}   # area_code -> {weather:{date:code}, wind:{date:str}, pops:{date:[(label,int)]}, temps:{point:{date:(min,max)}}}
    weekly = {}  # weekly_code -> {weather:{date:(code,pop)}}
    wtemps = {}  # point_code -> {date:(min,max)}（週間予報の気温。短期の3日目の補完にも使う）
    for oc in area["offices"]:
        try:
            fc = fetch_json(FORECAST.format(oc))
        except Exception:
            continue  # 014030, 460040 は単独ファイルがなく、親ファイル側に含まれる
        rep_s, rep_w = fc[0], fc[1]
        ts = rep_s["timeSeries"]
        dates0 = [dstr(d) for d in ts[0]["timeDefines"]]
        for a in ts[0]["areas"]:
            e = short.setdefault(a["area"]["code"], {"weather": {}, "wind": {}, "pops": {}, "temps": {}})
            for d, c in zip(dates0, a.get("weatherCodes", [])):
                e["weather"][d] = c
            for d, w in zip(dates0, a.get("winds", [])):
                e["wind"][d] = w.replace("　", " ").strip()
        for a in ts[1]["areas"]:
            e = short.setdefault(a["area"]["code"], {"weather": {}, "wind": {}, "pops": {}, "temps": {}})
            for td, v in zip(ts[1]["timeDefines"], a.get("pops", [])):
                iv = to_int(v)
                if iv is None:
                    continue
                h = int(td[11:13])
                e["pops"].setdefault(dstr(td), []).append((f"{h}-{h + 6}時", iv))
        for a in ts[2]["areas"]:
            pc = a["area"]["code"]
            # 気温は地点単位で別枠に集める（地点と区域の対応は data/points.json）
            for td, v in zip(ts[2]["timeDefines"], a.get("temps", [])):
                iv = to_int(v)
                if iv is None:
                    continue
                hour = int(td[11:13])
                slot = "min" if hour == 0 else "max" if hour == 9 else None
                if slot is None:
                    continue
                short.setdefault("__temps__", {}).setdefault(pc, {}).setdefault(dstr(td), {})[slot] = iv
        tw = rep_w["timeSeries"]
        wdates = [dstr(d) for d in tw[0]["timeDefines"]]
        for a in tw[0]["areas"]:
            e = weekly.setdefault(a["area"]["code"], {"weather": {}})
            for d, c, p in zip(wdates, a.get("weatherCodes", []), a.get("pops", [])):
                e["weather"][d] = (c, to_int(p))
        tdates = [dstr(d) for d in tw[1]["timeDefines"]]
        for a in tw[1]["areas"]:
            pc = a["area"]["code"]
            for d, mn, mx in zip(tdates, a.get("tempsMin", []), a.get("tempsMax", [])):
                wtemps.setdefault(pc, {})[d] = (to_int(mn), to_int(mx))
    temps_all = short.pop("__temps__", {})
    return short, weekly, temps_all, wtemps


def build_days(data: Data, area_code, short, weekly, temps_all, wtemps):
    """区域の7日分（短期優先マージ）を作る。"""
    s = short.get(area_code, {"weather": {}, "wind": {}, "pops": {}})
    wcode = data.area_weekly.get(area_code)
    w = weekly.get(wcode, {"weather": {}}) if wcode else {"weather": {}}

    def point_temp(pc, d):
        t = temps_all.get(pc, {}).get(d, {})
        if t:
            return (t.get("min"), t.get("max"))
        wt = wtemps.get(pc, {}).get(d)
        if wt and (wt[0] is not None or wt[1] is not None):
            return wt
        return None
    sdates = sorted(s["weather"])
    wdates = [d for d in sorted(w["weather"]) if d not in sdates]
    days = []
    for d in (sdates + wdates)[:7]:
        if d in sdates:
            code = s["weather"][d]
            slots = s["pops"].get(d, [])
            temps = []
            for pc in data.area_points.get(area_code, []):
                t = point_temp(pc, d)
                if t:
                    temps.append((data.points[pc]["name"], t[0], t[1]))
            rep_pc = next((pc for pc in data.area_points.get(area_code, []) if data.points[pc]["representative"]), None)
            tt = None
            if rep_pc:
                t = point_temp(rep_pc, d)
                if t:
                    tt = (data.points[rep_pc]["name"], t[0], t[1])
            if not temps and wcode:
                # 短期の気温は2日分しかない。3日目は週間予報の区域地点で補う（地点名つきで出す）
                for pc in data.weekly_areas[wcode]["points"]:
                    t = wtemps.get(pc, {}).get(d)
                    if t and (t[0] is not None or t[1] is not None):
                        temps.append((data.points[pc]["name"], t[0], t[1]))
                if temps:
                    tt = temps[0]
            days.append({
                "date": d, "kind": "short", "code": code, "weather": data.weather_text(code),
                "wind": s["wind"].get(d, ""), "slots": slots,
                "pop": max((v for _, v in slots), default=None),
                "temps": temps, "title_temps": tt,
            })
        else:
            code, pop = w["weather"][d]
            winfo = data.weekly_areas[wcode]
            temps = []
            for pc in winfo["points"]:
                t = wtemps.get(pc, {}).get(d)
                if t and (t[0] is not None or t[1] is not None):
                    temps.append((data.points[pc]["name"], t[0], t[1]))
            days.append({
                "date": d, "kind": "weekly", "code": code, "weather": data.weather_text(code),
                "wind": "", "slots": [], "pop": pop,
                "temps": temps, "title_temps": temps[0] if temps else None,
            })
    return days


def temp_segment(tt):
    if not tt:
        return ""
    _, mn, mx = tt
    if mn is not None and mx is not None:
        return f"{mx}℃｜{mn}℃"
    if mx is not None:
        return f"最高{mx}℃"
    if mn is not None:
        return f"最低{mn}℃"
    return ""


def day_title(day):
    parts = [day["weather"], temp_segment(day["title_temps"])]
    if day["pop"] is not None:
        parts.append(f"{day['pop']}%")
    return " ".join(p for p in parts if p)


def day_lines(day, generated_str):
    lines = []
    if day["weather"]:
        lines.append(f"天気：{day['weather']}")
    if day["slots"]:
        lines.append("降水確率：" + "｜".join(f"{label} {v}%" for label, v in day["slots"]))
    elif day["pop"] is not None:
        lines.append(f"降水確率：{day['pop']}%")
    if day["wind"]:
        lines.append(f"風：{day['wind']}")
    if day["temps"]:
        segs = []
        for name, mn, mx in day["temps"]:
            seg = name
            if mx is not None:
                seg += f" 最高{mx}℃"
            if mn is not None:
                seg += f" 最低{mn}℃"
            segs.append(seg)
        lines.append("気温：" + "｜".join(segs))
    lines.append(SOURCE_LINE)
    lines.append(f"更新：{generated_str}")
    return lines


def ical_escape(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    out, cur = [], ""
    for ch in line:
        if len((cur + ch).encode("utf-8")) > 75:
            out.append(cur)
            cur = " " + ch
        else:
            cur += ch
    out.append(cur)
    return "\r\n".join(out)


def render_ical(area_code, area_name, days, generated_str, stamp):
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0",
        "PRODID:-//nidoneru//jma-weather-feed//JA",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        f"X-WR-CALNAME:天気予報フィード（{area_name}）",
        "X-WR-TIMEZONE:Asia/Tokyo",
    ]
    for day in days:
        d = datetime.strptime(day["date"], "%Y-%m-%d").date()
        lines += [
            "BEGIN:VEVENT",
            f"UID:{area_code}-{day['date'].replace('-', '')}@jma-weather-feed",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{ical_escape(day_title(day))}",
            f"DESCRIPTION:{ical_escape(chr(10).join(day_lines(day, generated_str)))}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in lines) + "\r\n"


def xml_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_rss(area_code, area_name, days, generated_str, gen_dt):
    items = []
    for day in days:
        d = datetime.strptime(day["date"], "%Y-%m-%d").date()
        title = f"{area_name} {d.month}月{d.day}日（{WEEKDAYS[d.weekday()]}）{day_title(day)}"
        pub = datetime(d.year, d.month, d.day, 5, 0, tzinfo=JST)
        items.append(
            "<item>"
            f"<title>{xml_escape(title)}</title>"
            f"<link>{BASE}/#{area_code}</link>"
            f"<guid isPermaLink=\"false\">{area_code}-{d.strftime('%Y%m%d')}</guid>"
            f"<pubDate>{pub.strftime('%a, %d %b %Y %H:%M:%S %z')}</pubDate>"
            f"<description>{xml_escape(chr(10).join(day_lines(day, generated_str)))}</description>"
            "</item>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"><channel>'
        f"<title>天気予報フィード {xml_escape(area_name)}</title>"
        f"<link>{BASE}/#{area_code}</link>"
        f"<description>気象庁の天気予報（{xml_escape(area_name)}）。{SOURCE_LINE}しています。</description>"
        "<language>ja</language>"
        f"<lastBuildDate>{gen_dt.strftime('%a, %d %b %Y %H:%M:%S %z')}</lastBuildDate>"
        + "".join(items) +
        "</channel></rss>\n"
    )


def render_json(area_code, area_name, office_name, days, generated_str):
    out_days = []
    for day in days:
        out_days.append({
            "date": day["date"], "kind": day["kind"],
            "weatherCode": day["code"], "weather": day["weather"],
            "wind": day["wind"] or None,
            "pops": ([{"slot": label, "value": v} for label, v in day["slots"]] if day["slots"]
                     else ([{"value": day["pop"]}] if day["pop"] is not None else [])),
            "temps": [{"point": n, "min": mn, "max": mx} for n, mn, mx in day["temps"]],
        })
    return json.dumps({
        "area": {"code": area_code, "name": area_name, "office": office_name},
        "generated": generated_str, "source": SOURCE_LINE,
        "days": out_days,
    }, ensure_ascii=False, indent=1) + "\n"


INDEX_CSS = """
:root{
  --primary:#0b57d0; --on-primary:#ffffff; --primary-container:#d3e3fd; --on-primary-container:#041e49;
  --surface:#f8fafd; --surface-container:#eef2f7; --card:#ffffff; --on-surface:#1f1f1f;
  --on-surface-variant:#444746; --outline:#c4c7c5;
}
@media (prefers-color-scheme: dark){
  :root{
    --primary:#a8c7fa; --on-primary:#0a305f; --primary-container:#004a77; --on-primary-container:#d3e3fd;
    --surface:#131314; --surface-container:#1e1f20; --card:#1e1f20; --on-surface:#e3e3e3;
    --on-surface-variant:#c4c7c5; --outline:#444746;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--surface);color:var(--on-surface);
  font-family:"Roboto","Noto Sans JP",sans-serif;line-height:1.7}
main{max-width:880px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:28px;font-weight:400;margin:8px 0 4px}
h2{font-size:20px;font-weight:500;margin:0 0 12px}
.lead{color:var(--on-surface-variant);margin:0 0 8px}
.meta{color:var(--on-surface-variant);font-size:13px;margin:0 0 24px}
.card{background:var(--card);border:1px solid var(--outline);border-radius:24px;
  padding:20px;margin:0 0 16px}
.sample-title{font-size:18px;font-weight:500;margin:0 0 8px}
.sample-body{white-space:pre-line;color:var(--on-surface-variant);margin:0;font-size:14px}
.area-row{display:flex;align-items:center;gap:8px;padding:8px 0;flex-wrap:wrap}
.area-name{min-width:9em;font-weight:500}
.btn{display:inline-block;padding:6px 16px;border-radius:999px;text-decoration:none;
  background:var(--primary-container);color:var(--on-primary-container);font-size:14px}
.btn:hover{background:var(--primary);color:var(--on-primary)}
ol{margin:0;padding-left:1.4em}
ul{margin:0;padding-left:1.4em}
.small{font-size:13px;color:var(--on-surface-variant)}
a{color:var(--primary)}
.sec{margin:26px 0 12px}
.office{font-size:17px;font-weight:500;margin:20px 0 8px}
.figure{max-width:100%;height:auto;background:#fff;border:1px solid var(--outline);border-radius:12px}
.region{margin:0 0 16px}
.region:last-child{margin-bottom:0}
.region a{font-size:17px;font-weight:500;text-decoration:none}
.region .members{color:var(--on-surface-variant);font-size:13px}
.region .members a{font-size:13px;font-weight:400}
.updated-when{display:inline-block;font-size:26px;font-weight:700;background:#f7e7b4;color:#1f1f1f;
  padding:2px 12px;border-radius:8px;margin:2px 0 10px}
.note-list{margin:14px 0}
.gap{margin-top:14px}
.at{width:.95em;height:.95em;vertical-align:-.15em}
@media (max-width:640px){
  .area-name{flex-basis:100%}
}
"""


REGIONS = [
    ("hokkaido", "北海道",
     ["011000", "012000", "013000", "014030", "014100", "015000", "016000", "017000"],
     [("宗谷", "011000"), ("上川・留萌", "012000"), ("網走・北見・紋別", "013000"),
      ("十勝", "014030"), ("釧路・根室", "014100"), ("胆振・日高", "015000"),
      ("石狩・空知・後志", "016000"), ("渡島・檜山", "017000")]),
    ("tohoku", "東北",
     ["020000", "030000", "040000", "050000", "060000", "070000"],
     [("青森", "020000"), ("岩手", "030000"), ("宮城", "040000"),
      ("秋田", "050000"), ("山形", "060000"), ("福島", "070000")]),
    ("kanto", "関東・甲信越",
     ["080000", "090000", "100000", "110000", "120000", "130000", "140000", "150000", "190000", "200000"],
     [("茨城", "080000"), ("栃木", "090000"), ("群馬", "100000"), ("埼玉", "110000"),
      ("千葉", "120000"), ("東京", "130000"), ("神奈川", "140000"), ("新潟", "150000"),
      ("山梨", "190000"), ("長野", "200000")]),
    ("tokai", "東海・北陸",
     ["160000", "170000", "180000", "210000", "220000", "230000", "240000"],
     [("富山", "160000"), ("石川", "170000"), ("福井", "180000"), ("岐阜", "210000"),
      ("静岡", "220000"), ("愛知", "230000"), ("三重", "240000")]),
    ("kansai", "関西",
     ["250000", "260000", "270000", "280000", "290000", "300000"],
     [("滋賀", "250000"), ("京都", "260000"), ("大阪", "270000"),
      ("兵庫", "280000"), ("奈良", "290000"), ("和歌山", "300000")]),
    ("chugoku", "中国",
     ["310000", "320000", "330000", "340000", "350000"],
     [("鳥取", "310000"), ("島根", "320000"), ("岡山", "330000"),
      ("広島", "340000"), ("山口", "350000")]),
    ("shikoku", "四国",
     ["360000", "370000", "380000", "390000"],
     [("徳島", "360000"), ("香川", "370000"), ("愛媛", "380000"), ("高知", "390000")]),
    ("kyushu", "九州・沖縄",
     ["400000", "410000", "420000", "430000", "440000", "450000",
      "460100", "460040", "471000", "472000", "473000", "474000"],
     [("福岡", "400000"), ("佐賀", "410000"), ("長崎", "420000"), ("熊本", "430000"),
      ("大分", "440000"), ("宮崎", "450000"), ("鹿児島", "460100"), ("沖縄", "471000")]),
]

SAMPLE_STAMP = "2026年1月23日 4時56分"
# 表示例は固定の内容（2026-11-27 に本人が1行ずつ指定して確定）
SAMPLE = (
    "晴れ 9℃｜-2℃ 0%",
    "\n".join([
        "天気：晴れ",
        "降水確率：0-6時 0%｜6-12時 0%｜12-18時 0%｜18-24時 0%",
        "風：北西の風 ２３区西部 では 北西の風 やや強く",
        "気温：東京 最高9℃ 最低-2℃",
        SOURCE_LINE,
        f"更新：{SAMPLE_STAMP}",
    ]),
)
FORM_URL = "https://docs.google.com/forms/d/e/1FAIpQLSfDXD4EkW81uoFn-BVUP8hcEKdRLjafR5m5csddDG-_DtGU8A/viewform"
ISSUES_URL = "https://github.com/nidoneru/jma-weather-feed/issues"


def render_index(area, grouped, sample, generated_str):
    grouped_map = dict(grouped)

    def rows_html(areas):
        rows = []
        for code, name in areas:
            rows.append(
                '<div class="area-row">'
                f'<span class="area-name">{xml_escape(name)}</span>'
                f'<a class="btn" href="ical/{code}.ics">iCal</a>'
                f'<a class="btn" href="webcal://nidoneru.github.io/jma-weather-feed/ical/{code}.ics">webcal</a>'
                f'<a class="btn" href="rss/{code}.xml">RSS</a>'
                f'<a class="btn" href="json/{code}.json">JSON</a>'
                "</div>"
            )
        return "".join(rows)

    region_index = "".join(
        f'<p class="region"><a href="#region-{slug}">{xml_escape(rname)}</a><br>'
        f'<span class="members">'
        + "、".join(
            f'<a href="#office-{mcode}">{xml_escape(mname)}</a>' for mname, mcode in members
        )
        + "</span></p>"
        for slug, rname, _codes, members in REGIONS
    )
    region_sections = []
    for slug, rname, codes, _members in REGIONS:
        parts = [f'<h2 class="sec" id="region-{slug}">{xml_escape(rname)}</h2>']
        for oc in codes:
            if oc not in grouped_map:
                continue
            parts.append(
                f'<h3 class="office" id="office-{oc}">{xml_escape(area["offices"][oc]["name"])}</h3>'
                f'<section class="card">{rows_html(grouped_map[oc])}</section>'
            )
        region_sections.append("".join(parts))

    sample_html = ""
    if sample:
        sample_html = (
            '<h2 class="sec">表示例（東京地方）</h2>'
            '<section class="card">'
            f'<p class="sample-title">{xml_escape(sample[0])}</p>'
            f'<p class="sample-body">{xml_escape(sample[1])}</p></section>'
        )
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>天気予報フィード</title>
<meta name="description" content="気象庁の天気予報を、全国142地点ごとに iCal・RSS・JSON で配布するフィードです。短期予報と週間予報を統合した7日分を、Google カレンダーや Outlook カレンダー、RSS リーダーで受け取れます。">
<link rel="icon" type="image/png" href="favicon.png">
<style>{INDEX_CSS}</style>
<script data-goatcounter="https://jma-weather-feed.goatcounter.com/count"
        async src="//gc.zgo.at/count.js"></script>
</head>
<body>
<main>
<h1>天気予報フィード</h1>
<p class="lead">気象庁が発表する天気予報を、一次細分区域（全国142地点）ごとに iCal・RSS・JSON で配布しています。短期予報（今日〜明後日）と週間予報を統合した7日分です（表示は短期優先）。</p>
<h2 class="sec">更新日時</h2>
<section class="card">
<p class="updated-when">{generated_str}</p>
<p>各フィード（iCal・RSS・JSON）の末尾にも、同じ更新日時が入っています。</p>
</section>
{sample_html}
<h2 class="sec">カレンダーへの登録（Google カレンダー）</h2>
<section class="card">
<ol>
<li>下の<a href="#areas">「地点一覧」</a>で、自分の地域の「iCal」の URL をコピーします</li>
<li><a href="https://calendar.google.com/">Google カレンダー</a>の画面左側にある「他のカレンダー」の「＋」から「URLで追加」を選んで登録します</li>
<li>Google 側との同期が終了すると、スマホ側のカレンダーにも自動で表示されます</li>
</ol>
<p><img class="figure" src="gcal-short-route.png" alt="Google カレンダーで「他のカレンダー」の「＋」を押し、「URLで追加」を選んで、URL を貼り付ける手順の図"></p>
<ol start="4">
<li>URLの登録は、パソコンのブラウザからのみ行えます。スマホの Google カレンダーアプリからは登録できません</li>
</ol>
<p class="small">スマホしかない場合は、ブラウザで PC 版サイトを表示すれば同じ画面で登録できます。</p>
<ul class="small note-list">
<li>Chrome（Android）：右上の「︙」メニューから「PC版サイト」にチェック</li>
<li>Chrome（iPhone）：画面右下の「…」メニューから「PC版サイトを見る」</li>
<li>Safari（iPhone）：画面左下のボタンをタップし、右下の「…」から「デスクトップ用Webサイトを表示」</li>
</ul>
<p class="small">表示が小さくて操作しづらいですが、ピンチアウトで拡大表示するなどして登録してください。</p>
<p class="small gap">webcal のリンクは、対応アプリで開くとそのまま購読登録に進みます。iPhone のカレンダーは「照会カレンダーを追加」、Mac のカレンダーは「新規照会カレンダー」から、URL を入力して登録することもできます。</p>
</section>
<h2 class="sec">RSS リーダーへの登録</h2>
<section class="card">
<ul>
<li>1地点だけなら、その地点の「RSS」リンクをリーダーに登録します</li>
<li>まとめて登録するなら、<a href="feeds.opml">OPML ファイル</a>をリーダーに読み込ませます（予報区ごとのフォルダで入ります）</li>
</ul>
</section>
<h2 class="sec">出典と免責</h2>
<section class="card">
<p>気象庁の天気予報データを加工して作成しています。データは<a href="https://www.jma.go.jp/">気象庁ホームページ</a>で発表されたものです。</p>
<p>このサイトは、個人が運営する非公式なものです。予報の正確性は保証しません。防災など重要な判断には、必ず気象庁の公式情報を確認してください。</p>
</section>
<h2 class="sec" id="areas">地点一覧</h2>
<section class="card">
{region_index}
</section>
{''.join(region_sections)}
<h2 class="sec">お問い合わせ</h2>
<section class="card">
<ul>
<li>フォーム：<a href="{FORM_URL}">Google フォーム</a></li>
<li>メール：<span id="contact-mail"><noscript>aegithaloscaudatusjaponicus<svg class="at" viewBox="0 0 60 60" aria-hidden="true"><text x="2" y="46" font-size="50" fill="currentColor">@</text></svg>duck.com</noscript></span></li>
<li><a href="{ISSUES_URL}">GitHub Issues</a></li>
</ul>
<p class="small">個人で運営しているため、返信にお時間をいただく場合があります。</p>
</section>
<h2 class="sec">用語解説</h2>
<section class="card">
<p><strong>iCal</strong>：カレンダーアプリで予定をやり取りするための形式。このサイトでは天気予報を予定の形で配布していて、URL を登録するとカレンダーに予報が表示される。</p>
<p><strong>webcal</strong>：iCal の URL をカレンダーアプリで直接開くための、リンクの形式。押すと対応アプリが起動して、購読登録に進む。中身は iCal と同じ。</p>
<p><strong>RSS</strong>：Really Simple Syndication の略。サイトの更新内容を配信するための形式で、このサイトでは予報を記事の形で配信している。RSS リーダーに登録すると更新分が届く。</p>
<p><strong>OPML</strong>：Outline Processor Markup Language の略。RSS フィードの一覧をまとめて受け渡すための形式で、このサイトでは全地点の RSS を予報区ごとのフォルダに分けて1ファイルにまとめている。RSS リーダーに読み込ませると、全地点を一度に登録できる。</p>
<p><strong>JSON</strong>：JavaScript Object Notation の略。プログラムがデータを読み取るための形式で、このサイトでは天気・気温・降水確率などをそのまま配布している。アプリや AI などに取り込める。</p>
</section>
<h2 class="sec">参考リンク（気象庁）</h2>
<section class="card">
<ul>
<li><a href="https://www.jma.go.jp/bosai/forecast/">週間天気予報</a>：全国の予報の一覧表</li>
<li><a href="https://www.jma.go.jp/jma/kishou/know/kurashi/twoweek.html">2週間気温予報と早期天候情報</a>：2週間先までの気温の見通しと、著しい高温・低温の見通しを解説</li>
<li><a href="https://www.jma.go.jp/jma/kishou/know/yougo_hp/mokuji.html">予報用語</a>：「時々」「一時」など、天気予報で使う言葉の意味を解説</li>
<li><a href="https://www.data.jma.go.jp/developer/index.html">気象庁情報カタログ</a>：気象庁が配信しているデータの一覧</li>
<li><a href="https://xml.kishou.go.jp/tec_material.html">防災情報XMLの技術資料</a>：気象庁が配信するXML電文の仕様資料</li>
</ul>
</section>
<script>(function(){{var u=['aegithalos','caudatus','japonicus'].join('');var a=u+String.fromCharCode(64)+['duck','com'].join('.');var s=document.getElementById('contact-mail');var l=document.createElement('a');l.href='mailto:'+a;l.textContent=a;s.textContent='';s.appendChild(l);}})();</script>
</main>
</body>
</html>
"""


def render_opml(area, grouped):
    offices = []
    for oc, areas in grouped:
        rows = "".join(
            f'<outline text="{xml_escape(name)}" type="rss" '
            f'xmlUrl="{BASE}/rss/{code}.xml" htmlUrl="{BASE}/#{code}"/>'
            for code, name in areas
        )
        offices.append(f'<outline text="{xml_escape(area["offices"][oc]["name"])}">{rows}</outline>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<opml version="2.0"><head><title>天気予報フィード</title></head>'
        f'<body><outline text="天気予報フィード">{"".join(offices)}</outline></body></opml>\n'
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = Path(args.out)
    for sub in ("ical", "rss", "json"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    gen_dt = datetime.now(JST)
    generated_str = f"{gen_dt.year}年{gen_dt.month}月{gen_dt.day}日 {gen_dt.hour}時{gen_dt.minute:02d}分"
    stamp = gen_dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    data = Data()
    area = fetch_json(AREA_JSON)
    short, weekly, temps_all, wtemps = parse_forecasts(area)

    grouped = []
    for oc in sorted(area["offices"]):
        areas = sorted(
            (code, info["name"]) for code, info in area["class10s"].items() if info["parent"] == oc
        )
        if areas:
            grouped.append((oc, areas))

    sample = SAMPLE
    count = 0
    for oc, areas in grouped:
        office_name = area["offices"][oc]["name"]
        for code, name in areas:
            days = build_days(data, code, short, weekly, temps_all, wtemps)
            if not days:
                print(f"WARN no days for {code} {name}", file=sys.stderr)
                continue
            (out / "ical" / f"{code}.ics").write_text(
                render_ical(code, name, days, generated_str, stamp), encoding="utf-8", newline="")
            (out / "rss" / f"{code}.xml").write_text(
                render_rss(code, name, days, generated_str, gen_dt), encoding="utf-8")
            (out / "json" / f"{code}.json").write_text(
                render_json(code, name, office_name, days, generated_str), encoding="utf-8")
            count += 1
    (out / "index.html").write_text(render_index(area, grouped, sample, generated_str), encoding="utf-8")
    (out / "feeds.opml").write_text(render_opml(area, grouped), encoding="utf-8")
    for asset in ("favicon.png", "gcal-short-route.png"):
        f = ROOT / asset
        if f.exists():
            shutil.copy(f, out / asset)
    print(f"generated {count} areas")
    if data.unknown_codes:
        print(f"WARN unknown weather codes: {sorted(data.unknown_codes)}", file=sys.stderr)


if __name__ == "__main__":
    main()
