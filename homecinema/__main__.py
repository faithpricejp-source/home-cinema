"""命令行入口：python -m homecinema scan | fetch-metadata | serve | detect-segments | recommend | community-segments | fetch-extras | fetch-titles。"""

from __future__ import annotations

import argparse
import sys

from .config import load_config
from .db import Library


def cmd_scan(cfg, _args) -> int:
    from .scanner import run_scan
    db = Library(cfg.db_file)
    try:
        stats = run_scan(cfg, db)
    finally:
        db.close()
    print(f"扫描完成：电影 {stats['movies']} 部、剧集文件 {stats['episodes']} 集；"
          f"标记缺失：电影 {stats['missing_movies']}、剧集 {stats['missing_episodes']}")
    return 0


def cmd_fetch_metadata(cfg, args) -> int:
    from .metadata import MetadataService, TmdbClient
    key = cfg.read_tmdb_key()
    if not key:
        print(f"错误：TMDB key 文件不存在或为空（{cfg.tmdb_key_path}）", file=sys.stderr)
        return 2
    db = Library(cfg.db_file)
    try:
        client = TmdbClient(key, cfg.tmdb_language)
        def progress(done: int, total: int, label: str) -> None:
            print(f"[{done}/{total}] {label}", flush=True)

        service = MetadataService(db, client, cfg.images_dir, progress=progress,
                                  refresh=args.refresh, overrides=cfg.read_overrides())

        stats = service.enrich_all()
    finally:
        db.close()
    print(f"元数据完成：匹配 {stats['matched']}，未匹配 {stats['unmatched']}，"
          f"出错 {stats.get('failed', 0)}（共 {stats['total']}）")
    return 0


def cmd_detect_segments(cfg, args) -> int:
    from .detect import run_detect_segments
    db = Library(cfg.db_file)
    try:
        count = run_detect_segments(db, show_id=args.show, limit_seasons=args.limit_seasons)
    finally:
        db.close()
    print(f"片头片尾识别完成：处理 {count} 季")
    return 0


def cmd_recommend(cfg, args) -> int:
    from .metadata import TmdbClient
    from .recommend import generate
    key = cfg.read_tmdb_key()
    if not key:
        print(f"错误：TMDB key 文件不存在或为空（{cfg.tmdb_key_path}）", file=sys.stderr)
        return 2
    db = Library(cfg.db_file)
    try:
        client = TmdbClient(key, cfg.tmdb_language)

        def progress(done: int, total: int, label: str) -> None:
            print(f"[{done}/{total}] {label}", flush=True)

        stats = generate(db, client, cfg.cache_dir_path, cfg.images_dir,
                         refresh=args.refresh, progress=progress,
                         rerank=args.rerank)
    finally:
        db.close()
    print(f"推荐完成：电影 {stats['movies']} 部、剧集 {stats['shows']} 部"
          f"（来源 {stats['sources']} 条）")
    return 0


def cmd_community_segments(cfg, args) -> int:
    from .community import CommunityClient, run_community
    from .metadata import TmdbClient
    if args.limit is not None and not args.retry_none:
        print("错误：--limit 只配合 --retry-none 使用", file=sys.stderr)
        return 2
    key = cfg.read_tmdb_key()
    db = Library(cfg.db_file)
    try:
        tmdb = TmdbClient(key, cfg.tmdb_language) if key else None
        client = CommunityClient(theintrodb_key=cfg.read_theintrodb_key())
        stats = run_community(db, client, tmdb=tmdb,
                              ocr_cache_dir=str(cfg.cache_dir_path / "ocr_tail"),
                              refresh=args.refresh, retry_none=args.retry_none,
                              limit=args.limit)
    finally:
        db.close()
    if args.retry_none:
        print(f"重查 none 完成：重查 {stats['total']} 集，转为 ok {stats['found']}，"
              f"仍 none {stats['none']}，失败 {stats['error']}（失败的下次重跑再查）")
    else:
        print(f"社区片头片尾库查询完成：共 {stats['total']} 集，有数据 {stats['found']}，"
              f"没有 {stats['none']}，失败 {stats['error']}（失败的下次重跑再查）")
    return 0


def cmd_fetch_extras(cfg, args) -> int:
    from .extras import probe_heights, run_extras, run_people_aka
    from .metadata import TmdbClient
    key = cfg.read_tmdb_key()
    if not key:
        print(f"错误：TMDB key 文件不存在或为空（{cfg.tmdb_key_path}）", file=sys.stderr)
        return 2
    db = Library(cfg.db_file)
    try:
        client = TmdbClient(key, cfg.tmdb_language)
        stats = run_extras(db, client, cfg.images_dir, refresh=args.refresh)
        aka = run_people_aka(db, client, refresh=args.refresh)
        heights = probe_heights(db)
    finally:
        db.close()
    print(f"补充数据完成：{stats['ok']} 部成功、{stats['failed']} 部失败（共 {stats['total']}）；"
          f"人名别名 {aka['ok']} 人成功、{aka['failed']} 人失败；补分辨率 {heights} 部")
    return 0


def cmd_fetch_titles(cfg, args) -> int:
    from .metadata import TmdbClient, fill_aka
    key = cfg.read_tmdb_key()
    if not key:
        print(f"错误：TMDB key 文件不存在或为空（{cfg.tmdb_key_path}）", file=sys.stderr)
        return 2
    db = Library(cfg.db_file)
    try:
        def progress(done: int, total: int, label: str) -> None:
            print(f"[{done}/{total}] {label}", flush=True)
        stats = fill_aka(db, TmdbClient(key, cfg.tmdb_language), refresh=args.refresh,
                         progress=progress)
    finally:
        db.close()
    print(f"中文译名完成：{stats['ok']} 部成功、{stats['failed']} 部失败（共 {stats['total']}）")
    return 0


def cmd_serve(cfg, _args) -> int:
    import uvicorn
    from .server import create_app
    app = create_app(config=cfg)
    uvicorn.run(app, host="127.0.0.1", port=cfg.port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="homecinema", description="本机影音库")
    parser.add_argument("--config", default=None, help="config.toml 路径"
                        "（默认项目根的 config.toml，可用环境变量 HOMECINEMA_CONFIG 覆盖）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scan", help="扫描影片目录入库")
    fm = sub.add_parser("fetch-metadata", help="拉取 TMDB 元数据与图片")
    fm.add_argument("--refresh", action="store_true", help="已匹配的也重新匹配")
    sub.add_parser("serve", help="启动网页服务（127.0.0.1）")
    ds = sub.add_parser("detect-segments", help="识别剧集片头/片尾并落库")
    ds.add_argument("--show", type=int, default=None, help="只处理指定剧集 ID")
    ds.add_argument("--limit-seasons", type=int, default=None, help="最多处理 N 季")
    rc = sub.add_parser("recommend", help="按片库和观看记录生成推荐")
    rc.add_argument("--refresh", action="store_true", help="忽略缓存，重新拉取推荐")
    rc.add_argument("--rerank", action="store_true",
                    help="电影推荐第二段按片库口味重排（调用 LLM，只发聚合统计）")
    cs = sub.add_parser("community-segments", help="从社区库（TheIntroDB / IntroDB）查片头片尾")
    cs_mode = cs.add_mutually_exclusive_group()
    cs_mode.add_argument("--refresh", action="store_true", help="已查过的也重新查")
    cs_mode.add_argument("--retry-none", action="store_true",
                         help="只重查库里记为 none 的集（按查询时间从旧到新），与 --refresh 互斥")
    cs.add_argument("--limit", type=int, default=None, help="配合 --retry-none：本轮最多重查 N 集")
    fe = sub.add_parser("fetch-extras", help="补演职员、电影合集、剧集播出信息和电影分辨率")
    fe.add_argument("--refresh", action="store_true", help="已拉过的也重新拉")
    ft = sub.add_parser("fetch-titles", help="补电影/剧集的中文译名（供中文搜索）")
    ft.add_argument("--refresh", action="store_true", help="已补过的也重新拉")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:  # 10-05 审计 F05：配置校验的 ValueError 也走干净报错
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    handlers = {"scan": cmd_scan, "fetch-metadata": cmd_fetch_metadata,
                "serve": cmd_serve, "detect-segments": cmd_detect_segments,
                "recommend": cmd_recommend, "community-segments": cmd_community_segments,
                "fetch-extras": cmd_fetch_extras, "fetch-titles": cmd_fetch_titles}
    return handlers[args.command](cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
