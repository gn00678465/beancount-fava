# 匯入銀行明細

fava 的 Import 頁可以把銀行下載的明細轉成交易。image 內建一個 importer：

| importer | 檔案 | 位置 |
| --- | --- | --- |
| `TaishinImporter` | 台新銀行網路銀行「交易明細」匯出的 xlsx | `beancount_agent_api.importers.taishin` |

## 設定

帳本資料夾（容器的 `/ledger`）放一個設定檔，例如 `importers.py`：

```python
from beancount_agent_api.importers.taishin import TaishinImporter

CONFIG = [
    TaishinImporter(
        "Assets:TW:Taishin:Settlement",
        rules=[
            ("存款息", "Income:Interest"),
            ("劃撥轉帳", "Assets:TW:Taishin:Stocks"),
        ],
        fallback_account="Expenses:Uncategorized",
    ),
]
```

主帳本加上兩個 fava 選項。路徑相對於主帳本所在的資料夾：

```beancount
2000-01-01 custom "fava-option" "import-config" "importers.py"
2000-01-01 custom "fava-option" "import-dirs" "import"
```

`import-dirs` 是上傳的檔案存放的資料夾，要先建立，容器的使用者要能寫入。

## 轉換規則

- 每一列明細轉成一筆交易，日期是帳務日，摘要是備註；備註空白時用摘要。
- `rules` 依序比對摘要與備註，第一個出現在文字裡的字串決定另一邊的帳戶。都對不到時記到 `fallback_account`，flag 是 `!`，在匯入頁看得出來。`fallback_account` 要在帳本裡 `open`。
- 每筆交易帶 `taishin-id` metadata，內容是交易時間、金額與餘額。
- 每個有交易的日子，隔天寫一筆 `balance` 斷言，金額是銀行那天最後的餘額；明細第一天另寫一筆期初斷言。匯出當天之後可能還有交易，所以匯出日不寫。
- 明細的每一列都要接得上前一列的餘額，筆數要等於最後一列的「共 N 筆」。不符合時整份檔案不轉換，匯入失敗。

## 第一次匯入

期初斷言要和帳上的餘額相同。帳上在明細第一天之前少記或多記時，在那天前一天加一筆 `pad`，例如明細從 6/1 開始：

```beancount
2026-05-31 pad Assets:TW:Taishin:Settlement Equity:Balance-Adjustment
```

帳上同一期間有手寫、和銀行不同的 `balance` 時，刪掉它，以匯入的斷言為準。之後的明細從上一份的最後一天接著下載，或和上一份重疊，就不用再加 `pad`。兩份明細之間少了幾天時，期初斷言會失敗，這時補下載缺的日子，不要加 `pad`。

## 重複的交易

fava 匯入頁存檔時，跳過標成重複的項目，每一項都可以在匯入頁切換。

- 帳本裡帶相同 `taishin-id` 的交易，以及同一天、同帳戶、同金額的 `balance`，標成重複。
- 沒有 `taishin-id` 的交易（手動記的、經 agent API 記的），在這個帳戶的金額合計和明細的一列完全相同、日期相差 7 天以內時，標成重複。一筆只抵一列，日期最近的先配對，所以同一天兩筆同額的明細要帳上有兩筆才都標成重複。
- 配對沒抓到或配錯時，那一天之後的第一筆 `balance` 斷言會失敗。斷言失敗的日子就是要檢查的地方。
- 交割戶的進出只靠匯入記，最不會出錯。買股時在匯入頁把另一邊改成持股帳戶，填上成本與手續費，不另外手記。

## 已知限制

- 明細沒有帳號，同一個設定檔有兩個台新帳戶的 importer 時，兩個都認得這份檔案，匯入頁要選一個。
- 明細只有現金的進出。股票的股數與成本、賣出時的損益要在匯入頁或帳本裡補。
- 手記時把一列明細拆成兩筆交易（例如成本與手續費分開記），金額對不上，不會標成重複；斷言會在那天之後失敗。
- 只讀匯出檔的第一個工作表，而且只支援 inline 字串與數字儲存格；格式改變時 importer 不認得這份檔案。
