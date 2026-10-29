# citydao 工具

[良級懸賞 citydao.world](https://citydao.world) 的兩個小工具：

- **`citydao_auto_draw.py`**：每天自動抽「老闆發瘋蒐藏系列」1 抽，抽到的寶箱自動換成鑽石
- **`citydao_ev.py`**：算各套組的期望值，看哪套比較划算

只需要 Python 3（3.9 以上），不用另外裝套件。

---

## 每日自動抽「老闆發瘋蒐藏系列」

### 它會做什麼

1. 找目前開放的「老闆發瘋蒐藏系列」（單抽 1000 鑽、每人限 1 抽、每天中午 12:00 換新一套）
2. 用你的帳號登入；跑到一半被登出也會自動重新登入再試
3. 從還沒被抽走的號碼隨機挑 1 個，抽 1 抽
4. 到訂單紀錄把還沒處理的寶箱換成鑽石：A 金寶箱 5000、B 銀寶箱 3000、C 銅寶箱 1100

同一套只會抽一次（抽過的會記在 `citydao_auto_state.json`），重複執行也不會多抽。

> 腳本直接呼叫網站前端用的 API，不開瀏覽器。

### 安裝

1. 安裝 [Python 3](https://www.python.org/downloads/)，安裝時勾選「Add python.exe to PATH」
2. 下載這個 repo（綠色 **Code** 按鈕 → **Download ZIP**），解壓縮到一個固定的資料夾
3. 把 `citydao_secret.example.json` 複製一份，改名為 `citydao_secret.json`，填入自己的帳密：

   ```json
   {"email": "你的帳號(Email)", "password": "你的密碼"}
   ```

   `citydao_secret.json` 已列在 `.gitignore`，不會被上傳。

4. 在這個資料夾開 PowerShell，先手動跑一次確認沒問題：

   ```powershell
   python citydao_auto_draw.py
   ```

   看到「登入成功」「已抽號碼」「成功兌換」就代表正常。

### 設定每天 12:30 自動執行（Windows）

在同一個資料夾的 PowerShell 執行：

```powershell
powershell -ExecutionPolicy Bypass -File setup_schedule.ps1
```

這會在 Windows「工作排程器」建立一個每天 12:30 執行的工作「citydao 老闆發瘋每日抽」。

- 時間是照電腦的時區算的，電腦時區要是**台北時間**
- 那時電腦要開著，而且 Windows 帳號是登入狀態；在睡眠中會被喚醒來跑
- 錯過時間（例如關機）會在下次開機時補跑，但那時可能已經換成下一套
- 不想跑了：打開「工作排程器」刪掉那個工作

### 其他用法

| 想做什麼 | 指令 |
|---|---|
| 手動跑一次 | `python citydao_auto_draw.py` |
| 只把寶箱換鑽石 | `python citydao_auto_draw.py --exchange-only` |
| 只看今天是哪一套（不登入、不抽） | `python citydao_auto_draw.py --check` |

每次執行的結果都記在 `citydao_auto.log`。

### 注意

- 每抽一次扣 1000 鑽，所以帳號裡至少要有 1000 鑽才抽得了
- 獎項和換鑽石的數字是照目前網站的設定寫的，網站改規則的話腳本可能要跟著改
- 改了網站密碼，記得同步更新 `citydao_secret.json`

---

## 套組期望值 `citydao_ev.py`

| 想做什麼 | 指令 |
|---|---|
| 全站（約 45 秒） | `python citydao_ev.py` |
| 只看某個系列 | `python citydao_ev.py 33`（33 = 蘋果家族） |
| 單一套組（網址最後的數字） | `python citydao_ev.py --set 1696` |

系列編號：在網站篩選該系列後，看網址 `ipWorkIds=` 後面的數字。

### 輸出怎麼看

- **比值** = 剩餘獎項總值 ÷ 剩餘抽數 ÷ 單抽價，代表隨便抽一次平均拿回幾成。1 以下就是平均會賠
- **大賞機率** = 下一抽中大賞的機率
- **最佳停手**：一直抽到中為止、該停就停的策略。列在最後的是有正期望值的套組（期望淨利以鑽計）；
  沒有的話會列出「單抽價要降到多少才值得抽」。這裡假設只有你在抽、資金夠抽到底
