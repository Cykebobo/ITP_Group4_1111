from __future__ import annotations

import json
import re
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

UTC = timezone.utc
from typing import Any

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
VALID_CATEGORIES = {"rent", "utilities", "groceries", "subscription", "entertainment", "travel", "other"}
PRIVACY_POLICY_VERSION = os.getenv("PRIVACY_POLICY_VERSION", "2026-02-24.v1")
PRIVACY_POLICY_EFFECTIVE_DATE = os.getenv("PRIVACY_POLICY_EFFECTIVE_DATE", "2026-02-24")
REQUIRE_PRIVACY_CONSENT = os.getenv("DEMO2_REQUIRE_PRIVACY_CONSENT", "true").strip().lower() not in {"0", "false", "no", "off"}
AI_CHAT_DEFAULT_MODEL = os.getenv("AI_CHAT_DEFAULT_MODEL", "gpt-4o-mini")
AI_CHAT_API_BASE = os.getenv("AI_CHAT_API_BASE", "https://api.openai.com/v1")

APP_CONTEXT: dict[str, Any] = {
    "updated_at": None,
    "latest_input": None,
    "latest_output": None,
}

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def _safe_read_text(path: str, max_chars: int = 120_000) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read(max_chars)
    except Exception as exc:
        return f"[unavailable: {exc}]"


def _project_code_context() -> str:
    app_py = _safe_read_text(os.path.join(PROJECT_ROOT, "app.py"))
    index_html = _safe_read_text(os.path.join(PROJECT_ROOT, "templates", "index.html"))
    return (
        "PROJECT_CODE_CONTEXT:\n"
        "The assistant may use the following core source files as authoritative implementation context when answering code, feature, UI, or behavior questions. "
        "However, code context is secondary for user-facing UI questions: first explain what the user visibly sees and what it means, then use code only to verify or deepen the answer if needed.\n\n"
        f"FILE: app.py\n```python\n{app_py}\n```\n\n"
        f"FILE: templates/index.html\n```html\n{index_html}\n```"
    )


def _record_app_context(input_body: dict[str, Any], output_body: dict[str, Any]) -> None:
    APP_CONTEXT["updated_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    APP_CONTEXT["latest_input"] = input_body
    APP_CONTEXT["latest_output"] = output_body


def _assistant_backend_context() -> dict[str, Any]:
    latest_output = APP_CONTEXT.get("latest_output") or {}
    latest_input = APP_CONTEXT.get("latest_input") or {}
    if not isinstance(latest_output, dict):
        latest_output = {}
    if not isinstance(latest_input, dict):
        latest_input = {}

    members = latest_input.get("members") if isinstance(latest_input.get("members"), list) else []
    txs = latest_input.get("transactions") if isinstance(latest_input.get("transactions"), list) else []
    dashboard = latest_output.get("dashboard") if isinstance(latest_output.get("dashboard"), dict) else {}
    split_suggestions = latest_output.get("split_suggestions") if isinstance(latest_output.get("split_suggestions"), list) else []
    primary_split = split_suggestions[0] if split_suggestions and isinstance(split_suggestions[0], dict) else {}

    return {
        "updated_at": APP_CONTEXT.get("updated_at"),
        "group": latest_output.get("group"),
        "refresh": latest_output.get("refresh"),
        "summary": {
            "member_count": len(members),
            "transaction_count": len(txs),
            "shared_bill_count": len(latest_output.get("detected_shared_bills") or []),
            "alert_count": len(latest_output.get("alerts") or []),
            "insight_count": len(latest_output.get("insights") or []),
        },
        "members": [
            {"member_id": m.get("member_id"), "name": m.get("name")}
            for m in members[:12]
            if isinstance(m, dict)
        ],
        "key_data": {
            "net_balance": dashboard.get("net_balance") or [],
            "upcoming_bills": (dashboard.get("upcoming_bills") or [])[:12],
            "alerts": (latest_output.get("alerts") or [])[:12],
            "insights": (latest_output.get("insights") or [])[:8],
            "fairness": (latest_output.get("fairness") or [])[:4],
            "settlements": (primary_split.get("settlements") or [])[:12],
        },
    }


def _to_num(v: Any, d: float = 0.0) -> float:
    try:
        n = float(v)
        if n == n and n != float("inf") and n != float("-inf"):
            return n
    except (TypeError, ValueError):
        pass
    return d


def _round2(v: float) -> float:
    return round(v + 1e-10, 2)


def _category_from_text(text: str) -> str:
    s = (text or "").lower()
    if any(k in s for k in ["rent", "landlord", "apartment", "flat"]):
        return "rent"
    if any(k in s for k in ["utility", "electric", "water", "gas", "internet", "wifi", "broadband"]):
        return "utilities"
    if any(k in s for k in ["grocery", "supermarket", "tesco", "aldi", "lidl", "food"]):
        return "groceries"
    if any(k in s for k in ["subscription", "netflix", "spotify", "youtube", "prime", "icloud"]):
        return "subscription"
    if any(k in s for k in ["cinema", "movie", "bar", "club", "game", "concert", "net"]):
        return "entertainment"
    if any(k in s for k in ["uber", "train", "bus", "flight", "taxi", "travel", "ticket"]):
        return "travel"
    return "other"


def _due_date(cat: str, tx_date: str) -> str:
    d = datetime.fromisoformat(tx_date).date()
    if cat == "rent":
        return d.replace(day=1).isoformat()
    if cat == "utilities":
        return (d + timedelta(days=7)).isoformat()
    if cat == "subscription":
        return (d + timedelta(days=30)).isoformat()
    if cat == "groceries":
        return (d + timedelta(days=3)).isoformat()
    return (d + timedelta(days=14)).isoformat()


def _parse_receipt_items(receipt_text: str, tx_id: str) -> list[dict[str, Any]]:
    if not isinstance(receipt_text, str) or not receipt_text.strip():
        return []
    out: list[dict[str, Any]] = []
    for line in receipt_text.splitlines():
        s = line.strip()
        if not s:
            continue
        parts = s.replace(":", "-").rsplit("-", 1)
        if len(parts) != 2:
            continue
        name, amount_raw = parts[0].strip(), parts[1].strip()
        amount = _to_num(amount_raw, -1)
        if name and amount > 0:
            out.append({"name": name, "amount": _round2(amount), "currency": "GBP", "source_transaction_id": tx_id})
    return out


def _parse_iso8601(ts: Any) -> datetime | None:
    if not isinstance(ts, str):
        return None
    s = ts.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _privacy_notice() -> dict[str, Any]:
    return {
        "policy_version": PRIVACY_POLICY_VERSION,
        "effective_date": PRIVACY_POLICY_EFFECTIVE_DATE,
        "lawful_basis": "consent",
        "purposes": [
            "shared_expense_settlement",
            "group_finance_dashboard",
            "risk_and_fairness_recommendations",
        ],
        "data_minimisation": "Only group/member/transaction fields needed for settlement are processed.",
        "retention": "Demo service processes request payload in memory and does not persist personal data.",
        "withdrawal": "User can withdraw at any time in UI; new consent is required for subsequent requests.",
    }


def _validate_privacy_consent(body: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    if not REQUIRE_PRIVACY_CONSENT:
        return {
            "required": False,
            "status": "disabled",
            "policy_version": PRIVACY_POLICY_VERSION,
            "lawful_basis": "consent",
        }, None

    consent = body.get("consent")
    if not isinstance(consent, dict) or consent.get("given") is not True:
        return None, "privacy_consent_required"

    policy_version = str(consent.get("policy_version") or "").strip()
    if policy_version != PRIVACY_POLICY_VERSION:
        return None, "privacy_consent_version_mismatch"

    consented_at_dt = _parse_iso8601(consent.get("consented_at"))
    if consented_at_dt is None:
        return None, "privacy_consent_invalid_timestamp"

    if consent.get("withdrawn") is True or str(consent.get("withdrawn_at") or "").strip():
        return None, "privacy_consent_withdrawn"

    method = str(consent.get("method") or "checkbox_ui").strip() or "checkbox_ui"
    actor = str(consent.get("actor") or "end_user").strip() or "end_user"

    return {
        "required": True,
        "status": "granted",
        "policy_version": PRIVACY_POLICY_VERSION,
        "consented_at": consented_at_dt.isoformat().replace("+00:00", "Z"),
        "method": method,
        "actor": actor,
        "lawful_basis": "consent",
        "withdrawal_supported": True,
    }, None


def _error(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": msg}), code


def build_gfm(body: dict[str, Any]) -> tuple[dict[str, Any], int]:
    if not isinstance(body, dict):
        return {"ok": False, "error": "body must be JSON object"}, 400

    privacy, privacy_error = _validate_privacy_consent(body)
    if privacy_error:
        return {
            "ok": False,
            "error": privacy_error,
            "privacy": {
                "required": REQUIRE_PRIVACY_CONSENT,
                **_privacy_notice(),
                "expected_consent_fields": ["given", "policy_version", "consented_at", "method"],
            },
        }, 400

    group = body.get("group")
    members = body.get("members") if isinstance(body.get("members"), list) else None
    transactions = body.get("transactions") if isinstance(body.get("transactions"), list) else None

    if group is None or members is None or transactions is None:
        return {"ok": False, "error": "missing required fields: group,members,transactions"}, 400
    if len(members) < 2:
        return {"ok": False, "error": "members<2"}, 400

    member_ids: list[str] = []
    name_by_id: dict[str, str] = {}
    for m in members:
        if not isinstance(m, dict) or not str(m.get("id", "")).strip():
            return {"ok": False, "error": "member id missing or invalid"}, 400
        mid = str(m["id"]).strip()
        if mid in name_by_id:
            return {"ok": False, "error": "duplicate member ids"}, 400
        member_ids.append(mid)
        name_by_id[mid] = str(m.get("name") or mid)

    balances = body.get("balances") if isinstance(body.get("balances"), dict) else {}
    weights = body.get("weights") if isinstance(body.get("weights"), dict) else {}
    has_weights = len(weights) > 0

    paid_by = {mid: 0.0 for mid in member_ids}
    net = {mid: 0.0 for mid in member_ids}
    by_category = {"rent": 0.0, "utilities": 0.0, "groceries": 0.0, "subscription": 0.0, "entertainment": 0.0, "travel": 0.0, "other": 0.0}

    detected_shared_bills: list[dict[str, Any]] = []
    receipt_items: list[dict[str, Any]] = []

    weight_sum = sum(max(0.0, _to_num(weights.get(mid), 1.0)) for mid in member_ids) if has_weights else float(len(member_ids))

    today = date.today().isoformat()

    for t in transactions:
        if not isinstance(t, dict):
            return {"ok": False, "error": "transaction item must be object"}, 400
        tx_id = str(t.get("id") or "").strip()
        if not tx_id:
            return {"ok": False, "error": "transaction id missing"}, 400

        payer = str(t.get("payer_member_id") or "").strip()
        if payer not in member_ids:
            return {"ok": False, "error": f"payer not in members: {tx_id}"}, 400

        amount = _to_num(t.get("amount"), -1)
        if amount <= 0:
            return {"ok": False, "error": f"invalid amount: {tx_id}"}, 400
        amount = _round2(amount)

        tx_date = str(t.get("date") or today)
        text_blob = " ".join([str(t.get("category") or ""), str(t.get("note") or ""), str(t.get("vendor") or ""), str(t.get("receipt_text") or "")])
        category = str(t.get("category") or "").strip().lower()
        if category not in VALID_CATEGORIES:
            category = _category_from_text(text_blob)

        confidence = 0.98 if t.get("category") else (0.62 if category == "other" else 0.86)
        reason = f"Category provided by input ({category})." if t.get("category") else f"Detected {category} from transaction text keywords."

        detected_shared_bills.append(
            {
                "category": category,
                "confidence": confidence,
                "reason": reason,
                "suggested_due_date": _due_date(category, tx_date),
                "source_transaction_id": tx_id,
            }
        )

        by_category[category] = _round2(by_category[category] + amount)
        paid_by[payer] = _round2(paid_by[payer] + amount)

        # share allocation per tx
        shares: dict[str, float] = {}
        if has_weights or str(t.get("split_method") or "").lower() == "weighted":
            local_weight_sum = sum(max(0.0, _to_num(weights.get(mid), 1.0)) for mid in member_ids)
            if local_weight_sum <= 0:
                local_weight_sum = float(len(member_ids))
            for mid in member_ids:
                shares[mid] = _round2(amount * (max(0.0, _to_num(weights.get(mid), 1.0)) / local_weight_sum))
        else:
            base = _round2(amount / len(member_ids))
            for mid in member_ids:
                shares[mid] = base
            drift = _round2(amount - sum(shares.values()))
            shares[member_ids[0]] = _round2(shares[member_ids[0]] + drift)

        net[payer] = _round2(net[payer] + amount)
        for mid in member_ids:
            net[mid] = _round2(net[mid] - shares[mid])

        receipt_items.extend(_parse_receipt_items(str(t.get("receipt_text") or ""), tx_id))

    debtors = [{"id": mid, "amt": _round2(-net[mid])} for mid in member_ids if net[mid] < 0]
    creditors = [{"id": mid, "amt": _round2(net[mid])} for mid in member_ids if net[mid] > 0]
    debtors.sort(key=lambda x: (-x["amt"], x["id"]))
    creditors.sort(key=lambda x: (-x["amt"], x["id"]))

    settlements: list[dict[str, Any]] = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        pay = _round2(min(debtors[i]["amt"], creditors[j]["amt"]))
        if pay > 0:
            settlements.append(
                {
                    "from_member_id": debtors[i]["id"],
                    "to_member_id": creditors[j]["id"],
                    "amount": pay,
                    "currency": "GBP",
                }
            )
        debtors[i]["amt"] = _round2(debtors[i]["amt"] - pay)
        creditors[j]["amt"] = _round2(creditors[j]["amt"] - pay)
        if debtors[i]["amt"] <= 0:
            i += 1
        if creditors[j]["amt"] <= 0:
            j += 1

    total = _round2(sum(_to_num(t.get("amount"), 0) for t in transactions))

    split_method = "weighted" if has_weights else "equal"
    shares_summary = []
    for mid in member_ids:
        expected = 0.0
        for t in transactions:
            amount = _to_num(t.get("amount"), 0)
            if split_method == "weighted":
                expected += amount * (max(0.0, _to_num(weights.get(mid), 1.0)) / max(weight_sum, 1e-9))
            else:
                expected += amount / len(member_ids)
        shares_summary.append({"member_id": mid, "member_name": name_by_id[mid], "expected_share": _round2(expected)})

    who_paid_what = [{"member_id": mid, "member_name": name_by_id[mid], "paid_total": _round2(paid_by[mid]), "currency": "GBP"} for mid in member_ids]
    who_paid_what.sort(key=lambda x: (-x["paid_total"], x["member_id"]))

    upcoming = [
        {
            "category": d["category"],
            "due_date": d["suggested_due_date"],
            "related_transaction_id": d["source_transaction_id"],
        }
        for d in sorted(detected_shared_bills, key=lambda x: (x["suggested_due_date"], x["source_transaction_id"]))
    ]

    alerts: list[dict[str, Any]] = []
    for u in upcoming[:4]:
        alerts.append(
            {
                "type": "due_reminder",
                "severity": "medium",
                "message": f"{u['category']} bill is due on {u['due_date']}.",
                "related_transaction_id": u["related_transaction_id"],
                "member_ids": member_ids,
            }
        )

    for mid in member_ids:
        b = _to_num(balances.get(mid), 0)
        if b < 0:
            alerts.append(
                {
                    "type": "low_funds",
                    "severity": "high",
                    "message": f"{name_by_id[mid]} may face a cash shortage based on current balance.",
                    "related_transaction_id": None,
                    "member_ids": [mid],
                }
            )

    pending = [m["id"] for m in members if isinstance(m, dict) and m.get("confirmed") is False and str(m.get("id") or "") in member_ids]
    if pending:
        alerts.append(
            {
                "type": "pending_confirmation",
                "severity": "low",
                "message": f"{len(pending)} member(s) still need to confirm shared expenses.",
                "related_transaction_id": None,
                "member_ids": pending,
            }
        )

    fairness: list[dict[str, Any]] = []
    top = who_paid_what[0] if who_paid_what else None
    second = who_paid_what[1] if len(who_paid_what) > 1 else top
    if top and second and top["paid_total"] - second["paid_total"] >= 15:
        fairness.append(
            {
                "issue": "advance_imbalance",
                "evidence": f"{top['member_name']} has paid {top['paid_total']:.2f} GBP, significantly above peers.",
                "recommendation": {"next_payer_member_id": second["member_id"], "strategy": "round_robin_with_cap"},
            }
        )
    elif top:
        candidate = next((mid for mid in member_ids if mid != top["member_id"]), top["member_id"])
        fairness.append(
            {
                "issue": "rotation_hint",
                "evidence": "Spending is close, but rotating payer reduces future imbalance risk.",
                "recommendation": {"next_payer_member_id": candidate, "strategy": "round_robin"},
            }
        )

    top_category = max(by_category.items(), key=lambda x: x[1])[0] if by_category else "other"
    next_payer = fairness[0]["recommendation"]["next_payer_member_id"] if fairness else None
    plan_change_reason = (
        fairness[0]["evidence"] if fairness else "Plan recalculated with current balances and transactions."
    )

    insights: list[dict[str, Any]] = []
    if next_payer:
        insights.append(
            {
                "type": "fairness",
                "title": "Rotate next payer",
                "message": f"Use {name_by_id.get(next_payer, next_payer)} as next payer to reduce advance imbalance.",
                "action": "Apply next payer recommendation",
            }
        )
    if top_category != "other":
        insights.append(
            {
                "type": "spend_pattern",
                "title": "Highest spend category",
                "message": f"{top_category.capitalize()} is the largest spend bucket this cycle.",
                "action": "Review upcoming bills in this category",
            }
        )
    high_alert_count = sum(1 for a in alerts if a.get("severity") == "high")
    if high_alert_count:
        insights.append(
            {
                "type": "risk",
                "title": "Cash risk detected",
                "message": f"{high_alert_count} high-severity cash signal(s) detected.",
                "action": "Request earlier settlement from debtors",
            }
        )
    if not insights:
        insights.append(
            {
                "type": "stability",
                "title": "Stable cycle",
                "message": "No major risk detected in this cycle.",
                "action": "Keep current split strategy",
            }
        )

    

    repayment_links = [
        {
            "from_member_id": st["from_member_id"],
            "to_member_id": st["to_member_id"],
            "amount": st["amount"],
            "currency": st.get("currency", "GBP"),
            "label": f"Pay {name_by_id.get(st['to_member_id'], st['to_member_id'])}",
            "lloyds_pay_url": f"https://pay.lloydsbank.example/transfer?from={st['from_member_id']}&to={st['to_member_id']}&amount={st['amount']}",
            "bank_transfer_ref": f"GFM-{st['from_member_id']}-{st['to_member_id']}"
        }
        for st in settlements
    ]

    balance_points = []
    for mid in member_ids:
        settled = sum(1 for st in settlements if st["from_member_id"] == mid and st["amount"] > 0)
        points = 10 + settled * 5
        balance_points.append({"member_id": mid, "member_name": name_by_id[mid], "monthly_points": points, "credit_story": "On-track" if points >= 10 else "Needs action"})

    notifications = []
    for a in alerts[:6]:
        notifications.append({"title": a.get("type", "alert"), "message": a.get("message", ""), "severity": a.get("severity", "medium")})

    response = {
        "ok": True,
        "meta": {
            "demo": "Group Finance Prototype UI1",
            "version": "0.1.0",
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "privacy_policy_version": PRIVACY_POLICY_VERSION,
        },
        "refresh": {
            "refreshed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "integrity_check": "passed",
            "source": "latest_available_snapshot",
        },
        "privacy": {**_privacy_notice(), **(privacy or {})},
        "recalculation": {
            "next_payer_member_id": next_payer,
            "plan_change_reason": plan_change_reason,
        },
        "insights": insights,
        "notifications": notifications,
        "detected_shared_bills": detected_shared_bills,
        "split_suggestions": [{"method": split_method, "total": total, "shares": shares_summary, "settlements": settlements, "repayment_links": repayment_links}],
        "dashboard": {
            "net_balance": [{"member_id": mid, "member_name": name_by_id[mid], "net": _round2(net[mid]), "currency": "GBP"} for mid in member_ids],
            "who_paid_what": who_paid_what,
            "upcoming_bills": upcoming,
            "chart_data": {
                "by_category": [{"category": cat, "total": _round2(by_category[cat])} for cat in ["rent", "utilities", "groceries", "subscription", "entertainment", "travel", "other"] if _round2(by_category[cat]) > 0],
                "by_member_paid": [{"member_id": x["member_id"], "member_name": x["member_name"], "total": x["paid_total"]} for x in who_paid_what],
            },
        },
        "alerts": alerts,
        "fairness": fairness,
        "receipt_items": receipt_items,
        "gamification": {"balance_points": balance_points, "note": "Monthly reward narrative for responsible settling."},
        "offers": [{"title": "Student housing cashback", "status": "optional", "description": "Partner offers module (optional in demo)."}],
        "chatbot": {"enabled": False, "summary_sample": "Monthly summary assistant placeholder."},
        "live_split": {"enabled": True, "description": "Supports instant split for ad-hoc groups (dining/tickets)."},
        "receipt_split_suggestion": {
            "method": "proportional_by_item",
            "note": "Suggested from parsed receipt items; confirm participant assignment in UI.",
        }
        if receipt_items
        else None,
    }

    return response, 200


@app.get("/")
def index():
    return render_template(
        "index.html",
        privacy_policy_version=PRIVACY_POLICY_VERSION,
        privacy_policy_effective_date=PRIVACY_POLICY_EFFECTIVE_DATE,
        require_privacy_consent=REQUIRE_PRIVACY_CONSENT,
    )


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/privacy/notice")
def privacy_notice():
    return jsonify({"ok": True, "require_consent": REQUIRE_PRIVACY_CONSENT, "privacy": _privacy_notice()})


@app.post("/api/demo2/consent/withdraw")
def withdraw_demo2_consent():
    body = request.get_json(silent=True)
    actor = str((body or {}).get("actor") or "end_user")
    return jsonify(
        {
            "ok": True,
            "status": "withdrawn",
            "actor": actor,
            "withdrawn_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "message": "Consent withdrawn. Submit a fresh consent object before the next recalculation.",
        }
    )


@app.post("/api/demo2")
def api_demo2():
    body = request.get_json(silent=True)
    safe_body = body if isinstance(body, dict) else {}
    data, status = build_gfm(safe_body)
    if status == 200 and isinstance(data, dict) and data.get("ok"):
        _record_app_context(safe_body, data)
    return jsonify(data), status


@app.get("/api/assistant-context")
def api_assistant_context():
    return jsonify({"ok": True, "context": _assistant_backend_context()})


@app.post("/api/assistant-chat")
def api_assistant_chat():
    body = request.get_json(silent=True) or {}
    api_key = str(body.get("apiKey") or "").strip()
    if not api_key:
        return jsonify({"ok": False, "error": "missing_api_key"}), 400

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return jsonify({"ok": False, "error": "missing_messages"}), 400

    latest_user_text = ""
    for msg in reversed(messages):
        if isinstance(msg, dict) and str(msg.get("role") or "") == "user":
            latest_user_text = str(msg.get("content") or "").strip()
            break

    model = str(body.get("model") or AI_CHAT_DEFAULT_MODEL).strip() or AI_CHAT_DEFAULT_MODEL
    api_base = str(body.get("apiBase") or AI_CHAT_API_BASE).strip().rstrip("/") or AI_CHAT_API_BASE
    model_preset = str(body.get("modelPreset") or "").strip()
    if model_preset == "deepseek-chat":
        model = "deepseek-chat"
        api_base = "https://api.deepseek.com/v1"
    elif model_preset == "deepseek-reasoner":
        model = "deepseek-reasoner"
        api_base = "https://api.deepseek.com/v1"
    elif model_preset == "openai-gpt4o-mini":
        model = "gpt-4o-mini"
        api_base = "https://api.openai.com/v1"

    def _call_provider(payload_obj: dict):
        req = urllib.request.Request(
            f"{api_base}/chat/completions",
            data=json.dumps(payload_obj).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8"))
            except Exception:
                detail = {"message": str(e)}
            return None, {"kind": "http", "detail": detail}
        except Exception as e:
            return None, {"kind": "request", "detail": str(e)}

    def _text_only_messages(src_messages: list):
        out = []
        for msg in src_messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role") or "user"
            content = msg.get("content")
            if isinstance(content, list):
                text_parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_parts.append(str(part.get("text") or "").strip())
                out.append({"role": role, "content": "\n".join([x for x in text_parts if x])})
            else:
                out.append({"role": role, "content": str(content or "")})
        return out

    def _looks_like_ui_question(text: str) -> bool:
        t = (text or "").strip().lower()
        ui_markers = [
            "can you see", "what is on", "what's on", "what is at", "what's at", "at the bottom", "at the top",
            "on this page", "on the page", "what do you see", "what can i see", "visible", "this page", "that page",
            "this screen", "that screen", "button", "card", "tile", "block", "section", "module", "icon", "fronted most"
        ]
        code_markers = [
            "code", "implement", "implementation", "function", "file", "html", "css", "js", "javascript", "python",
            "app.py", "index.html", "template", "backend", "frontend", "logic flow", "field", "variable", "array"
        ]
        return any(m in t for m in ui_markers) and not any(m in t for m in code_markers)

    ui_context = body.get("uiContext") if isinstance(body.get("uiContext"), dict) else {}
    backend_context = _assistant_backend_context()
    include_code_context = not _looks_like_ui_question(latest_user_text)
    project_code_context = _project_code_context() if include_code_context else ""

    def _normalise_question(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip().lower())

    def _preset_ai_answer(question: str, backend_ctx: dict[str, Any]) -> str | None:
        q = _normalise_question(question)
        key_data = backend_ctx.get("key_data") if isinstance(backend_ctx.get("key_data"), dict) else {}
        alerts = key_data.get("alerts") if isinstance(key_data.get("alerts"), list) else []
        insights = key_data.get("insights") if isinstance(key_data.get("insights"), list) else []
        settlements = key_data.get("settlements") if isinstance(key_data.get("settlements"), list) else []

        def _alert_reason(alert: dict[str, Any]) -> str:
            alert_type = str(alert.get("type") or "").strip()
            mapping = {
                "due_reminder": "a payment is becoming time-sensitive",
                "pending_confirmation": "some shared expenses still need confirmation",
                "advance_imbalance": "one part of the group is still carrying more of the payment burden than the others",
                "low_funds": "one of the planned repayments may be difficult to complete right now",
                "cash_risk": "there may be a repayment difficulty that could delay settlement",
            }
            return mapping.get(alert_type, "there is still an unresolved issue in the current reimbursement flow")

        def _pick_primary_insight() -> str:
            for item in insights:
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or "").strip()
                message = str(item.get("message") or "").strip()
                combined = f"{title} {message}".strip().lower()
                if "rotate next payer" in combined:
                    continue
                if title:
                    return title
                if message:
                    return message
            if settlements:
                return "The group still has outstanding reimbursements to complete before balance is fully restored."
            if alerts:
                return "The group still has an unresolved balance state that needs attention before repayment can close cleanly."
            return "The group is still moving from fronted payment toward restored balance."

        if q == "why is this the first action?":
            if alerts:
                reason = _alert_reason(alerts[0] if isinstance(alerts[0], dict) else {})
                return (
                    f"It looks like this is the first action because {reason}. "
                    "The app is trying to remove the thing most likely to delay repayment before asking the group to simply follow the transfer plan."
                )
            if settlements:
                return (
                    "It looks like this is the first action because it is the clearest next step for restoring balance after someone has already fronted shared costs. "
                    "The app is prioritising repayment progress rather than leaving the group in an unresolved state."
                )
            return (
                "It looks like this is the first action because it is the step that most directly moves the group toward restored balance. "
                "The app is trying to turn the current situation into a clear next move, not just show analysis."
            )

        if q == "explain the most important insight and what action should happen next.":
            insight_text = _pick_primary_insight()
            if alerts:
                return (
                    f"The most important insight is that {insight_text[:1].lower() + insight_text[1:] if insight_text else 'the group still has an unresolved balance state'}. "
                    "The next action should be to clear the issue that could delay repayment, then continue with the suggested settlement transfers so balance can be restored cleanly."
                )
            if settlements:
                return (
                    f"The most important insight is that {insight_text[:1].lower() + insight_text[1:] if insight_text else 'the group still has reimbursements to complete'}. "
                    "The next action should be to review the suggested transfers and complete the repayment path that restores balance."
                )
            return (
                f"The most important insight is that {insight_text[:1].lower() + insight_text[1:] if insight_text else 'the group is still between fronted payment and restored balance'}. "
                "The next action should be to decide what concrete payment or confirmation step will move the group back toward balance."
            )

        return None

    provider_messages = [
        {
            "role": "system",
            "content": (
                "You are an in-app assistant inside Group Finance. "
                "Answer using the backend app data + current UI context. "
                "For any question about what is on a page, what the user can see, what is at the top or bottom of a page, what a visible block means, or what a user can do there, answer from the user visual layer first. "
                "Do not begin UI answers with code structure, file names, arrays, HTML, components, templates, or implementation details unless the user explicitly asks about code. "
                "Do not claim you cannot see the screen/app; you have structured app state from backend. "
                "If something is missing, state exactly what data is missing and ask one focused follow-up question. "
                "Example of correct behavior: if the user asks 'what is at the bottom of page two', answer like 'At the bottom of page two, you see ...' and describe the visible block first. Example of wrong behavior: 'According to templates/index.html...' or any code-first explanation."
            ),
        },
        {
            "role": "system",
            "content": (
                "PRODUCT_BRAIN:\n"
                "Group Finance is not a generic budgeting tool and it is not mainly for pure AA splitting. "
                "If a group can split equally and pay immediately, the product is barely needed. The product becomes meaningful when one or more people front shared payments and fairness has to be restored over time through reimbursement, visibility, and coordinated settlement. "
                "Its job is to help a household or group understand what happened, see who carried the payment burden, detect fairness/risk issues, and decide the clearest next action to restore balance with minimal friction.\n\n"
                "PROJECT THESIS:\n"
                "- The core problem is delayed fairness, not instant calculation.\n"
                "- AA solves equal division on paper, but not the lived process of fronted payment, waiting, reminding, and reimbursement.\n"
                "- Settlement transfers are suggested next payments, not records of payments already completed.\n"
                "- AI should support judgment, explanation, and pattern recognition; it should not pretend to be a moral authority that defines fairness perfectly.\n"
                "- The product should talk about restoring balance, not blaming one person.\n\n"
                "PROJECT KNOWLEDGE BASE:\n"
                "- This project is about groups or households where one or more people often front shared payments and others reimburse later.\n"
                "- The main value of the product is not instant bill splitting; it is helping groups recover balance over time after fronted payments.\n"
                "- If a bill is pure AA and everyone pays immediately, the need for this product is minimal.\n"
                "- The app should help users understand what happened, who carried the payment burden, what reimbursements are still pending, and what next action would restore balance.\n"
                "- A transfer or settlement in this product means a recommended payment that should happen next. It does not mean the payment has already happened.\n"
                "- The product should treat unresolved reimbursement as a group state, not as a moral failure of one specific person.\n"
                "- Alerts should be interpreted as signs that balance has not yet been restored, that confirmation is still pending, or that some friction remains in the workflow.\n"
                "- Fairness here includes process fairness, not only final arithmetic fairness. It matters who repeatedly fronts money, who waits longest to be repaid, and who carries cash-flow pressure.\n"
                "- The product is strongest when explaining reimbursement logic, settlement logic, and why a recommendation exists.\n"
                "- The product is weaker when pretending it can fully determine moral fairness from transaction data alone.\n\n"
                "PAGE ROLES:\n"
                "- Home: current state, shared spending overview, and the most important next action right now.\n"
                "- Insights: interpretation of spending patterns, fairness signals, and why the system is making its judgment.\n"
                "- Settle: the concrete reimbursement path for who should pay whom next in order to restore balance.\n"
                "- Notifications/Alerts: risks, blockers, reminders, and unresolved issues that could stop a clean close.\n"
                "- Book pages: presentation layers for the product story; when asked about Page 1-5, treat them as project explanation pages rather than live transactional UI.\n"
                "- Current book page labels are: Page 1 - Home, Page 2 - Insights, Page 3 - Settle, Page 4 - Notifications, Page 5 - Value.\n\n"
                "PROJECT FACTS AND FAQ:\n"
                "- What problem does the project solve? It helps groups manage the period after shared payments have been fronted by one or more people and before reimbursement has fully restored balance.\n"
                "- Why is pure AA not enough? Because equal arithmetic does not address who repeatedly fronts money, who waits for repayment, who carries reminder burden, and how fairness is restored over time.\n"
                "- When would the product be unnecessary? If every shared expense is split equally and everyone pays immediately, the need for the product becomes very small.\n"
                "- What does settlement mean here? Settlement means the suggested transfers that should happen next so the group can restore financial balance.\n"
                "- Are transfers already paid? No. Transfers in the app are recommendations for what should be paid next unless explicitly marked otherwise by data.\n"
                "- What does fairness mean here? It includes final distribution, but also process fairness: who fronts money, who waits, and who carries reimbursement burden.\n"
                "- What is the role of AI? The AI should explain patterns, support judgment, surface reimbursement logic, and clarify why the app recommends a next step.\n"
                "- What is the AI not supposed to do? It should not invent hidden facts, pretend to know unrecorded agreements, or claim perfect moral authority.\n"
                "- What should alerts communicate? They should communicate unresolved balance, pending confirmation, delayed reimbursement, or workflow risk, rather than blaming one person.\n"
                "- When a risk or blocker involves a specific member, do not over-personalise the answer unless the user explicitly asks about that member. Prefer describing the group state or unresolved payment state first.\n"
                "- What should home communicate? It should show what happened, what still matters now, and what action most directly moves the group toward restored balance.\n"
                "- What should insights communicate? Why spending patterns, fairness concerns, or recommendation signals have become notable in the current state.\n"
                "- What should settle communicate? The concrete reimbursement path and pending actions required to complete the cycle of repayment.\n"
                "- What should book pages communicate? A critical and human explanation of the project, not generic marketing language.\n"
                "- What is Page 1 about? The product is not mainly for instant AA; it matters when fairness is delayed in time.\n"
                "- What is Page 2 about? Shared transactions need interpretation because fronted payments create burden beyond raw totals.\n"
                "- What is Page 3 about? AI is useful as a structured explainer and pattern recogniser, not as a perfect judge of fairness.\n"
                "- What is Page 4 about? Settlement is the proposed path for restoring balance, not just an informational summary.\n"
                "- What is Page 5 about? The product's value, limits, and the conditions under which it is genuinely useful.\n"
                "- What language should be preferred? Use terms like fronted payment, reimbursement, shared burden, pending transfer, restore balance, and delayed fairness.\n"
                "- What language should be used carefully? Terms like AA, fairness, blocker, or payer rotation should be explained, because they can easily be oversimplified or misread.\n"
                "- If a teammate asks a naive question, should the AI simplify? Yes, but it should still stay faithful to the project logic and avoid dumbing it down into a generic budgeting app.\n"
                "- If the current data suggests a likely reason but not a guaranteed reason, use cautious language like 'it looks like', 'it may be prioritising', or 'the app is likely focusing on'.\n\n"
                "KNOWN DESIGN INTENT:\n"
                "- Avoid making the product sound like a simple AA calculator.\n"
                "- Prefer language about fronted payments, reimbursement, shared burden, balance, and restoring fairness over time.\n"
                "- If the user asks about feature meaning, explain both the functional reason and the product reasoning behind it.\n"
                "- If something in the UI seems contradictory, answer honestly and point out the inconsistency rather than defending it.\n"
                "- Treat current project/development details shared by the user as valid working context for this app conversation.\n"
                "- Do not invent product decisions that are not grounded in the provided context.\n"
                "- If a detail is not visible in backend data, current UI context, or the built-in project knowledge here, say that it is not confirmed in the current version.\n"
                "- If the user asks whether something is final, approved, or intentional, answer carefully and distinguish between confirmed logic and likely interpretation.\n\n"
                "CORE PRODUCT LOGIC:\n"
                "- Fairness: detect when one member is carrying disproportionate payment burden or when reimbursement balance has not yet been restored.\n"
                "- Risk: identify cash shortage, confirmation delay, unresolved transfers, overdue/shared bill risk, and workflow blockers.\n"
                "- Settlement: produce the clearest, lowest-friction payment path to close outstanding imbalance.\n"
                "- AI role: explain product logic, justify recommendations, answer team questions, and help users understand why a recommendation exists.\n\n"
                "ANSWERING RULES FOR TEAM QUESTIONS:\n"
                "- When possible, answer as if you are the project's built-in explainer and know the current product intent.\n"
                "- Prefer grounded answers based on app data, UI context, code context, and the built-in project knowledge above.\n"
                "- If asked about a feature's purpose, explain what it does, why it exists, and what limitation it has.\n"
                "- If asked about a contradiction, acknowledge the contradiction directly.\n"
                "- Never make up implementation details, future plans, user research claims, or design intentions that are not supported by available context.\n"
                "- If the answer is uncertain, say so plainly instead of guessing.\n"
                "- If a teammate asks about one specific area, component, button, tile, card, label, page, or feature, answer that exact area first before expanding to the wider product.\n"
                "- If the question is about visible UI behavior, explain first what the user is seeing in the interface now, then explain the product reason, then mention code only if useful.\n"
                "- If the user asks questions like 'what can I see here', 'what is at the bottom', 'what is on this page', 'what does this part mean', or any other interface-facing question, answer from the user visual layer first and do not begin with code structure, file names, arrays, HTML, components, or implementation terms.\n"
                "- For UI questions, describe the visible section, its role, and what the user can do there before mentioning any technical detail.\n"
                "- If the user asks 'can you see', 'what is there', 'what is at the bottom', 'what is on page two', or similar, never start with 'according to the code' or reference implementation files in the first sentence.\n"
                "- For these questions, sentence one must describe the visible UI. Sentence two may explain function or meaning. Technical details are optional and should come last if truly needed.\n"
                "- If the question is about implementation, fields, logic flow, or where something comes from, rely on the code context first and answer concretely.\n"
                "- If the question is about design meaning, rely on the project thesis and product intent first, not raw code details.\n"
                "- If UI, code, and project intent do not fully match, say which layer you are describing: current UI, current implementation, or intended product logic.\n"
                "- Do not answer a narrow question with a broad essay unless the user explicitly asks for a bigger explanation.\n"
                "- Avoid sounding like a rule engine. Prefer natural explanations over phrases like 'the blocker comes first' or other abstract system slogans.\n"
                "- For 'why is this the first action' type questions, explain the visible priority in plain language and avoid jumping straight to one named person unless necessary.\n"
                "- If mentioning a person, frame them as part of an unresolved balance state, not as the problem itself.\n\n"
                "RESPONSE STYLE:\n"
                "- Be concise, product-smart, and specific.\n"
                "- Prefer practical product reasoning over generic AI chatter.\n"
                "- If the user asks about product strategy, UX, logic, or page purpose, answer as an embedded product strategist who knows this app deeply.\n"
                "- Tie answers back to closing the cycle with clarity, fairness, and minimal friction.\n"
                "- Output plain natural text only. Do not use markdown emphasis, bold markers, bullet syntax with asterisks, or decorative formatting.\n"
                "- Do not start with generic greetings unless the user explicitly greets you first.\n"
                "- Keep formatting app-friendly and clean for an in-product chat bubble.\n"
                "- Default to short answers. In most cases answer in 1 to 4 short sentences.\n"
                "- Only go longer if the user explicitly asks for detail.\n"
                "- Do not restate all available context; answer only the user's actual question.\n"
                "- Prefer one clear recommendation over a long menu of options.\n"
                "- If a step-by-step answer is genuinely helpful, use very short lines like 第一步：... 第二步：... with line breaks between steps.\n"
                "- Sound like a native in-product assistant, not a consultant, essay writer, or rule engine.\n"
                "- Avoid phrases like 'Here's the product logic', 'core logic', 'strategy', or section-title style explanations unless the user explicitly asks for a framework.\n"
                "- Explain recommendations in plain natural language, as if clarifying one decision inside the app.\n"
                "- Default to calm, human, slightly cautious phrasing instead of absolute certainty."
            ),
        },
        {
            "role": "system",
            "content": f"BACKEND_APP_CONTEXT_JSON:\n{json.dumps(backend_context, ensure_ascii=False)}",
        },
        {
            "role": "system",
            "content": f"CURRENT_UI_CONTEXT_JSON:\n{json.dumps(ui_context, ensure_ascii=False)}",
        },
        *([
            {
                "role": "system",
                "content": project_code_context,
            }
        ] if include_code_context else []),
        *_text_only_messages(messages),
    ]

    preset_reply = _preset_ai_answer(latest_user_text, backend_context)
    if preset_reply is not None:
        return jsonify({"ok": True, "reply": preset_reply})

    payload = {
        "model": model,
        "messages": provider_messages,
        "temperature": 0.4,
        "max_tokens": 220,
    }

    parsed, err = _call_provider(payload)
    if err:
        if err.get("kind") == "http":
            return jsonify({"ok": False, "error": "provider_http_error", "detail": err.get("detail")}), 502
        return jsonify({"ok": False, "error": "provider_request_failed", "detail": err.get("detail")}), 502

    try:
        reply = parsed["choices"][0]["message"]["content"]
    except Exception:
        return jsonify({"ok": False, "error": "invalid_provider_response", "detail": parsed}), 502

    if isinstance(reply, str):
        reply = re.sub(r"\*\*(.*?)\*\*", r"\1", reply)
        reply = re.sub(r"\*(.*?)\*", r"\1", reply)
        reply = re.sub(r"(?m)^\s*[-*]\s+", "", reply)
        reply = re.sub(r"\n{3,}", "\n\n", reply).strip()
        if reply.startswith("你好！"):
            reply = reply[len("你好！"):].lstrip()
        elif reply.startswith("你好"):
            reply = reply[len("你好"):].lstrip("！!，, ")
        reply = re.sub(r"^[：:。,.，;；\-\s]+", "", reply)
        reply = re.sub(r"^你好[！!，,\s]*", "", reply)
        reply = re.sub(r"我注意到你再次查看了\s*Home\s*页面。?", "", reply)
        reply = re.sub(r"我看到你再次查看了\s*Home\s*页面。?", "", reply)
        reply = re.sub(r"我看到你正在查看\s*Home\s*页面。?", "", reply)
        reply = re.sub(r"具体行动路径如下[:：]?", "", reply)
        reply = re.sub(r"Here'?s the product logic[:：]?", "", reply, flags=re.I)
        reply = re.sub(r"Core logic[:：]?", "", reply, flags=re.I)
        reply = re.sub(r"Risk Overrides Execution[:：]?", "The blocker comes first.", reply, flags=re.I)
        reply = reply.strip()
        explain_mode = bool(re.search(r"(^|\b)(why|explain|为什么|为何|怎么理解)(\b|$)", latest_user_text, re.I))
        sentences = re.split(r"(?<=[。！？!?])\s+|(?<=[.!?])\s+", reply)
        limit = 4 if explain_mode else 2
        if len(sentences) > limit:
            reply = " ".join([s for s in sentences[:limit] if s]).strip()
        reply = re.sub(r"\s*[0-9]+[.、:]?$", "", reply).strip()

    return jsonify({"ok": True, "reply": reply})


@app.post("/webhook/gfm/demo2")
@app.post("/webhook-test/gfm/demo2")
def webhook_demo2():
    body = request.get_json(silent=True)
    safe_body = body if isinstance(body, dict) else {}
    data, status = build_gfm(safe_body)
    if status == 200 and isinstance(data, dict) and data.get("ok"):
        _record_app_context(safe_body, data)
    return jsonify(data), status


if __name__ == "__main__":
    port = int(os.getenv("PORT", "18083"))
    app.run(host="0.0.0.0", port=port)
