"""Public authentication projection; runtime profiles and credentials stay private."""


def authentication_view(package, value):
    authentication = (package.body or {}).get("authentication") or {}
    interaction = authentication.get("interaction") or {}
    result = {"method": interaction.get("mode", "unsupported"), "status": value.get("status", "unknown")}
    idp = authentication.get("idp") or interaction.get("idp")
    if isinstance(idp, str):
        result["idp"] = idp
    for key in ("verification_uri", "user_code", "expires_at", "poll_after_seconds"):
        if key in value:
            result[key] = value[key]
    if interaction.get("mode") == "browser-cookie":
        result["cookie_name"] = interaction["cookie_name"]
    return result
