"""Small API-facing application view model.

The console should render these facts, not rediscover lifecycle rules from raw
state strings.  This module deliberately stays a plain function and dict; it
is presentation data, not a second application state machine.
"""

from __future__ import annotations

from collections.abc import Iterable

from .ledger import ApplicationRow

STATE_LABELS = {
    "queued": "排队中",
    "preparing": "准备中",
    "waiting_for_input": "待你补充",
    "waiting_for_approval": "待你批准",
    "submitting": "提交中",
    "submitted_verified": "已确认提交",
    "submitted_unverified": "已发送·未确认",
    "failed": "失败",
    "cancelled": "已取消",
    "skipped": "已跳过",
    "legacy_imported": "历史导入",
}


def application_view(
    row: ApplicationRow,
    *,
    missing: Iterable[str] = (),
    company_policy: str = "",
) -> dict:
    """Turn one ledger row into the facts and actions a thin console needs."""
    missing_items = [str(item) for item in missing if str(item).strip()]
    state = row.state
    reason_code = ""
    reason_text = ""
    actions: list[str] = []

    if state == "waiting_for_input":
        reason_code = "missing_answer"
        reason_text = (
            f"未填写：{', '.join(missing_items)}" if missing_items else "需要补充申请信息"
        )
        actions = ["answer", "skip"]
    elif state == "waiting_for_approval":
        if company_policy == "review":
            reason_code = "review_required"
            reason_text = "该公司在人工确认名单中，需要你确认"
            actions = ["approve", "skip", "allow_company"]
        else:
            reason_code = "approval_required"
            reason_text = "申请已准备好，等待人工确认"
            actions = ["approve", "skip"]
    elif state == "submitted_unverified":
        reason_code = "unverified_result"
        reason_text = "已尝试提交，但还没有得到可归属的确认结果"
        actions = ["reconcile"]
    elif state == "failed":
        reason_code = "failed"
        reason_text = "提交未完成，可以重新准备或跳过"
        actions = ["retry", "skip"]
    elif state == "queued":
        reason_code = "queued"
        reason_text = "等待运行"
        actions = ["prepare", "skip"]
    elif state == "submitted_verified":
        reason_code = "submitted"
        reason_text = "已收到提交确认"
    elif state == "skipped":
        reason_code = "skipped"
        reason_text = "已按策略跳过"

    return {
        "id": row.id,
        "job_key": row.job_key,
        "job_url": row.job_url,
        "route": row.route,
        "platform": row.platform,
        "state": state,
        "display_state": STATE_LABELS.get(state, state),
        "reason_code": reason_code,
        "reason_text": reason_text,
        "available_actions": actions,
        "company": row.company,
        "title": row.title,
    }
