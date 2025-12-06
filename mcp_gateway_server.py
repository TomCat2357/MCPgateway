import argparse
import asyncio
import json
import logging
import os
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime
import time
from typing import Any, Dict, Optional, TextIO

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # Python 3.10以前
    except ModuleNotFoundError:
        tomllib = None  # TOMLサポートなし

from mcp.server.fastmcp import FastMCP
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

class ChildrenConfigError(Exception):
    """子MCPサーバー設定の読み込みに関するエラー。"""

# stderrを捨てるためのnullデバイス（lifespanで管理）
_devnull_handle: Optional[TextIO] = None

def setup_logging():
    """シンプルなロギング設定。DEBUG_MCP=1で詳細ログ、MCP_LOG_FILE指定でファイル出力。"""
    log_level = logging.DEBUG if os.getenv("DEBUG_MCP") else logging.WARNING
    log_handlers = []

    formatter = logging.Formatter(
        fmt="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log_file = os.getenv("MCP_LOG_FILE")
    if log_file:
        try:
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(formatter)
            log_handlers.append(file_handler)
        except Exception as e:
            print(f"Warning: failed to open MCP_LOG_FILE='{log_file}': {e}", file=sys.stderr)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    log_handlers.append(stream_handler)

    logging.basicConfig(level=log_level, handlers=log_handlers, force=True)


logger = logging.getLogger("parent_mcp")

def get_devnull() -> TextIO:
    """DEVNULLハンドルを取得（遅延初期化）"""
    global _devnull_handle
    if _devnull_handle is None or _devnull_handle.closed:
        _devnull_handle = open(os.devnull, 'w')
    return _devnull_handle

def get_errlog() -> TextIO:
    """子プロセスstderrの出力先。DEBUG_MCPが有効ならstderrに流す。"""
    if os.getenv("DEBUG_MCP"):
        return sys.stderr
    return get_devnull()

# ---------------------------------------------------------
# 定数・設定
# ---------------------------------------------------------
GENERAL_DESCRIPTION = """
# Parent MCP Server Overview
これは複数の「子MCPサーバー(Children)」を動的に管理する「親MCPサーバー(Parent)」です。
ユーザーの要求に応じて適切な子サーバーを起動し、ツールを実行することで、
コンテキストサイズを節約しつつ、多様な機能を提供します。
リソースの中に使用したい子サーバーがあったときは、
get_schemaを使ってスキーマを確認し、execute_child_toolでツールを実行してください。
"""

# グローバルconfig変数（起動時に設定）
_CHILDREN_CONFIG: Optional[dict] = None
_ACTIVE_SESSIONS: Dict[str, Dict[str, Any]] = {}
_CHILDREN_STATUS: Dict[str, Dict[str, Any]] = {}


# ---------------------------------------------------------
# ヘルパー関数
# ---------------------------------------------------------

def parse_children_config(config_path: str) -> dict:
    """
    子MCPサーバーの設定ファイルをパースします。
    .jsonまたは.tomlファイルに対応しています。

    Args:
        config_path: 設定ファイルのパス

    Returns:
        パースされた設定辞書（mcpServersキーを含む）

    Raises:
        ChildrenConfigError: ファイルが存在しない、拡張子が不正、パースエラーの場合
    """
    # ファイルの存在確認
    if not os.path.exists(config_path):
        raise ChildrenConfigError(f"Config file not found: {config_path}")

    # 拡張子の確認
    if config_path.endswith('.json'):
        # JSONファイルをパース
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
        except json.JSONDecodeError as e:
            raise ChildrenConfigError(
                f"Failed to parse JSON config file '{config_path}': {e}"
            ) from e
        except Exception as e:
            raise ChildrenConfigError(
                f"Failed to read config file '{config_path}': {e}"
            ) from e

    elif config_path.endswith('.toml'):
        # TOMLファイルをパース
        if tomllib is None:
            raise ChildrenConfigError(
                "TOML support is not available. "
                "Please install 'tomli' package (pip install tomli) or use Python 3.11+."
            )

        try:
            with open(config_path, 'rb') as f:
                config = tomllib.load(f)
        except Exception as e:
            raise ChildrenConfigError(
                f"Failed to parse TOML config file '{config_path}': {e}"
            ) from e

    else:
        raise ChildrenConfigError(
            f"Config file must be .json or .toml, got: {config_path}"
        )

    # mcpServersキーまたはmcp_serversキーの確認
    if 'mcpServers' not in config and 'mcp_servers' not in config:
        raise ChildrenConfigError(
            "Config file must contain 'mcpServers' (JSON) or 'mcp_servers' (TOML) key."
        )

    # TOMLの場合、mcp_serversをmcpServersに変換
    if 'mcp_servers' in config:
        config['mcpServers'] = config.pop('mcp_servers')

    return config


def resolve_children_config_path(argv=None) -> str:
    """
    CLI引数から--children-configで指定された設定ファイルパスを解決します。

    Args:
        argv: コマンドライン引数（Noneの場合はsys.argvを使用）

    Returns:
        設定ファイルのパス

    Raises:
        SystemExit: --children-configが指定されていない場合
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--children-config",
        dest="children_config",
        required=True,
        help="Path to children config file (.json or .toml)",
    )

    try:
        args, _ = parser.parse_known_args(argv)
    except SystemExit:
        print(
            "Error: --children-config argument is required. "
            "Please specify a .json or .toml config file.",
            file=sys.stderr
        )
        sys.exit(1)

    return args.children_config

def resolve_children_abstract_path(argv=None):
    """CLI引数から--children-abstractで指定された.jsonファイルパスを解決する（未指定ならNone）。"""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--children-abstract",
        dest="children_abstract",
        help="Path to children_abstract.json (provides summaries for child servers).",
    )
    args, _ = parser.parse_known_args(argv)

    if args.children_abstract:
        # .json拡張子のチェック
        if not args.children_abstract.endswith('.json'):
            print(
                f"Warning: --children-abstract must specify a .json file. Got: {args.children_abstract}",
                file=sys.stderr,
            )
            return None
        return args.children_abstract

    return None


CHILDREN_ABSTRACT_PATH: Optional[str] = None


def get_config() -> dict:
    """グローバル設定を取得します。"""
    if _CHILDREN_CONFIG is None:
        raise RuntimeError("Children config not loaded. Server not properly initialized.")
    return _CHILDREN_CONFIG


def truncate_output(text: str, head: Optional[int], tail: Optional[int]) -> str:
    """指定された文字数に基づいてテキストを切り詰めるヘルパー関数"""
    if head is None and tail is None:
        return text

    h = head if head is not None else tail
    t = tail if tail is not None else head

    if len(text) <= (h + t):
        return text

    omitted_count = len(text) - (h + t)
    return f"{text[:h]}\n...({omitted_count} characters omitted)...\n{text[-t:]}"

def normalize_trim_value(value, name: str) -> Optional[int]:
    """head_chars/tail_charsを安全にintへ変換する。"""
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a non-negative integer (got {value!r})")
    if parsed < 0:
        raise ValueError(f"{name} must be a non-negative integer (got {parsed})")
    return parsed


# ---------------------------------------------------------
# Session management: 常駐セッション + 排他制御（lockつき）
# ---------------------------------------------------------
# 子サーバーごとにセッションを起動して保持し、asyncio.Lockで同時実行を抑制する。
#
# 【設計方針】
# - 各子サーバー専用のLockを持つことで、異なる子サーバー同士は並列動作可能
# - 同じ子サーバーへの複数リクエストは、Lockによって順次実行される
# - これにより、パイプ（stdin/stdout）の読み書き競合を防ぐ
#
# 【データ構造】
# _ACTIVE_SESSIONS = {
#     "child_name": {
#         "session": ClientSession,  # MCPセッション
#         "lock": asyncio.Lock(),    # 排他制御用（この子専用）
#         "stack": AsyncExitStack,   # クリーンアップ管理
#     }
# }
#
# _CHILDREN_STATUS = {
#     "child_name": {
#         "status": "running" | "starting" | "failed_start" | "stopped",
#         "error": Optional[str],
#         "started_at": Optional[float],
#     }
# }

async def start_child_session(child_name: str) -> bool:
    """
    指定した子サーバーのセッションを起動（既に起動済みなら何もしない）。

    Args:
        child_name: 起動する子サーバーの名前

    Returns:
        True: セッションが利用可能（起動成功 or 既に起動済み）
        False: 起動失敗（設定不足やプロセス起動エラー）

    Note:
        - 冪等性: 既に起動済みの場合は何もせずTrueを返す
        - エラー時は _CHILDREN_STATUS にエラー情報を記録
        - 起動成功時は _ACTIVE_SESSIONS に登録（session, lock, stack）
    """
    config = get_config()
    child_conf = config.get("mcpServers", {}).get(child_name)
    if not child_conf:
        return False

    if child_name in _ACTIVE_SESSIONS:
        status_entry = _CHILDREN_STATUS.setdefault(child_name, {})
        status_entry.update({"status": "running", "error": None})
        return True

    status_entry = _CHILDREN_STATUS.setdefault(child_name, {})
    status_entry.update({"status": "starting", "error": None, "started_at": None})

    stack = AsyncExitStack()
    try:
        env = os.environ.copy()
        env.update(child_conf.get("env", {}))

        params = StdioServerParameters(
            command=child_conf["command"],
            args=child_conf.get("args", []),
            env=env,
        )

        read, write = await stack.enter_async_context(
            stdio_client(params, errlog=get_errlog())
        )
        session = await stack.enter_async_context(ClientSession(read, write))
        await asyncio.wait_for(session.initialize(), timeout=30.0)

        status_entry.update({"status": "running", "error": None, "started_at": time.time()})
        _ACTIVE_SESSIONS[child_name] = {
            "session": session,
            "lock": asyncio.Lock(),
            "stack": stack,
        }
        logger.info("Child server '%s' started and session initialized.", child_name)
        return True
    except Exception as exc:
        status_entry.update({"status": "failed_start", "error": f"{type(exc).__name__}: {exc}"})
        logger.exception("Failed to start child server '%s'", child_name)
        _ACTIVE_SESSIONS.pop(child_name, None)
        try:
            await stack.aclose()
        except Exception:
            logger.debug("Cleanup failed for child '%s' after startup error.", child_name)
        return False


async def stop_child_session(child_name: str) -> None:
    """
    指定した子サーバーのセッションを停止し、状態を更新する。

    Args:
        child_name: 停止する子サーバーの名前

    Note:
        - _ACTIVE_SESSIONS から削除し、AsyncExitStack でクリーンアップ
        - _CHILDREN_STATUS を "stopped" に更新
        - 既に停止済みの場合は何もしない（冪等性）
        - lockを取得してから停止するため、実行中のツールが完了してから停止
    """
    info = _ACTIVE_SESSIONS.pop(child_name, None)
    status_entry = _CHILDREN_STATUS.setdefault(child_name, {})
    if not info:
        status_entry["status"] = "stopped"
        return

    lock: asyncio.Lock = info["lock"]
    async with lock:
        try:
            await info["stack"].aclose()
            logger.debug("Closed child server '%s'.", child_name)
        except BaseException:
            logger.exception("Error while closing child server '%s'", child_name)
    status_entry.update({"status": "stopped"})


@asynccontextmanager
async def server_lifespan(app):
    """
    サーバーの起動・終了時の処理を管理（常駐セッション管理）

    起動時:
        - 全ての子サーバーのセッションを起動（start_child_session）
        - 各セッションは専用のlock付きで _ACTIVE_SESSIONS に登録

    終了時:
        - 全ての子サーバーのセッションを停止（stop_child_session）
        - AsyncExitStack でクリーンアップ
        - DEVNULLハンドルをクローズ
    """
    global _devnull_handle

    logger.debug("Initializing server lifespan.")
    config = get_config()

    try:
        # 起動時に全子サーバーのセッションを初期化
        for child_name, child_conf in config.get("mcpServers", {}).items():
            await start_child_session(child_name)

        yield {}
    finally:
        # セッション終了処理
        for child_name in list(_ACTIVE_SESSIONS.keys()):
            try:
                await stop_child_session(child_name)
            except BaseException:
                logger.exception("Error while closing child server '%s'", child_name)
        _ACTIVE_SESSIONS.clear()

        # DEVNULLハンドルをクローズ
        if _devnull_handle is not None and not _devnull_handle.closed:
            try:
                _devnull_handle.close()
            except Exception:
                pass
        logger.debug("Server lifespan cleanup completed.")


# Parent Serverの初期化（lifespanを指定）
mcp = FastMCP("ParentManager", lifespan=server_lifespan)


# ---------------------------------------------------------
# Resources: Parentの情報提供
# ---------------------------------------------------------

@mcp.resource("mcp://server_summary")
def get_server_summary() -> str:
    """このParentサーバーの概要を返します。"""
    return GENERAL_DESCRIPTION.strip()


@mcp.resource("mcp://children_servers")
def get_children_servers_resource() -> str:
    """登録されている子MCPサーバーの概要情報を返します。"""
    if not CHILDREN_ABSTRACT_PATH:
        return "{}"

    if not os.path.exists(CHILDREN_ABSTRACT_PATH):
        return "{}"

    try:
        with open(CHILDREN_ABSTRACT_PATH, "r", encoding="utf-8") as f:
            content = f.read()

        # JSONとして検証
        json.loads(content)

        return content
    except json.JSONDecodeError:
        return "{}"
    except Exception:
        return "{}"


# ---------------------------------------------------------
# Tools
# ---------------------------------------------------------

@mcp.tool()
def list_registered_children() -> str:
    """登録されている子MCPサーバーの名前一覧を取得します。"""
    config = get_config()
    names = list(config.get("mcpServers", {}).keys())
    if not names:
        return "No child servers registered."
    return "\n".join(f"- {name}" for name in names)


@mcp.tool()
def get_child_status() -> str:
    """
    登録済みの子サーバーの状態を返します（起動済み/失敗/停止中など）。
    """
    config = get_config()
    statuses = []

    for child_name, child_conf in config.get("mcpServers", {}).items():
        status_entry = _CHILDREN_STATUS.get(child_name, {})
        running = child_name in _ACTIVE_SESSIONS
        started_at = status_entry.get("started_at")
        statuses.append(
            {
                "name": child_name,
                "status": status_entry.get("status", "not_started"),
                "running": running,
                "command": child_conf.get("command"),
                "args": child_conf.get("args", []),
                "started_at": (
                    datetime.fromtimestamp(started_at).isoformat()
                    if started_at
                    else None
                ),
                "error": status_entry.get("error"),
            }
        )

    return json.dumps(statuses, indent=2, ensure_ascii=False)





@mcp.tool()
async def close_child_session(child_name: str) -> str:
    """
    指定した子サーバーのセッションをクローズする。

    Note:
        - stop_child_sessionを呼び出し、セッションを停止
        - lockを取得してから停止するため、実行中のツールが完了してから停止
        - 停止後、再度ツールを実行すると自動的に再起動される（start_child_sessionの冪等性）
    """
    await stop_child_session(child_name)
    status_entry = _CHILDREN_STATUS.get(child_name, {})
    return json.dumps(
        {
            "server": child_name,
            "status": status_entry.get("status", "stopped"),
            "error": status_entry.get("error"),
        },
        indent=2,
        ensure_ascii=False,
    )


@mcp.tool()
async def get_schema(child_name: str) -> str:
    """
    指定した子サーバーのToolsとResourcesの定義情報を取得します。
    常駐セッション（lockつき）を使用してスキーマを取得します。
    セッションが未起動の場合は起動を試みます。
    実際のツール実行にはexecute_child_toolを使用してください。

    Args:
        child_name: スキーマを取得する子サーバーの名前
    """
    config = get_config()
    child_conf = config.get("mcpServers", {}).get(child_name)
    if not child_conf:
        return f"Error: child server '{child_name}' not found in config."

    # セッションが未起動なら起動を試みる
    await start_child_session(child_name)

    status_entry = _CHILDREN_STATUS.get(child_name, {})
    active = _ACTIVE_SESSIONS.get(child_name)

    if not active:
        return json.dumps(
            {
                "server": child_name,
                "status": status_entry.get("status", "failed_start"),
                "error": status_entry.get("error", "session not available"),
            },
            indent=2,
            ensure_ascii=False,
        )

    session = active["session"]
    lock: asyncio.Lock = active["lock"]

    schema_info = {
        "server": child_name,
        "status": "running",
        "tools": [],
        "resources": [],
        "errors": [],
    }

    logger.debug("Fetching schema for child '%s' using persistent session", child_name)

    async with lock:
        # ツール一覧を取得
        try:
            tools_response = await session.list_tools()
            schema_info["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.inputSchema,
                }
                for t in tools_response.tools
            ]
        except Exception as e:
            logger.exception("list_tools failed for '%s'", child_name)
            schema_info["errors"].append(f"list_tools failed: {str(e)}")

        # リソース一覧を取得（存在すれば）
        try:
            resources_response = await session.list_resources()
            schema_info["resources"] = [
                {
                    "uri": str(r.uri),
                    "name": r.name,
                    "description": r.description,
                }
                for r in resources_response.resources
            ]
        except Exception as e:
            logger.debug("list_resources failed or unsupported for '%s': %s", child_name, e)

    if not schema_info["errors"]:
        schema_info.pop("errors", None)

    return json.dumps(schema_info, indent=2, ensure_ascii=False)


@mcp.tool()
async def execute_child_tool(
    child_name: str,
    tool_name: str,
    tool_args: Dict[str, Any],
    head_chars: Optional[int] = None,
    tail_chars: Optional[int] = None
) -> str:
    """
    子サーバーのツールを実行します（常駐セッション・lockつき）。
    結果が長大な場合、head_chars/tail_charsを指定して出力を要約できます。

    Args:
        child_name: 子サーバーの名前
        tool_name: 実行したいツールの名前
        tool_args: ツールに渡す引数オブジェクト (JSON)
        head_chars: 結果の先頭から取得する文字数 (省略可)
        tail_chars: 結果の末尾から取得する文字数 (省略可)。片方のみ指定した場合、もう一方も同じ値になります。
    """
    config = get_config()
    child_conf = config.get("mcpServers", {}).get(child_name)
    if not child_conf:
        return f"Error executing tool '{tool_name}' on '{child_name}': config not found."

    try:
        head = normalize_trim_value(head_chars, "head_chars")
        tail = normalize_trim_value(tail_chars, "tail_chars")
    except ValueError as ve:
        logger.warning(
            "Invalid trim parameters for '%s/%s': head=%r tail=%r (%s)",
            child_name, tool_name, head_chars, tail_chars, ve,
        )
        return f"Error executing tool '{tool_name}' on '{child_name}': {ve}"

    # セッションが未起動なら起動を試みる（lifespanで起動済みの場合は何もしない）
    await start_child_session(child_name)

    status_entry = _CHILDREN_STATUS.get(child_name, {})
    active = _ACTIVE_SESSIONS.get(child_name)
    if not active:
        logger.error("Child server '%s' is not running (session not initialized).", child_name)
        status_label = status_entry.get("status", "not_running")
        error_detail = status_entry.get("error")
        msg = (
            f"Error executing tool '{tool_name}' on '{child_name}': "
            f"child server is not running (status={status_label})."
        )
        if error_detail:
            msg += f" Last error: {error_detail}"
        return msg

    session = active["session"]
    lock: asyncio.Lock = active["lock"]

    logger.debug("Executing tool '%s' on child '%s' using persistent session with lock", tool_name, child_name)

    # lockを使って排他制御（同じ子サーバーへの同時アクセスを防ぐ）
    async with lock:
        try:
            result = await asyncio.wait_for(
                session.call_tool(tool_name, arguments=tool_args),
                timeout=60.0
            )
        except asyncio.TimeoutError:
            logger.error("Timeout (60s) executing tool '%s/%s'", child_name, tool_name)
            return f"Error executing tool '{tool_name}' on '{child_name}': Timeout after 60 seconds"
        except Exception as e:
            logger.exception("Error executing tool '%s/%s'", child_name, tool_name)
            return f"Error executing tool '{tool_name}' on '{child_name}': {type(e).__name__}: {str(e)}"

    # 結果を整形
    full_output_parts = []
    for content in result.content:
        if content.type == "text":
            full_output_parts.append(content.text)
        elif content.type == "image":
            full_output_parts.append(f"[Image Data: {content.mimeType}]")
        else:
            full_output_parts.append(str(content))

    full_text = "\n".join(full_output_parts)
    return truncate_output(full_text, head, tail)




def init_parent_server(config: dict, children_abstract_path: Optional[str] = None) -> None:
    """
    親MCPサーバーのグローバル設定を初期化する。
    他のモジュールから利用する場合もここを呼び出す。
    """
    global _CHILDREN_CONFIG, CHILDREN_ABSTRACT_PATH
    _CHILDREN_CONFIG = config
    CHILDREN_ABSTRACT_PATH = children_abstract_path


def main(argv=None):
    global _CHILDREN_CONFIG
    setup_logging()
    # --children-configからconfigファイルパスを取得してパース
    config_path = resolve_children_config_path(argv)
    try:
        _CHILDREN_CONFIG = parse_children_config(config_path)
    except ChildrenConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    children_abstract_path = resolve_children_abstract_path(argv)
    if not children_abstract_path:
        print(
            "Warning: --children-abstract not provided. "
            "get_children_abstract tool will not be available.",
            file=sys.stderr,
        )

    init_parent_server(_CHILDREN_CONFIG, children_abstract_path)

    mcp.run(transport='stdio')


if __name__ == "__main__":
    main()
