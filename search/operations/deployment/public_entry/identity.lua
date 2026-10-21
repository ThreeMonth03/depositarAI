-- Trust only the preceding OIDC plugin, or our dedicated encrypted guest cookie.
local core = require("apisix.core")
local session = require("resty.session")
local random = require("resty.random")
local to_hex = require("resty.string").to_hex
local sha256 = require("resty.sha256")

local _M = {}

local function hex_subject(value)
    return type(value) == "string" and #value == 64
        and not string.find(value, "[^0-9a-f]")
end

local function user_subject(encoded, options)
    if type(encoded) ~= "string" or #encoded > 32768 then return nil end
    local decoded = ngx.decode_base64(encoded)
    local claims = decoded and core.json.decode(decoded)
    if type(claims) ~= "table" or claims.iss ~= options.issuer
        or type(claims.sub) ~= "string" or #claims.sub < 1 or #claims.sub > 255
        or string.find(claims.sub, "[%z%c]") then return nil end
    local audience_ok, audience_count = false, 1
    if type(claims.aud) == "string" then
        audience_ok = claims.aud == options.client_id
    elseif type(claims.aud) == "table" then
        audience_count = #claims.aud
        if audience_count < 1 or audience_count > 16 then return nil end
        local entries = 0
        for key, value in pairs(claims.aud) do
            if type(key) ~= "number" or key ~= math.floor(key)
                or key < 1 or key > audience_count or type(value) ~= "string"
                or #value < 1 or #value > 255 then return nil end
            entries = entries + 1
            if value == options.client_id then audience_ok = true end
        end
        if entries ~= audience_count then return nil end
    end
    if not audience_ok or (claims.azp ~= nil and claims.azp ~= options.client_id)
        or (audience_count > 1 and claims.azp ~= options.client_id) then return nil end
    local digest = sha256:new()
    if not digest or not digest:update(claims.iss .. string.char(0) .. claims.sub) then
        return nil
    end
    return to_hex(digest:final())
end

local function has_login_cookie(ctx)
    if ctx.depositar_had_login_cookie ~= nil then
        return ctx.depositar_had_login_cookie
    end
    local cookie = core.request.header(ctx, "Cookie")
    return type(cookie) == "string"
        and string.find(";" .. cookie, ";%s*__Host%-depositar%-login=") ~= nil
end

local function guest_subject(options)
    local secret = os.getenv("DEPOSITAR_GUEST_SESSION_SECRET")
    if not hex_subject(secret) then return nil end
    -- open(), not start(), avoids extending the absolute guest identity lifetime.
    local ok, current, _, exists = pcall(session.open, {
        secret = secret,
        audience = "depositar-guest:" .. options.bucket,
        cookie_name = "__Host-depositar-guest",
        cookie_path = "/",
        cookie_secure = true,
        cookie_http_only = true,
        cookie_same_site = "Lax",
        absolute_timeout = options.quota_window_seconds,
        idling_timeout = 0,
        rolling_timeout = 0,
        remember = false,
    })
    if not ok or not current then return nil end
    local subject = exists and current:get("id")
    local expires = exists and current:get("expires")
    if exists and hex_subject(subject) and current:get("bucket") == options.bucket
        and type(expires) == "number" and expires > ngx.time()
        and expires <= ngx.time() + options.quota_window_seconds then
        current:close()
        return subject
    end
    local bytes = random.bytes(32, true)
    if not bytes then current:close(); return nil end
    subject = to_hex(bytes)
    current:set_data({id = subject, bucket = options.bucket,
        expires = ngx.time() + options.quota_window_seconds})
    local saved, result = pcall(current.save, current)
    current:close()
    if not saved or not result then return nil end
    return subject
end

local function set_header(ctx, name, value)
    core.request.set_header(ctx, name, tostring(value))
    local variable = "http_" .. string.lower(name):gsub("-", "_")
    ctx.var[variable] = nil
end

local function reject(code)
    return 401, {error = {code = code or "authentication_required"}}
end

function _M.handler(options)
    return function(_, ctx)
        local edge_key = os.getenv("DEPOSITAR_EDGE_KEY")
        if not hex_subject(edge_key) then
            return 503, {error = {code = "service_unavailable"}}
        end
        local encoded = core.request.header(ctx, "X-ID-Token")
        local subject, kind
        if encoded ~= nil then
            subject, kind = user_subject(encoded, options), "user"
            if not subject then return reject() end
        elseif options.access_mode ~= "mixed" or options.redirect or has_login_cookie(ctx) then
            -- Expired/invalid login must not silently become a new guest session.
            return reject()
        else
            subject, kind = guest_subject(options), "guest"
            if not subject then return 503, {error = {code = "service_unavailable"}} end
        end
        for _, name in ipairs({"X-ID-Token", "X-Raw-ID-Token", "X-Userinfo",
            "X-Access-Token", "X-Refresh-Token", "Cookie"}) do
            core.request.set_header(ctx, name, nil)
        end
        core.request.set_header(ctx, "Authorization", ctx.depositar_authorization)
        set_header(ctx, "X-Depositar-Subject", subject)
        set_header(ctx, "X-Depositar-Edge-Key", edge_key)
        set_header(ctx, "X-Depositar-Principal-Kind", kind)
        set_header(ctx, "X-Depositar-Access-Mode", options.access_mode)
        set_header(ctx, "X-Depositar-Turn-Limit",
            kind == "guest" and options.quota_per_guest or options.quota_per_account)
        set_header(ctx, "X-Depositar-Window-Seconds", options.quota_window_seconds)
        set_header(ctx, "X-Depositar-Quota-Bucket", options.bucket)
        set_header(ctx, "X-Depositar-Quota-Pool-Limit",
            kind == "guest" and options.quota_guest_pool or options.quota_user_pool)
        local now = ngx.time()
        for _, window in ipairs({
            {"Quota", options.quota_window_seconds},
            {"Session", options.session_window_seconds},
        }) do
            set_header(ctx, "X-Depositar-" .. window[1] .. "-Period", math.floor(now / window[2]))
            set_header(ctx, "X-Depositar-" .. window[1] .. "-TTL", window[2] - (now % window[2]))
        end
        if options.redirect then ngx.header["Location"] = "/"; return 302 end
    end
end

return _M
