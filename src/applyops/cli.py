"""The `applyops-init` setup wizard.

Two ways to set this project up, and they must not drift apart. A terminal user
runs this command; someone driving the project from an MCP harness is asked the
same questions by the model, which reads them from `ProfileStore.questionnaire()`.
Both walk `PROFILE_FIELDS`, so adding a field adds it to both paths at once.

Answers are written after every single field rather than at the end. Setup for
this project is a two-minute conversation that a user may well interrupt, and a
Ctrl-C that discards twelve answers is the kind of thing that stops people from
ever finishing. Persisting per field also makes re-running the command resume
rather than restart, which is what makes it safe to run twice.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .profile import FIELDS_BY_KEY, ProfileStore, migrate_legacy_profile, write_example

CLEAR = "-"

BANNER = f"""\

  ApplyOps setup
  ──────────────
  这个向导会把填表需要的个人信息问一遍，写进一个可以直接手改的 markdown 文件。

  · 直接回车 = 保留当前值（没填过就跳过）
  · 输入 {CLEAR}    = 清空这一项
  · Ctrl-C        = 随时中断，已答的都会保存
"""


def _is_interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _ask(spec, current: str) -> str | None:
    """Ask one field. Returns the new value, or None to keep what is there.

    Re-asks on invalid input instead of storing it: a phone number with too few
    digits is a typo the user would rather fix now than discover after a form
    submission goes wrong.
    """
    print()
    flag = "必填" if spec.required else "选填"
    print(f"  [{flag}] {spec.question}")
    if spec.choices:
        print("         取值：" + " / ".join(spec.choices))
    elif spec.kind == "bool":
        print("         取值：yes / no")
    elif spec.kind == "path":
        print("         取值：文件的绝对路径")
    if spec.hint:
        print(f"         提示：{spec.hint}")
    if spec.example:
        print(f"         示例：{spec.example}")
    if current:
        print(f"         当前：{current}")

    for _ in range(3):
        try:
            raw = input("  > ").strip()
        except EOFError:
            return None

        if raw == "":
            return None
        if raw == CLEAR:
            return ""
        return raw
    return None


def _validate(store: ProfileStore, key: str, value: str) -> str | None:
    """Try a value through the real validator. Returns an error, or None."""
    if value == "":
        return None
    report = store.set_many({key: value})
    warnings = report["warnings"]
    return warnings[0] if warnings else None


def _ask_validated(store: ProfileStore, spec) -> None:
    current = store.value(spec.key)
    for _ in range(4):
        answer = _ask(spec, current)
        if answer is None:
            return
        problem = _validate(store, spec.key, answer)
        if problem is None:
            return
        print(f"\n  ✗ {problem}")
        current = store.value(spec.key)


def _walk_group(store: ProfileStore, group: dict, only_missing: bool) -> int:
    specs = [FIELDS_BY_KEY[f["key"]] for f in group["fields"]]
    if only_missing:
        specs = [s for s in specs if not store.value(s.key)]
    if not specs:
        return 0

    print(f"\n── {group['label']} " + "─" * max(0, 44 - len(group["label"])))
    if group.get("note"):
        print(f"   {group['note']}")

    answered = 0
    for spec in specs:
        before = store.value(spec.key)
        _ask_validated(store, spec)
        if store.value(spec.key) != before:
            answered += 1
    return answered


def _progress(store: ProfileStore) -> str:
    total = len([f for f in FIELDS_BY_KEY.values() if f.required])
    missing = store.missing_required()
    return f"{total - len(missing)}/{total} 必填字段已填"


def _next_steps(store: ProfileStore) -> str:
    root = Path(__file__).resolve().parent.parent.parent
    venv = root / ".venv" / "bin"
    return f"""\

  ──────────────────────────────────────────────
  档案：{store.path}

  下一步：

  1. 注册 MCP server（如果还没做）—— 在 WorkBuddy 的连接器管理页
     右上角「自定义连接器」里对 applyops 点 Trust。

  2. 导入浏览器登录态（LinkedIn 需要，仅 macOS + Chrome）：

       {venv}/python {root}/tools/import_chrome_session.py --verify

  3. 在 harness 里说：「投这个 LinkedIn 职位：<url>」

  所有字段的详细说明见 {root / 'profile.example.md'}
"""


def init_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="applyops-init",
        description="问一遍投递需要的个人信息，写进 profile.md。可重复运行，只补空缺。",
    )
    parser.add_argument("--profile", help="profile.md 路径（默认 data/profile.md）")
    parser.add_argument("--show", action="store_true", help="只打印当前状态，不提问")
    parser.add_argument("--all", action="store_true", help="连选填字段也一起问")
    parser.add_argument(
        "--write-example", action="store_true",
        help="从字段定义重新生成 profile.example.md",
    )
    args = parser.parse_args(argv)

    if args.write_example:
        target = write_example()
        print(f"wrote {target}")
        return 0

    store = ProfileStore(args.profile) if args.profile else ProfileStore()
    moved = migrate_legacy_profile(store, store.path.parent / "memory.json")
    if moved:
        print(f"\n  从 memory.json 搬过来 {moved} 项已有信息。")

    if args.show:
        status = store.status()
        print(f"profile: {status['profile_path']}")
        print(_progress(store))
        if status["missing_required"]:
            print("\n还缺（必填）：")
            for key, question in zip(
                status["missing_required"], status["missing_required_questions"]
            ):
                print(f"  {key:24} {question}")
        else:
            print("\n必填字段齐全，可以开始投递。")
        if status["missing_optional"]:
            print(f"\n选填字段还空着 {len(status['missing_optional'])} 项。")
        if status["unknown_keys"]:
            print(f"\n自定义字段：{', '.join(status['unknown_keys'])}")
        return 0

    print(BANNER)
    print(f"  profile: {store.path}")
    print(f"  {_progress(store)}")

    if not store.is_ready() and not _is_interactive():
        print(
            "\n不是交互式终端，无法提问。\n"
            "  直接在 data/profile.md 里手填（格式见 profile.example.md），\n"
            "  或者在 harness 里让我调用 setup_status 把问卷发出来。"
        )
        return 1

    if store.is_ready():
        print("\n  必填字段已经齐了。")
        try:
            go = input("  还要再过一遍选填字段吗？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            go = "n"
        if go not in ("y", "yes"):
            print(_next_steps(store))
            return 0

    answered = 0
    try:
        # Required fields first, in spec order, across every group. Doing it in
        # one pass means the user reaches "ready" as early as possible instead of
        # answering 20 optional questions before the tool becomes usable.
        for group in store.questionnaire(include_optional=False):
            answered += _walk_group(store, group, only_missing=True)

        # Then the optional ones, group by group, each skippable as a block.
        optional_groups = [
            g for g in store.questionnaire(include_optional=True)
            if any(not FIELDS_BY_KEY[f["key"]].required for f in g["fields"])
        ]
        for group in optional_groups:
            if args.all:
                answered += _walk_group(store, group, only_missing=True)
                continue
            remaining = [
                f for f in group["fields"]
                if not store.value(f["key"])
                and not FIELDS_BY_KEY[f["key"]].required
            ]
            if not remaining:
                continue
            try:
                go = input(
                    f"\n  填「{group['label']}」吗？（{len(remaining)} 项，"
                    f"直接回车跳过）[y/N] "
                ).strip().lower()
            except EOFError:
                break
            if go in ("y", "yes"):
                answered += _walk_group(store, group, only_missing=True)
    except KeyboardInterrupt:
        print(f"\n\n  已中断。{_progress(store)} —— 再跑一次会接着问剩下的。")
        print(_next_steps(store))
        return 130

    print(f"\n\n  写好了 {answered} 项，现在 {_progress(store)}。")
    if not store.is_ready():
        missing = store.missing_required()
        print(f"\n  ⚠ 还有 {len(missing)} 个必填字段空着：{', '.join(missing)}")
        print("    缺这些的话，投递会停在半路。可以再跑一次补上。")
    print(_next_steps(store))
    return 0


if __name__ == "__main__":
    sys.exit(init_main())
