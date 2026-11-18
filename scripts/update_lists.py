#!/usr/bin/env python3
"""제주 도서 큐레이션 도우미 - 추천목록 자동 수집

1) 국립중앙도서관 사서추천도서 Open API에서 최근 목록을 받는다 (NL_API_KEY).
2) 국립어린이청소년도서관 홈페이지 사서추천도서 목록(격월, 연령별)을 읽는다 (인증키 불필요).
2-1) manual/ 폴더의 CSV(북스타트, 어도연 등 API가 없는 목록)를 함께 읽는다.
3) 서명만 있는 책은 정보나루에서 ISBN을 찾는다 (DATA4LIB_KEY).
4) 책마다 제주 도내 소장 도서관을 정보나루에서 미리 확인해 둔다.
   - 하루 한도(500건)를 넘지 않게 1회 실행당 MAX_CALLS 건까지만 쓰고, 나머지는 다음 실행에 이어서 한다.
4-1) 정보나루 이달의 키워드, 대출 급상승 도서(제주 소장 포함)를 함께 받는다.
5) 결과를 data/lists.json 하나로 저장한다. 도우미는 이 파일만 받아 쓰므로 사서는 인증키가 필요 없다.

외부 라이브러리 없이 파이썬 표준 라이브러리만 쓴다.
"""
import csv, datetime as dt, json, os, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "lists.json"
MANUAL = ROOT / "manual"
NL_KEY = os.environ.get("NL_API_KEY", "").strip()
D4L_KEY = os.environ.get("DATA4LIB_KEY", "").strip()
MAX_CALLS = int(os.environ.get("MAX_CALLS", "450"))
D4L_PAUSE = float(os.environ.get("D4L_PAUSE", "0.7"))
REFRESH_DAYS = int(os.environ.get("REFRESH_DAYS", "30"))
FROM_YEAR = int(os.environ.get("FROM_YEAR", "2023"))       # 이 해 1월부터 추천목록을 모은다
REGION = "39"                                                 # 제주
NL_URL = os.environ.get("NL_API_URL", "https://nl.go.kr/NL/search/openApi/saseoApi.do")
D4L = os.environ.get("D4L_API_URL", "https://data4library.kr/api/")
TODAY = dt.date.today()
calls = 0

# manual/ 파일 이름 앞머리 → 출처 코드
SRC_PREFIX = {"bookstart": "bookstart", "북스타트": "bookstart",
              "childbook": "childbook", "어도연": "childbook", "어린이도서연구회": "childbook",
              "nlcy": "nlcy", "국립어린이": "nlcy", "nl": "nl", "국중": "nl"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get(url, params, as_json=True, tries=3, timeout=30):
    global calls
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(url + "?" + q, headers={"User-Agent": "jeju-curation-lists/1.0"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
            break
        except Exception as e:  # 일시 오류는 두 번 더 시도
            if attempt == tries - 1:
                raise
            log("retry", url, e)
            time.sleep(3)
    return json.loads(body) if as_json else body


def d4l(endpoint, params):
    global calls
    if not D4L_KEY:
        raise RuntimeError("DATA4LIB_KEY 없음")
    calls += 1
    time.sleep(D4L_PAUSE)  # 정보나루에 호출이 몰리지 않게 간격을 둔다
    data = get(D4L + endpoint, {**params, "authKey": D4L_KEY, "format": "json"}, tries=2, timeout=15)
    resp = data.get("response", {})
    if resp.get("error"):
        raise RuntimeError("정보나루: " + str(resp["error"]))
    return resp


def norm(s):
    return re.sub(r"[\s()\[\]\-:·,.'\"!?~]", "", str(s or "")).lower()


def clean_isbn(s):
    s = re.sub(r"[^0-9Xx]", "", str(s or ""))
    return s if len(s) in (10, 13) else ""


# ---------------- 국립중앙도서관 사서추천도서 ----------------
FIELDS = {  # 국립중앙도서관 '사서추천도서' 활용방법 페이지 기준 항목명
    "t": ["recom_title", "recomtitle", "title"],
    "a": ["recom_author", "recomauthor", "author"],
    "p": ["recom_publisher", "recompublisher", "publisher"],
    "isbn": ["recome_isbn", "recom_isbn", "recomisbn", "isbn"],
    "g": ["decode", "drCodeName"],
    "y": ["recom_year", "recomYear"],
    "m": ["recom_month", "recomMonth"],
    "c": ["recom_contents", "recomcontens"],
}


def pick(rec, keys):
    low = {k.lower(): v for k, v in rec.items()}
    for k in keys:
        v = low.get(k.lower())
        if v not in (None, ""):
            return str(v).strip()
    return ""


def fetch_nl():
    if not NL_KEY:
        log("NL_API_KEY 없음 - 사서추천도서 자동 수집 건너뜀")
        return []
    recs, body = [], ""
    for year in range(FROM_YEAR, TODAY.year + 1):   # 한 해씩 나눠 받는다 (1회 최대 500권)
        params = {"key": NL_KEY, "startRowNumApi": 1, "endRowNumApi": 500,
                  "start_date": f"{year}0101", "end_date": min(dt.date(year, 12, 31), TODAY).strftime("%Y%m%d")}
        body = get(NL_URL, params, as_json=False)
        try:  # 응답이 XML인 경우
            root = ET.fromstring(body)
            for node in root.iter():
                kids = list(node)
                if kids and all(len(list(k)) == 0 for k in kids) and any(k.tag.lower() in ("recom_title", "recomtitle", "title") for k in kids):
                    recs.append({k.tag: (k.text or "") for k in kids})
        except ET.ParseError:  # JSON인 경우
            data = json.loads(body)
            stack = [data]
            while stack:
                x = stack.pop()
                if isinstance(x, dict):
                    if any(k.lower() in ("recom_title", "recomtitle", "title") for k in x):
                        recs.append(x)
                    else:
                        stack.extend(x.values())
                elif isinstance(x, list):
                    stack.extend(x)
        time.sleep(1)
    if not recs:
        log("사서추천도서 응답에서 도서를 찾지 못함. 응답 앞부분:", body[:300])
    groups = {}
    for r in recs:
        b = {k: pick(r, v) for k, v in FIELDS.items()}
        b["isbn"] = clean_isbn(b["isbn"])
        if not b["t"]:
            continue
        name = f"{b['y']}년 {int(b['m'])}월" if b["y"] and b["m"].isdigit() else "최근"
        item = {k: b[k] for k in ("t", "a", "p", "isbn", "g")}
        if b["c"]:
            item["c"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", b["c"])).strip()[:600]  # 책 소개(나중에 AI 추천 이유 근거)
        g = groups.setdefault(name, [])
        if not any((x["isbn"] and x["isbn"] == item["isbn"]) or norm(x["t"]) == norm(item["t"]) for x in g):  # 연도 경계 중복 방지
            g.append(item)
    lists = [{"src": "nl", "name": n, "books": bs} for n, bs in groups.items()]
    def ym(n):
        m = re.match(r"(\d{4})년 (\d{1,2})월", n)
        return (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    lists.sort(key=lambda x: ym(x["name"]), reverse=True)
    log(f"사서추천도서 {sum(len(x['books']) for x in lists)}권, 목록 {len(lists)}개")
    return lists


# ---------------- 국립어린이청소년도서관 사서추천도서 (홈페이지 목록) ----------------
NLCY_URL = os.environ.get("NLCY_URL", "https://www.nlcy.go.kr/NLCY/contents/C10600000000.do")
NLCY_GROUPS = [("_nlcy_normal0801", "유아"), ("_nlcy_normal0802", "초등 저학년"),
               ("_nlcy_normal0803", "초등 고학년"), ("_nlcy_normal0804", "청소년")]


def _txt(h):
    import html as _h
    return re.sub(r"\s+", " ", _h.unescape(re.sub(r"<[^>]+>", " ", h or ""))).strip()


def parse_nlcy(page):
    """목록 페이지 HTML에서 책을 뽑는다. 책마다 'cate_head' 블록으로 시작한다."""
    out = []
    for blk in page.split('class="cate_head"')[1:]:
        m = re.search(r"\((\d{4})년\s*(\d{1,2})월\s*추천도서\)", blk)
        t = re.search(r'<p class="tit">(.*?)</p>', blk, re.S)
        if not (m and t):
            continue
        info = {}
        for k, v in re.findall(r"<b>\s*(.*?)\s*</b>\s*<span>(.*?)</span>", blk, re.S):
            info[_txt(k)] = _txt(v)
        parts = [x.strip() for x in info.get("도서정보", "").split("|")]
        a = re.sub(r"\s*(지음|글·그림|글|저자)$", "", re.sub(r"^(글·그림|글|지음|저자)\s*:\s*", "", parts[0])) if parts else ""
        isbn, alt = "", []
        d = re.search(r"fnDetailSearch\(\s*'[^']*'\s*,\s*'[^']*'\s*,\s*'(?:[^'\\]|\\.)*'\s*,\s*'([^']*)'", blk)
        if d:
            cands = [c for c in (clean_isbn(x) for x in d.group(1).split()) if c]
            c13 = [c for c in cands if len(c) == 13]
            isbn = next((c for c in c13 if c.startswith("979")), c13[0] if c13 else (cands[0] if cands else ""))
            alt = [c for c in c13 if c != isbn]
        b = {"t": _txt(t.group(1)), "a": a, "p": parts[1] if len(parts) > 1 else "", "isbn": isbn,
             "g": info.get("주제구분", ""), "ym": (int(m.group(1)), int(m.group(2)))}
        if d and alt:
            b["alt"] = alt
        if info.get("책소개"):
            b["c"] = info["책소개"][:600]
        out.append(b)
    return out


def fetch_nlcy():
    cutoff = FROM_YEAR * 12   # FROM_YEAR 1월부터
    groups = {}
    for code, label in NLCY_GROUPS:
        for year in range(TODAY.year, FROM_YEAR - 1, -1):
            page = get(NLCY_URL, {"schM": "list", "page": 1, "viewCount": 200, "schOpt2": f"{year}-",
                                  "ext2": 1, "schOpt3": "search", "schBdcode": code}, as_json=False)
            for b in parse_nlcy(page):
                y, mth = b.pop("ym")
                if y * 12 + mth <= cutoff:
                    continue
                b["g"] = label + (" · " + b["g"] if b["g"] else "")
                bucket = groups.setdefault((y, mth), [])
                if not any((x["isbn"] and x["isbn"] == b["isbn"]) or norm(x["t"]) == norm(b["t"]) for x in bucket):
                    bucket.append(b)
            time.sleep(1)
    lists = [{"src": "nlcy", "name": f"{y}년 {m}월", "books": bs} for (y, m), bs in sorted(groups.items(), reverse=True)]
    log(f"국립어린이청소년도서관 {sum(len(x['books']) for x in lists)}권, 목록 {len(lists)}개")
    if not lists:
        raise RuntimeError("국립어린이청소년도서관 목록에서 책을 찾지 못함(홈페이지 구조 변경 또는 접속 차단)")
    return lists


# ---------------- 이달의 키워드 · 대출 급상승 (정보나루) ----------------
def _items(resp, key, inner):
    out = []
    for x in resp.get(key, []) or []:
        x = x.get(inner, x) if isinstance(x, dict) else x
        if isinstance(x, dict):
            out.append(x)
    return out


def fetch_trends():
    tr = {}
    first = TODAY.replace(day=1)
    for mon in (first, (first - dt.timedelta(days=1)).replace(day=1)):   # 이번 달이 아직 비었으면 지난달
        kws = _items(d4l("monthlyKeywords", {"month": mon.strftime("%Y-%m")}), "keywords", "keyword")
        kws = [{"w": k.get("word", "").strip(), "n": float(k.get("weight") or 0)} for k in kws if k.get("word")]
        if kws:
            kws.sort(key=lambda k: -k["n"])
            tr["month"], tr["keywords"] = mon.strftime("%Y-%m"), kws[:30]
            break
    for back in (1, 2, 3):
        day = TODAY - dt.timedelta(days=back)
        res = _items(d4l("hotTrend", {"searchDt": day.isoformat()}), "results", "result")
        res = [r for r in res if r.get("docs")]
        if not res:
            continue
        res.sort(key=lambda r: str(r.get("date", "")), reverse=True)
        books, seen = [], set()
        for d in [x for r in res for x in r["docs"]]:   # 최근 며칠 치를 합치고 같은 책은 한 번만
            d = d.get("doc", d)
            k = clean_isbn(d.get("isbn13", "")) or d.get("bookname", "")
            if k in seen:
                continue
            seen.add(k)
            books.append({"t": d.get("bookname", ""), "a": d.get("authors", ""), "p": d.get("publisher", ""),
                          "y": str(d.get("publication_year", "")), "isbn": clean_isbn(d.get("isbn13", "")),
                          "kdc": str(d.get("class_no", "")), "kn": d.get("class_nm", ""), "add": str(d.get("addition_symbol", "")),
                          "rank": str(len(books) + 1), "diff": d.get("difference", "")})
        tr["hot"] = {"date": str(res[0].get("date") or day.isoformat()), "from": str(res[-1].get("date", "")), "books": [b for b in books if b["t"]]}
        break
    log(f"이달의 키워드 {len(tr.get('keywords', []))}개, 대출 급상승 {len(tr.get('hot', {}).get('books', []))}권")
    return tr


# ---------------- manual CSV ----------------
def read_manual():
    lists = []
    for f in sorted(MANUAL.glob("*.csv")):
        raw = f.read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp949")
        rows = list(csv.reader(text.splitlines()))
        if not rows:
            continue
        head = [h.strip().lower() for h in rows[0]]
        def col(*names):
            for i, h in enumerate(head):
                if any(n in h for n in names):
                    return i
            return -1
        iT, iA, iP, iI, iG = col("서명", "도서명", "title"), col("저자", "author"), col("출판", "publisher"), col("isbn"), col("대상", "학년", "연령", "분야")
        iL = col("목록")
        stem = f.stem
        src = next((v for k, v in SRC_PREFIX.items() if stem.lower().startswith(k.lower())), "etc")
        name = re.sub(r"^(bookstart|childbook|nlcy|nl|북스타트|어도연|어린이도서연구회|국립어린이|국중)[_\s-]*", "", stem, flags=re.I).replace("_", " ").strip() or stem
        groups = {}
        for r in rows[1:]:
            g = lambda i: (r[i].strip() if 0 <= i < len(r) else "")
            if not g(iT):
                continue
            key = (g(iL) if iL >= 0 else "") or name
            groups.setdefault(key, []).append({"t": g(iT), "a": g(iA), "p": g(iP), "isbn": clean_isbn(g(iI)), "g": g(iG)})
        for key, books in groups.items():
            lists.append({"src": src, "name": key, "books": books, "manual": True})
        if groups:
            log(f"수동 목록 {f.name}: {len(groups)}개 목록 {sum(map(len, groups.values()))}권 ({src})")
    return lists


# ---------------- 정보나루: ISBN 찾기 · 소장 확인 ----------------
def fill_isbn(lists, cache):
    for L in lists:
        for b in L["books"]:
            if b["isbn"] or not b["t"]:
                continue
            ck = norm(b["t"] + "|" + b["a"])
            hit = cache.get(ck)
            if hit and hit != "?2":
                b["isbn"], b["auto"] = hit, True
                continue
            if hit == "?2":  # 저자까지 넣어 찾아도 없던 책
                continue
            if calls >= MAX_CALLS - 1:
                return
            a_raw = re.split(r"[ ,;(|/]", (b["a"] or "").strip() or " ")[0]
            a0 = norm(a_raw)
            pick = lambda docs: next((d for d in docs if norm(d.get("bookname")).startswith(norm(b["t"])) and (not a0 or a0 in norm(d.get("authors")))), None) \
                or next((d for d in docs if norm(d.get("bookname")) == norm(b["t"])), None)
            best = None
            if a_raw:  # 짧은 그림책 제목은 제목만으로는 못 찾아서 저자를 함께 넣는다
                best = pick([d.get("doc", d) for d in d4l("srchBooks", {"title": b["t"], "author": a_raw, "pageSize": 10}).get("docs", [])])
            if not best:
                best = pick([d.get("doc", d) for d in d4l("srchBooks", {"title": b["t"], "pageSize": 10}).get("docs", [])])
            cache[ck] = clean_isbn(best.get("isbn13")) if best else ""
            b["isbn"], b["auto"] = cache[ck], bool(cache[ck])
            if not cache[ck]:
                cache[ck] = "?2"
            if best and not b["p"]:
                b["p"] = best.get("publisher", "")


def fill_holdings(lists, holdings):
    isbns = sorted({b["isbn"] for L in lists for b in L["books"] if b["isbn"]})
    alts = {b["isbn"]: b["alt"] for L in lists for b in L["books"] if b.get("isbn") and b.get("alt")}
    def stale(i):
        h = holdings.get(i)
        return not h or (TODAY - dt.date.fromisoformat(h["d"])).days >= REFRESH_DAYS
    todo = [i for i in isbns if stale(i)]
    todo.sort(key=lambda i: (i in holdings, holdings.get(i, {}).get("d", "")))  # 처음 보는 책 먼저
    for i in todo:
        if calls >= MAX_CALLS:
            break
        libs = []
        for q in [i] + alts.get(i, []):
            resp = d4l("libSrchByBook", {"isbn": q, "region": REGION, "pageSize": 50})
            for x in resp.get("libs", []):
                l = [str(x.get("lib", x).get("libCode")), x.get("lib", x).get("libName")]
                if l not in libs:
                    libs.append(l)
        holdings[i] = {"d": TODAY.isoformat(), "libs": libs}
    left = sum(1 for i in isbns if stale(i))
    return left


def main():
    old = json.loads(OUT.read_text("utf-8")) if OUT.exists() else {}
    holdings = old.get("holdings", {})
    isbn_cache = old.get("isbnCache", {})
    lists = []
    try:
        lists += fetch_nl()
    except Exception as e:
        log("사서추천도서 수집 실패:", e)
        lists += [L for L in old.get("lists", []) if L["src"] == "nl"]  # 실패하면 지난번 것 유지
    try:
        lists += fetch_nlcy()
    except Exception as e:
        log("국립어린이청소년도서관 수집 실패:", e)
        lists += [L for L in old.get("lists", []) if L["src"] == "nlcy" and not L.get("manual")]
    lists += read_manual()
    left = None
    trends = old.get("trends", {})
    if D4L_KEY:
        try:
            trends = fetch_trends() or trends
        except Exception as e:
            log("키워드·급상승 수집 실패(지난번 것 유지):", e)
        try:
            fill_isbn(lists, isbn_cache)
            hot = [{"books": [b for b in trends.get("hot", {}).get("books", []) if b["isbn"]]}]
            left = fill_holdings(lists + hot, holdings)
        except Exception as e:
            log("정보나루 처리 중 중단:", e)
    keep = {b["isbn"] for L in lists for b in L["books"] if b["isbn"]} | {b["isbn"] for b in trends.get("hot", {}).get("books", []) if b["isbn"]}
    holdings = {k: v for k, v in holdings.items() if k in keep}
    out = {"type": "jeju-curation-lists", "v": 1, "updated": TODAY.isoformat(),
           "lists": lists, "trends": trends, "holdings": holdings, "isbnCache": isbn_cache,
           "pending": left}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), "utf-8")
    log(f"완료: 목록 {len(lists)}개, 소장 확인 {len(holdings)}권, 남은 확인 {left}, 정보나루 호출 {calls}건")


if __name__ == "__main__":
    main()
