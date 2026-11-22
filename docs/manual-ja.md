# AGENTIaC 日本語クイックガイド

日本語の簡潔な入口です。詳しいコマンド契約は [日常ガイド](usage.md)、配備・復旧・退役手順は
[運用ガイド](operations.md)、評価と証拠管理は [評価ガイド](testing.md)、命名規則は
[命名規則](model-naming.md)、管理画面は [ローカル管理ダッシュボード](dashboard.md)、障害切り分けは
[トラブルシューティング](troubleshooting.md) を参照してください。

## 1. 初回設定と確認

[README の環境構築](../README.md#control-environment) に従って制御用仮想環境を構築し、
`make install` で `agentiac` と bash 補完を有効にします（サイトリポジトリを使う場合は各サイトのラッパーコマンドを使用します）。

```sh
agentiac --help
agentiac status
agentiac model names
agentiac doctor
```

常設の専用ワーカー（dedicated）を既定とし、借用ワーカー（borrowed）は目的を明示して一時利用します。
現在選択中の `route`、宣言された既定値、および実際にロード中のモデル状態はそれぞれ別の情報として確認します。

## 2. pi で作業する

```sh
agentiac code ~/repo
agentiac code --resume
agentiac code -m <target>
agentiac worktrees review --diff
agentiac worktrees apply
```

既定では `~/agent-worktrees/` に新しい worktree を作成して作業し、元の作業ツリーにある未コミット変更は持ち込みません。
変更内容を確認・テストしてから `worktrees apply`（fast-forward のみ、未コミット変更や分岐がある場合は拒否）で反映します。
`--in-place` は指定したチェックアウトを直接編集します。compaction、thinking、loop guard、セッション保存先の詳細は
[Work on a repository with pi](usage.md#work-on-a-repository-with-pi) を参照してください。

## 3. 名前を区別する

- **ノード**: 物理ホストの識別子。
- **target**: `<node>-<backend>-<context>` 形式の配備構成キー兼短縮 API alias。
- **backend**: `<engine>-<version>-<model>-<quantization>[-<variant>]` 形式のマニフェスト名。
- **full_name**: カタログ表示用の長いラベル（API 引数には使わない）。

詳細は [命名規則](model-naming.md) を参照してください。エンジンが返す served ID の接尾辞だけで重み精度を判断しないでください。

## 4. 読み込み・借用・解放

```sh
agentiac warm <host>
agentiac unload <host>
agentiac claim <host> [--backend <id>] [--context <N>]
agentiac release <host>
```

モデルの一時アンロード（`unload`）と借用ホストの返却（`release`）は異なります。
借用時は他者の稼働中ワークロードを保護し、利用後に `release` で専用ワーカーの `default` へ戻します。
オンデマンド制御や復旧手順の詳細は [On-demand hosts](usage.md#on-demand-hosts) および [運用ガイド](operations.md) を参照してください。

## 5. 検証と記録

```sh
agentiac check --no-pi -m <target>
agentiac log-task --help
agentiac eval suite --help
```

`check` は推論を発生させるため、未ロードのワーカーではコールドロードが走ります。
評価スイートの実行・失敗分類・provenance・証拠 DB については [評価ガイド](testing.md) を参照してください。

## 6. 運用・制約

- gateway は control ホストの loopback 専用です。外部クラウド alias（`cloud-gpt-sol`）は明示時のみ使用し、自動フォールバックや評価には使いません。
- 認証情報、モデル重み、詳細なプロンプト/応答トレースは Git にコミットしません。
- 削除（`ops retire`）、ネットワーク変更、サービス停止、ドライバ/JetPack の交換は明示的な承認手順に従います。
- 配備・バックアップ・リストア・管理ダッシュボードの運用は [運用ガイド](operations.md) と [ローカル管理ダッシュボード](dashboard.md) を参照してください。
