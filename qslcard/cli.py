"""Command line interface (SRS FR-SYS-002).

Every subcommand shares one application object, so the database and vault are
opened once per run.  The GUI uses the same object, which keeps behaviour
identical between the two front ends.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import getpass
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .adif import write_adif
from .cards import plan_cards, summarize_cards
from .config import (
    AppConfig,
    PrintConfig,
    default_config_path,
    load_config,
    resolve_secret,
    save_config,
)
from .geometry import CardSpec, LayoutError, SheetSpec, best_layout
from .model import QSO, business_key, normalize_date
from .net import ResponseCache, TransportError
from .pdfout import CardRenderer, RenderOptions, render_single_cards
from .privacy import CredentialVault, EgressDenied, EgressPolicy, audit_for_secrets, mask_secret
from .rules import CardRule, RuleContext, select_qsos
from .sources import SOURCES, SourceContext, apply_record, available_sources, get_source
from .store import CARD_STATUSES, ImportStats, QsoFilter, Store
from .templates import available_templates, load_template
from .tracking import card_log_csv, pull_logbook, qrz_pusher, record_dispatch, record_returns

__all__ = ["App", "build_parser", "main"]

#: Credential field names each connector may need.
CREDENTIAL_KEYS: dict[str, tuple[str, ...]] = {
    "qrz": ("qrz_username", "qrz_password", "qrz_logbook_key"),
    "hamqth": ("hamqth_username", "hamqth_password"),
    "lotw": ("lotw_password",),
    "clublog": ("clublog_email", "clublog_password", "clublog_api_key"),
    "eqsl": (),
    "adif": (),
}

CSV_COLUMNS = (
    "call",
    "qso_date",
    "time_on",
    "band",
    "mode",
    "rst_sent",
    "rst_rcvd",
    "name",
    "qth",
    "address",
    "state",
    "zip",
    "country",
    "gridsquare",
    "qsl_via",
    "qsl_sent",
    "lotw_qsl_rcvd",
    "eqsl_qsl_rcvd",
)


@dataclass(slots=True)
class App:
    """Shared runtime state for the CLI and the GUI."""

    config: AppConfig
    store: Store
    vault: CredentialVault | None = None
    policy: EgressPolicy | None = None
    cache: ResponseCache | None = None
    #: Where configuration is written back to (used by the GUI settings dialog).
    config_path: str = ""

    @classmethod
    def open(
        cls,
        config_path: str | None = None,
        *,
        db_path: str | None = None,
        need_vault: bool = False,
    ) -> App:
        config = load_config(config_path)
        if db_path:
            config.database = db_path
        resolved_config_path = str(config_path) if config_path else str(default_config_path())
        store = Store(config.database)
        policy = EgressPolicy(offline=config.offline)
        cache = ResponseCache(str(Path(config.database).parent / "cache"))
        vault: CredentialVault | None = None
        if need_vault or config.vault_path:
            vault = _open_vault(config)
        return cls(
            config=config,
            store=store,
            vault=vault,
            policy=policy,
            cache=cache,
            config_path=resolved_config_path,
        )

    def close(self) -> None:
        self.store.close()

    # -- credential plumbing ---------------------------------------------
    def credentials(self) -> dict[str, str]:
        found: dict[str, str] = {}
        for name in SOURCES:
            section = self.config.sources.get(name)
            if not isinstance(section, dict):
                continue
            known = CREDENTIAL_KEYS.get(name, ())
            for key, value in section.items():
                if not isinstance(value, str) or not value:
                    continue
                if key in known or value.startswith(("env:", "vault:")):
                    resolved = resolve_secret(value, self.vault)
                    if resolved:
                        found[key] = resolved
        if self.vault is not None:
            for key in {k for keys in CREDENTIAL_KEYS.values() for k in keys}:
                if key not in found:
                    stored = self.vault.get(key)
                    if stored:
                        found[key] = stored
        return found

    def source_context(self, name: str, *, runner: Any = None) -> SourceContext:
        section = self.config.sources.get(name)
        options = dict(section) if isinstance(section, dict) else {}
        for key in CREDENTIAL_KEYS.get(name, ()):
            options.pop(key, None)
        options.pop("enabled", None)
        return SourceContext(
            store=self.store,
            transport=None,
            cache=self.cache,
            policy=self.policy,
            station=self.config.station,
            credentials=self.credentials(),
            options=options,
            runner=runner,
        )

    def template(self, reference: str | None = None) -> Any:
        return load_template(reference or self.config.template, self.config.template_dir)

    def card_spec(self) -> CardSpec:
        cfg = self.config.print
        return CardSpec(
            width_mm=cfg.card_width_mm,
            height_mm=cfg.card_height_mm,
            bleed_mm=cfg.bleed_mm,
            calibration_mm=cfg.calibration_mm,
        )

    def rule(self, name: str) -> CardRule:
        data = self.config.rules.get(name)
        if data is None:
            data = {}
        return CardRule.from_dict(dict(data), name=name)

    def rule_context(self, rule: CardRule) -> RuleContext:
        confirmed: set[str] = set()
        calls: set[str] = set()
        if rule.only_new_dxcc:
            for qso in self.store.iter_qsos(QsoFilter(confirmed=True)):
                if qso.country:
                    confirmed.add(qso.country)
        if rule.route_exists:
            for qso in self.store.iter_qsos():
                if qso.address or qso.qth:
                    calls.add(qso.call)
        return RuleContext(confirmed_dxcc=frozenset(confirmed), address_calls=frozenset(calls))


def _open_vault(config: AppConfig) -> CredentialVault:
    password = os.environ.get("QSLCARD_MASTER_PASSWORD") or None
    return CredentialVault(config.vault_path, master_password=password)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_import(app: App, args: argparse.Namespace) -> int:
    import io

    from .adif import iter_qsos

    paths: list[str] = list(args.paths)
    if not paths:
        paths = [str(p) for p in app.config.sources.get("adif", {}).get("paths", [])]
    if not paths:
        print("请提供 ADIF 文件或目录，或在配置中设置 sources.adif.paths")
        return 2
    source = get_source("adif")
    context = app.source_context("adif")
    context.options = {**dict(context.options), "paths": paths}
    started = True
    stats = app.store.import_qsos(source.fetch(context), strategy=args.strategy, source="adif")
    batch_id = app.store.record_batch("import", "adif", stats)
    app.store.add_audit("import", ",".join(paths), str(stats.as_dict()))
    print(
        f"导入完成（批次 {batch_id}）：共 {stats.total} 条，新增 {stats.inserted}，"
        f"更新 {stats.updated}，跳过 {stats.skipped}"
    )
    del io, iter_qsos, started
    return 0


def cmd_sync(app: App, args: argparse.Namespace) -> int:
    """Pull from every configured source, including the QRZ logbook and LoTW."""
    names = args.source or [n for n in available_sources() if n != "adif"]
    failures = 0
    for name in names:
        try:
            context = app.source_context(name)
            outcome = pull_logbook(app.store, name, context, since=args.since or "")
            for message in outcome.messages:
                print(f"- {message}")
        except (EgressDenied, RuntimeError, OSError, KeyError, TransportError) as exc:
            failures += 1
            print(f"- {name}: 失败：{exc}")
    return 1 if failures else 0


def cmd_callbook(app: App, args: argparse.Namespace) -> int:
    rule = app.rule(args.rule)
    context = app.rule_context(rule)
    provider_names = [p.strip() for p in args.provider.split(",") if p.strip()]
    limit = args.limit or rule.limit or 0
    pending: list[QSO] = []
    for qso in select_qsos(app.store, rule, context):
        if qso.address and not args.refresh:
            continue
        pending.append(qso)
        if limit and len(pending) >= limit:
            break
    if not pending:
        print("没有需要补全地址的记录")
        return 0
    calls = sorted({q.call for q in pending if q.call})
    records: dict[str, Any] = {}
    for name in provider_names:
        source = get_source(name)
        source_context = app.source_context(name)
        if not source.is_configured(source_context):
            print(f"- {name}: 未配置，跳过")
            continue
        try:
            for record in source.lookup(calls, source_context):
                records.setdefault(record.call, record)
            print(f"- {name}: 查询 {len(calls)} 个呼号")
        except (EgressDenied, RuntimeError) as exc:
            print(f"- {name}: 失败：{exc}")
    updated = 0
    for qso in pending:
        record = records.get(qso.call)
        if record and apply_record(qso, record):
            updated += 1
    if updated:
        app.store.import_qsos(pending, strategy="merge")
        app.store.add_audit("callbook", ",".join(provider_names), f"{updated} updated")
    print(f"补全完成：{updated} 条记录已更新")
    return 0


def cmd_cards(app: App, args: argparse.Namespace) -> int:
    template = app.template(args.template)
    if args.card_width_mm:
        template.card.width_mm = args.card_width_mm
    if args.card_height_mm:
        template.card.height_mm = args.card_height_mm
    if args.bleed_mm is not None:
        template.card.bleed_mm = args.bleed_mm
    rule = app.rule(args.rule)
    if args.limit:
        rule.limit = args.limit
    context = app.rule_context(rule)
    selected = list(select_qsos(app.store, rule, context))
    if not selected:
        print("没有符合规则的记录")
        return 0

    cards = plan_cards(selected, group_by=args.group_by, max_per_card=args.max_per_card)
    sheet = args.paper or app.config.print.paper
    layout = best_layout(
        SheetSpec.from_name(sheet),
        template.card,
        gap_mm=app.config.print.gap_mm,
        margin_mm=app.config.print.margin_mm,
    )
    print(f"拼版：{layout.describe()}")
    for note in layout.warnings:
        print(f"注意：{note}")

    print_config = app.config.print
    if args.paper:
        print_config = PrintConfig(**{**_print_dict(app.config.print), "paper": args.paper})
    options = RenderOptions(
        template=template,
        station=app.config.station,
        print_config=print_config,
        duplex=not args.no_duplex,
        crop_marks=not args.no_crop_marks,
        calibration_lines=not args.no_calibration,
        compress=not args.no_compress,
        axis=args.flip,
        font_path="" if args.core_font else None,
    )
    out = args.out or str(Path(app.config.output_dir) / f"cards-{os.getpid()}.pdf")
    try:
        if args.single_page:
            result = render_single_cards(cards, options, out)
        else:
            result = CardRenderer(options).render(cards, out)
    except LayoutError as exc:
        print(f"拼版失败：{exc}")
        return 2
    stats = ImportStats(total=len(selected))
    batch_id = app.store.record_batch("cards", template.name, stats)
    app.store.record_items(batch_id, [business_key(q) for card in cards for q in card.qsos])
    summary = summarize_cards(cards)
    print(
        f"已生成 {result.path}：{summary['cards']} 张卡片，{result.sheets} 张纸，"
        f"{result.pages} 页，{result.bytes_written // 1024} KB（批次 {batch_id}）"
    )
    for note in result.warnings:
        print(f"注意：{note}")
    if args.mark_sent:
        keys = [business_key(q) for card in cards for q in card.qsos]
        app.store.update_status(keys, qsl_sent="Y")
        print(f"已标记 {len(keys)} 条记录为已发卡")
    return 0


def _print_dict(config: PrintConfig) -> dict[str, Any]:
    return {
        "paper": config.paper,
        "preset": config.preset,
        "duplex": config.duplex,
        "bleed_mm": config.bleed_mm,
        "gap_mm": config.gap_mm,
        "margin_mm": config.margin_mm,
        "dpi": config.dpi,
        "color": config.color,
        "card_width_mm": config.card_width_mm,
        "card_height_mm": config.card_height_mm,
        "calibration_lines": config.calibration_lines,
        "calibration_mm": config.calibration_mm,
        "crop_marks": config.crop_marks,
    }


def cmd_export(app: App, args: argparse.Namespace) -> int:
    rule = (
        app.rule(args.rule)
        if args.rule
        else CardRule(require_unconfirmed=False, require_unsent=False)
    )
    context = app.rule_context(rule)
    qsos = list(select_qsos(app.store, rule, context))
    out = args.out
    if args.format == "adif":
        count = write_adif(qsos, out)
    else:
        with open(out, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
            writer.writeheader()
            for qso in qsos:
                writer.writerow({name: getattr(qso, name, "") for name in CSV_COLUMNS})
        count = len(qsos)
    print(f"已导出 {count} 条记录到 {out}")
    return 0


def _resolve_keys(app: App, args: argparse.Namespace) -> list[str]:
    """Business keys named by --batch and/or --call."""
    keys: list[str] = []
    if getattr(args, "batch", 0):
        keys.extend(app.store.batch_bkeys(args.batch))
    if getattr(args, "call", ""):
        keys.extend(business_key(q) for q in app.store.query(QsoFilter(call=args.call)))
    return list(dict.fromkeys(keys))


def _pusher(app: App, args: argparse.Namespace, *, sent_date: str, received: bool) -> Any:
    """Build the QRZ write-back callable when --push-qrz was given."""
    if not getattr(args, "push_qrz", False):
        return None
    context = app.source_context("qrz")
    return qrz_pusher(context, sent_date=sent_date, via=args.via or "", received=received)


def cmd_track(app: App, args: argparse.Namespace) -> int:
    """Record card dispatch and returns, keeping the card log in step."""
    keys = _resolve_keys(app, args)
    if not keys:
        print("没有找到要更新的记录（请给出 --batch 或 --call）")
        return 1
    qsos = app.store.get_many(keys)
    if not qsos:
        print("登记表里找不到这些记录")
        return 1
    date = normalize_date(args.date) or datetime.now().strftime("%Y%m%d")

    if args.status == "reset":
        updated = app.store.update_status(keys, qsl_sent="", qsl_rcvd="", qslsdate="", qslrdate="")
        app.store.add_audit("track", str(args.batch or args.call), f"reset {updated}")
        print(f"已重置 {updated} 条记录的发卡/回卡状态")
        return 0

    if args.status == "received":
        outcome = record_returns(
            app.store,
            qsos,
            returned_date=date,
            via=args.via or "",
            note=args.note or "",
            pusher=_pusher(app, args, sent_date=date, received=True),
        )
        print(f"回卡登记：{outcome.summary}")
    else:
        outcome = record_dispatch(
            app.store,
            qsos,
            sent_date=date,
            via=args.via or "",
            batch_id=args.batch or 0,
            note=args.note or "",
            pusher=_pusher(app, args, sent_date=date, received=False),
        )
        print(f"发卡登记：{outcome.summary}")
    for warning in outcome.warnings:
        print(f"注意：{warning}")
    return 0


def cmd_cardlog(app: App, args: argparse.Namespace) -> int:
    """The QSL card dispatch table: list, statistics, returns, export, push."""
    if args.action == "stats":
        info = app.store.dispatch_stats()
        print(f"发卡记录：共 {info['total']} 条")
        print(f"  已发出：{info['sent']}")
        print(f"  已回卡：{info['returned']}")
        print(f"  待回卡：{info['pending']}")
        print(f"  退回/退件：{info['bounced']}")
        print(f"  回卡率：{info['return_rate'] * 100:.1f}%")
        for status, count in sorted(info["by_status"].items()):
            print(f"  {status}: {count}")
        return 0

    if args.action == "list":
        rows = app.store.card_log(
            status=args.status or "",
            call=args.call or "",
            batch_id=args.batch or 0,
            limit=args.limit or 40,
        )
        if not rows:
            print("发卡记录为空")
            return 0
        print(
            f"{'呼号':<10}{'通联日期':<10}{'波段':<6}{'方式':<8}{'发卡日期':<10}{'状态':<9}{'回卡日期':<10}批次"
        )
        for row in rows:
            print(
                f"{row['call']:<10}{row['qso_date'] or '':<10}{row['band'] or '':<6}"
                f"{row['sent_via'] or '':<8}{row['sent_date'] or '':<10}"
                f"{row['status']:<9}{row['returned_date'] or '':<10}{row['batch_id'] or ''}"
            )
        return 0

    if args.action == "return":
        keys = _resolve_keys(app, args)
        if not keys:
            print("请用 --batch 或 --call 指定要登记回卡的记录")
            return 1
        qsos = app.store.get_many(keys)
        outcome = record_returns(
            app.store,
            qsos,
            returned_date=args.date,
            via=args.via or "",
            note=args.note or "",
            pusher=_pusher(app, args, sent_date=args.date, received=True),
        )
        print(f"回卡登记：{outcome.summary}")
        for warning in outcome.warnings:
            print(f"注意：{warning}")
        return 0

    if args.action == "export":
        if not args.out:
            print("请用 --out 指定导出文件")
            return 2
        count = card_log_csv(app.store, args.out, status=args.status or "", call=args.call or "")
        print(f"已导出 {count} 条发卡记录到 {args.out}")
        return 0

    if args.action == "push-qrz":
        rows = app.store.card_log(
            status=args.status or "", batch_id=args.batch or 0, limit=args.limit or 500
        )
        if not rows:
            print("没有可回写的记录")
            return 0
        qsos = app.store.get_many([row["bkey"] for row in rows])
        context = app.source_context("qrz")
        push = qrz_pusher(
            context,
            sent_date=args.date or "",
            via=args.via or "",
            received=args.status == "returned",
        )
        try:
            pushed = push(qsos)
        except (TransportError, EgressDenied, RuntimeError) as exc:
            print(f"回写 QRZ Logbook 失败：{exc}")
            return 1
        print(f"已回写 QRZ Logbook {pushed} 条记录的 QSL 状态")
        return 0

    return 2


def cmd_db(app: App, args: argparse.Namespace) -> int:
    if args.vacuum:
        app.store.vacuum()
        print("数据库已整理")
    if args.optimize:
        app.store.optimize()
        print("数据库统计已刷新")
    if args.info or not (args.vacuum or args.optimize):
        info = app.store.stats()
        print(f"数据库：{info['path']}")
        print(f"  QSO 总数：{info['total']}")
        print(f"  已确认：{info['confirmed']}")
        print(f"  待制卡：{info['needs_card']}")
        print(f"  不同呼号：{info['calls']}")
        print(f"  日期范围：{info['first_date'] or '-'} ~ {info['last_date'] or '-'}")
        print(f"  schema：v{info['schema_version']}")
        for batch in app.store.recent_batches(5):
            print(
                f"  批次 {batch['id']} {batch['kind']}/{batch['source']} "
                f"新增 {batch['inserted']} 更新 {batch['updated']}"
            )
    return 0


def cmd_privacy(app: App, args: argparse.Namespace) -> int:
    secrets: dict[str, str] = {}
    if app.vault is not None:
        secrets = {k: v for k, v in app.vault.load().items()}
    candidates: list[str] = list(args.paths)
    if not candidates:
        candidates = [
            str(Path(app.config.database).parent / "config.json"),
            str(app.config.vault_path),
            str(Path(app.config.database).parent),
        ]
    findings = audit_for_secrets(candidates, secrets)
    print("隐私自检")
    print(f"  凭证后端：{'-' if app.vault is None else app.vault.backend}")
    print(f"  凭证数量：{len(secrets)}")
    print(f"  网络模式：{'离线' if (app.policy and app.policy.offline) else '在线（仅白名单）'}")
    print(f"  允许域名：{len(app.policy.allowed_hosts) if app.policy else 0}")
    print("  遥测：无")
    if findings:
        for finding in findings:
            print(f"  风险[{finding.severity}] {finding.path}: {finding.detail}")
        return 1
    print("  未在检查范围内发现凭证明文")
    return 0


def cmd_vault(app: App, args: argparse.Namespace) -> int:
    if app.vault is None:
        print("未配置凭证库")
        return 2
    if args.action == "set":
        value = args.value or (os.environ.get(args.from_env, "") if args.from_env else "")
        if not value:
            value = getpass.getpass(f"请输入 {args.name} 的值：")
        app.vault.set(args.name, value)
        print(f"已保存 {args.name}（{mask_secret(value)}）到本机凭证库")
    elif args.action == "list":
        masked = app.vault.masked()
        if not masked:
            print("凭证库为空")
        for name, value in masked.items():
            print(f"  {name} = {value}")
    elif args.action == "delete":
        print("已删除" if app.vault.delete(args.name) else "未找到该凭证")
    elif args.action == "clear":
        app.vault.clear()
        print("已清空本机凭证库")
    elif args.action == "export":
        app.vault.export_encrypted(args.path, getpass.getpass("请设置导出密码："))
        print(f"已导出加密凭据包到 {args.path}")
    elif args.action == "import":
        count = app.vault.import_encrypted(args.path, getpass.getpass("请输入凭据包密码："))
        print(f"已导入 {count} 项凭证")
    return 0


def cmd_templates(app: App, args: argparse.Namespace) -> int:
    for name in available_templates():
        template = load_template(name)
        card = template.card
        print(
            f"  {name:<9} {card.width_mm:g}x{card.height_mm:g} mm 出血 {card.bleed_mm:g} mm  "
            f"{template.description}"
        )
    return 0


def cmd_sources(app: App, args: argparse.Namespace) -> int:
    credentials = app.credentials()
    for name in available_sources():
        source = get_source(name)
        context = app.source_context(name)
        try:
            configured = source.is_configured(context)
        except Exception:  # noqa: BLE001 - reporting only
            configured = False
        print(f"  {name:<9} {'已配置' if configured else '未配置'}")
    print(f"  已载入凭证：{', '.join(sorted(credentials)) or '无'}")
    return 0


def cmd_config(app: App, args: argparse.Namespace) -> int:
    if args.action == "init":
        path = save_config(app.config, args.path)
        print(f"已写入默认配置到 {path}")
    elif args.action == "show":
        print(f"配置文件：{args.path or '(默认)'}")
        print(f"  台站呼号：{app.config.station.callsign or '(未设置)'}")
        print(f"  纸张：{app.config.print.paper}  预设：{app.config.print.preset}")
        print(f"  卡片：{app.config.print.card_width_mm:g}x{app.config.print.card_height_mm:g} mm")
        print(
            f"  出血：{app.config.print.bleed_mm:g} mm  "
            f"校准线：{app.config.print.calibration_mm:g} mm"
        )
        print(f"  数据库：{app.config.database}")
        print(f"  输出目录：{app.config.output_dir}")
        print(f"  离线模式：{app.config.offline}")
    return 0


def cmd_gui(app: App, args: argparse.Namespace) -> int:
    from .gui import run_gui

    return run_gui(app)


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qslcard",
        description="业余无线电 QSO 记录聚合与 QSL 卡片打印系统",
    )
    parser.add_argument("--version", action="version", version=f"qslcard {__version__}")
    parser.add_argument("-c", "--config", help="配置文件路径（.json 或 .yaml）")
    parser.add_argument("--db", help="覆盖数据库路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("import", help="导入 ADIF 文件或目录")
    p.add_argument("paths", nargs="*", help="ADIF 文件或目录")
    p.add_argument("--strategy", choices=("skip", "merge", "overwrite"), default="merge")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("sync", help="从在线数据源同步（含 QRZ Logbook 与 LoTW）")
    p.add_argument("--source", action="append", help="仅同步指定数据源，可重复")
    p.add_argument("--since", default="", help="仅拉取该日期之后变更的记录（QRZ Logbook）")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("callbook", help="补全对方台站与地址信息")
    p.add_argument("--rule", default="needs_card")
    p.add_argument("--provider", default="qrz,hamqth")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--refresh", action="store_true", help="覆盖已有地址")
    p.set_defaults(func=cmd_callbook)

    p = sub.add_parser("cards", help="按规则生成 QSL 卡片打印文件")
    p.add_argument("--rule", default="needs_card")
    p.add_argument("--template", default="", help="模板名或 JSON 路径")
    p.add_argument("--paper", default="", help="纸张（A4、LETTER、A3 等）")
    p.add_argument("--out", default="", help="输出 PDF 路径")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--group-by", choices=("call", "country", "none"), default="call")
    p.add_argument("--max-per-card", type=int, default=4)
    p.add_argument("--bleed-mm", type=float, default=None)
    p.add_argument("--card-width-mm", type=float, default=0.0)
    p.add_argument("--card-height-mm", type=float, default=0.0)
    p.add_argument("--flip", choices=("x", "y"), default="x", help="双面翻转方向")
    p.add_argument("--single-page", action="store_true", help="每页一张卡片而非拼版")
    p.add_argument("--no-duplex", action="store_true")
    p.add_argument("--no-crop-marks", action="store_true")
    p.add_argument("--no-calibration", action="store_true")
    p.add_argument("--no-compress", action="store_true")
    p.add_argument(
        "--core-font",
        action="store_true",
        help="使用内置 PDF 基本字体（不嵌入 TrueType，输出可直接检索文本）",
    )
    p.add_argument("--mark-sent", action="store_true", help="生成后标记为已发卡")
    p.set_defaults(func=cmd_cards)

    p = sub.add_parser("export", help="导出 ADIF 或 CSV")
    p.add_argument("format", choices=("adif", "csv"))
    p.add_argument("--out", required=True)
    p.add_argument("--rule", default="")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("track", help="登记发卡与回卡（同时写入发卡记录表）")
    p.add_argument("--batch", type=int, default=0, help="制卡批次号")
    p.add_argument("--call", default="", help="按呼号选择记录")
    p.add_argument("--status", choices=("sent", "received", "reset"), required=True)
    p.add_argument("--via", default="", help="BURO / DIRECT / OQRS")
    p.add_argument("--date", default="", help="默认今天")
    p.add_argument("--note", default="")
    p.add_argument("--push-qrz", action="store_true", dest="push_qrz", help="同时回写 QRZ Logbook")
    p.set_defaults(func=cmd_track)

    p = sub.add_parser("cardlog", help="QSL 发卡记录表与回卡追踪")
    p.add_argument("action", choices=("list", "stats", "return", "export", "push-qrz"))
    p.add_argument("--status", choices=CARD_STATUSES, default="")
    p.add_argument("--call", default="")
    p.add_argument("--batch", type=int, default=0)
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--date", default="")
    p.add_argument("--via", default="")
    p.add_argument("--note", default="")
    p.add_argument("--out", default="")
    p.add_argument("--push-qrz", action="store_true", dest="push_qrz")
    p.set_defaults(func=cmd_cardlog)

    p = sub.add_parser("db", help="数据库信息与维护")
    p.add_argument("--info", action="store_true")
    p.add_argument("--vacuum", action="store_true")
    p.add_argument("--optimize", action="store_true")
    p.set_defaults(func=cmd_db)

    p = sub.add_parser("privacy", help="隐私与凭证自检")
    p.add_argument("paths", nargs="*")
    p.set_defaults(func=cmd_privacy)

    p = sub.add_parser("vault", help="本机凭证库管理")
    p.add_argument("action", choices=("set", "list", "delete", "clear", "export", "import"))
    p.add_argument("name", nargs="?", default="")
    p.add_argument("--value", default="")
    p.add_argument("--from-env", default="", dest="from_env")
    p.add_argument("--path", default="")
    p.set_defaults(func=cmd_vault)

    p = sub.add_parser("templates", help="列出内置模板")
    p.set_defaults(func=cmd_templates)

    p = sub.add_parser("sources", help="列出数据源配置状态")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("config", help="配置初始化与查看")
    p.add_argument("action", choices=("init", "show"))
    p.add_argument("--path", default="")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("gui", help="启动图形界面")
    p.set_defaults(func=cmd_gui)

    return parser


def _configure_console() -> None:
    """Emit UTF-8 so Chinese output survives legacy Windows code pages."""
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:  # noqa: BLE001 - cosmetic only
            pass
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    _configure_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    need_vault = args.command in {"vault", "privacy", "callbook", "sync"}
    app = App.open(args.config, db_path=args.db, need_vault=need_vault)
    try:
        return int(args.func(app, args))
    except KeyboardInterrupt:
        print("已取消")
        return 130
    except (EgressDenied, RuntimeError, OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    finally:
        app.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
