"""良級懸賞 (citydao.world) 每日限抽套組自動抽 + 換鑽石 + 開寶箱

直接打網站自己前端用的 API (跟 citydao_ev.py 同一套)，不開瀏覽器。
網頁有 Cloudflare 機器人驗證會擋自動化瀏覽器，API 不受影響。

流程 (有多個帳號就每個帳號各做一遍):
    1. 找所有開放中、有「每人限 N 抽」的套組 (每天中午 12:00 換新)，
       例如老闆發瘋蒐藏系列 (3000/抽 限 1 抽)、特別活動蒐藏系列 (10/抽 限 100 抽)
    2. 登入 (每次執行都重新登入；跑到一半被登出也會自動登回去)
    3. 把每套的額度抽完：限 1 抽的抽 1 抽，多抽的每次 10 抽 (10 抽才有寶箱)
    4. 訂單紀錄裡這些套組「尚未選擇」的獎品全部換成鑽石
    5. 寶箱管理裡的寶箱全部開啟
    6. 有套組沒抽滿 (例如別人正在抽、套組被保護中) 就隔 10 分鐘再試，直到抽到不能再抽；
       鑽石不夠的話換完鑽石、開完寶箱夠了就馬上接著抽，還是不夠才放棄

用法:
    python citydao_auto_draw.py                  # 正式執行 (排程每天 12:30 跑這個)
    python citydao_auto_draw.py --check          # 只列出今天會抽哪些套組，不登入不抽
    python citydao_auto_draw.py --exchange-only  # 只做換鑽石 + 開寶箱

帳密放在同資料夾的 citydao_secret.json，一個帳號:
    {"email": "...", "password": "..."}
多個帳號 (name 可省略，會叫 帳號1、帳號2…，log 和抽獎紀錄用這個名字區分):
    [{"name": "主帳", "email": "...", "password": "..."},
     {"name": "副帳", "email": "...", "password": "..."}]
紀錄寫在 citydao_auto.log。
"""
import argparse
import json
import logging
import random
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from citydao_ev import BASE, HEADERS, get, list_sets

HERE = Path(__file__).resolve().parent
SECRET = HERE / "citydao_secret.json"
STATE = HERE / "citydao_auto_state.json"

BATCH = 10                      # 多抽的套組每次抽 10 抽，才會送寶箱
RETRY_WAIT = 600                # 沒抽滿時隔多久再試 (秒)
BOX_NAMES = ("金寶箱", "銀寶箱", "銅寶箱")
LIMIT_RE = re.compile(r"每人有抽獎數量\s*(\d+)\s*限制[，,]\s*您剩下\s*(\d+)\s*單")

log = logging.getLogger("citydao")
_current = {"account": ""}      # 目前在處理哪個帳號，log 每行前面會加 [帳號名]
_root_state = {}


class AccountTag(logging.Filter):
    def filter(self, record):
        record.account = f"[{_current['account']}] " if _current["account"] else ""
        return True


class ApiError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(account)s%(message)s", "%Y-%m-%d %H:%M:%S")
    log.addFilter(AccountTag())
    fh = logging.FileHandler(HERE / "citydao_auto.log", encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stdout:     # pythonw 沒有 console
        sys.stdout.reconfigure(encoding="utf-8")
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    log.setLevel(logging.INFO)


def load_accounts():
    """citydao_secret.json 可以是單一帳號 {...} 或多帳號 [{...}, {...}]。"""
    data = json.loads(SECRET.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = [data]
    accounts = []
    for i, a in enumerate(data, 1):
        accounts.append({"name": a.get("name") or f"帳號{i}",
                         "email": a.get("email", ""), "password": a.get("password", "")})
    return accounts


def load_state(first_name):
    """每個帳號各自記錄抽過哪些套組。舊版 (單帳號) 的紀錄歸給第一個帳號。"""
    global _root_state
    try:
        root = json.loads(STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        root = {}
    root.setdefault("accounts", {})
    if "drawn_sets" in root:
        old = {"drawn_sets": root.pop("drawn_sets"), "daily_sets": root.pop("daily_sets", [])}
        root["accounts"].setdefault(first_name, old)
    _root_state = root
    return root


def account_state(root, name):
    st = root["accounts"].setdefault(name, {})
    st.setdefault("drawn_sets", [])                     # 額度已抽完的套組
    st.setdefault("daily_sets", list(st["drawn_sets"]))  # 抽過的限抽套組，換鑽石只換這些
    return st


def save_state():
    STATE.write_text(json.dumps(_root_state, ensure_ascii=False, indent=2), encoding="utf-8")


def remember(state, key, set_id):
    if set_id not in state[key]:
        state[key] = (state[key] + [set_id])[-200:]
        save_state()


def api(method, path, body=None, token=None):
    """網站 API。回應可能是 {code:0, data:...}、{error:{...}} 或直接是資料，比照前端處理。"""
    headers = dict(HEADERS)
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["AuthCat"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    try:
        u = json.loads(raw)
    except ValueError:
        raise ApiError(f"HTTP {status} 非 JSON 回應: {raw[:200]!r}", status)
    if isinstance(u, dict):
        if "code" in u:
            if u["code"] != 0:
                raise ApiError(u.get("message") or f"請求失敗 code={u['code']}", status)
            return u.get("data")
        if u.get("error"):
            raise ApiError(u["error"].get("message") or "請求失敗", status)
    if status >= 400:
        raise ApiError(f"HTTP {status}: {str(u)[:200]}", status)
    return u


def login(creds):
    """回傳 (token, 目前鑽石)。"""
    if not creds.get("email") or not creds.get("password"):
        raise ApiError(f"請先在 {SECRET.name} 填入帳號密碼")
    d = api("POST", "/member/login", {"account": creds["email"], "password": creds["password"]})
    points = d.get("gamePoints", 0)
    log.info("登入成功，目前鑽石 %s", f"{points:,}")
    return d["token"], points


class Client:
    """帶登入狀態的 API 呼叫。被登出 (401) 就自動重新登入再送一次；401 代表請求沒被處理，重送不會重複抽。"""

    def __init__(self, creds):
        self.creds = creds
        self.relogin()

    def relogin(self):
        self.token, self.points = login(self.creds)

    def call(self, method, path, body=None):
        try:
            return api(method, path, body, self.token)
        except ApiError as e:
            if e.status != 401:
                raise
            log.info("被登出了 (%s)，重新登入再試", e)
            self.relogin()
            return api(method, path, body, self.token)


def find_daily_sets(tries=6, wait=120):
    """所有開放中、有每人限抽的套組，貴的先抽 (鑽石不夠時優先抽回本的)。剛換套時可能還沒上架，會重試。"""
    for attempt in range(tries):
        now_ms = time.time() * 1000
        try:
            sets = [s for s in list_sets()
                    if (s.get("drawLimit") or 0) > 0 and s["remainingSets"] > 0
                    and s["drawStartTime"] <= now_ms <= s["drawEndTime"]]
        except Exception as e:
            log.warning("查詢套組失敗: %s", e)
            sets = []
        if sets:
            return sorted(sets, key=lambda s: s["clientPrice"], reverse=True)
        if attempt < tries - 1:
            log.info("還沒找到開放中的限抽套組，%d 秒後重試 (%d/%d)", wait, attempt + 1, tries)
            time.sleep(wait)
    return []


def draw_numbers(client, set_id, n):
    """隨機挑 n 個還沒被抽的號碼一次抽 (等同網頁「隨機選號」)。號碼剛好被別人搶走就重挑。"""
    for attempt in range(3):
        detail = get(f"/lottery-draw/detail/{set_id}")
        free = [d["drawNumber"] for d in detail["draws"] if not d["isDrawn"]]
        if len(free) < n:
            raise ApiError(f"剩餘號碼不足 {n} 個 (剩 {len(free)})")
        nums = sorted(random.sample(free, n))
        try:
            return client.call("POST", "/lottery-draw/draw",
                               {"lotterySetId": set_id, "drawNumbers": nums, "drawMethod": 1})
        except ApiError as e:
            if attempt < 2 and ("號碼" in str(e) or "已被" in str(e)):
                log.info("號碼被搶走了 (%s)，重挑", e)
                continue
            raise


def draw_set(client, s, state):
    """抽一輪，把一套的每人額度抽完。限 1 抽的抽 1 抽，其他每次抽 10 抽。
    回傳 "done" 不能再抽了、"retry" 這次被擋要再試、"poor" 鑽石不夠。"""
    limit, price = s["drawLimit"], s["clientPrice"]
    batch = min(BATCH, limit)
    rounds = limit // batch
    log.info("--- %s (id %s)：%s 鑽/抽，每人限 %s 抽，每次 %s 抽", s["name"], s["id"], price, limit, batch)
    if limit % batch:
        log.info("額度 %s 不是 %s 的倍數，零頭 %s 抽不抽 (不滿 10 抽沒有寶箱)", limit, batch, limit % batch)
    done = 0
    for _ in range(rounds):
        cost = batch * price
        if client.points < cost:
            log.warning("鑽石不足 (剩 %s，這次要 %s)，這輪先停在 %s 抽", f"{client.points:,}", f"{cost:,}", done)
            return "poor"
        try:
            r = draw_numbers(client, s["id"], batch)
        except (ApiError, OSError) as e:
            m = LIMIT_RE.search(str(e))
            if m and int(m.group(2)) < batch:
                log.info("這套額度已用完 (限 %s 抽，剩 %s 抽)", m.group(1), m.group(2))
                remember(state, "drawn_sets", s["id"])
                return "done"
            if any(w in str(e) for w in ("上限", "限制", "已達")):
                log.info("這套不能再抽了: %s", e)
                remember(state, "drawn_sets", s["id"])
                return "done"
            if "剩餘號碼不足" in str(e):
                log.info("這套快抽完了，不能再抽: %s", e)
                remember(state, "drawn_sets", s["id"])
                return "done"
            log.warning("這輪抽到 %s 抽被擋: %s", done, e)
            return "retry"
        remember(state, "daily_sets", s["id"])
        done += batch
        client.points = r.get("gamePointsAfter", client.points - cost)
        prizes = r.get("prizes") or []
        got = {}
        for p in prizes:
            key = f"{p.get('prizeRank', '?')}賞 {p.get('prizeName', '')}"
            got[key] = got.get(key, 0) + 1
        log.info("抽了 %s 抽 (這輪累計 %s，每人限 %s)：%s，剩 %s 鑽", batch, done, limit,
                 "、".join(f"{k} x{v}" for k, v in got.items()), f"{client.points:,}")
        extra = {k: v for k, v in r.items() if k not in ("prizes", "drawNumbers")}
        log.info("  其他回應欄位: %s", json.dumps(extra, ensure_ascii=False)[:600])
    remember(state, "drawn_sets", s["id"])
    return "done"


def exchange_items(client, set_ids):
    """訂單紀錄裡「尚未選擇」(status 0)、來自每日限抽套組的獎品全部換鑽石。
    指定 id 換 (同網頁單筆「換鑽石」)，大賞也會換；其他套組抽到的不動。"""
    items, page = [], 1
    while True:
        d = client.call("GET", f"/lottery-order/items?current={page}&pageSize=50&status=0")
        batch = d.get("list") or []
        items += batch
        if not batch or len(items) >= d.get("total", 0):
            break
        page += 1
    ids = set(set_ids)
    todo = [i for i in items if i.get("status") == 0 and not i.get("shippingOnly")
            and (i.get("lotterySetId") in ids or any(n in (i.get("prizeName") or "") for n in BOX_NAMES))]
    if not todo:
        log.info("沒有待換鑽石的獎品")
        return 0
    summary = {}
    for i in todo:
        key = f"{i.get('prizeRank', '?')}賞 {i.get('prizeName')} ({i.get('prizePoints')} 鑽)"
        summary[key] = summary.get(key, 0) + 1
    for k, v in summary.items():
        log.info("待換: %s x%s", k, v)
    total = 0
    for k in range(0, len(todo), 50):
        r = client.call("POST", "/lottery-order/items/batch-convert-to-point",
                        {"itemIds": [i["id"] for i in todo[k:k + 50]]})
        log.info("成功兌換 %s 個獎品，獲得 %s 鑽石", r.get("successCount"), f"{r.get('totalPoints', 0):,}")
        total += r.get("totalPoints", 0)
    return total


def open_boxes(client):
    """寶箱管理裡的寶箱全部開啟 (同網頁「全部開啟」)。"""
    d = client.call("GET", "/member-item/grouped")
    groups = [g for g in (d.get("list") or []) if (g.get("count") or 0) > 0]
    if not groups:
        log.info("沒有未開的寶箱")
        return 0
    total = 0
    for g in groups:
        name = (g.get("item") or {}).get("name") or "寶箱"
        try:
            r = client.call("POST", "/member-item/use-all", {"itemId": g["itemId"]})
        except ApiError as e:
            log.error("開 %s 失敗: %s", name, e)
            continue
        log.info("開了 %s x%s，獲得 %s 鑽石", name, r.get("usedCount", g["count"]), f"{r.get('totalReward', 0):,}")
        total += r.get("totalReward", 0) or 0
    return total


def run_cycle(client, sets, pending, state):
    """一個帳號跑一輪：抽 → 換鑽石 → 開寶箱。回傳 (下一輪還要抽的套組, 是否要等 10 分鐘)。"""
    results = [(s, draw_set(client, s, state)) for s in pending]
    time.sleep(3)    # 等訂單、寶箱建立
    exchange_items(client, state["daily_sets"] + [s["id"] for s in sets])
    open_boxes(client)
    client.relogin()    # 順便拿到換完之後的正確鑽石數

    pending, wait = [], False
    for s, result in results:
        if result == "retry":
            if (time.time() + RETRY_WAIT) * 1000 < s["drawEndTime"]:
                pending.append(s)
                wait = True
            else:
                log.warning("%s 快結束了，不再重試", s["name"])
        elif result == "poor":
            if client.points >= min(BATCH, s["drawLimit"]) * s["clientPrice"]:
                pending.append(s)
            else:
                log.warning("%s 鑽石不夠，換完鑽石也抽不了，這次不抽了", s["name"])
    return pending, wait


def run_until_done(accounts, sets):
    """每個帳號輪流跑一輪，一直重複到每個帳號的每套都不能再抽，或套組結束。
    帳號之間輪流，一個帳號在等重試時不會卡住其他帳號。一個帳號出錯也不影響其他帳號。"""
    todo = {a["name"]: [s for s in sets if s["id"] not in a["state"]["drawn_sets"]] for a in accounts}
    active = list(accounts)
    while active:
        wait = False
        for a in list(active):
            _current["account"] = a["name"]
            try:
                if "client" not in a:
                    a["client"] = Client(a)
                todo[a["name"]], w = run_cycle(a["client"], sets, todo[a["name"]], a["state"])
                wait = wait or w
            except Exception:
                log.exception("這個帳號執行失敗，今天先跳過")
                todo[a["name"]] = []
            if not todo[a["name"]]:
                active.remove(a)
        _current["account"] = ""
        if active and wait:
            log.info("還有 %s 個帳號沒抽滿，%d 分鐘後再試", len(active), RETRY_WAIT // 60)
            time.sleep(RETRY_WAIT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只列出今天會抽哪些套組，不登入不抽")
    ap.add_argument("--exchange-only", action="store_true", help="只做換鑽石 + 開寶箱")
    args = ap.parse_args()
    setup_logging()
    log.info("===== 開始 =====")
    try:
        accounts = load_accounts()
        root = load_state(accounts[0]["name"])
        for a in accounts:
            a["state"] = account_state(root, a["name"])
        sets = [] if args.exchange_only else find_daily_sets(tries=1 if args.check else 6)
        for s in sets:
            done = [a["name"] for a in accounts if s["id"] in a["state"]["drawn_sets"]]
            log.info("限抽套組: %s (id %s) %s 鑽/抽，每人限 %s 抽，剩 %s/%s%s", s["name"], s["id"],
                     s["clientPrice"], s["drawLimit"], s["remainingSets"], s["totalSets"],
                     f"，已抽完額度: {'、'.join(done)}" if done else "")
        if not sets and not args.exchange_only:
            log.warning("找不到開放中的限抽套組")
        if args.check:
            log.info("帳號: %s", "、".join(a["name"] for a in accounts))
            return

        ready = [a for a in accounts if a["email"] and a["password"]]
        for a in accounts:
            if a not in ready:
                log.warning("[%s] 帳號或密碼是空的，跳過", a["name"])
        if not ready:
            raise ApiError(f"請先在 {SECRET.name} 填入帳號密碼")
        run_until_done(ready, sets)
    except Exception:
        log.exception("執行失敗")
        sys.exit(1)
    log.info("===== 結束 =====")


if __name__ == "__main__":
    main()
