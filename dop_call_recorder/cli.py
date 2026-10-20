"""`dop-recorder` command line entry point."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dop_call_recorder import __version__, config as cfgmod, log
from dop_call_recorder.transcribe import AudioError, BackendError


def cmd_transcribe(args, cfg) -> int:
    from dop_call_recorder.transcribe import transcribe_file

    if args.model:
        cfg["model"] = args.model
    if getattr(args, "backend", None):
        cfg["backend"] = args.backend
    if getattr(args, "save", False):
        from dop_call_recorder import reprocess

        print(reprocess.save_file(cfg, args.file, title=args.title))
        return 0
    res = transcribe_file(args.file, cfg)
    if args.json:
        res["segments"] = [vars(s) for s in res["segments"]]
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(res["text"])
    return 0


def cmd_config(args, cfg) -> int:
    print(json.dumps(cfg, ensure_ascii=False, indent=2))
    return 0


def cmd_detect(args, cfg) -> int:
    from dop_call_recorder import detector

    return detector.main(args.rest) or 0


def cmd_ax(args, cfg) -> int:
    """X1 diagnostics: Accessibility state / parsed Teams state / raw AX tree dump (via the helper app)."""
    import json as _json

    from dop_call_recorder import axpoll

    if not axpoll.helper_built():
        print("axhelper not built: run app/build.sh", file=sys.stderr)
        return 1
    if args.what == "check":
        r = axpoll.run_helper(["check"])
    elif args.what == "request":
        r = axpoll.run_helper(["request"], timeout=60)
    elif args.what == "snapshot":
        raw = axpoll.run_helper(["snapshot"], 25)
        if not raw or raw.get("error"):
            print(_json.dumps(raw or {"error": "no answer"}))
            return 3
        from dop_call_recorder import axparse
        r = axparse.parse(raw.get("trees", []))
    else:
        r = axpoll.run_helper(["dump"], 60)
        if r is None:
            print("no answer from axhelper", file=sys.stderr)
            return 3
        if args.out:
            Path(args.out).write_text(_json.dumps(r, indent=1, ensure_ascii=False), encoding="utf-8")
            print(f"wrote {args.out}")
            return 0
    print(_json.dumps(r, indent=2, ensure_ascii=False))
    return 0 if r and not r.get("error") else 3


def cmd_apps(args, cfg) -> int:
    from dop_call_recorder import recorder_bridge

    return recorder_bridge.main(["--all"] if args.all else []) or 0


def cmd_run(args, cfg) -> int:
    from dop_call_recorder import pipeline

    return pipeline.run(cfg, recover_only=args.recover_only)


def cmd_process(args, cfg) -> int:
    from pathlib import Path

    from dop_call_recorder import pipeline

    if args.backend:
        cfg["backend"] = args.backend
    out = pipeline.process_folder(cfg, Path(args.dir).expanduser(), keep_raw=args.keep_raw)
    print(out)
    return 0


def cmd_index(args, cfg) -> int:
    from dop_call_recorder import search

    print(json.dumps(search.update(cfg, rebuild=args.rebuild)))
    return 0


def cmd_search(args, cfg) -> int:
    from dop_call_recorder import search

    try:
        hits = search.search(cfg, args.query, since=args.since, app=args.app, speaker=args.speaker, lang=args.lang,
                             limit=args.limit)
    except search.SearchError as e:
        print(f"dop-recorder: {e}", file=sys.stderr)
        return 2
    if args.json:
        for h in hits:
            h["snip"] = h["snip"].replace(search.MARK_A, "**").replace(search.MARK_B, "**")
            h["timestamp"] = search.hms(h["ts"])
            if args.open:
                h["link"] = search.link(h, args.open)
        print(json.dumps(hits, ensure_ascii=False, indent=2))
    else:
        print(search.format_hits(hits, color=sys.stdout.isatty(), open_kind=args.open))
    return 0


def cmd_list(args, cfg) -> int:
    from dop_call_recorder import search

    try:
        rows = search.list_calls(cfg, since=args.since, limit=args.limit)
    except search.SearchError as e:
        print(f"dop-recorder: {e}", file=sys.stderr)
        return 2
    print(json.dumps(rows, ensure_ascii=False, indent=2) if args.json else search.format_calls(rows))
    return 0


def cmd_reprocess(args, cfg) -> int:
    from dop_call_recorder import reprocess

    try:
        print(reprocess.reprocess(cfg, args.target, model=args.model, backend_name=args.backend,
                                  vocab_file=args.vocab_file))
    except reprocess.ReprocessError as e:
        print(f"dop-recorder: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_status(args, cfg) -> int:
    from dop_call_recorder import status

    s = status.gather(cfg)
    print(json.dumps(s, ensure_ascii=False, indent=2) if args.json else status.render(s))
    return 0 if s["running"] else 3


def cmd_pause(args, cfg) -> int:
    from dop_call_recorder import agentstate

    agentstate.set_paused(True)
    print("paused: no new recordings; any running recording is stopped + queued for transcription (agent applies this within ~2 s)")
    return 0


def cmd_resume(args, cfg) -> int:
    from dop_call_recorder import agentstate

    agentstate.set_paused(False)
    print("resumed")
    return 0


def cmd_setup(args, cfg) -> int:
    from dop_call_recorder import service

    try:
        return service.setup(cfg, prompt=not args.no_prompt, fetch_model=not args.no_model)
    except Exception as e:
        print(f"dop-recorder: setup failed: {e}", file=sys.stderr)
        return 1


def cmd_start(args, cfg) -> int:
    from dop_call_recorder import service

    return service.start(cfg, menubar=not args.no_menubar, dry_run=args.dry_run)


def cmd_stop(args, cfg) -> int:
    from dop_call_recorder import service

    return service.stop()


def cmd_uninstall(args, cfg) -> int:
    from dop_call_recorder import service

    return service.uninstall(cfg, purge=args.purge, purge_models=args.purge_models)


def cmd_doctor(args, cfg) -> int:
    from dop_call_recorder import doctor

    checks = doctor.run_all(cfg)
    if args.json:
        print(json.dumps([vars(c) for c in checks], ensure_ascii=False, indent=2))
    else:
        print(doctor.render(checks))
    return doctor.exit_code(checks)


def cmd_permissions(args, cfg) -> int:
    from dop_call_recorder import health

    if args.what == "fix":
        r = health.fix(args.id, cfg)
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r["ok"] else 1
    doc = health.document(cfg, probe=args.probe)
    if args.write:
        health.write(doc)
    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        for i in doc["items"]:
            print(f"{doctor_icon(i['status'])} [{i['group']}] {i['name']}: {i['detail']}" + (f"\n     fix: {i['action']['label']}" if i.get("action") else ""))
        print(f"\n{doc['summary']['title']}")
    return 1 if doc["summary"]["attention"] else 0


def doctor_icon(st: str) -> str:
    from dop_call_recorder import doctor

    return doctor.ICON[st]


def main(argv=None) -> int:
    from dop_call_recorder import paths
    paths.ensure_path()
    ap = argparse.ArgumentParser(prog="dop-recorder", description="DOP Call Recorder")
    ap.add_argument("--version", action="version", version=f"dop-recorder {__version__}")
    ap.add_argument("--config", help="path to config.toml (default: <state dir>/config.toml, else the packaged config.toml; $DOP_CR_CONFIG)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("transcribe", help="transcribe an audio file and print the text")
    t.add_argument("file")
    t.add_argument("--model", help="override model name from config")
    t.add_argument("--backend", help="override backend from config (openai-whisper | mlx-whisper | whisper-cpp)")
    t.add_argument("--json", action="store_true", help="print full JSON with segments")
    t.add_argument("--save", action="store_true", help="process like a call: folder in output_dir, md/vtt/meta, indexed")
    t.add_argument("--title", help="with --save: call title (default: from filename)")
    t.set_defaults(fn=cmd_transcribe)
    c = sub.add_parser("config", help="print effective configuration")
    c.set_defaults(fn=cmd_config)
    a = sub.add_parser("apps", help="list running apps using audio (JSON, via the recorder app)")
    a.add_argument("--all", action="store_true", help="include idle audio clients")
    a.set_defaults(fn=cmd_apps)
    r = sub.add_parser("run", help="foreground loop: detector -> recorder -> pipeline (recovers orphaned sessions first)")
    r.add_argument("--recover-only", action="store_true", help="only recover/transcribe orphaned sessions, then exit")
    r.set_defaults(fn=cmd_run)
    pr = sub.add_parser("process", help="finalize + transcribe a session dir or a recorded folder (others_NNNN.wav/me_NNNN.wav)")
    pr.add_argument("dir")
    pr.add_argument("--keep-raw", action="store_true", help="do not delete raw WAV chunks afterwards")
    pr.add_argument("--backend", help="override backend")
    pr.set_defaults(fn=cmd_process)
    ix = sub.add_parser("index", help="update the FTS5 search index from the transcripts on disk")
    ix.add_argument("--rebuild", action="store_true", help="drop and rebuild from scratch")
    ix.set_defaults(fn=cmd_index)
    se = sub.add_parser("search", help="full-text search (FTS5 syntax: \"phrase\", prefix*, AND/OR/NOT)")
    se.add_argument("query")
    se.add_argument("--since", help="7d | 2w | 3m | YYYY-MM-DD")
    se.add_argument("--app", help="e.g. teams, zoom")
    se.add_argument("--speaker", help="Me | Others | Mixed | <name>")
    se.add_argument("--lang", help="e.g. en")
    se.add_argument("--limit", type=int, default=20)
    se.add_argument("--json", action="store_true")
    se.add_argument("--open", nargs="?", const="vscode", choices=["vscode", "file"],
                    help="print a vscode://file/...:line (default) or file:// link per hit")
    se.set_defaults(fn=cmd_search)
    ls = sub.add_parser("list", help="recent calls")
    ls.add_argument("--since", help="7d | 2w | 3m | YYYY-MM-DD")
    ls.add_argument("--limit", type=int, default=50)
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(fn=cmd_list)
    rp = sub.add_parser("reprocess", help="re-transcribe a finished call from its retained call.m4a")
    rp.add_argument("target", help="call dir, or call id (folder name / unique substring, e.g. 2026-10-01_1400)")
    rp.add_argument("--model")
    rp.add_argument("--backend")
    rp.add_argument("--vocab-file")
    rp.set_defaults(fn=cmd_reprocess)
    st_ = sub.add_parser("status", help="agent state, active session, transcription progress/ETA, queue (exit 3 if agent not running)")
    st_.add_argument("--json", action="store_true")
    st_.set_defaults(fn=cmd_status)
    sub.add_parser("pause", help="pause recording (stops a running recording cleanly; transcription continues)").set_defaults(fn=cmd_pause)
    sub.add_parser("resume", help="resume call detection/recording").set_defaults(fn=cmd_resume)
    ax = sub.add_parser("ax", help="X1: Teams Accessibility helper: check | request | snapshot (parsed title/roster/speaking/self) | dump (raw tree)")
    ax.add_argument("what", choices=["check", "request", "snapshot", "dump"])
    ax.add_argument("--out", help="dump: write the raw tree JSON here (it contains on-screen Teams text!)")
    ax.set_defaults(fn=cmd_ax)
    dr = sub.add_parser("doctor", help="check environment, models, recorder, permissions, launchd (exit 1 on failures)")
    dr.add_argument("--json", action="store_true")
    dr.set_defaults(fn=cmd_doctor)
    pm = sub.add_parser("permissions", help="permissions / health model of the menu-bar submenu: [--json] [--probe] [--write] | fix <id>")
    pm.add_argument("what", nargs="?", choices=["show", "fix"], default="show")
    pm.add_argument("id", nargs="?")
    pm.add_argument("--json", action="store_true")
    pm.add_argument("--probe", action="store_true", help="also run the 1 s system-audio tap probe (skipped during a recording)")
    pm.add_argument("--write", action="store_true", help="write <state_dir>/health.json for the menu app")
    pm.set_defaults(fn=cmd_permissions)
    su = sub.add_parser("setup", help="one-time per-user setup: copy the apps to ~/Applications, fetch the model, request permissions")
    su.add_argument("--no-model", action="store_true", help="skip the model download/convert")
    su.add_argument("--no-prompt", action="store_true", help="do not trigger the macOS permission prompts")
    su.set_defaults(fn=cmd_setup)
    sa = sub.add_parser("start", help="start the background agent (+ menu-bar icon) via launchd; refuses while permissions are missing")
    sa.add_argument("--no-menubar", action="store_true", help="agent only, no menu-bar icon")
    sa.add_argument("--dry-run", action="store_true", help="render + lint the LaunchAgent plists and report the gate, change nothing")
    sa.set_defaults(fn=cmd_start)
    sub.add_parser("stop", help="stop the agent + menu-bar app and remove their LaunchAgents").set_defaults(fn=cmd_stop)
    un = sub.add_parser("uninstall", help="stop, remove the apps and reset macOS permissions (transcripts are never deleted)")
    un.add_argument("--purge", action="store_true", help="also delete state (sessions/raw audio) and logs")
    un.add_argument("--purge-models", action="store_true", help="also delete downloaded MLX models")
    un.set_defaults(fn=cmd_uninstall)
    d = sub.add_parser("detect", help="call detector (--watch, --app, --interval ...)", add_help=False)
    d.add_argument("rest", nargs=argparse.REMAINDER)
    d.set_defaults(fn=cmd_detect)
    argv = sys.argv[1:] if argv is None else list(argv)
    first_sub = next((a for a in argv if a in sub.choices), None)
    if first_sub == "detect":
        i = argv.index("detect")
        args = ap.parse_args(argv[:i + 1])
        args.rest = argv[i + 1:]
    else:
        args = ap.parse_args(argv)
    cfg = cfgmod.load(args.config)
    log.setup(cfg["logging"]["level"], cfg["logging"]["file"],
              int(cfg["logging"].get("max_mb", 10)) * 1024 * 1024, int(cfg["logging"].get("backups", 5)))
    try:
        return args.fn(args, cfg)
    except (AudioError, BackendError) as e:
        print(f"dop-recorder: error: {e}", file=sys.stderr)
        return 1
    except Exception:
        log.get("cli").exception("command_failed", extra={"cmd": args.cmd})
        return 1


if __name__ == "__main__":
    sys.exit(main())
