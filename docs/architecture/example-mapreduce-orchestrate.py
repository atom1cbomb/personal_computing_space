async def orchestrate(ctx: "OrchestrationContext", input_ref: "ResourceRef") -> "ResourceRef":
    data = await ctx.read_json(input_ref)
    handles = []
    for i, job in enumerate(data["jobs"]):
        handles.append(ctx.emit_node("map-" + str(i), data["worker"]["package"],
            data["worker"]["descriptor"], [job["input"]], activation_ref=job["activation"]))
    results = []
    for handle in handles:
        results.append(await ctx.result(handle))
    merged = ctx.emit_node("reduce", data["reducer"]["package"],
        data["reducer"]["descriptor"], results, activation_ref=data["reducer"]["activation"])
    return await ctx.result(merged)
