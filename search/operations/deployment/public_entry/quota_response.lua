-- Translate native quota failures only. No counters or admission decisions live here.
local core = require("apisix.core")
local _M = {
    version = 0.1,
    priority = -1001,
    name = "depositar-quota-response",
    schema = {type = "object", properties = {}, additionalProperties = false},
}
local MAX_BODY = 4096

function _M.check_schema(conf)
    return core.schema.check(_M.schema, conf)
end

function _M.header_filter(_, ctx)
    if ngx.status ~= 429 then return end
    for _, prefix in ipairs({"Global", "Pool", "Individual"}) do
        local remaining = ngx.header["X-" .. prefix .. "-RateLimit-Remaining"]
        if remaining ~= nil then
            -- Later rules have no headers when an earlier counter rejects.
            if tonumber(remaining) ~= 0 then return end
            ctx.depositar_quota_response = {
                prefix = prefix,
                kind = core.request.header(ctx, "X-Depositar-Principal-Kind"),
                reset = tonumber(ngx.header["X-" .. prefix .. "-RateLimit-Reset"]),
                chunks = {}, size = 0,
            }
            -- Body length may change only after we verify the native JSON below.
            ngx.header["Content-Length"] = nil
            return
        end
    end
end

local function only_field(value, field)
    if type(value) ~= "table" then return false end
    local count = 0
    for key in pairs(value) do
        if key ~= field then return false end
        count = count + 1
    end
    return count == 1
end

function _M.body_filter(_, ctx)
    local state = ctx.depositar_quota_response
    if not state then return end
    local chunk = ngx.arg[1] or ""
    state.size = state.size + #chunk
    if state.size > MAX_BODY then
        ngx.arg[1] = table.concat(state.chunks) .. chunk
        ctx.depositar_quota_response = nil
        return
    end
    state.chunks[#state.chunks + 1] = chunk
    ngx.arg[1] = nil
    if not ngx.arg[2] then return end
    local original = table.concat(state.chunks)
    local value = core.json.decode(original)
    if only_field(value, "error") and only_field(value.error, "code")
        and value.error.code == "quota_exceeded" then
        local code
        if state.prefix == "Global" then code = "global_quota_exceeded"
        elseif state.prefix == "Pool" then code = "pool_quota_exceeded"
        elseif state.kind == "guest" then code = "guest_quota_exceeded"
        elseif state.kind == "user" then code = "user_quota_exceeded" end
        if code then
            value.error.code = code
            if state.reset and state.reset >= 0 and state.reset <= 604800 then
                value.error.retry_after_seconds = math.max(1, math.ceil(state.reset))
            end
            ngx.arg[1] = core.json.encode(value)
        else ngx.arg[1] = original end
    else ngx.arg[1] = original end
    ctx.depositar_quota_response = nil
end

return _M
