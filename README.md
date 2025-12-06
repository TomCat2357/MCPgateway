# Parent MCP Server

複数の子MCPサーバーを一元管理する親MCPサーバーです。

## 概要

このサーバーは、複数の子MCPサーバー（filesystem, sqlite など）を登録・管理し、統一されたインターフェースで操作できるようにします。

## セットアップ

### 1. 依存関係のインストール

```bash
uv sync
```

### 2. 子サーバー設定ファイルを用意

`children_config.example.json` / `children_abstract.example.json` をテンプレートとしてコピーし、実環境に合わせて編集してください。

```bash
cp children_config.example.json children_config.json
cp children_abstract.example.json children_abstract.json
```

### 3. uvxで起動（推奨）

GitHubに公開後、uvxから直接起動できます。

```bash
uvx --from git+https://github.com/OWNER/ParentMCPserver \
  parent-mcp-server \
  --children-config /absolute/path/to/children_config.json \
  --children-abstract /absolute/path/to/children_abstract.json
```

### 4. ローカルでの起動（リポジトリclone済みの場合）

```bash
uv run python parent_server.py \
  --children-config /absolute/path/to/children_config.json \
  --children-abstract /absolute/path/to/children_abstract.json
```

## 設定例

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "./files"],
      "env": {},
      "description": "ファイル操作を行うサーバー"
    },
    "sqlite": {
      "command": "uvx",
      "args": ["mcp-server-sqlite", "--db-path", "./test.db"],
      "env": {},
      "description": "SQLiteデータベース操作サーバー"
    }
  }
}
```

## 使用方法

### このプロジェクトで使用する場合

`.mcp.json` に以下を設定（OWNERはご自身のGitHubアカウントに置き換えてください）：

```json
{
  "mcpServers": {
    "parent-manager": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/OWNER/ParentMCPserver",
        "parent-mcp-server",
        "--children-config",
        "/absolute/path/to/children_config.json",
        "--children-abstract",
        "/absolute/path/to/children_abstract.json"
      ],
      "env": {
        "PYTHONUTF8": "1"
      }
    }
  }
}
```

### 別プロジェクトから使用する場合

他のプロジェクトの `.mcp.json` にも同様に設定できます（上記と同じ構成を追加してください）。

**重要**: `--children-config` と `--children-abstract` で実際の絶対パスを指定してください（children-abstract未指定の場合は警告を出し、children_abstractリソースは登録されません）。

## 提供されるツール

### 基本ツール

- `list_registered_children()` - 登録された子サーバー一覧を取得
- `wake_child_and_get_schema(child_name)` - 子サーバーを起動してスキーマを取得
- `execute_child_tool(child_name, tool_name, tool_args, head_chars=None, tail_chars=None)` - 子サーバーのツールを実行し、長い出力を省略可能
- `get_children_abstract()` - 子サーバー概要JSONの内容を返す（`--children-abstract`指定時のみ）

### セッション管理ツール

- `get_active_sessions()` - 現在アクティブな子サーバーセッションの一覧を取得
- `close_child_session(child_name)` - 特定の子サーバーとのセッションを閉じる
- `check_child_session_health(child_name)` - セッションの健全性をチェック
- `reconnect_child_session(child_name)` - セッションを強制的に再接続

## 高度な機能

### デバッグモード

環境変数 `DEBUG_MCP=1` を設定すると、子サーバーのstderr出力を確認できるデバッグモードが有効になります。

```bash
DEBUG_MCP=1 uv run python parent_server.py \
  --children-config /path/to/children_config.json \
  --children-abstract /path/to/children_abstract.json
```

### 自動リトライ機能

接続失敗時は自動的に最大3回までリトライします（指数バックオフ: 1秒、2秒、4秒）。

## プロジェクト構造

```
ParentMCPserver/
├── parent_server.py     # メインサーバーコード
├── children_config.example.json   # 子サーバー設定テンプレート
├── children_abstract.example.json # 子サーバー概要テンプレート
├── example.mcp.json               # .mcp.json設定例
├── pyproject.toml      # プロジェクト定義
├── .venv/              # 仮想環境（uv syncで作成）
└── README.md           # このファイル
```

## トラブルシューティング

### Python環境が見つからない

```bash
uv sync
```

を実行して、プロジェクト専用の仮想環境を作成してください。

### 子サーバーが起動しない

- `config.json` の設定を確認
- 子サーバーのコマンド（npx, uvx など）が利用可能か確認
- 子サーバーの依存関係がインストールされているか確認
