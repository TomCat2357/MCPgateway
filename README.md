# MCP Gateway (Parent MCP Server)

複数の子MCPサーバーを一元管理し、スキーマ取得やツール実行を肩代わりするゲートウェイです。

## 概要

- `children_config.json` に定義した子サーバーを起動し、常駐セッション+lockで衝突を防ぎながらツールを呼び出します。
- 子サーバーのスキーマ取得(`get_schema`)や任意ツール呼び出し(`execute_child_tool`)を1つのインターフェースで提供します。
- `children_abstract.json` があれば、子サーバー概要をリソースとして返します。

## 子サーバー例（同梱テンプレート）

`children_config.example.json` では以下を登録しています。`children_abstract.example.json` と合わせて参考にしてください。

- `serena`: コードベースのシンボリック検索/編集、メモ管理、限定的なシェル実行（tools: read_file, find_file, find_symbol, replace_content, execute_shell_command など）
- `context7`: ライブラリ名→Context7互換ID解決とドキュメント取得（resolve-library-id, get-library-docs）
- `codegraph`: コードグラフ検索、依存/呼び出し探索、ファイル読取り、GraphRAG検索、再インデックス（query_codebase, read_file_content, reindex_repository など）

## セットアップ

1. 依存関係インストール
   ```bash
   uv sync
   ```
2. 設定ファイルを作成（テンプレートをコピー）
   ```bash
   cp children_config.example.json children_config.json
   cp children_abstract.example.json children_abstract.json
   ```
   `children_config.json` 内で各子サーバーのコマンド/引数/環境変数を環境に合わせて修正してください。

## 起動方法

### ローカル実行（リポジトリclone済み）
```bash
uv run mcp-gateway \
  --children-config /absolute/path/to/children_config.json \
  --children-abstract /absolute/path/to/children_abstract.json
```

### uvx経由（GitHub公開後）
```bash
uvx --from git+https://github.com/OWNER/MCPgateway \
  mcp-gateway \
  --children-config /absolute/path/to/children_config.json \
  --children-abstract /absolute/path/to/children_abstract.json
```
※ OWNER/リポジトリ名は実際のものに置き換えてください。

### 他プロジェクトから使う例（.mcp.json）
```json
{
  "mcpServers": {
    "mcp-gateway": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/OWNER/MCPgateway",
        "mcp-gateway",
        "--children-config",
        "/absolute/path/to/children_config.json",
        "--children-abstract",
        "/absolute/path/to/children_abstract.json"
      ],
      "env": { "PYTHONUTF8": "1" }
    }
  }
}
```

## 親サーバーが提供する主なツール

- `list_registered_children()` 登録済み子サーバー名の一覧
- `get_child_status()` 子サーバーの起動状態/エラー情報
- `get_schema(child_name)` 子サーバーのツール・リソースのスキーマ取得（常駐セッション経由）
- `execute_child_tool(child_name, tool_name, tool_args, head_chars=None, tail_chars=None)` 子サーバーツールの実行（長文はhead/tailで省略可能）
- `close_child_session(child_name)` セッションを明示的に停止
- Resources: `mcp://server_summary`, `mcp://children_servers`（abstract提供時）

### 使い方のヒント
- スキーマ確認: `get_schema("serena")`, `get_schema("codegraph")` 等でツール一覧を取得できます。
- ツール実行: 例）`execute_child_tool("serena", "find_file", {"file_mask": "*.py", "relative_path": "."})`
- codegraphで再インデックスが必要な場合は `execute_child_tool("codegraph", "reindex_repository", {"incremental": true})` を実行してください。

## プロジェクト構造

```
MCPgateway/
├── mcp_gateway_server.py         # 親MCPサーバー本体（FastMCPベース）
├── children_config.example.json  # 子サーバー設定テンプレート
├── children_abstract.example.json# 子サーバー概要テンプレート
├── children_config.json          # 実運用用（ユーザー作成）
├── example.mcp.json              # .mcp.json設定例
├── pyproject.toml                # パッケージ定義・エントリポイント(mcp-gateway)
└── README.md
```

## トラブルシューティング

- Python環境が見つからない場合: `uv sync` を実行して仮想環境を作成してください。
- 子サーバーが起動しない場合:
  - `children_config.json` のコマンド/パス/環境変数を確認
  - npx/uvx など依存コマンドが使えるか確認
  - 子サーバー側の依存をインストール
- 子サーバーのstderrを確認したい場合: `DEBUG_MCP=1` を設定して起動するとstderrを出力します。
