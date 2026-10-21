-- Execute the real adapter and response plugin with the pinned OpenResty runtime.
-- HTTP/OIDC/cookie cryptography is covered separately by the full Compose fixture.
local real_ngx = ngx
local json = require("cjson.safe")
require("resty.session")
local headers = {}
package.preload["apisix.core"] = function()
    return {
        json = json,
        request = {
            header = function(_, name) return headers[string.lower(name)] end,
            set_header = function(_, name, value) headers[string.lower(name)] = value end,
        },
        schema = {check = function() return true end},
    }
end
_G.ngx = {
    time = real_ngx.time,
    decode_base64 = real_ngx.decode_base64,
    header = {}, arg = {}, status = 200,
}
local identity = dofile("/source/search/operations/deployment/public_entry/identity.lua")
local response = dofile("/source/search/operations/deployment/public_entry/quota_response.lua")
local options = {
    access_mode = "mixed", issuer = "https://identity.example.org", client_id = "test-client",
    bucket = string.rep("a", 64), quota_per_guest = 3, quota_per_account = 10,
    quota_guest_pool = 20, quota_user_pool = 80, quota_window_seconds = 86400,
    session_window_seconds = 3600,
}
local function claims(value)
    return real_ngx.encode_base64(assert(json.encode(value)))
end
local function call(value, had_cookie)
    headers = value and {["x-id-token"] = value} or {}
    local ctx = {var = {}, depositar_had_login_cookie = had_cookie,
        depositar_authorization = "Bearer fixture-search-token"}
    return identity.handler(options)(nil, ctx)
end
assert(call(claims({iss = options.issuer, aud = options.client_id, sub = "alice"})) == nil)
assert(headers["x-depositar-principal-kind"] == "user")
assert(#headers["x-depositar-subject"] == 64)
assert(headers["x-depositar-turn-limit"] == "10")
assert(headers["authorization"] == "Bearer fixture-search-token")
assert(headers["x-id-token"] == nil and headers["cookie"] == nil)
assert(tonumber(headers["x-depositar-quota-ttl"]) >= 1)
assert(tonumber(headers["x-depositar-quota-ttl"]) <= 86400)
local alice = headers["x-depositar-subject"]
-- Every tier uses the same epoch and expires at the next aligned boundary.
for _, case in ipairs({
    {86399, "0", "1", "23", "1"},
    {86400, "1", "86400", "24", "3600"},
}) do
    ngx.time = function() return case[1] end
    assert(call(claims({iss = options.issuer, aud = options.client_id, sub = "alice"})) == nil)
    assert(headers["x-depositar-quota-period"] == case[2])
    assert(headers["x-depositar-quota-ttl"] == case[3])
    assert(headers["x-depositar-session-period"] == case[4])
    assert(headers["x-depositar-session-ttl"] == case[5])
end
ngx.time = real_ngx.time
assert(call(claims({iss = options.issuer, aud = {options.client_id, "other"},
    azp = options.client_id, sub = "alice"})) == nil)
assert(headers["x-depositar-subject"] == alice)
for _, invalid in ipairs({
    "not-base64", claims({iss = "other", aud = options.client_id, sub = "alice"}),
    claims({iss = options.issuer, aud = "other", sub = "alice"}),
    claims({iss = options.issuer, aud = {options.client_id, "other"}, sub = "alice"}),
    claims({iss = options.issuer, aud = options.client_id, sub = "bad\nsubject"}),
}) do assert(call(invalid) == 401) end
assert(call(nil, true) == 401) -- expired login cannot become a guest
options.access_mode = "login_required"
assert(call(nil, false) == 401)

local function filtered(prefix, kind, body, reset, chunk_size)
    ngx.status, ngx.header = 429, {
        ["X-" .. prefix .. "-RateLimit-Remaining"] = 0,
        ["X-" .. prefix .. "-RateLimit-Reset"] = reset or 12.2,
        ["Retry-After"] = "2", ["Content-Length"] = #body,
    }
    headers = {["x-depositar-principal-kind"] = kind}
    local ctx = {}
    response.header_filter({}, ctx)
    local output = {}
    chunk_size = chunk_size or #body
    for offset = 1, #body, chunk_size do
        ngx.arg = {body:sub(offset, offset + chunk_size - 1), offset + chunk_size > #body}
        response.body_filter({}, ctx)
        output[#output + 1] = ngx.arg[1] or ""
    end
    assert(ngx.header["Retry-After"] == "2")
    return table.concat(output)
end
local native = '{"error":{"code":"quota_exceeded"}}'
for _, case in ipairs({
    {"Individual", "guest", "guest_quota_exceeded"},
    {"Individual", "user", "user_quota_exceeded"},
    {"Pool", "guest", "pool_quota_exceeded"},
    {"Pool", "user", "pool_quota_exceeded"},
    {"Global", "guest", "global_quota_exceeded"},
}) do
    local result = assert(json.decode(filtered(case[1], case[2], native, 12.2, 7)))
    assert(result.error.code == case[3])
    assert(result.error.retry_after_seconds == 13)
end
for _, original in ipairs({
    '{"error":{"code":"busy"}}', '{"error":{"code":"session_capacity"}}',
    '{"error":{"code":"rate_limited"}}', '{"error":{"code":"quota_exceeded","extra":1}}',
    "invalid-json", string.rep("x", 5000),
}) do assert(filtered("Global", "guest", original, 10, 17) == original) end
assert(filtered("Individual", "invalid", native) == native)
print("identity and quota response Lua contracts passed")
