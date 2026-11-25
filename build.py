#!/usr/bin/env python3
"""
Збірка автооновлюваного IPTV-плейлиста:
  джерела (iptv-org API + m3u) -> фільтри (без RU) -> дедуп -> health-check -> групи -> m3u

Запуск:
  python build.py                 # повна збірка
  python build.py --no-check      # без перевірки живості (швидкий тест фільтрів/груп)
  python build.py --limit 300     # перевірити лише N каналів (тест)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

import aiohttp
import yaml

ROOT = Path(__file__).resolve().parent
QUALITY_RANK = {"2160p": 6, "1440p": 5, "1080p": 4, "1080i": 4, "720p": 3, "576p": 2, "576i": 2, "480p": 1, "480i": 1}
FREETV_MARKS = re.compile(r"\s*[ⒼⓈⓎⓉⓋ]\s*")   # маркери Free-TV у назвах
ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')
COUNTRY_NAMES = {
    "united states": "US", "usa": "US", "canada": "CA", "united kingdom": "GB", "uk": "GB", "germany": "DE",
    "france": "FR", "italy": "IT", "spain": "ES", "austria": "AT", "switzerland": "CH", "india": "IN",
    "australia": "AU", "nz": "NZ", "new zealand": "NZ", "mexico": "MX", "brazil": "BR", "argentina": "AR",
    "chile": "CL", "denmark": "DK", "sweden": "SE", "norway": "NO", "finland": "FI", "netherlands": "NL",
    "belgium": "BE", "ireland": "IE", "portugal": "PT", "south korea": "KR", "korea": "KR", "japan": "JP",
    "poland": "PL", "ukraine": "UA", "russia": "RU", "belarus": "BY", "luxembourg": "LU",
}


@dataclass
class Entry:
    url: str
    name: str
    source: str
    priority: int
    tvg_id: str = ""
    logo: str = ""
    country: str = ""
    categories: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    quality: str = ""
    labels: list[str] = field(default_factory=list)
    referrer: str = ""
    user_agent: str = ""
    chno: int = 9999
    src_group: str = ""
    nsfw: bool = False
    known: bool = False          # tvg-id знайдено в базі iptv-org
    isolated: bool = False       # FAST-джерело: лише у групах з sources: [...]

    @property
    def key(self) -> str:
        """Ключ дедупу: iptv-org id без @feed; інакше назва (+країна для не-FAST)."""
        if self.tvg_id and self.known:
            return self.tvg_id.split("@")[0].lower()
        n = "name:" + re.sub(r"[^\w]+", "", self.name.lower())
        return n if self.isolated else f"{n}|{self.country}"


# ------------------------------------------------------------------ utils
def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


async def fetch_json(session, url):
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=180)) as r:
        r.raise_for_status()
        return json.loads(await r.text())


async def fetch_text(session, url):
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as r:
        r.raise_for_status()
        return await r.text(errors="replace")


# ------------------------------------------------------------------ sources
class Catalog:
    """Метадані iptv-org: країна, категорії, мови, лого — для матчингу будь-яких tvg-id."""

    def __init__(self, channels, feeds, logos, blocklist):
        self.ch = {c["id"]: c for c in channels}
        self.feed = {(f["channel"], f["id"]): f for f in feeds}
        self.main_feed = {f["channel"]: f for f in feeds if f.get("is_main")}
        self.logo = {}
        for lg in logos:
            if lg.get("feed") is None and lg.get("in_use", True):
                self.logo.setdefault(lg["channel"], lg["url"])
        self.blocked = {b["channel"] for b in blocklist}

    def enrich(self, e: Entry, feed_id: str | None = None):
        cid = e.tvg_id.split("@")[0] if e.tvg_id else ""
        if not feed_id and "@" in e.tvg_id:
            feed_id = e.tvg_id.split("@", 1)[1]
        c = self.ch.get(cid)
        if not c:
            return
        e.known = True
        e.country = e.country or c.get("country") or ""
        e.categories = e.categories or list(c.get("categories") or [])
        e.nsfw = e.nsfw or bool(c.get("is_nsfw")) or cid in self.blocked
        if c.get("closed"):
            e.labels.append("closed")
        f = self.feed.get((cid, feed_id)) if feed_id else None
        f = f or self.main_feed.get(cid)
        if f and not e.languages:
            e.languages = list(f.get("languages") or [])
        e.logo = e.logo or self.logo.get(cid, "")


def entries_from_iptv_org(streams, cat: Catalog, cfg) -> list[Entry]:
    out = []
    for s in streams:
        cid = s.get("channel")
        if not cid and not cfg["iptv_org"].get("include_unlinked_streams"):
            continue
        feed = s.get("feed")
        c = cat.ch.get(cid, {})
        e = Entry(
            url=s["url"],
            name=s.get("title") or c.get("name") or cid or "",
            source="iptv-org",
            priority=2,
            tvg_id=f"{cid}@{feed}" if cid and feed else (cid or ""),
            quality=s.get("quality") or "",
            labels=list(s.get("labels") or []),
            referrer=s.get("referrer") or "",
            user_agent=s.get("user_agent") or "",
        )
        cat.enrich(e, feed)
        e.categories = e.categories or ["general"]
        out.append(e)
    return out


def parse_m3u(text: str, src: dict, cat: Catalog) -> list[Entry]:
    out, cur, opts = [], None, {}
    gmap = [(re.compile(k, re.I), v) for k, v in (src.get("group_categories") or {}).items()]
    nmap = [(re.compile(k, re.I), v) for k, v in (src.get("name_categories") or {}).items()]
    keep_only = src.get("keep_only_matched", False)
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF"):
            attrs = dict(ATTR_RE.findall(line))
            name = line.rsplit(",", 1)[-1].strip() if "," in line else attrs.get("tvg-name", "")
            cur = (attrs, name)
            opts = {}
        elif line.startswith("#EXTVLCOPT:"):
            k, _, v = line[11:].partition("=")
            opts[k.strip().lower()] = v.strip()
        elif line and not line.startswith("#") and cur:
            attrs, name = cur
            group = attrs.get("group-title", "")
            labels = []
            if "Ⓖ" in name:
                labels.append("Geo-blocked")
            if "Ⓨ" in name:
                labels.append("YouTube")
            chno = attrs.get("tvg-chno", "")
            e = Entry(
                url=line,
                name=FREETV_MARKS.sub(" ", name).strip(),
                source=src["name"],
                priority=int(src.get("priority", 1)),
                tvg_id=attrs.get("tvg-id", ""),
                logo=attrs.get("tvg-logo", ""),
                country=(attrs.get("tvg-country", "") or src.get("default_country", "")).split(";")[0].upper(),
                labels=labels,
                referrer=opts.get("http-referrer", "") or attrs.get("http-referrer", ""),
                user_agent=opts.get("http-user-agent", "") or attrs.get("http-user-agent", ""),
                isolated=bool(src.get("isolated")),
                chno=int(chno) if chno.isdigit() else 9999,
                src_group=group,
            )
            if not e.country and src.get("country_from_group"):
                e.country = COUNTRY_NAMES.get(group.strip().lower(), "")
            for rx, catg in gmap:
                if rx.search(group):
                    e.categories = [catg]
                    break
            if not e.categories and nmap:
                e.categories = [c for rx, c in nmap if rx.search(e.name)][:2]
            if keep_only and not e.categories:
                cur = None
                continue
            cat.enrich(e)
            if not e.categories:
                e.categories = list(src.get("default_categories") or [])
            if not e.categories:
                e.categories = ["general"]          # без метаданих -> "Загальні", а не "Інше"
            out.append(e)
            cur = None
    return out


# ------------------------------------------------------------------ filters
class Filter:
    def __init__(self, cfg):
        f, s = cfg["filters"], cfg["selection"]
        self.ex_countries = set(f["exclude_countries"])
        self.ex_cats = set(f["exclude_categories"])
        self.drop_labels = set(f["drop_labels"])
        self.rus_ok = set(f["rus_allowed_categories"])
        self.url_rx = [re.compile(p, re.I) for p in f["url_block_patterns"]]
        self.name_rx = [re.compile(p, re.I) for p in f["name_block_patterns"]]
        self.id_block = {i.lower() for i in f.get("id_blocklist") or []}
        self.skip_dash = f.get("skip_dash", True)
        self.skip_yt = f.get("skip_youtube", True)
        self.countries = set(s["countries"])
        self.topic_any = set(s["topic_any_country"])
        self.reasons = Counter()

    def ok(self, e: Entry) -> bool:
        r = self._reason(e)
        if r:
            self.reasons[r] += 1
            return False
        return True

    def _reason(self, e: Entry) -> str | None:
        u = e.url.lower()
        if not u.startswith(("http://", "https://")):
            return "bad_url"
        if e.nsfw or "xxx" in e.categories:
            return "nsfw"
        if "closed" in e.labels:
            return "closed"
        if e.country in self.ex_countries:
            return "country_excluded"
        if any(rx.search(e.url) for rx in self.url_rx):
            return "url_blocked"
        if any(rx.search(e.name) for rx in self.name_rx):
            return "name_blocked"
        if e.key in self.id_block or e.tvg_id.lower() in self.id_block:
            return "id_blocked"
        if self.drop_labels & set(e.labels):
            return "label_dropped"
        if self.skip_yt and ("YouTube" in e.labels or "youtube.com" in u or "youtu.be" in u):
            return "youtube"
        if self.skip_dash and ".mpd" in u:
            return "dash"
        cats = set(e.categories)
        if cats and cats <= self.ex_cats:
            return "category_excluded"
        if "rus" in e.languages and e.country != "UA" and not (cats & self.rus_ok):
            return "rus_lang_not_allowed"
        if e.country == "UA" or e.country in self.countries or (cats & self.topic_any):
            return None
        return "country_not_selected"


# ------------------------------------------------------------------ health check
class Checker:
    def __init__(self, cfg):
        h = cfg["health"]
        self.sem = asyncio.Semaphore(h["concurrency"])
        self.timeout = aiohttp.ClientTimeout(total=h["timeout_sec"], sock_connect=min(6, h["timeout_sec"]))
        self.seg_bytes = h["segment_bytes"]
        self.ua = h["user_agent"]

    def _headers(self, e: Entry):
        hd = {"User-Agent": e.user_agent or self.ua}
        if e.referrer:
            hd["Referer"] = e.referrer
        return hd

    async def _get(self, session, url, headers, limit):
        async with session.get(url, headers=headers, timeout=self.timeout, allow_redirects=True, ssl=False) as r:
            if r.status >= 400:
                return None, None
            body = await r.content.read(limit)
            return str(r.url), body

    async def _hls(self, session, base, text, headers, depth=0) -> bool:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        if depth > 3:
            return False
        if any(l.startswith("#EXT-X-STREAM-INF") for l in lines):
            # master: беремо варіант з найменшим bandwidth (швидше), але будь-який живий = ок
            variants = []
            for i, l in enumerate(lines):
                if l.startswith("#EXT-X-STREAM-INF") and i + 1 < len(lines) and not lines[i + 1].startswith("#"):
                    bw = re.search(r"BANDWIDTH=(\d+)", l)
                    variants.append((int(bw.group(1)) if bw else 0, lines[i + 1]))
            for _, v in sorted(variants)[:2]:
                url, body = await self._get(session, urljoin(base, v), headers, 512 * 1024)
                if body and await self._hls(session, url, body.decode("utf-8", "replace"), headers, depth + 1):
                    return True
            return False
        # media playlist
        if any("#EXT-X-KEY" in l and ("SAMPLE-AES" in l or "KEYFORMAT" in l) for l in lines):
            return False  # DRM
        segs = [l for l in lines if not l.startswith("#")]
        if not segs:
            return False
        seg = segs[-2] if len(segs) > 2 else segs[-1]   # ближче до live edge
        _, body = await self._get(session, urljoin(base, seg), headers, self.seg_bytes)
        return bool(body)

    async def alive(self, session, e: Entry) -> bool:
        async with self.sem:
            try:
                hd = self._headers(e)
                url, body = await self._get(session, e.url, hd, 512 * 1024)
                if not body:
                    return False
                head = body[:2048].decode("utf-8", "replace")
                if "#EXTM3U" in head:
                    return await self._hls(session, url, body.decode("utf-8", "replace"), hd)
                return len(body) >= 1024     # прямий ts/mp4 потік
            except Exception:
                return False


async def select_alive(groups_by_key: dict[str, list[Entry]], cfg, state, check: bool, limit: int | None):
    """Для кожного каналу: перший живий кандидат; інакше — кандидат у межах grace."""
    h = cfg["health"]
    now = int(time.time())
    chosen: dict[str, Entry] = {}
    pending = {k: v[: h["max_candidates"]] for k, v in groups_by_key.items()}
    if limit:
        pending = dict(list(pending.items())[:limit])
    if not check:
        return {k: v[0] for k, v in pending.items()}, Counter(alive_now=len(pending))

    stats = Counter()
    checker = Checker(cfg)
    conn = aiohttp.TCPConnector(limit=h["concurrency"], limit_per_host=h["per_host"], ttl_dns_cache=600)
    async with aiohttp.ClientSession(connector=conn, trust_env=True) as session:
        rnd = 0
        todo = {k: 0 for k in pending}             # key -> індекс поточного кандидата
        results: dict[str, list[bool]] = defaultdict(list)
        while todo:
            rnd += 1
            keys = list(todo)
            log(f"health round {rnd}: {len(keys)} streams")
            res = await asyncio.gather(*(checker.alive(session, pending[k][todo[k]]) for k in keys))
            nxt = {}
            for k, ok in zip(keys, res):
                e = pending[k][todo[k]]
                st = state.setdefault(e.url, {"fail": 0, "ok_ts": 0})
                if ok:
                    st.update(fail=0, ok_ts=now)
                    chosen[k] = e
                    stats["alive_now"] += 1
                else:
                    st["fail"] += 1
                    results[k].append(False)
                    if todo[k] + 1 < len(pending[k]):
                        nxt[k] = todo[k] + 1
            todo = nxt
        # grace: нічого живого зараз, але раніше працювало і падінь < N
        for k, cands in pending.items():
            if k in chosen:
                continue
            for e in cands:
                st = state.get(e.url, {})
                if st.get("ok_ts") and st.get("fail", 99) < h["grace_fails"]:
                    chosen[k] = e
                    stats["kept_grace"] += 1
                    break
            else:
                stats["dead"] += 1
    return chosen, stats


# ------------------------------------------------------------------ grouping & output
def assign_groups(e: Entry, cfg) -> list[tuple[str, str, tuple]]:
    """-> [(group_title, file_slug, rank)]; rank = (tier, group_idx, sub) — tier 0 = UA/озвучка, 1 = зарубіжне."""
    out = []
    labels = cfg["language_labels"]
    for idx, g in enumerate(cfg["groups"]):
        gsrc = g.get("sources")
        if bool(gsrc) != e.isolated or (gsrc and e.source not in gsrc):
            continue
        hit = (e.country in g.get("countries", [])) or bool(set(e.categories) & set(g.get("categories", [])))
        if not hit:
            continue
        if g.get("foreign_only") and e.country == "UA":
            continue                                   # UA-канали вже в "🇺🇦 Україна"
        if g.get("split_by_language"):
            lang = next((l for l in ("ukr", "rus", "eng") if l in e.languages), None)
            if not lang and e.country == "UA":
                lang = "ukr"
            if lang in ("ukr", "rus"):
                title = f"{g['name']} | {labels[lang]}"
                slug = f"{g['file']}_{'ua' if lang == 'ukr' else 'ru'}"
                rank = (0, idx, 0 if lang == "ukr" else 1)
            else:
                lab = labels.get(lang, labels["_other"]) if lang else labels["_other"]
                title = f"{g['foreign_name']} | {lab}"
                slug = f"{g['file']}_foreign"
                rank = (1, idx, 0 if lang == "eng" else 1)
        else:
            title, slug, rank = g["name"], g["file"], (g.get("tier", 1), idx, 0)
        out.append((title, slug, rank))
        if len(out) >= cfg["max_groups_per_channel"]:
            break
    if not out and e.isolated:
        return []                                   # FAST/нестабільні поза цільовими категоріями — не беремо
    fb = cfg["fallback_group"]
    return out or [(fb["name"], fb["file"], (1, 999, 0))]


def esc(s: str) -> str:
    return (s or "").replace('"', "'").replace("\n", " ")


def extinf(e: Entry, group: str) -> str:
    attrs = [
        f'tvg-id="{esc(e.tvg_id)}"',
        f'tvg-name="{esc(e.name)}"',
        f'tvg-logo="{esc(e.logo)}"',
        f'tvg-country="{e.country}"',
        f'tvg-language="{";".join(e.languages)}"',
        f'group-title="{esc(group)}"',
    ]
    if e.chno != 9999:
        attrs.insert(0, f'tvg-chno="{e.chno}"')
    lines = [f"#EXTINF:-1 {' '.join(attrs)},{e.name}"]
    if e.referrer:
        lines.append(f"#EXTVLCOPT:http-referrer={e.referrer}")
    if e.user_agent:
        lines.append(f"#EXTVLCOPT:http-user-agent={e.user_agent}")
    lines.append(e.url)
    return "\n".join(lines)


def write_m3u(path: Path, header: str, rows):
    path.write_text(header + "\n" + "\n".join(extinf(e, t) for t, _, _, e in rows) + "\n", encoding="utf-8")


def write_outputs(chosen: dict[str, Entry], cfg, stats, freasons, total_in):
    o = cfg["output"]
    epg = ",".join(cfg.get("epg_urls") or [])
    header = f'#EXTM3U x-tvg-url="{epg}" url-tvg="{epg}"' if epg else "#EXTM3U"

    rows = []                                     # (title, slug, rank, entry)
    for e in chosen.values():
        for title, slug, rank in assign_groups(e, cfg):
            rows.append((title, slug, rank, e))
    rows.sort(key=lambda r: (r[2], r[3].chno, r[3].name.lower()))

    write_m3u(ROOT / o["main"], header, rows)
    ua_rows = [r for r in rows if r[2][0] == 0]
    if o.get("ua_only"):
        write_m3u(ROOT / o["ua_only"], header, ua_rows)

    per_dir = ROOT / o["per_group_dir"]
    per_dir.mkdir(exist_ok=True)
    by_file = defaultdict(list)
    for r in rows:
        by_file[r[1]].append(r)
    for old in per_dir.glob("*.m3u"):
        if old.stem not in by_file:
            old.unlink()
    for slug, items in by_file.items():
        write_m3u(per_dir / f"{slug}.m3u", header, items)

    gcount, grank = Counter(), {}
    for t, _, rk, _ in rows:
        gcount[t] += 1
        grank[t] = rk
    rep = [
        "# Звіт збірки\n",
        f"Оновлено: {time.strftime('%d.%m.%Y %H:%M UTC', time.gmtime())}\n",
        f"- Стрімів із джерел: {total_in}",
        f"- Унікальних каналів після фільтрів: {stats.get('_candidates', 0)}",
        f"- Живі зараз: {stats.get('alive_now', 0)}",
        f"- Тримаються в grace: {stats.get('kept_grace', 0)}",
        f"- Мертві/видалені: {stats.get('dead', 0)}",
        f"- Рядків у playlist.m3u (з дублями по групах): {len(rows)}",
        f"- Рядків у {o.get('ua_only', '-')}: {len(ua_rows)}\n",
        "## Групи\n", "| Група | Каналів |", "|---|---|",
        *[f"| {g} | {gcount[g]} |" for g in sorted(gcount, key=lambda g: grank[g])],
        "\n## Відфільтровано\n", "| Причина | К-сть |", "|---|---|",
        *[f"| {r} | {n} |" for r, n in freasons.most_common()],
    ]
    (ROOT / o["report"]).write_text("\n".join(rep) + "\n", encoding="utf-8")
    log(f"written {o['main']}: {len(rows)} rows, {len(gcount)} groups; {o.get('ua_only')}: {len(ua_rows)} rows")


# ------------------------------------------------------------------ main
async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-check", action="store_true", help="не перевіряти живість")
    ap.add_argument("--limit", type=int, default=None, help="обмежити к-сть каналів (тест)")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    state_path = ROOT / cfg["output"]["state"]
    state = json.loads(state_path.read_text()) if state_path.exists() else {}

    entries: list[Entry] = []
    async with aiohttp.ClientSession(trust_env=True) as s:
        base = cfg["iptv_org"]["api_base"].rstrip("/")
        names = ["channels", "feeds", "logos", "blocklist", "streams"]
        data = dict(zip(names, await asyncio.gather(*(fetch_json(s, f"{base}/{n}.json") for n in names))))
        cat = Catalog(data["channels"], data["feeds"], data["logos"], data["blocklist"])
        if cfg["iptv_org"]["enabled"]:
            entries += entries_from_iptv_org(data["streams"], cat, cfg)
            log(f"iptv-org: {len(entries)} streams")
        for src in cfg.get("m3u_sources") or []:
            if not src.get("enabled", True):
                continue
            try:
                got = parse_m3u(await fetch_text(s, src["url"]), src, cat)
                entries += got
                log(f"{src['name']}: {len(got)} streams")
            except Exception as ex:                      # одне джерело впало — збірка йде далі
                log(f"WARN source {src['name']} failed: {ex}")

    total_in = len(entries)
    flt = Filter(cfg)
    kept = [e for e in entries if flt.ok(e)]

    # дедуп: однаковий URL -> один; далі групуємо по каналу, сортуємо кандидатів
    seen, uniq = set(), []
    for e in kept:
        if e.url not in seen:
            seen.add(e.url)
            uniq.append(e)
    by_key: dict[str, list[Entry]] = defaultdict(list)
    for e in uniq:
        by_key[e.key].append(e)
    for k in by_key:
        sel = cfg["selection"]["countries"]
        by_key[k].sort(key=lambda e: (e.priority, "Not 24/7" in e.labels,
                                      sel.index(e.country) if e.country in sel else 99,
                                      -QUALITY_RANK.get(e.quality, 0)))
    log(f"after filters: {len(kept)} streams, {len(by_key)} channels; dropped: {dict(flt.reasons)}")

    check = cfg["health"]["enabled"] and not args.no_check
    chosen, stats = await select_alive(by_key, cfg, state, check, args.limit)
    stats["_candidates"] = len(by_key) if not args.limit else min(args.limit, len(by_key))

    min_alive = cfg["health"].get("min_alive", 1)
    if check and len(chosen) < min_alive:
        log(f"ERROR: живих каналів {len(chosen)} < min_alive={min_alive} — не перезаписую плейлист (ймовірно, мережа раннера)")
        sys.exit(1)

    # чистимо state від URL, яких більше нема в джерелах
    live_urls = {e.url for v in by_key.values() for e in v}
    state = {u: v for u, v in state.items() if u in live_urls}
    if check:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    write_outputs(chosen, cfg, stats, flt.reasons, total_in)


if __name__ == "__main__":
    asyncio.run(main())
