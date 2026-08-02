# ポケ情報ボード

ポケモン関連の最新情報(ニュース記事・公式YouTube動画・公式X ※ベストエフォート)を
**6時間ごとに自動収集・AI要約**し、カテゴリ別タブで一覧できる静的サイトです。

- サーバー不要: GitHub Pages + GitHub Actions のみ(無料枠内)
- 設定は `feeds.yml` を編集するだけ
- メインカラー: `#86B3E0`

## 仕組み

```
feeds.yml … カテゴリ・情報源の設定(編集するのはこのファイルだけ)
scripts/collect.py … 収集 → 重複除去 → AI要約 → docs/ を生成
.github/workflows/update.yml … 6時間ごとの自動実行 + 手動実行
docs/index.html, docs/data.json … 生成物(GitHub Pagesで配信)
```

## セットアップ手順

1. **Publicリポジトリを作成**し、このリポジトリのファイルを配置(またはフォーク)
2. **Settings → Pages** で `Deploy from a branch` / ブランチ `main`・フォルダ `/docs` を選択
3. (任意)**Settings → Secrets and variables → Actions** に `ANTHROPIC_API_KEY` を登録
   - 未登録でも動作します(AI要約の代わりに記事の抜粋を表示)
4. **Actions タブ → Update board → Run workflow** で手動実行
5. 数分後、`https://<ユーザー名>.github.io/<リポジトリ名>/` で公開されます

### APIキーについて

- 取得先: [console.anthropic.com](https://console.anthropic.com)
- 使用モデルは Claude Haiku(コスト重視)。**新着記事のみ**要約し、既存記事の要約は
  `docs/data.json` から引き継ぐため、概算コストは**月数円〜数十円**程度です。

## feeds.yml のカスタマイズ

### カテゴリを追加する

```yaml
categories:
  - id: mycat            # 英数字のID
    name: 表示名          # タブに出る名前
    color: "#99AEC2"     # カテゴリカラー
    news_queries:        # Googleニュースの検索語(複数可)
      - "検索キーワード"
    feeds: []
    x_rss: []
```

### YouTubeチャンネルを追加する

チャンネルID(`UC...`)を調べて、該当カテゴリの `feeds:` に追加します。

```yaml
    feeds:
      - url: "https://www.youtube.com/feeds/videos.xml?channel_id=UCxxxxxxxxxxxx"
        source: チャンネル名
        type: video
```

複数カテゴリの話題が混ざるチャンネルは `shared_sources:` に追加し、
`routing:` のキーワードで振り分けます(該当なしは「その他」タブへ)。

### 更新頻度を変える

`.github/workflows/update.yml` の cron を変更します(例: 3時間ごと → `0 */3 * * *`)。
表示件数・保持期間は `feeds.yml` の `max_items_per_category` / `max_age_days` で調整できます。

## X(Twitter)の取得について

XはAPI有料化のため、**無料での確実な取得手段がありません**。
`feeds.yml` の `x_rss:` に RSSHub 等のブリッジURL(例:
`https://rsshub.app/twitter/user/アカウント名`、または自前RSSHubインスタンス)を
設定すると取得を試みますが、公開インスタンスはレート制限で失敗しがちです。
取得失敗は警告ログのみでスキップされ、サイト生成は止まりません。

**確実に追いたい場合は、公式アカウントのみを入れたXリストの併用を推奨します。**
主な公式アカウント:

| アカウント | ハンドル |
|---|---|
| ポケモン公式 | [@Pokemon_cojp](https://x.com/Pokemon_cojp) |
| ポケモンセンター公式 | [@pokemoncenterPR](https://x.com/pokemoncenterPR) |
| ポケモンカードチャンネル公式 | [@PokecaCH](https://x.com/PokecaCH) |
| Pokémon Sleep公式 | [@PokemonSleepApp](https://x.com/PokemonSleepApp) |
| ポケモンチャンピオンズ公式 | [@Poke_Champ_jp](https://x.com/Poke_Champ_jp) |

## ローカルでの実行

```bash
pip install -r requirements.txt
python scripts/collect.py          # docs/index.html, docs/data.json を生成
# ANTHROPIC_API_KEY=sk-... python scripts/collect.py  # AI要約あり
```
