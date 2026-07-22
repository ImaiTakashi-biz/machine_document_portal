# アーキテクチャ

## 全体構成

```text
Browser / Tablet
       |
FastAPI routers + Jinja2 templates
       |
Production / Document services
       |
GoogleSheetsMemorySyncService -------- SampleDataService
       |            |                         |
       |            +-- SharePointService     +-- machines.json
       |            |      |
       |            |      +-- Microsoft Graph / SharePoint
       |            |
       |            +-- NasDrawingService -- NAS PDF
       |
MemoryDashboardStore -- プロセス内メモリ -- 号機一覧
       |
       +-- dashboard_snapshot.json

DOCUMENT_REFRESH_TIMES
       +-- GoogleSheetsMemorySyncService.sync()
       +-- Google Sheets「生産中」/ SharePoint / NASを完全同期

ScheduledOperationsService
       +-- Google Sheets 翌営業日 B36:K36 / B40:K40
       +-- SharePoint / NAS 照合 → ARAICHAT（13:00、14:30）
       +-- NAS PDF → Windows Printer（15:00）
       +-- scheduled_job_state.json
```

FastAPIは画面と操作APIを公開します。Jinja2、vanilla CSS、最小限のJavaScriptで構成します。

`app/main.py` はアプリの生成と起動・終了処理だけを担当し、バックグラウンド処理の時刻判定と実行制御は `app/scheduling.py` に分離します。これにより、HTTPアプリの初期化と定時処理を個別に確認・テストできます。

## 現在の保存方針

標準設定は `PERSISTENCE_MODE=memory` です。PostgreSQLへ接続・保存しません。

`MemoryDashboardStore` が次をプロセス内に保持します。

- 号機ごとの現在品番、品名、稼働状態
- 工程内検査シートと加工図のURL・取得状態
- 最終更新日時と外部エラー状態

ストアはロック付きで同一プロセス内の複数リクエストから利用できます。返却時はデータを複製し、画面処理から内部状態が意図せず変更されないようにします。

最新の `DashboardData` はJSONスナップショットへ原子的に保存し、再起動時に復元してから完全同期します。外部検索エラー時は前回の正常リンクを再利用しません。メモリと定時処理ロックは複数ワーカー間で共有されないため、Uvicornは1ワーカーで起動します。

## 各層の役割

- `app/routers`: HTTP入力、画面表示、手動更新API
- `app/scheduling.py`: 定期同期と13:00・14:30・15:00処理の実行制御
- `app/services`: 業務判断、メモリ保持、サンプル切替、外部連携
- `app/services/document_search.py`: SharePointなどの資料検索で共通利用する結果型
- `app/services/next_day_notification.py`: 13:00・14:30の利用者向け通知文生成
- `app/services/memory_store.py`: 画面データのプロセス内保持と最新スナップショット保存
- `app/schemas`: UIとメモリストアで共通利用する型付きデータ
- `app/utils`: 品番正規化、号機自然順、UTF-8ログ
- `sample_data/machines.json`: 号機構成と画面確認用の初期データ

## データの流れ

本番では `GoogleSheetsMemorySyncService` が「生産中」シートのA/D/H/I列を取得します。同じ同期処理で、設定したSharePointフォルダ配下を再帰検索し、H列の品番をファイル名の拡張子を除いた文字列と正規化せず照合します。完全一致を優先し、完全一致しないファイルは`品番-[1-9][0-9]*`の場合だけ関連ファイルとして扱います。NASは従来どおり同名PDFだけを検索します。D列に存在する号機だけで `MemoryDashboardStore` を置き換え、画面と更新APIは同じストアを共有します。

完全同期では同じ品番が複数号機に設定されていても、SharePointとNASの検索は品番ごとに1回だけ行い、結果を各号機の表示へ展開します。SharePointの照合は、ファイルごとに完全一致または末尾の連番を1回判定し、全品番との総当たりを避けます。

```text
Google Sheets ─┐
SharePoint  ───┼→ GoogleSheetsMemorySyncService → MemoryDashboardStore → 号機一覧
NAS PDF ───────┘
```

サンプルモードでは `SampleDataService` がJSONから号機を生成します。本番では起動時と手動更新時にGoogle Sheets、SharePoint、NASを完全同期します。`AUTO_REFRESH_SECONDS` の間隔ではGoogle Sheetsの前回値と号機番号で比較し、H列の品番が新規・変更された号機だけSharePointとNASを再検索します。`DOCUMENT_REFRESH_TIMES` の指定時刻には、Google Sheetsの「生産中」シートを再取得して画面へ反映し、その時点の全号機についてSharePointとNASも完全更新します。ブラウザはダッシュボードの更新完了時刻を10秒間隔と画面復帰時に確認し、表示中の値から変化していればページを再読込します。同期途中や失敗時には更新完了時刻が変わらないため再読込せず、利用者による更新・印刷操作中は再読込を保留します。

更新確認APIは更新完了時刻だけをメモリストアから読み取り、号機一覧全体の複製を行いません。このAPIを各ブラウザが10秒間隔で呼び出しても、号機数に比例する不要なコピーが発生しない構成です。

## キャッシュと品番変更

メモリ内の品番管理には `normalize_part_number()` の結果も保持しますが、SharePointとNASの照合にはGoogle Sheetsから取得した元の品番文字列を使用します。完全同期のたびに外部サービスを再検索します。

加工図プレビューはPDFの更新日時とサイズをキーに、最大64件かつ128MiBまでアプリ内へキャッシュします。同じPDFへの同時アクセスは1回だけ変換し、他の要求はその結果を再利用します。

現在の加工図表示は、専用の別タブで開く1ページ目JPEGプレビューです。`NasDrawingPreviewService` を介しているため、工程内検査シートとの分割表示や複数ページ表示へ変更する際も、NAS検索・アクセス制御を保ったまま表示方式を差し替えられます。

SharePoint連携はクライアント資格情報フローでMicrosoft Graphのアクセストークンを取得し、設定した `driveId` / `folderId` を起点に配下の全サブフォルダを幅優先で再帰走査します。各フォルダのページングを最後まで取得し、同じフォルダIDは重複走査しません。権限は `Sites.Selected` と対象サイトの `Read` に限定し、アプリからファイルの作成・更新・削除は行いません。

SharePoint候補は完全一致、連番の数値、ファイル名、相対フォルダの順で安定して並べます。候補が複数の場合、`DocumentState` に候補名・URL・相対フォルダを保持し、`/inspections/{machine_id}` の選択画面へ遷移します。候補URLはHTTPまたはHTTPSだけを許可します。

サイドバーの工程内検査シート、出荷検査表、測定機器点検表は、`.env` に設定した共通URLをそのまま別タブで開く静的リンクです。品番別のSharePoint検索処理とは独立しています。

## 定時通知と印刷

13:00に翌営業日シートの `B36:K36` と `B40:K40` をGoogle Sheetsの1回の一括取得で読み込み、列ごとに日付と品番を対応させます。B36:K36の日付が対象日より前の列を除外し、対象日と同じ日付または対象日より後の列だけをSharePointとNASで確認します。13:00通知の見出しは「【翌営業日セット予定分の検査シート・加工図面確認通知】」です。14:30に13:00で確定した同じシートを再取得し、同じ日付条件で残っている不足だけを「【翌営業日セット予定分の再確認（14:30）】」として、別の冪等キーで再通知します。通知本文は品番単位で必要な対応をまとめ、SharePointとNASの保存場所を表示します。資料確認用のサイドバー状態は保持しません。15:00にも同じシートの両範囲を再取得し、同じ日付条件で対象となった品番について、NASに存在するPDFをRAW印刷ジョブとしてWindowsプリンターへ送信します。対象日付、シート名、`sheetId`、スプレッドシートIDから重複防止キーを生成し、13:00通知、14:30通知、品番別印刷状態をJSONへ原子的に保存します。

15:00時点で存在したPDFをプリンターへ送信できなかった品番は、`PRINT_RETRY_DELAYS_SECONDS` の間隔で自動再実行します。既定の `180,300,600` では、最初の失敗から3分後、5分後、10分後に未印刷品番だけを処理します。15:00時点でPDFが存在しない品番は `manual_required` として記録し、自動再検索・自動印刷・「印刷の確認」サイドバーの対象から除外します。品番ごとの `pending`、`failed`、`manual_required`、`uncertain`、`submitted` を同じJSONへ保存し、アプリ再起動後も送信済み品番を除外します。プリンター送信の自動再実行を終えても未完了の場合だけ、左サイドバーに利用者向けの「印刷の確認」を表示します。手動操作もサーバー側の `RawPdfPrinter` を呼び出すため、ブラウザを開いている端末のプリンターは使用しません。

本番切替では、従来の印刷スクリプトやタスクスケジューラを停止してから定時処理を有効化します。スケジューラーは起動後30秒ごとに日本時間を確認するため、13:00以降の起動では13:00処理を、14:30以降では再確認処理も、15:00以降では印刷処理も順に開始します。旧処理と状態ファイルを共有しないため、両方を同時稼働させません。

## エラー時の扱い

外部検索に失敗した場合は前回の正常リンクを保持せず、対象資料をエラー状態にします。ARAICHATや印刷の処理は正常終了した項目だけを完了として記録します。ARAICHATの送信結果が不明な場合は `ambiguous` として自動再送を止めます。印刷データを書き込み始めた後に結果が不明になった品番も自動再印刷せず、利用者による用紙確認を待ちます。利用者画面には現在必要な操作だけを表示し、処理履歴と技術的なエラー詳細はログだけに記録します。

## PostgreSQL構成

SQLAlchemyモデル、Repository、Alembicマイグレーションは、将来永続化が必要になった場合の選択肢として残しています。`PERSISTENCE_MODE=postgresql` を明示しない限り、DB Sessionは生成されません。

## ログ

ログは業務データの保存先ではありません。Python `logging` と `RotatingFileHandler` を使用し、動作状況とエラー詳細だけをUTF-8でファイル出力します。認証情報や資料内容は記録しません。
