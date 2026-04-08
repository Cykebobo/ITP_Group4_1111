

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

    return jsonify({"ok": True, "reply": "Assistant endpoint configured."})


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
