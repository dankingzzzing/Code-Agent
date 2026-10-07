"""Command line interface with batch review, interactive memory and Web launch."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .app import create_agent, save_session
from .config import Config
from .errors import AgentError


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Code Agent：代码审查、工具调用和上下文记忆")
    result.add_argument("--version", action="version", version=__version__)
    commands = result.add_subparsers(dest="command", required=True)

    def common(command):
        command.add_argument("--root", type=Path, default=Path.cwd(), help="工具可读取的工作区根目录")
        command.add_argument("--demo", action="store_true", help="使用离线规则规划器（不调用 LLM）")
        command.add_argument("--allow-exec", action="store_true", help="允许执行工作区中可信的 unittest 测试")

    review = commands.add_parser("review", help="审查文件/目录并输出报告")
    common(review)
    review.add_argument("target", nargs="?", default="examples/buggy", help="相对工作区的目标路径")
    review.add_argument("--task", default="审查代码，指出潜在问题，并验证测试。")
    review.add_argument("--session", help="保存/继续本地会话，例如 homework1")
    review.add_argument("--json", action="store_true", help="输出含工具轨迹的 JSON")
    review.add_argument("--output", type=Path, help="将 Markdown 或 JSON 报告写入文件")
    review.add_argument("--trace", action="store_true", help="在 stderr 输出行动轨迹")
    chat = commands.add_parser("chat", help="多轮交互，/exit 退出")
    common(chat)
    chat.add_argument("--target", default="examples/buggy")
    chat.add_argument("--session", default="chat")
    serve = commands.add_parser("serve", help="启动本机 Web 界面")
    common(serve)
    serve.add_argument("--port", type=int, default=8765)
    doctor = commands.add_parser("doctor", help="检查配置，不发送 API 请求")
    doctor.add_argument("--root", type=Path, default=Path.cwd())
    return result


def _trace(event):
    print(f"[{event.step:02d} {event.kind}] {event.summary}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        if not args.root.is_dir():
            raise AgentError("--root 必须指向存在的工作区目录。")
        if args.command == "doctor":
            config = Config.from_env(args.root)
            print(json.dumps({"python": sys.version.split()[0], "version": __version__,
                              "root": str(args.root.resolve()), "demo_ready": True,
                              "llm_key_configured": bool(config.api_key), "model": config.model,
                              "api_style": config.api_style, "base_url": config.base_url,
                              "llm_ready": bool(config.api_key and config.model)}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "serve":
            if not 1 <= args.port <= 65535:
                raise AgentError("端口必须在 1 至 65535 之间。")
            from .web import serve
            serve(args.root, demo=args.demo, allow_exec=args.allow_exec, port=args.port)
            return 0
        agent = create_agent(args.root, demo=args.demo, allow_exec=args.allow_exec, session=args.session)
        if args.command == "chat":
            print(f"{agent.provider.label}；目标 {args.target}；会话 {args.session}。/exit 退出，/reset 清空记忆。")
            while True:
                try:
                    task = input("\n你 > ").strip()
                except EOFError:
                    break
                if task == "/exit":
                    break
                if task == "/reset":
                    agent.memory.turns.clear()
                    save_session(agent, args.session)
                    print("记忆已清空。")
                    continue
                if not task:
                    continue
                try:
                    result = agent.run(task, args.target, on_event=_trace)
                    save_session(agent, args.session)
                    print("\n" + result.answer)
                except AgentError as exc:
                    print(f"错误：{exc}", file=sys.stderr)
            return 0
        result = agent.run(args.task, args.target, on_event=_trace if args.trace else None)
        save_session(agent, args.session)
        output = json.dumps(result.to_dict(), ensure_ascii=False, indent=2) if args.json else result.answer
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(output + "\n", encoding="utf-8")
        print(output)
        return 0 if result.status == "completed" else 2
    except (AgentError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已停止。", file=sys.stderr)
        return 130
