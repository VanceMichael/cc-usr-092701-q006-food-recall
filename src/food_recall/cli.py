"""命令行：构建演示情景并打印追召总览。"""

from __future__ import annotations

import argparse

from . import scenario


def _row(label: str, value: object) -> str:
    return f"  {label:<14}{value}"


def report(svc) -> str:
    lines: list[str] = []
    affected = svc.affected_packages()
    lines.append("一、真实受影响包装批（按批次谱系，而非按品牌）：")
    for pid, basis in affected.items():
        lines.append(f"  ● {pid}：{len(basis)} 项风险依据")
        for b in basis:
            lines.append(f"      - [{b['type']}] {b.get('ref', '')} {b['detail']}")
    safe = [p for p in svc.packagings if p not in affected]
    lines.append(f"  同品牌但合格、不应查封：{', '.join(safe) if safe else '无'}")

    lines.append("")
    lines.append("二、各受影响批次守恒账：")
    for pid in affected:
        r = svc.conservation(pid)
        lines.append(f"  ● {r['pkg_id']}（产量 {r['produced']}）")
        lines.append(_row("在库", f"{r['in_stock']}｜封存 {r['sealed']}，"
                                  f"在库销毁 {r['stock_destroyed']}，"
                                  f"复检放行 {r['stock_released']}，"
                                  f"待处置 {r['stock_pending']}"))
        lines.append(_row("出货", f"{r['shipped']}｜现实控有 {r['controlled']}"))
        for ch in r["per_channel"]:
            flag = "（已挂起）" if ch["suspended"] else ""
            lines.append(
                f"      - {ch['channel']}{flag}[{ch['platform']}]：出货 {ch['shipped']}"
                f" = 退货 {ch['returned']} + 销毁 {ch['destroyed']} "
                f"+ 复检放行 {ch['released']} + 未追回 {ch['outstanding']}"
            )
        lines.append(_row("恒等式",
                          f"{r['shipped']} = {r['returned']}+{r['destroyed']}"
                          f"+{r['released']}+{r['outstanding']}"))

    lines.append("")
    lines.append("三、当前结案障碍：")
    blockers = svc.closure_blockers()
    if blockers:
        lines.extend(f"  ✗ {b}" for b in blockers)
    else:
        lines.append("  无，可结案")

    lines.append("")
    lines.append("四、证据链（抽样/封存/认定/回报/协调/处置，均有责任人）：")
    for e in svc.evidence_chain():
        lines.append(f"  {e['on']} #{e['seq']:<2} {e['action']:<8}"
                     f"{e['responsible']:<10} → {e['target']}｜{e['detail']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="食品风险批次追召演示")
    parser.add_argument("--conflict", action="store_true",
                        help="追加一条内容矛盾的平台回执，演示渠道挂起")
    parser.add_argument("--retest", action="store_true",
                        help="演示复检回避、批准与停机后期限推进")
    parser.add_argument("--coordinate", action="store_true",
                        help="由上级完成跨地区协调")
    args = parser.parse_args(argv)

    from datetime import date, timedelta
    svc = scenario.build()

    if args.conflict:
        # 同一回执号 DB-7788 报不同数量 → 内容矛盾，直播渠道挂起
        try:
            svc.submit_receipt("CH-LIVE", "PKG-A", "DB-7788", 260, 0,
                               actor="渠道联络员周琦", on=date(2026, 9, 21))
        except ValueError as exc:
            print(f"【拦截】{exc}\n")

    if args.coordinate:
        svc.coordinate_cross_region(
            "ORD-02", superior="省局协调处孙浩",
            remote_authority="岭东市市场监管局",
            note="请协助控制直播间在岭东仓储的剩余货物并回报数量")

    if args.retest:
        # 抽检人李进尝试批准自己抽样的复检 → 被拦
        try:
            svc.approve_retest("rt-PKG-A-SMP-2026-031", "检测员李进")
        except ValueError as exc:
            print(f"【回避】{exc}")
        svc.approve_retest("rt-PKG-A-SMP-2026-031", "复检授权人钱敏")
        # 系统停机 3 天；恢复后期限不顺延，催办按自然日补推
        svc.advance(date(2026, 9, 23), running=False)
        svc.advance(date(2026, 9, 26), running=True)
        deadline = svc.retests["rt-PKG-A-SMP-2026-031"].deadline
        print(f"【期限】复检截止仍为 {deadline}（停机不顺延）；"
              f"今日 {svc.today}\n")

    print(report(svc))


if __name__ == "__main__":
    main()
