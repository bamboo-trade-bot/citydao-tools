"""良級懸賞 (citydao.world)「老闆發瘋蒐藏系列」每日自動抽 + 換鑽石

直接打網站自己前端用的 API (跟 citydao_ev.py 同一套)，不開瀏覽器。
網頁有 Cloudflare 機器人驗證會擋自動化瀏覽器，API 不受影響。

流程:
    1. 找目前開放中的「老闆發瘋蒐藏系列」(單抽 1000 鑽，每天中午 12:00 換新一套，每人限 1 抽)
    2. 登入 (每次執行都重新登入，等於被登出也會自動登回去)
    3. 從還沒被抽走的號碼隨機挑 1 個抽
    4. 訂單紀錄裡「尚未選擇」的寶箱 (A 金 5000 / B 銀 3000 / C 銅 1100) 全部換成鑽石

用法:
    python citydao_auto_draw.py                  # 正式執行 (排程每天 12:30 跑這個)
    python citydao_auto_draw.py --check          # 只找套組，不登入不抽
    python citydao_auto_draw.py --exchange-only  # 只做換鑽石

帳密放在同資料夾的 citydao_secret.json: {"email": "...", "password": "..."}
紀錄寫在 citydao_auto.log。
"""
import argparse
import json
import logging
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from citydao_ev import BASE, HEADERS, get, list_sets

HERE = Path(__file__).resolve().parent
SECRET = HERE / "citydao_secret.json"
STATE = HERE / "citydao_auto_state.json"

IP_WORK_ID = 18                 # 老闆發瘋
SET_PREFIX = "老闆發瘋蒐藏系列"
SET_PRICE = 1000
BOX_NAMES = ("金寶箱", "銀寶箱", "銅寶箱")   # A / B / C 賞

log = logging.getLogger("citydao")


class ApiError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(HERE / "citydao_auto.log", encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stdout:     # pythonw 沒有 console
        sys.stdout.reconfigure(encoding="utf-8")
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    log.setLevel(logging.INFO)


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"drawn_sets": []}


def save_state(state):
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


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


def login():
    creds = json.loads(SECRET.read_text(encoding="utf-8"))
    d = api("POST", "/member/login", {"account": creds["email"], "password": creds["password"]})
    log.info("登入成功，目前鑽石 %s", f"{d.get('gamePoints', 0):,}")
    return d["token"]


class Client:
    """帶登入狀態的 API 呼叫。被登出 (401) 就自動重新登入再送一次；401 代表請求沒被處理，重送不會重複抽。"""

    def __init__(self):
        self.token = login()

    def call(self, method, path, body=None):
        try:
            return api(method, path, body, self.token)
        except ApiError as e:
            if e.status != 401:
                raise
            log.info("被登出了 (%s)，重新登入再試", e)
            self.token = login()
            return api(method, path, body, self.token)


def find_open_set(tries=6, wait=120):
    """目前開放抽的那套 (一套開放 24 小時，中午 12:00 換新)。剛換套時可能還沒上架，會重試。"""
    for attempt in range(tries):
        now_ms = time.time() * 1000
        try:
            sets = [s for s in list_sets(IP_WORK_ID)
                    if s["name"].startswith(SET_PREFIX) and s["clientPrice"] == SET_PRICE
                    and s["drawStartTime"] <= now_ms <= s["drawEndTime"] and s["remainingSets"] > 0]
        except Exception as e:
            log.warning("查詢套組失敗: %s", e)
            sets = []
        if sets:
            return max(sets, key=lambda s: s["drawStartTime"])
        if attempt < tries - 1:
            log.info("還沒找到開放中的套組，%d 秒後重試 (%d/%d)", wait, attempt + 1, tries)
            time.sleep(wait)
    return None


def draw_once(client, set_id):
    """隨機挑一個還沒被抽的號碼抽 1 抽 (等同網頁「隨機選號」數量 1)。號碼剛好被別人搶走就換一個再試。"""
    for attempt in range(3):
        detail = get(f"/lottery-draw/detail/{set_id}")
        free = [d["drawNumber"] for d in detail["draws"] if not d["isDrawn"]]
        if not free:
            raise ApiError("這套已經抽完了")
        num = random.choice(free)
        try:
            r = client.call("POST", "/lottery-draw/draw",
                            {"lotterySetId": set_id, "drawNumbers": [num], "drawMethod": 1})
        except ApiError as e:
            if attempt < 2 and ("號碼" in str(e) or "已被" in str(e)):
                log.info("號碼 %s 被搶走了 (%s)，換一個", num, e)
                continue
            raise
        log.info("抽獎回應: %s", json.dumps(r, ensure_ascii=False)[:1500])
        return num, r


def exchange_boxes(client):
    """訂單紀錄裡「尚未選擇」(status 0) 的寶箱全部換鑽石。
    寶箱算大賞，網頁的「全部小賞換鑽石」不會換到，要指定 id 換 (同網頁單筆「換鑽石」按鈕)。"""
    items, page = [], 1
    while True:
        d = client.call("GET", f"/lottery-order/items?current={page}&pageSize=50&status=0")
        batch = d.get("list") or []
        items += batch
        if not batch or len(items) >= d.get("total", 0):
            break
        page += 1
    boxes = [i for i in items if i.get("status") == 0 and not i.get("shippingOnly")
             and any(n in (i.get("prizeName") or "") for n in BOX_NAMES)]
    if not boxes:
        log.info("沒有待換的寶箱")
        return 0
    for b in boxes:
        log.info("待換: %s賞 %s → %s 鑽", b.get("prizeRank", "?"), b.get("prizeName"), b.get("prizePoints"))
    r = client.call("POST", "/lottery-order/items/batch-convert-to-point", {"itemIds": [b["id"] for b in boxes]})
    log.info("成功兌換 %s 個獎品，獲得 %s 鑽石", r.get("successCount"), f"{r.get('totalPoints', 0):,}")
    return r.get("totalPoints", 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只找套組，不登入不抽")
    ap.add_argument("--exchange-only", action="store_true", help="只把訂單紀錄的寶箱換鑽石")
    args = ap.parse_args()
    setup_logging()
    log.info("===== 開始 =====")
    state = load_state()
    try:
        s = None if args.exchange_only else find_open_set(tries=1 if args.check else 6)
        if s:
            log.info("目前套組: %s (id %s) 剩 %s/%s，每人限 %s 抽",
                     s["name"], s["id"], s["remainingSets"], s["totalSets"], s.get("drawLimit"))
        elif not args.exchange_only:
            log.warning("找不到開放中的「%s」(可能已抽完或還沒上架)", SET_PREFIX)
        if args.check:
            return

        client = Client()
        if s and s["id"] in state["drawn_sets"]:
            log.info("這套已經抽過了，跳過抽獎")
        elif s:
            try:
                num, _ = draw_once(client, s["id"])
                log.info("已抽號碼 %s", num)
                state["drawn_sets"] = (state["drawn_sets"] + [s["id"]])[-60:]
                save_state(state)
            except ApiError as e:
                log.error("抽獎失敗: %s", e)
                if any(w in str(e) for w in ("上限", "限抽", "已抽", "最多")):
                    state["drawn_sets"] = (state["drawn_sets"] + [s["id"]])[-60:]
                    save_state(state)
            time.sleep(3)    # 等訂單建立
        exchange_boxes(client)
    except Exception:
        log.exception("執行失敗")
        sys.exit(1)
    log.info("===== 結束 =====")


if __name__ == "__main__":
    main()
